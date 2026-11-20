#!/usr/bin/env python3
"""
check_isr_flash_safe.py - fail the build if interrupt (IRAM) code touches flash.

WHY
  The LTC sample ISRs are registered with ESP_INTR_FLAG_IRAM so a flash write (NVS save, OTA, WiFi
  start) cannot delay them. That is only safe if EVERYTHING they call or read lives in internal
  RAM/ROM. If an ISR calls a flash-resident function (e.g. Arduino dacWrite(), libgcc __divdi3) or reads
  flash-resident data while the cache is disabled, the chip panics ("Cache disabled but cached memory
  region accessed") and reboots. This script catches that at build time.

HOW
  1. (optionally) compiles the sketch with arduino-cli,
  2. reads the relocation records of every sketch object file and collects the symbols referenced from
     sections placed in IRAM (.iram1.*, i.e. IRAM_ATTR functions),
  3. looks each symbol up in the linked ELF and classifies its address (IRAM / ROM / DRAM / flash ...),
  4. exits 1 if any referenced symbol (or any .text/.rodata section) is flash-resident.

USAGE
  python tools/check_isr_flash_safe.py --sketch master_LTC_Device_Jamsync
  python tools/check_isr_flash_safe.py --build-path <existing build dir> --chip esp32   # skip compiling

  --chip is inferred from --fqbn (esp32 | esp32s3).  Needs only the Python standard library, the
  Arduino IDE/arduino-cli with the esp32 core installed.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

# address regions per chip: (name, start, end, ok_in_isr)
REGIONS = {
    "esp32": [
        ("IRAM", 0x40080000, 0x400A0000, True),
        ("ROM", 0x40000000, 0x40070000, True),
        ("DRAM", 0x3FFAE000, 0x40000000, True),
        ("peripheral", 0x3FF00000, 0x3FF80000, True),
        ("RTC", 0x3FF80000, 0x3FF82000, True),
        ("RTC-fast", 0x400C0000, 0x400C2000, True),
        ("FLASH-code", 0x400C2000, 0x40C00000, False),
        ("FLASH-rodata", 0x3F400000, 0x3F800000, False),
        ("external-RAM", 0x3F800000, 0x3FC00000, False),
    ],
    "esp32s3": [
        ("IRAM", 0x40370000, 0x403E0000, True),
        ("ROM", 0x40000000, 0x40060000, True),
        ("DRAM", 0x3FC80000, 0x3FD00000, True),
        ("peripheral", 0x60000000, 0x60100000, True),
        ("RTC", 0x50000000, 0x50002000, True),
        ("FLASH-code", 0x42000000, 0x44000000, False),
        ("FLASH-rodata/ext-RAM", 0x3C000000, 0x3E000000, False),
    ],
}
# section-relative relocation targets that are fine (same object, internal RAM)
OK_SECTION_PREFIXES = (".iram1", ".dram1", ".bss", ".data", ".noinit", ".rtc")
BAD_SECTION_PREFIXES = (".text", ".rodata", ".flash", ".irom", ".drom")


def find_arduino_cli():
    p = shutil.which("arduino-cli")
    if p:
        return p
    cands = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"),
        "/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    sys.exit("arduino-cli not found (put it on PATH or pass --compile-skip with --build-path)")


def find_tool(chip, name):
    prefix = "xtensa-esp32s3-elf-" if chip == "esp32s3" else "xtensa-esp32-elf-"
    exe = prefix + name + (".exe" if os.name == "nt" else "")
    roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Arduino15\packages\esp32\tools"),
        os.path.expanduser("~/.arduino15/packages/esp32/tools"),
        os.path.expanduser("~/Library/Arduino15/packages/esp32/tools"),
    ]
    for r in roots:
        hits = glob.glob(os.path.join(r, "**", exe), recursive=True)
        if hits:
            return sorted(hits)[-1]
    sys.exit("toolchain tool not found: " + exe)


def classify(chip, addr):
    for name, lo, hi, ok in REGIONS[chip]:
        if lo <= addr < hi:
            return name, ok
    return "unknown", False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sketch", help="sketch folder to compile and check")
    ap.add_argument("--fqbn", default="esp32:esp32:esp32")
    ap.add_argument("--build-path", help="use an existing build directory instead of compiling")
    ap.add_argument("--chip", choices=("esp32", "esp32s3"), help="default: inferred from --fqbn")
    ap.add_argument("--keep", action="store_true", help="keep the temporary build directory")
    a = ap.parse_args()

    chip = a.chip or ("esp32s3" if "esp32s3" in a.fqbn else "esp32")
    tmp = None
    build = a.build_path
    if not build:
        if not a.sketch:
            ap.error("give --sketch (to compile) or --build-path (existing build)")
        cli = find_arduino_cli()
        tmp = tempfile.mkdtemp(prefix="isrcheck_")
        build = tmp
        print(f"[compile] {a.sketch}  ({a.fqbn})")
        r = subprocess.run([cli, "compile", "--fqbn", a.fqbn, "--build-path", build, a.sketch],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:], r.stderr[-3000:])
            sys.exit("compile failed")

    nm = find_tool(chip, "nm")
    objdump = find_tool(chip, "objdump")
    elfs = glob.glob(os.path.join(build, "*.elf"))
    if not elfs:
        sys.exit("no .elf in build path")
    elf = elfs[0]
    objs = glob.glob(os.path.join(build, "sketch", "**", "*.o"), recursive=True)
    if not objs:
        sys.exit("no sketch object files found under <build>/sketch")

    # symbol table from the linked ELF
    symtab = {}
    out = subprocess.run([nm, elf], capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3 and re.fullmatch(r"[0-9a-fA-F]{8}", p[0]):
            symtab.setdefault(p[2], int(p[0], 16))

    refs = {}      # symbol -> set(source object)
    bad_sections = set()
    iram_sections = 0
    for o in objs:
        txt = subprocess.run([objdump, "-r", o], capture_output=True, text=True).stdout
        cur = None
        for line in txt.splitlines():
            m = re.match(r"RELOCATION RECORDS FOR \[(.*)\]", line)
            if m:
                cur = m.group(1)
                if cur.startswith(".iram1") and not cur.endswith(".literal"):
                    iram_sections += 1
                continue
            m = re.match(r"[0-9a-fA-F]+\s+\S+\s+(\S+)", line)
            if not (m and cur and cur.startswith(".iram1")):
                continue
            sym = re.sub(r"\+0x[0-9a-fA-F]+$", "", m.group(1))
            if sym.startswith("."):
                if sym.startswith(BAD_SECTION_PREFIXES):
                    bad_sections.add((os.path.basename(o), cur, sym))
                continue  # section-relative, same object
            refs.setdefault(sym, set()).add(os.path.basename(o))

    failures = []
    unresolved = []
    for sym in sorted(refs):
        if sym not in symtab:
            unresolved.append(sym)
            continue
        region, ok = classify(chip, symtab[sym])
        if not ok:
            failures.append((sym, hex(symtab[sym]), region, sorted(refs[sym])))

    print(f"[scan] chip={chip}  IRAM sections={iram_sections}  referenced symbols={len(refs)}")
    if unresolved:
        print("[note] not in the ELF symbol table (usually compiler built-ins / inlined): " + ", ".join(unresolved))
    for o, sec, sym in sorted(bad_sections):
        failures.append((sym, "-", "flash section referenced from " + sec, [o]))
    if failures:
        print("\nFAIL - interrupt (IRAM) code references flash-resident symbols:")
        for sym, addr, region, where in failures:
            print(f"  {sym}  @ {addr}  [{region}]  from {', '.join(where)}")
        print("\nThese will panic with 'Cache disabled but cached memory region accessed' when an ISR fires"
              "\nduring a flash write (NVS save, OTA, WiFi start). Replace them with IRAM-safe code.")
        rc = 1
    else:
        print("PASS - everything the interrupt code references is in IRAM / ROM / DRAM / peripheral space.")
        rc = 0
    if tmp and not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
