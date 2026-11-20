# Field test: one real shoot

One shooting day, 25 October 2026, with three TimecodeMaster units: one on a Canon EOS R6, one on a Canon EOS R8 (its files are named MVI_xxxx), and one feeding a Zoom H6 recorder. Every unit wrote its own camera ID into the LTC user bits (1, 3 and 2). All were set to 25 fps. This note records what the LTC Sync App read from the recorded files. It was done after the shoot, on the files only, and nothing in the files was changed.

## What was recorded

| Source | Files | Duration | Unit ID | LTC found |
|---|---:|---:|:---:|---|
| Canon EOS R6, video clips | 71 | 0.95 h | 1 | 71 of 71 |
| Canon EOS R8, video clips (MVI) | 93 | 1.10 h | 3 | 93 of 93 |
| Zoom H6, LTC track of each take (Tr1) | 23 takes | 5.3 h | 2 | 23 of 23 |
| Zoom H6, microphone tracks (LR, Tr3) | 31 | | | none, as expected |

LTC runs from 13:05 to 18:36 (time of day). The LTC was on audio channel 2 of the EOS R6 clips, channel 1 of the MVI clips, and track 1 of the Zoom. All 187 LTC files were read as 25 fps non-drop, dated 10/25/26 from the user bits, and the current LTC Sync App reported no timecode-jump warnings.

The first LTC frame that can be decoded sits 33 to 73 ms into the camera files (1599 to 3486 samples at 48 kHz, median about 2400). That is up to two frames, which is why the app measures the start offset in samples and subtracts it when placing a clip.

## Limits

One shoot, one day, 164 camera clips. This note covers decoding only. It makes no claim about how closely the units agreed with each other.
