#!/usr/bin/env python3
"""Build hardware/bom.csv from a KiCad XML netlist.

Usage: python make_bom.py netlist.xml bom.csv
Parts are grouped by (value, footprint). Mounting holes are skipped.
Function text for the main parts is kept in FUNCTION below, keyed by reference.
"""
import csv
import re
import sys
import xml.etree.ElementTree as ET

FUNCTION = {
    "U1": "Quad analog switch (LINE/MIC level, LTC jack direction)",
    "U2": "USB ESD / TVS array",
    "U3": "Real-time clock with integrated TCXO",
    "U4": "Hex Schmitt-trigger inverter (LTC output buffer, input conditioning)",
    "U5": "Li-ion linear charger, 1 A max",
    "U6": "Dual N-channel MOSFET (battery protection switch)",
    "U7": "Li-ion protection controller",
    "U8": "3.3 V LDO (500 mA), analog rail",
    "U16": "Single rail-to-rail op-amp, 2.2 to 5.5 V (microphone preamp)",
    "U10": "ESP32-WROOM-32E module: LTC device",
    "U11": "Buck-boost converter, 3.3 V system rail",
    "U12": "ESP32-WROOM-32E module: Utility device",
    "U13": "3.5 mm jacks: U13 microphone output, U14 LTC I/O",
    "U15": "16-bit I2C ADC (battery voltage)",
    "X1": "25 MHz 0.5 ppm TCXO",
    "Y1": "I2C programmable clock generator",
    "Q1": "P-channel MOSFET, SOT-23 (Q1, Q2 level control; Q3 power switch; Q4 battery path)",
    "D1": "Bi-colour LED (charge / standby)",
    "D2": "Schottky diode (USB to supply OR-ing)",
    "D3": "Blue status LED (Utility GPIO2)",
    "D4": "Bidirectional ESD diode on the LTC jack tip (R48 limits current behind it)",
    "J1": "USB-C receptacle, power only",
    "J2": "RTC backup battery pads",
    "J3": "OLED connector (SSD1306, I2C)",
    "J4": "Button board connector",
    "MK1": "Electret microphone",
    "S1": "Slide switch (jack routing)",
    "SW1": "Main power switch pads",
    "SW2": "OTA button",
    "BT1": "Li-ion cell",
    "L1": "1.5 uH power inductor",
    "JP1": "Solder jumper: converter power-save mode",
    "FB1": "Ferrite bead",
}
ORDER = ["U", "X", "Y", "Q", "D", "J", "S", "L", "F", "M", "B", "C", "R"]


def split(ref):
    m = re.match(r"([A-Za-z]+)(\d+)", ref)
    return m.group(1), int(m.group(2))


def compress(refs):
    refs = sorted(refs, key=split)
    out, i = [], 0
    while i < len(refs):
        j = i
        while (j + 1 < len(refs) and split(refs[j + 1])[0] == split(refs[i])[0]
               and split(refs[j + 1])[1] == split(refs[j])[1] + 1):
            j += 1
        out.append(refs[i] if j == i else (f"{refs[i]}, {refs[j]}" if j == i + 1 else f"{refs[i]}-{refs[j]}"))
        i = j + 1
    return ", ".join(out)


def main(net, out):
    root = ET.parse(net).getroot()
    groups = {}
    for c in root.find("components"):
        ref = c.get("ref")
        if ref.startswith("MH"):
            continue
        value = c.findtext("value") or ""
        fp = (c.findtext("footprint") or "").split(":")[-1]
        fields = {p.get("name"): p.text for p in c.iter("field")}
        desc = fields.get("Description") or fields.get("Description_1") or ""
        g = groups.setdefault((value.lower(), fp), {"refs": [], "value": value, "fp": fp, "desc": desc})
        g["refs"].append(ref)

    def sort_key(g):
        first = sorted(g["refs"], key=split)[0]
        prefix, num = split(first)
        return (ORDER.index(prefix) if prefix in ORDER else 99, num)

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Qty", "References", "Value", "Footprint", "Function"])
        for g in sorted(groups.values(), key=sort_key):
            first = sorted(g["refs"], key=split)[0]
            if first in FUNCTION:
                text = FUNCTION[first]
            elif first.startswith("C"):
                text = "Capacitor"
            elif first.startswith("R"):
                text = "Resistor"
            elif first.startswith("FB"):
                text = "Ferrite bead"
            else:
                text = g["desc"].split(",")[0]
            w.writerow([len(g["refs"]), compress(g["refs"]), g["value"], g["fp"], text])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
