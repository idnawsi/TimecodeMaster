# Design notes: why the classic ESP32, and not the ESP32-S3

These notes explain why the design is built on the classic ESP32 (the ESP32-WROOM-32E module) and not on the newer ESP32-S3, and look at what moving to the S3 would involve. Everything about the S3 here is an assessment from reading the design and the firmware. Nothing has been ported, built or measured.

## Why the classic ESP32

The author chose the classic ESP32 for two reasons: to keep the cost down, and because it has its own DAC.

The plan for the DAC was to set the LTC output level, lower or higher, directly from the chip, with no resistor divider and no buffer. Part way into the design it turned out that the ESP32's DAC was too noisy for that. So the output became a digital one: the DAC (or any pin) writes two levels, a 74HC14 Schmitt-trigger buffer squares them up, and the level is set by a resistor divider that a CD4066 switches between LINE and MIC (see [hardware.md](hardware.md)). By then the design was about halfway done and it had to work, so the project stayed on the classic ESP32 and the Schmitt buffer fixed the output instead of starting over.

That history is why the DAC is still the one thing in the design that is specific to the classic chip: the sample interrupt writes through GPIO25, the DAC channel of the original ESP32, and the ESP32-S3 has no DAC. Everything else the design needs is available on both chips:

- ESP-NOW for pairing, registration, the time sync and the run state, with no router (see [protocol.md](protocol.md)).
- A Wi-Fi access point and the web page on the same chip.
- A fast interrupt from IRAM at level 3 for the 48 kHz (47.952 kHz for 23.976 and 29.97) sample clock, with frame building, the OLED and the buttons on the other core (see [timing.md](timing.md)).
- An SPI slave with DMA for the link between the two chips of a unit.
- A pre-certified module (the WROOM-32E), so the board has no RF design.
- The Arduino core and the libraries the firmware uses (Etherkit Si5351, RTClib, Adafruit SSD1306 and ADS1X15, ESPAsyncWebServer).

Because the DAC now only writes two levels (and 128 when the output is idle), a plain GPIO would do the same job (see below).

## What an ESP32-S3 would change

### The DAC is not a blocker

The ESP32-S3 has no DAC. But the sample interrupt in `master_LTC_Device_Jamsync` writes only `ltcHigh` and `ltcLow` (and 128 when the output is idle), and the two inverters of the 74HC14 (U4 pins 1 to 4) restore a full-swing square wave before the level divider. A plain GPIO driving U4 pin 1 does the same job. When the output is idle, a steady level is better than 128, which sits right on the Schmitt threshold; the coupling capacitor C28 makes both silent.

### What it would improve

- **Native USB.** Flashing and serial output over the USB-C port, with no module pads and no UART header. J1 is power-only today, so D+ and D- would have to be routed (U2 already protects the USB lines).
- **More flash.** S3 modules come with 8 or 16 MB. The master Utility image is at 97 % of its app slot (see [firmware.md](firmware.md)), and a larger partition scheme would end that problem.
- **No input-only pins.** The current design uses GPIO34, 35 and 36 as input-only pins. On the S3 every pin can be an input or an output.
- **Peripherals.** The pulse counter could take over the clock-integrity count that the CPU does now. In principle the I2S, RMT or SPI-DMA blocks could generate and decode LTC clocked by the Si5351 with no 48 kHz interrupt, which might allow one chip to do both jobs. That is an idea for a bench experiment, not a drop-in change.

### What it would cost

- **A new board.** The module has a different footprint (41 pins against 38) and every GPIO assignment changes (see [pinout.md](pinout.md)). GPIO19 and 20 are the USB pins, and the LTC device uses 19 and 18 for buttons today. GPIO0, 3, 45 and 46 are strapping pins. Some module variants with octal flash or PSRAM use more GPIOs than they expose, so choose a variant without PSRAM and check its datasheet.
- **A firmware port.** All four sketches need the new pin maps, and the LTC images need the DAC call (`hal/dac_ll.h`) replaced. The level-3 GPIO interrupt, the SPI slave and the Arduino core 3.x all exist for the S3, but the interrupt timing, the flash-safety check (`firmware/tools/check_isr_flash_safe.py`) and the RF performance would all have to be measured again.
- **Price and power.** The modules usually cost a little more. The current draw has not been compared.

### Suggested approach

For a new board revision, move to the S3 mainly for native USB and flash size, and keep the two-chip split for the first S3 version, because it changes the least. Treat the single-chip, hardware-timed design as a separate experiment on a bench board.
