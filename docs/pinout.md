# Pinout

GPIO numbers are ESP32 GPIO numbers. "Pad" is the ESP32-WROOM-32E module pad number from the schematic. Direction is from the ESP32's point of view. The tables for the master board are taken from the KiCad netlist and checked against the firmware.

## LTC device (U10) - master and client LTC images

| GPIO | Pad | Net | Dir | Function |
|---|---|---|---|---|
| 25 | 10 | GPIO25/LTC | out | DAC1, LTC signal to U4 pin 1 |
| 35 | 7 | GPIO35/LTC | in | LTC input from U4 pin 8 (jam sync), any-edge interrupt. Input-only pin |
| 34 | 6 | GPIO34/LTC | in | Si5351 CLK0 sample clock, rising-edge interrupt. Input-only pin |
| 27 | 12 | GPIO27/LTC | out | Jack direction: low = output, high = input (jam-sync search). CD4066 pin 12 and Q1 gate |
| 21 | 33 | GPIO21/LTC | out | Output level: high = LINE, low = MIC. CD4066 pin 13 and Q2 gate |
| 19 | 31 | LVL-BTN | in, pull-up | LEVEL button. Short: LINE/MIC. Long (3 s): jam-sync on/off |
| 26 | 11 | FPS-BTN | in, pull-up | FPS button. Short: next frame rate. Long (5 s): lock/unlock |
| 18 | 30 | Wifi_Show_Btn | in, pull-up | Show Wi-Fi name and password on the OLED for 30 s |
| 4 | 26 | GPIO4/LTC | I2C SCL | OLED (J3) and ADS1115. Firmware uses `Wire1` |
| 5 | 29 | GPIO5/LTC | I2C SDA | OLED (J3) and ADS1115 |
| 36 (VP) | 4 | GPIO36-VP/LTC | in | TP4056 CHRG: low = charging. Input-only pin |
| 39 (VN) | 5 | Wake_Pin | in | USB present (5 V divided); deep-sleep wake, level high |
| 14 | 13 | /SPI_SCLK | in | SPI slave clock |
| 12 | 14 | /SPI_MISO | out | SPI slave data out |
| 13 | 16 | /SPI_MOSI | in | SPI slave data in |
| 15 | 23 | /SPI_CS | in | SPI slave chip select |

Not connected on the board: GPIO0, 2, 16, 17, 22, 23, 32, 33, and the UART pins (GPIO1, GPIO3). GPIO6 to 11 belong to the module's flash and are not available.

I2C devices on this bus: SSD1306 at 0x3C, ADS1115 at 0x48 (battery on AIN0).

## Utility device (U12) - master Utility image

| GPIO | Pad | Net | Dir | Function |
|---|---|---|---|---|
| 21 | 33 | GPIO21/Util | I2C SDA | Si5351 (0x60), DS3231M (0x68) |
| 22 | 36 | GPIO22/Util | I2C SCL | same bus |
| 14 | 13 | /SPI_SCLK | out | SPI master clock (HSPI) |
| 12 | 14 | /SPI_MISO | in | SPI master data in |
| 13 | 16 | /SPI_MOSI | out | SPI master data out |
| 15 | 23 | /SPI_CS | out | SPI chip select to the LTC device |
| 26 | 11 | Sync_BTN | in, pull-up | SYNC button. Hold 3 s: sync all clients now |
| 33 | 9 | (SW2) | in, pull-up | OTA button. Hold 3 s: enter OTA mode |
| 2 | 24 | Net-(D3-A) | out | Blue status LED through R38 |
| 39 (VN) | 5 | Wake_Pin2 | in | USB present; deep-sleep wake, level high |

Not connected: GPIO0, 4, 5, 16, 17, 18, 19, 23, 25, 27, 32, 34, 35, 36 and the UART pins.

## Utility device - client Utility image

The client Utility firmware reuses most of the master assignments and differs in these points. The repository contains no client PCB, so the table lists what the firmware drives.

| GPIO | Function in the client image | Note |
|---|---|---|
| 5 | `POWER_EN_PIN`, drives a P-channel FET that powers the OLED and Si5351 | Not connected on the master board |
| 4 | `SYNC_LED_PIN`, sync indicator LED | Not connected on the master board |
| 2 | `STATUS_LED_PIN` | Same as master |
| 26 | `PAIR_BUTTON_PIN`. Hold 3 s while paired: unpair | The master uses the same pin for its SYNC button |
| 33 | OTA button, hold 3 s | Same as master |
| 21 / 22 | I2C to Si5351 | Same as master |
| 12 / 13 / 14 / 15 | SPI master to the client LTC device | Same as master |

The client LTC image uses the same GPIOs as the master LTC image. Its OLED is 128 x 32.

## Strapping pins

GPIO0, 2, 5, 12 and 15 are sampled at reset. On this board GPIO12 and GPIO15 carry SPI, GPIO2 drives the status LED on the Utility device, and GPIO0 is not connected. GPIO12 must be low at reset on a module with 3.3 V flash, so do not add a pull-up to the SPI nets.

## Connectors

### J1, USB-C (power only)

| Pin | Net |
|---|---|
| A9, B9 | +5V |
| A12, B12, shield | GND |
| A5 | CC1 (5.1 kOhm to ground, R4) |
| B5 | CC2 (5.1 kOhm to ground, R2) |

No data pins are connected.

### J3, OLED (4 pin pads)

| Pin | Net |
|---|---|
| 1 | GND |
| 2 | +3.3V |
| 3 | SCL (LTC GPIO4) |
| 4 | SDA (LTC GPIO5) |

### J4, button board (JST PH 5 pin, side entry)

| Pin | Net | Switch on the control board |
|---|---|---|
| 1 | GND | common |
| 2 | Sync_BTN (Utility GPIO26) | SW6 |
| 3 | Wifi_Show_Btn (LTC GPIO18) | SW5 |
| 4 | LVL-BTN (LTC GPIO19) | SW4 |
| 5 | FPS-BTN (LTC GPIO26) | SW3 |

The mounting pin is connected to ground. Each switch connects its line to ground; the ESP32 internal pull-ups are used.

### J2, RTC backup cell

| Pin | Net |
|---|---|
| 1 | DS3231M VBAT |
| 2 | GND |

### SW1, power switch pads

| Pin | Net |
|---|---|
| A | G-Switch (gate of Q3) |
| B | GND |

### U14, LTC I/O jack (PJ-342B)

| Pin | Net |
|---|---|
| 6 | LTC I/O (the signal; coupled through C7 to U1) |
| 3 | to S1 pin 2 |
| 2, 7 | GND |
| 4, 5 | not connected |

### U13, microphone jack (PJ-342B)

| Pin | Net |
|---|---|
| 4, 5 | Mic-Output (from U16 through R36 and C21) |
| 3 | R39 (4.7 kOhm) |
| 6 | R43 (4.7 kOhm) |
| 2, 7 | GND |

### MK1, microphone capsule

| Pin | Net |
|---|---|
| 1 (S) | source, to C18 and R25 |
| 2 (D) | drain, to R26 |
| 3 (G) | GND |

### BT1, battery

| Pin | Net |
|---|---|
| 1 | +BATT |
| 2 | negative lead, through the FS8205A protection switch to ground |

## Inter-chip SPI link

| Signal | Utility pin (master) | LTC pin (slave) |
|---|---|---|
| SCLK | GPIO14 | GPIO14 |
| MISO | GPIO12 (in) | GPIO12 (out) |
| MOSI | GPIO13 (out) | GPIO13 (in) |
| CS | GPIO15 (out) | GPIO15 (in) |

SPI mode 1, packets of 68 bytes (64 bytes in the client images, where both ends are built together). See [protocol.md](protocol.md).
