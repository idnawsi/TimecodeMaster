# Operation

## Controls

Each unit has a slide power switch, a USB-C port (charging), a 128 x 64 (master) or 128 x 32 (client) OLED, four buttons on the control board, an OTA button on the Utility module, and two 3.5 mm jacks.

| Button | Short press | Long press |
|---|---|---|
| LEVEL (LTC GPIO19) | Toggle output level LINE / MIC | 3 s: start or leave jam sync |
| FPS (LTC GPIO26) | Next frame rate: 23.976, 24, 25, 29.97 non-drop, 29.97 drop, 30 | 5 s: lock or unlock the unit |
| WIFI (LTC GPIO18) | Show the Wi-Fi name and password on the OLED for 30 s | |
| SYNC (Utility GPIO26), master | | 3 s: send the master's time to all clients now |
| PAIR (Utility GPIO26), client | | 3 s while paired: unpair from the master |
| OTA (Utility GPIO33) | | 3 s: enter local OTA mode (see [firmware.md](firmware.md)) |

A locked unit ignores the front-panel buttons and switches its wireless off, so the setting cannot be changed by accident or by another unit. A 5 s FPS press unlocks it again. A unit also locks itself after a successful jam sync.

The OLED status word shows `MASTER` (normal), `JSRC` (searching for external LTC), `JRUN` (jammed, running on its own clock), or `LOCKED`. The battery icon is at the top left, with a charging animation while USB charges and a blinking outline when the cell is nearly empty.

## First start of a master

1. Switch on. At start-up the master sets its timecode to the time of day held by its RTC, with the frames counted from the moment the RTC second changes (the master waits up to 1.1 s for that tick). If the RTC battery was missing the page shows a warning.
2. Press the WIFI button, then join the network `TimecodeMaster-XXXX` with the password on the OLED. It is 8 random characters from the hardware random number generator, without look-alike characters (no 0, O, 1, l, I). It is generated once and stored, so it stays the same across restarts. A unit updated from firmware older than 1.3.35 replaces its old password once, and phones have to join again. See [security.md](security.md).
3. Open `http://192.168.4.1`. The page shows the timecode, the RTC, the clients, and the controls.
4. Use "set RTC from phone" if the time is wrong. The write is timed so that the RTC lands on the exact second.

## Setting the frame rate and time

- Frame rate and drop frame: from the page or the FPS button. A change is sent to the clients, which reprogram their own clocks. The Si5351 and LTC device need about two frame times to settle, and the firmware waits before resyncing.
- Timecode: enter hours, minutes, seconds and frames on the page. The master pushes it to its LTC device first, then to the clients.
- Run and stop: from the page. The state is sent to every client that is not in jam-sync or locked state.
- Camera ID: two characters, stored per unit and written into the LTC user bits with the date.

## Pairing a client

1. On the master page, start pairing. It stays open for 60 s.
2. Switch on the client. An unpaired client announces itself automatically. Its MAC address appears in the unpaired list.
3. Confirm the client. Both sides store the pairing. The client then registers with a heartbeat every 5 s, and the master sends it the time.
4. Give the client a name from the page if you want.

A client keeps its pairing across power cycles. It is removed from the page with "unpair", or by holding the PAIR button for 3 s on the client. After unpairing, the client's sync LED blinks five times.

Up to 10 clients can be paired.

## Synchronising

The master sends the time to every paired client when:

- the client registers for the first time after power-up, or its uptime restarts,
- the master boots,
- you press SYNC on the master or "Sync all" on the page,
- the optional automatic interval expires (set on the page, 0 minutes = off).

The sync LED on a client blinks three times when it accepts a sync. Clients in jam-sync, searching, or locked state ignore it.

After the sync, each unit keeps its own time. Plan for the drift budget in [timing.md](timing.md) and resync during breaks on long days.

### LTC output offset

Cameras record the LTC signal through their audio path, and the delay differs between models. The page has an output offset per unit, -30 to +30 frames (positive = the written timecode is ahead). It changes only the LTC signal. The OLED shows `OFS+n` when the offset is not zero.

## Jam sync to an external LTC source

1. Connect the external LTC source to the LTC I/O jack.
2. Hold LEVEL for 3 s. The OLED shows `JSRC` and the output stops. The unit decodes the incoming signal.
3. When two consecutive valid frames have been read, the unit loads the time, restarts its frame counter in phase, resumes output (`JRUN`), and locks.
4. Disconnect the source. Hold FPS for 5 s to unlock the unit.

Leaving jam-sync mode with a LEVEL long press during `JSRC` stops the search. The jack is both the output and the input, so do not leave the cable to a camera connected while searching; no LTC is output until the unit has locked or you leave the mode.

## External microphone

An external microphone can be connected to the microphone jack (U13). It must be mono: use a mono 3.5 mm plug, because a stereo (TRS) microphone is not supported. A mechanical switch on the board (S1) selects the external microphone. The microphone path is analogue only, and the firmware does not use it.

## Battery and charging

- Charging starts when USB is connected (up to 1 A). The bi-colour LED on the board shows charging and standby.
- The OLED shows a four-level indicator: full above 3.8 V, then 3.6, 3.4, and 3.2 V. Below 3.2 V the icon blinks. At 3.1 V, held for 5 s, the unit shows a warning for 2 s and powers down. It resumes when the voltage is back above 3.2 V.
- During the first 10 s after power-up the battery reading is ignored, and while charging the reading is corrected by about 0.2 V.
- The master page shows the battery level of every client.
- A missing battery is detected, and the unit runs from USB.
- Run time: with a 3000 mAh 18650 cell, a unit in lock mode (wireless off) runs for about 16 hours. That is the author's figure for the whole unit, not a measured table. It is an average draw of roughly 190 mA from the cell (3000 mAh over 16 h). With the radio on it will be shorter, and it has not been measured. For a long day, charge the cell fully, lock the units after the sync, or carry a USB power bank.

### Radio power save

Set on the master page, off by default. When on, the master sends a short beacon every 2 s and clients keep their radios off between beacons. Commands for a client wait for its next listening window, so changes appear with a delay of up to 2 s. Use it when battery life matters more than response time.

## Troubleshooting

| Symptom | Check |
|---|---|
| Client does not appear while pairing | Is pairing mode open (60 s)? Is the client unpaired, powered, and within range? Is it locked (wireless off)? |
| Client accepts no sync | Its OLED should not show `JSRC`, `JRUN` or `LOCKED`. Unlock with a 5 s FPS press. Check that it is paired, and that its heartbeat is visible on the master page |
| Timecode wrong by hours | The RTC lost its backup cell. Set it from the phone and press "Sync from RTC" |
| Timecode correct but camera shows an offset | Use the LTC output offset for that unit |
| Output level too low or high for the camera input | Toggle LINE / MIC with the LEVEL button |
| Unit resets or reboots under load | The master's firmware page shows reset reasons and brownout counts. Repeated brownouts point to the supply or the battery |
| Clock check shows missed or extra pulses | See the clock integrity section in [timing.md](timing.md). Check the CLK0 wiring and the supply of the Si5351 |

## Checking the output

The LTC Analyzer in `software/ltc-analyzer` reads LTC from a computer's audio input or from recorded audio and video files, and reports timecode accuracy, stability, skipped and duplicated frames. Its comparator mode compares several live LTC sources against each other with a configurable frame threshold, which is a direct way to measure the offset between two units.
