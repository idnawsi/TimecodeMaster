# Changelog

Firmware releases. Each version applies to all four images unless a line names one. Dates are commit dates. Versions before 1.3.1 are not listed.

## 1.3.37 (2026-11-20)

Master Utility only: Sync from RTC runs from loop(), like Sync All, so the wait for the RTC second tick no longer blocks the web task.

## 1.3.36 (2026-11-20)

Master Utility only: Sync from RTC counts frames from the RTC second tick. The time of day was off by up to 1 s before.

## 1.3.35 (2026-11-20)

Master Utility only: the Wi-Fi password is 8 random characters from the hardware random number generator (no look-alike characters). Passwords made by older firmware are replaced once, so phones have to join again with the password shown on the OLED.

## 1.3.34 (2026-10-25)

LTC output offset shown on the OLED (OFS+n, only when not 0); client radio flag reset when wireless comes back after a lock.

## 1.3.33 (2026-10-25)

LTC output offset in frames per unit (master + each client), set from the master's main page, kept in the LTC chip's NVS.

## 1.3.32 (2026-10-23)

Show the last crash of the master Utility (core dump summary: task, pc, backtrace) on the firmware page; Clear reset log erases it.

## 1.3.31 (2026-10-23)

Battery-present flag no longer flickers when the battery is missing (sticky absent, 5-sample check while anything looks odd).

## 1.3.30 (2026-10-23)

Power save - lock reaches clients before the master goes quiet; no continuous listening when the master is gone (hunt 2.5 s / sleep 12 s); sleep-test card removed.

## 1.3.29 (2026-10-23)

Power save - clients announce they are awake and the master sends time-critical messages only then; longer window when a command is coming.

## 1.3.28 (2026-10-23)

Optional radio power save (clients sleep between 2 s beacons; default OFF), set on the master's main page.

## 1.3.27 (2026-10-23)

Count low-battery shutdowns of the master and show them in the Reset log (the shutdown leaves no other trace).

## 1.3.26 (2026-10-23)

Sleep test shows every client (one slot each), toast fits a phone, RTC phone-time task stack 12 KB, I2C bus recovery at boot.

## 1.3.25 (2026-10-23)

Sleep test page shows whether a client with the test firmware is present and whether it answered; commands acted on once; test task stack 12 KB.

## 1.3.24 (2026-10-23)

Radio sleep test phases 10 s each (about 1 minute).

## 1.3.23 (2026-10-23)

Radio sleep test runs from the master's page (no serial needed), RADIO OFF N s button; test image waits for the page's command.

## 1.3.22 (2026-10-22)

RTC set from the phone lands on the exact second (write timed on the master, network delay compensated).

## 1.3.21 (2026-10-22)

Set RTC from the phone's clock in one tap.

## 1.3.20 (2026-10-22)

RTC protection - I2C lock for RTC + Si5351, validated RTC writes (POST only), warning when the RTC battery was lost.

## 1.3.19 (2026-10-22)

Remove the TX power slider (it did not change the real signal); radio test compares the two rates.

## 1.3.18 (2026-10-22)

Radio test - measure how loudly each side really hears the other at every TX power and rate.

## 1.3.17 (2026-10-22)

Prove the TX power setting - radio readback and both signal directions on the main page.

## 1.3.16 (2026-10-22)

Radio settings on the MAIN page, signal strength per client, pages protected against corruption.

## 1.3.15 (2026-10-22)

WiFi TX power and ESP-NOW rate configurable on the master's page; ESP-NOW now 6 Mbps by default.

## 1.3.14 (2026-10-22)

"Clear reset log" button; main-page warning is a badge, not a renamed button.

## 1.3.13 (2026-10-22)

Main page no longer throws on every update; reset-log table fits a phone.

## 1.3.12 (2026-10-22)

Low-battery shutdown at 3.1 V (was 3.0 V).

## 1.3.11 (2026-10-08)

Reset log - every chip reports why it last restarted; brownouts/watchdog/crashes counted.

## 1.3.10 (2026-10-06)

Show the saved auto-sync interval on the firmware page.

## 1.3.9 (2026-10-06)

Record WHEN wrong-pulse windows happen (first/last second, count) per client.

## 1.3.8 (2026-10-05)

Thread-safe WebSocket broadcasts, bounded per-browser queue.

## 1.3.7 (2026-10-05)

Memory/stack readout on the firmware page; no polling from hidden tabs.

## 1.3.6 (2026-10-05)

Choose which clients to update, one at a time or up to 4 at once.

## 1.3.5 (2026-10-05)

A manual sync restarts the clock check on every unit.

## 1.3.4 (2026-10-05)

Update result shows for 10 min after the restart only; no message flicker.

## 1.3.3 (2026-10-05)

Fix heap corruption from Strings shared between loop() and web handlers.

## 1.3.2 (2026-10-05)

Enable the Si5351 sample clock only after the PLL reset has settled.

## 1.3.1 (2026-10-05)

Version bump so the running build is visible; LTC checks a private copy of each update packet.
