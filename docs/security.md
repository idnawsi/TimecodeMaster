# Security notes

TimecodeMaster is built for a film set, where the people in radio range are your own crew. It is not hardened against an attacker, and none of the items below have been tested against one. They are listed so you can decide how much they matter where you use it. Everything here comes from reading the firmware.

## Wi-Fi access point password

The master runs an access point named `TimecodeMaster-XXXX` with WPA2. The password has 8 characters chosen at random from 56 letters and digits (no 0, O, 1, l, I, because it is read off the OLED), using the ESP32 hardware random number generator. That is about 46 bits. It is generated once, stored in flash and kept across restarts.

Firmware before 1.3.35 built the password differently: the four hex digits of the network name plus four characters from a software generator seeded by a boot-time counter. That left very little to guess, and a captured WPA2 handshake could be cracked offline in seconds. Version 1.3.35 replaces a password made by older firmware once, the first time it starts, so phones have to join again. If your master was flashed before 1.3.35, update it.

Eight characters is still short. Someone who records the WPA2 handshake can test candidates offline at their own speed, so treat the password as protection against casual joining.

## Web interface

The web page and its WebSocket have no login. Anyone who has joined the access point can change the frame rate, set the time, start and stop, pair and unpair clients, and open the firmware pages. Several of these actions are plain HTTP GET requests (`/command`, `/setfps`, `/settimecode`, `/startpairing`, `/pairclient`, `/unpairclient`), so a page opened in a browser on a phone that is joined to the network could trigger them too.

## ESP-NOW

All peers are added with encryption off, and pairing identifies a unit by its MAC address only. Messages are sent in the clear, and a device in range that knows the protocol (see [protocol.md](protocol.md)) can read them or send its own. A MAC address can be copied.

The practical protection is the lock. A locked unit switches its wireless off, and a unit locks itself after a jam sync. For a take that must not be disturbed, sync, then lock the clients (hold FPS for 5 s).

## Over-the-air updates

The OTA password is `timecodemaster` in all four images (`OTA_PASSWORD`). It is shown on the OLED and on the upload page. OTA mode only starts when someone holds the OTA button on the unit, and it ends after 5 minutes, but anyone who is there can use it. Change the password before using the system around people you do not trust.

The identity tag (`TCM-FWID`) stops a wrong file from going onto a unit. It is a label, not a signature. Secure boot and flash encryption are not enabled.

## If you need more

The changes that would help most, in order: encrypt ESP-NOW (set a primary and local master key and mark the peers as encrypted), make the Wi-Fi password longer, move state-changing endpoints to POST with a token, and give each unit its own OTA password. None of this is done.
