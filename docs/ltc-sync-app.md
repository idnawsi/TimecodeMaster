# LTC Sync App

The LTC Sync App is the post-production half of the system. TimecodeMaster units put the same LTC on every camera and recorder during the shoot. The app reads that LTC back from the recorded files, places every clip on one timeline by its timecode, and exports the result as a timeline for DaVinci Resolve, Premiere Pro or Final Cut Pro X.

It is a Python and Tkinter desktop program. Source: [software/ltc-sync-app](../software/ltc-sync-app/). It runs on Windows, Linux and macOS.

## Where it fits

```
 shoot                                                  post
 -----                                                  ----
 TimecodeMaster units  --LTC on 3.5 mm-->  camera audio / recorder track
                                                  |
                                                  v
                          video and audio files with LTC recorded in them
                                                  |
                                                  v
                                       LTC Sync App: decode, align
                                                  |
                                                  v
                          FCPXML / EDL  -->  Resolve, Premiere, Final Cut
```

Without an app like this, multi-camera and separate-sound footage is lined up by waveform matching or by hand. With LTC on every file, each clip carries its own absolute time, and alignment is exact and repeatable.

## What goes on set

For the app to work well, record the units' output correctly.

1. Connect each unit's LTC I/O jack to one audio channel of its camera, or to one track of the sound recorder. Use LINE or MIC level (LEVEL button) to match the input, and keep the recording level moderate. LTC is a loud square wave and clips easily.
2. Set the same frame rate on all units before the first take (see [operation.md](operation.md)), and set the same frame rate in the cameras.
3. Sync all clients to the master and check the OLEDs. Resync during breaks on long days (see the drift budget in [timing.md](timing.md)).
4. Give each unit a two-digit numeric camera ID (00 to 99). The unit writes it, with the date, into the LTC user bits, and the app uses it to name and group cameras. Letters are not read as camera numbers.
5. If a camera shows its picture slightly early or late against sound, note which one. The app can correct this per camera afterwards, or set the LTC output offset on the unit.

Leave the LTC channel in the file. The app finds it, uses it for alignment, and leaves it out of the exports unless told otherwise.

## Install and run

Requirements: Python 3, FFmpeg, and VLC (64-bit, for preview playback). Install the Python packages and start the app:

```
cd software/ltc-sync-app
pip install -r requirements.txt
python ltc_sync_app.py
```

On Windows, `run_ltc_sync.bat` does the same. To build a standalone executable with PyInstaller, see [BUILD_INSTRUCTIONS.md](../software/ltc-sync-app/BUILD_INSTRUCTIONS.md). Video decoding for preview can use the GPU (PyAV is optional); LTC analysis reads audio only and runs on the CPU.

## Basic workflow

1. **Add media.** Use "+ Add Files" or "+ Add Folder", or drag files into the window. Video and audio files are accepted. Files from one recorder that were split by size (-0001, -0002) are joined into one recording.
2. **Analyze.** The app scans each file's audio for LTC, reads the starting timecode, the frame rate, drop-frame state, and the user bits (date and camera ID). Files that have no LTC but carry embedded timecode (BWF time reference or camera metadata) can use that instead. If LTC does not start at the beginning of a file, the app searches the first 30 s for it. If the timecode jumps somewhere inside a file (a re-jam or a reset), the clip card shows a warning.
3. **Check the table.** Each clip shows its start timecode, frame rate and camera. If the frame rate is wrong, set "Expected FPS" (Auto, 23.976, 24, 25, 29.97 DF, 29.97 NDF, 30) and re-analyze, or override a single clip from its context menu.
4. **Sync.** The timeline places every clip at its timecode. Press "Resync" after changing any setting.
5. **Review.** Play the timeline, step frame by frame, and check a clap or any shared sound across cameras. Trim, split, group, lock or nudge clips if needed.
6. **Export.** Choose the target program, set the export options, and press "Export XML".

Projects can be saved and loaded ("Save Project", "Load Project"). A project keeps the clip list, trims, split points, locks, groups, track states, camera offsets and export settings.

## Sync logic

Chosen in the "Sync Logic" box.

| Mode | Use it when |
|---|---|
| TC Only | One shooting day, every device on the same timecode. Clips are placed by timecode alone |
| TC + Date | A multi-day shoot where timecode repeats. The date from the LTC user bits separates the days, so each day's clips are laid out apart |
| TC + Filename | Separate sources whose timecode overlaps and that must stay apart. Grouping is by file name pattern or folder |

The timecode source per clip is Auto (global setting), LTC (audio), or Embedded (file). "Use Emb for No-LTC" switches every clip without LTC to its embedded timecode in one step. A clip's source can also be set from its right-click menu, under "TC Source for Sync".

## Reading cameras

The camera of a clip is taken from the LTC user bits ("Camera ID source: LTC User Bits") or from the file name. Cameras get their own colour and their own video track on the timeline. The numbers also reach Resolve (see below).

## Export

Targets: Premiere Pro, DaVinci Resolve, Final Cut Pro X. All three use FCPXML 1.8. An EDL and a text report ("Report") of all clips and their timecodes can also be written.

| Option | Effect |
|---|---|
| Mute LTC | The LTC channel is exported muted. It is never exported as audio you could hear by accident |
| Camera Audio, "Choose..." | Pick which channels of each camera go into the timeline. Each channel becomes its own mono track, and the LTC channel is always left out |
| Split Stereo | Stereo audio is split into two mono tracks |
| Use Linked Audio | Multi-track audio from a recorder is linked to its LTC reference track |
| Multicam | Resolve and Final Cut: the timeline is one multicam clip with an angle per camera and per recorder track |
| Share Tracks | Takes from the same recorder track that do not overlap share one audio track, instead of each take getting its own |
| Remove Gaps (0, 1, 2, 5, 10 s) | The export leaves out empty time between takes and keeps the chosen gap. Overlapping clips move together, so sync is preserved. The timeline in the app keeps the real timecode |
| Cam Offset... | Shifts all clips of a camera by a number of frames. Picture late against sound: enter a negative number. Picture early: positive. Saved in the project and applied to every sync and export |
| Drift fix (1 to 30 min) | Resolve export only. Long recordings that carry LTC are cut every N minutes on the timeline and each piece is placed by its own LTC. No new files are written; every piece points at the original WAV. This corrects a recorder's own clock error, which for a recorder of ordinary quality can reach several frames over a long day |

Premiere Pro exports put each camera on its own video track. The sequence frame rate is the most common frame rate among the video clips.

### Importing into Resolve

A Resolve export writes the FCPXML and `<name>_camera_metadata.csv`. To get the camera numbers into the media pool: File, Import Metadata To, Media Pool; select the CSV; tick "Ignore file extension" and untick "Match using clip start and end timecode".

## Writing LTC into files

"Apply LTC" writes the decoded timecode into the files' metadata (broadcast WAV time reference) so that the timecode survives in programs that do not read LTC from audio. You can write to copies in an output directory or overwrite the originals. For 23.976 and 29.97 the time reference is computed with the exact rate (30000/1001).

When a video file is overwritten, the app first checks that there is enough free space, writes the new file next to the original, checks that it has the same stream types and nearly the same duration, and only then swaps it in. A failed check leaves the original untouched. Overwriting is still irreversible and has no backup, so work on copies unless you have your own backup.

## Frame rates and the non-integer rates

The app handles 23.976, 24, 25, 29.97 (drop and non-drop) and 30 fps. For 23.976 and 29.97 it uses the exact rates (24000/1001 and 30000/1001), and recorded 50p and 59.94p video is counted at its own frame rate while the LTC stays at 25 or 29.97. Timecode that wraps past 24 hours in a long recording is handled.

## Field test

On one real shoot (two cameras and a Zoom H6, 25 fps, about 7.4 hours of media) the app found LTC in all 164 camera clips and all 23 recorder takes, with no warnings. The details are in [field-test.md](field-test.md).

## Try it without footage

`software/ltc-sync-app/tools/make_test_media.py` writes a small synthetic shoot: three "camera" clips (FFmpeg test video, a tone on channel 1 and LTC on channel 2) and one recorder WAV that runs underneath them. The LTC carries the date and a camera ID in the user bits, in the layout the firmware uses.

```
python tools/make_test_media.py test_media --fps 25
```

Add the files from `test_media` to the app, press Analyze and Resync. The recorder starts at 09:59:55:00 and the cameras at 10:00:00:00, 10:00:12:10 and 10:00:25:00, so the cameras appear on the timeline about 5, 17 and 30 seconds after the recorder. The clip list shows CAM01 and CAM03 starting at 10:00:00:01 and 10:00:25:01, one frame after the generated start. The first LTC frame of those clips is not decodable (probably the AAC encoder start-up), so the first frame the app reads is the next one, and it records where that frame starts in samples (1920, which is 40 ms at 48 kHz). The timeline subtracts that offset, so the clips land at 5.00, 17.40 and 30.00 s after the recorder, exactly as generated. The app takes the date and camera ID from the most common value across the decoded frames, so a damaged first frame does not decide the clip's date.

## Known limitations

These come from testing on generated media and one real shoot (see [field-test.md](field-test.md)).

- **LTC that starts very late.** If no channel has LTC at the start of a file, the app searches the first 30 s. LTC that starts later than that is reported as no LTC. Starts up to 20 s were found with the right offset in the tests.
- **Timecode jumps.** The jump warning compares the end of the file with its start. A jump that is undone before the end is not seen. The clip is still placed by its start timecode, so check a flagged clip before you trust the timeline.
- **Very weak LTC at low audio bitrate.** In one test, weak-level 25 fps LTC through 64 kbps AAC was detected as 30 fps in Auto mode with status "done", and at 32 kbps no LTC was found. A stronger test signal stayed at 25 fps through 32 to 128 kbps AAC. Set "Expected FPS" when a camera records low-level, low-bitrate audio.
- **Source timecode of clips with a damaged first frame.** The clip is placed correctly, but the source timecode written into the FCPXML for it appears to be the first readable frame, one frame after the true start (10:00:00:01 instead of 10:00:00:00 in the test shoot). Placement is not affected.
- **Recorder files with LTC on one channel.** In one Resolve export test a stereo recorder file with LTC on channel 2 did not appear on the timeline with the default settings (camera audio selection is off by default). Check that the recorder tracks arrived after you import.
- **Saved projects.** A project saved before the sub-frame start offset was kept in the analysis stores zero for it. Re-analyse the clips (Ctrl+Shift+R), and re-measure any camera offset you set before.

## Keyboard shortcuts

Press F1 in the app for the full list. The most used:

| Key | Action |
|---|---|
| Space | Play or pause |
| J, K, L | Reverse, pause, forward (repeat for faster) |
| Left, Right | Step one frame |
| I, O | Set in and out points |
| S | Split the clip at the playhead |
| M | Add a marker |
| N | Toggle snapping |
| Ctrl+Z, Ctrl+Y | Undo, redo |
| + , - | Zoom the timeline |

## Troubleshooting

| Symptom | Check |
|---|---|
| No LTC detected on a clip | Is the LTC channel really recorded? Is its level reasonable, not clipped and not far too low? Try the other channel layout in the camera. Set "Expected FPS" to the rate used on set, then Re-analyze |
| Wrong frame rate detected | Set "Expected FPS" or override the clip. Note that 29.97 drop and non-drop look alike in a short file |
| Clips of two days on top of each other | Use "TC + Date". The date comes from the unit's RTC, so check the RTC was set correctly on set |
| Camera shows 00 or no number | The camera ID on the unit was empty or contained letters. Set a two-digit number on the unit before shooting |
| Picture is a frame or two off against sound | Use "Cam Offset..." for that camera |
| Long recorder file slowly drifts against cameras | Switch on "Drift fix" for the Resolve export |
| Resolve shows no camera numbers | Import the `_camera_metadata.csv` as described above |
| Preview does not play | Install VLC (64-bit) and check that FFmpeg is on the path |

## Check a unit before the shoot

The [LTC Analyzer](../software/ltc-analyzer/) reads LTC live from a sound-card input, so you can verify a unit's output, or compare two units, before any footage exists. Its output also shows whether the level at the camera input is acceptable.
