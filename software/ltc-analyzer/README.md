# LTC Analyzer

Desktop tool that reads linear timecode from an audio input or from audio and video files, and reports accuracy and stability: frame continuity, skipped and duplicated frames, and drift. It is used to check the output of TimecodeMaster units.

## Modes

- **Live.** Decodes LTC from a sound-card input and shows the running timecode with continuity statistics.
- **Comparator.** Compares several live LTC sources against each other and flags differences above a frame threshold.
- **File analysis.** Scans audio or video files (through FFmpeg) and writes an HTML or text report.

## Install and run

Python 3.

```bash
pip install -r requirements.txt
python ltc_analyzer.py
```

On Windows, `install_and_run.bat` installs the dependencies and starts the tool, and `run.bat` starts it. File analysis needs FFmpeg; `ffmpeg_manager.py` looks for it on the path, and on Windows downloads a static build into the program folder when it is missing.

## Files

| File | Purpose |
|---|---|
| `ltc_analyzer.py` | Main window, live decoding, comparator |
| `file_analyzer.py` | File-based LTC scan |
| `report_generator.py` | HTML and text reports |
| `ffmpeg_manager.py` | Locates or installs FFmpeg |

Settings are saved to `ltc_analyzer_settings.json` next to the program and are not part of the repository.
