# LTC Sync App

A desktop application for syncing video and audio files using LTC (Linear Timecode). Automatically aligns multi-camera footage and audio recordings based on embedded timecode.

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)
![License](https://img.shields.io/badge/License-PolyForm%20Noncommercial-blue.svg)

## Features

### Timecode
- **LTC Detection** - Automatically reads Linear Timecode from audio tracks
- **BWF Support** - Reads embedded timecode from Broadcast Wave Format files
- **Apply LTC** - Embed timecode into audio files (non-destructive, fast overwrite)
- **FPS Detection** - Auto-detect 23.976, 24, 25, 29.97, 30 fps
- **Manual FPS Override** - Override detected FPS per clip

### Sync
- **Multi-camera Sync** - Align footage from unlimited cameras
- **Audio Sync** - Sync audio recorders (ZOOM, Sound Devices, etc.)
- **Split File Handling** - Seamless support for recorder file splits (-0001, -0002)
- **24h+ Recording** - Handles timecode wrap for recordings over 24 hours
- **Linked Audio** - Auto-link multi-track audio to LTC reference track

### Sync Modes
- **TC Only** - Simple timecode-based sync
- **TC + Date** - Multi-day shoots with overlapping timecodes
- **TC + Filename** - Separate sources that share same timecode

### Timeline
- **Visual Timeline** - Drag, trim, and arrange clips
- **Video Preview** - Real-time video playback with VLC
- **Audio Waveforms** - Visual audio waveform display
- **Multi-track** - Separate video and audio lanes
- **Mute/Solo** - Per-track audio control

### Export
- **DaVinci Resolve** - FCPXML 1.8 with full metadata
- **Final Cut Pro** - Native FCPXML support
- **Premiere Pro** - Import via FCPXML
- **Preserves Metadata** - Camera names, reel numbers, timecode
- **Camera Audio Choice** - "Choose..." picks which channels of each camera to keep; each is exported as its own mono track and the LTC channel is always left out
- **Multicam** - Resolve / FCPX: the exported timeline is one multicam clip, an angle per camera and per recorder track
- **Share Tracks** - takes of the same recorder track share one audio track when they don't overlap; another recorder always gets its own tracks
- **Remove Gaps** - exports leave out the empty time between takes; the number next to it is the gap in seconds to keep (0 = about 0.1 s). Overlapping clips move together, so sync is kept; the app's timeline keeps the real timecode
- **Cam Offset...** - shift every clip of a camera by frames (picture late = minus, early = plus); saved and applied on every sync and export
- **Camera numbers in Resolve** - a Resolve export also writes `<name>_camera_metadata.csv`: File > Import Metadata To > Media Pool, tick "Ignore file extension", untick "Match using clip start and end timecode"
- **Premiere Pro** - each camera on its own video track

### Performance
- **Decode: Auto / GPU / CPU** - video decoding for preview, playback and thumbnails. Auto uses the GPU where it measured faster (VLC playback, thumbnails) and the CPU for OpenCV frame grabs. LTC analysis reads audio only and runs on the CPU

## Requirements

- **FFmpeg** - [Download](https://ffmpeg.org/download.html)
- **VLC** - [Download](https://www.videolan.org/vlc/)

## Installation

### Run from source
```bash
# Clone the TimecodeMaster repository
git clone https://github.com/idnawsi/TimecodeMaster.git
cd TimecodeMaster/software/ltc-sync-app

# Install dependencies
pip install -r requirements.txt

# Run
python ltc_sync_app.py
```

### Build an executable
See [BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md). No pre-built executable is published yet.

## Quick Start

1. **Add Files** - Drag & drop video/audio files into the app
2. **Analyze** - App automatically detects LTC timecode
3. **Sync** - Click "Sync by TC" to align all clips
4. **Export** - Export FCPXML for your NLE

## Sync Modes

| Mode | Use Case |
|------|----------|
| **TC Only** | Single day shoot, all devices synced to same TC |
| **TC + Date** | Multi-day shoots with overlapping timecodes |
| **TC + Filename** | Multiple cameras with same TC, separate by source |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play/Pause |
| `J` / `L` | Reverse / Forward playback |
| `K` | Stop |
| `←` / `→` | Frame step |
| `I` / `O` | Set In/Out points |
| `M` | Mute/Unmute track |

## Export Formats

- **DaVinci Resolve** - FCPXML 1.8
- **Final Cut Pro** - FCPXML 1.8
- **Premiere Pro** - FCPXML (import via Media Browser)

## Building from Source

```bash
# Install build tools
pip install pyinstaller

# Build
python build.py

# Output: dist/LTC Sync/
```

See [BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md) for detailed build instructions.

## License

PolyForm Noncommercial License 1.0.0 with an additional permission. Free for personal and other noncommercial use, and for small independent users (revenue under US$100,000 a year) on their own paid work; other commercial use needs a paid license. See [LICENSE](../../LICENSE), [ADDITIONAL-PERMISSION.md](../../ADDITIONAL-PERMISSION.md) and [COMMERCIAL.md](../../COMMERCIAL.md).

## Support

- [Report issues](https://github.com/idnawsi/TimecodeMaster/issues)
