# Firmware

Four Arduino sketches in `firmware/`. Each unit needs one LTC image and one Utility image. The firmware was written by AI; the hardware it runs on and the testing of the units were done by the project author.

| Sketch folder | Chip | Unit | Main libraries |
|---|---|---|---|
| `master_LTC_Device_Jamsync` | LTC device | master | Adafruit GFX, Adafruit SSD1306, Adafruit ADS1X15, Preferences |
| `Master_Utility_Device_JamSync_Wireless` | Utility device | master | ESPAsyncWebServer, AsyncTCP, RTClib, Si5351 (Etherkit), ESP-NOW |
| `Client_LTC_Device` | LTC device | client | as master LTC |
| `Client_Utility_Device_Wireless` | Utility device | client | Si5351 (Etherkit), ESP-NOW, WebServer |

The Master Utility sketch folder contains three files that the Arduino build joins into one program: the main file, `MasterOTA.ino` (streams a new image to the master LTC chip over SPI) and `ClientOTA.ino` (pushes images to clients, radio power save, radio test). Do not move them apart.

Current version: 1.3.37 for the master Utility image and 1.3.34 for the other three. The version number is repeated in each sketch (`FIRMWARE_VERSION_MAJOR / MINOR / PATCH`) and shown on the master's firmware page, so keep them in step when you release.

## Toolchain

- Arduino IDE 2.x or `arduino-cli`.
- `esp32` board package by Espressif, version 3.x (ESP-IDF 5.x). The images were built with the IDF 5.5 based core. Older 2.x cores lack some of the calls used.
- Board: "ESP32 Dev Module" (`esp32:esp32:esp32`).
- Partition scheme: "Default 4MB with spiffs (1.2MB APP / 1.5MB SPIFFS)". This gives two 1.25 MB app slots, a 1.4 MB data partition and a 64 kB core dump partition. The master Utility uses the spare app slot and the data partition to hold client images during a fleet update, and the core dump partition to report crashes. The images are 1.0 to 1.3 MB, and the master Utility image is now 97 % of the app slot (1,275,319 of 1,310,720 bytes with ESP32 core 3.3.1), so there is almost no room to grow.
- Everything else at the defaults. The LTC firmware sets the CPU to 240 MHz itself.

Libraries (install from the Library Manager unless noted):

| Library | Used by |
|---|---|
| Adafruit GFX Library | LTC images |
| Adafruit SSD1306 | LTC images |
| Adafruit ADS1X15 | LTC images |
| RTClib | master Utility |
| Etherkit Si5351 (v2.x, provides `set_ms`) | both Utility images |
| ESPAsyncWebServer and AsyncTCP | master Utility. Use versions that support arduino-esp32 3.x; the maintained ESP32Async forks do |

## Build

```
arduino-cli core install esp32:esp32
arduino-cli lib install "Adafruit GFX Library" "Adafruit SSD1306" "Adafruit ADS1X15" RTClib "Etherkit Si5351"
# AsyncTCP and ESPAsyncWebServer: install the 3.x-compatible versions from their repositories

cd firmware
arduino-cli compile --fqbn esp32:esp32:esp32 master_LTC_Device_Jamsync
arduino-cli compile --fqbn esp32:esp32:esp32 Master_Utility_Device_JamSync_Wireless
arduino-cli compile --fqbn esp32:esp32:esp32 Client_LTC_Device
arduino-cli compile --fqbn esp32:esp32:esp32 Client_Utility_Device_Wireless
```

Each build produces `<sketch>.ino.bin` (the over-the-air image), `.bootloader.bin`, `.partitions.bin` and `.merged.bin` (the full flash image for a first flash).

### Interrupt safety check

The sample-clock and input interrupts run from IRAM so that flash operations (NVS writes, OTA, Wi-Fi start) cannot delay them. That only holds if everything the interrupt code calls or reads is itself in IRAM, ROM or DRAM. A call into flash while the cache is disabled makes the chip panic. After changing any interrupt code, run from `firmware/`:

```
python tools/check_isr_flash_safe.py --sketch master_LTC_Device_Jamsync
python tools/check_isr_flash_safe.py --sketch Client_LTC_Device
```

It exits 1 and names the offending symbol if interrupt code references anything in flash.

## First flash

The board has no USB-serial bridge and the schematic exposes no programming header (see [hardware.md](hardware.md)). For the first flash, connect a 3.3 V USB-serial adapter to the module pads (TXD0, RXD0, EN, GPIO0, GND, 3V3), hold GPIO0 low while releasing EN, and write the merged image:

```
esptool.py --chip esp32 --port COMx write_flash 0x0 master_LTC_Device_Jamsync.ino.merged.bin
```

Do this once per chip. The two modules on one board are flashed separately. Later updates use the over-the-air paths below.

## Over-the-air updates

| What | How |
|---|---|
| Master Utility | Hold the OTA button (GPIO33) for 3 s. Join `TCM-U-XXXX`, open `http://192.168.4.1`, upload `Master_Utility_Device_JamSync_Wireless.ino.bin` |
| Master LTC | On the master's firmware page, choose the LTC image. The master Utility streams it over SPI with CRC and MD5 checks (`MasterOTA.ino`) |
| Client Utility and client LTC | On the master's `/client-update` page, upload both client images and choose the clients. They download over the master's access point, one at a time or up to four at once |

Safety measures built into the images:

- **Identity tag.** Each image contains a string such as `TCM-FWID:CLIENT-UTILITY`. The uploader refuses an image whose tag does not match the target. The master assembles the expected tags at run time, so its own image never contains another unit's tag.
- **Rollback.** The images override `verifyRollbackLater()` and mark themselves valid at the end of `setup()`. A new image that crashes or resets during start-up is rolled back by the bootloader instead of boot-looping.
- **Timeouts.** Local OTA mode ends after 5 minutes. A client update is abandoned after 10 minutes.
- **Version display.** The master shows the Utility and LTC firmware versions of every client, and the build fingerprint (first bytes of the ELF SHA-256) of the master LTC image.

The OTA password `timecodemaster` is a compile-time constant (`OTA_PASSWORD`). Change it before using the system in an environment where others can reach the access point.

## Diagnostics without a serial port

The board exposes no UART, and the LTC and master Utility images do not use the serial port (the client Utility image still contains some `Serial` messages from development). Diagnostics are reported through the master's web page instead:

- The firmware page shows reset reasons per chip, with counts of brownout, watchdog and panic resets (kept in NVS), and the number of low-battery shutdowns.
- The last crash of the master Utility (task, program counter, backtrace) is read from the core dump partition and shown on the same page. Keep the `.elf` of each released build to translate the addresses.
- The clock check (missed and extra sample pulses, longest interrupt gap, time of errors) is shown per unit.
- A radio test on the master's page measures the signal strength in both directions at each rate.

## Persistent settings

Stored in NVS (`Preferences`) on each chip, so they survive a power cycle: frame rate and drop-frame flag, last sample rate, camera ID, sync interval, Wi-Fi password, pairing, LTC output offset, ESP-NOW rate and power-save choice, reset counters, RTC "time is set" flag.

## Known limitations

- **Time of day.** "Sync from RTC" and the sync at start-up wait for the RTC second to change (up to 1.1 s, polled every 2 ms) and count frames from that tick, so the time of day is correct to a few milliseconds (versions before 1.3.36 could be off by up to a second). The DS3231 keeps whole seconds only, so the tick can be found only to within one poll period, about 4 ms. At 23.976 and 29.97 fps (non-drop) the timecode runs 0.1 % slow after the sync, so it stops matching the clock after the first minute or so. If the RTC stops ticking, the old millisecond phase is used.
- **Sync from RTC is not rate limited.** Requests are queued by a flag and run from the main loop, each taking up to 1.1 s. A client that sends them continuously, which anyone on the access point can do (see [security.md](security.md)), keeps the main loop busy.
- **Client table.** The list of paired clients is changed from the Wi-Fi task and from the main loop without a lock, and the ESP-NOW receive callback can end up waiting for a frame boundary and doing SPI transfers. This was read from the code and has not been reproduced as a failure.
- **Polarity bit.** The LTC polarity-correction bit is not set. Decoders cope, but the output is not strictly by the book.
- **Security.** See [security.md](security.md).
