# Third-party material

The PolyForm Noncommercial License in [LICENSE](LICENSE), with the [additional permission](ADDITIONAL-PERMISSION.md), covers the author's own work in this repository: the firmware, the desktop applications, the schematic and board design, the documentation and the scripts. It does not change the terms of anything below, which belongs to other people and keeps its own license.

## Inside the design files

- **KiCad standard libraries.** Symbols and footprints from the KiCad libraries are embedded in the schematic and board files, and some 3D models appear in the STEP exports. KiCad publishes these libraries under Creative Commons Attribution-ShareAlike 4.0 with an exception for designs made with them, so a design that uses them is not forced to take that license. The libraries themselves stay under their own terms.
- **Imported parts.** A few symbols and footprints (for example the PJ-342B jack, the TPS63070 converter, the TSX-3225 crystal, the SMF05C array, the RGB LED and the TL2285EE switch) were imported from part libraries or manufacturer models. Their own terms apply to them. Check those terms before you redistribute them on their own, outside this design.
- **Datasheet links** in the footprints point to the manufacturers' documents. The documents are not included.

## Not included, but required

- **Firmware libraries.** The sketches need libraries that are installed separately (see [docs/firmware.md](docs/firmware.md)): Adafruit GFX, SSD1306 and ADS1X15, RTClib, Etherkit Si5351, ESPAsyncWebServer and AsyncTCP, and the Espressif Arduino core. Each has its own license. None is copied into this repository.
- **Python packages and programs.** The desktop applications use Python packages (installed from `requirements.txt`) and call FFmpeg and VLC as separate programs. FFmpeg builds can be under the GPL. They are separate programs with their own licenses, and the applications can download an FFmpeg build when you ask them to. None of this is bundled in the repository.

If you redistribute a built executable of the desktop applications, for example one made with PyInstaller, check the licenses of everything that goes into it.
