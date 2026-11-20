"""
FFmpeg Manager - Handles portable FFmpeg installation for video file support.
Checks system PATH first, then downloads to app folder if not found.
"""

import os
import sys
import zipfile
import shutil
import tempfile
import subprocess
import threading
from pathlib import Path

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


class FFmpegManager:
    """Manages FFmpeg - checks PATH first, then falls back to portable installation."""

    # FFmpeg download URL (Windows static build from BtbN)
    FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

    def __init__(self, app_dir=None):
        """Initialize FFmpeg manager.

        Args:
            app_dir: Application directory. Defaults to the directory containing this file.
        """
        if app_dir is None:
            self.app_dir = Path(__file__).parent
        else:
            self.app_dir = Path(app_dir)

        self.ffmpeg_dir = self.app_dir / "ffmpeg"

        # These will be set by _detect_ffmpeg()
        self.ffmpeg_path = None
        self.ffprobe_path = None
        self._use_system_ffmpeg = False
        self._download_cancelled = False

        # Detect FFmpeg location on init
        self._detect_ffmpeg()

    def _detect_ffmpeg(self):
        """Detect FFmpeg - check system PATH first, then app folder."""
        # First, check system PATH (use existing installation if available)
        ffmpeg_in_path = shutil.which("ffmpeg")
        ffprobe_in_path = shutil.which("ffprobe")

        if ffmpeg_in_path and ffprobe_in_path:
            # Use system FFmpeg from PATH
            self.ffmpeg_path = Path(ffmpeg_in_path)
            self.ffprobe_path = Path(ffprobe_in_path)
            self._use_system_ffmpeg = True
            return

        # Second, check app folder (downloaded version)
        local_ffmpeg = self.ffmpeg_dir / ("ffmpeg.exe" if sys.platform == 'win32' else "ffmpeg")
        local_ffprobe = self.ffmpeg_dir / ("ffprobe.exe" if sys.platform == 'win32' else "ffprobe")

        if local_ffmpeg.exists() and local_ffprobe.exists():
            self.ffmpeg_path = local_ffmpeg
            self.ffprobe_path = local_ffprobe
            self._use_system_ffmpeg = False
            return

        # Not found anywhere - set to app folder for future download
        self.ffmpeg_path = local_ffmpeg
        self.ffprobe_path = local_ffprobe
        self._use_system_ffmpeg = False

    def is_available(self) -> bool:
        """Check if FFmpeg is available (either in PATH or app folder).

        Returns:
            True if ffmpeg and ffprobe are available.
        """
        # Re-detect in case PATH changed or files were downloaded
        self._detect_ffmpeg()

        if self._use_system_ffmpeg:
            return True

        return self.ffmpeg_path.exists() and self.ffprobe_path.exists()

    def get_status(self) -> str:
        """Get human-readable FFmpeg status.

        Returns:
            Status string describing FFmpeg availability and location.
        """
        self._detect_ffmpeg()

        if self._use_system_ffmpeg:
            return f"FFmpeg: System PATH ({self.ffmpeg_path.parent})"
        elif self.ffmpeg_path.exists() and self.ffprobe_path.exists():
            return "FFmpeg: App folder"
        else:
            return "FFmpeg: Not installed"

    def cancel_download(self):
        """Cancel ongoing download."""
        self._download_cancelled = True

    def download_ffmpeg(self, progress_callback=None, status_callback=None) -> bool:
        """Download and extract FFmpeg to the app folder.

        Note: This downloads to the app folder even if system FFmpeg exists,
        allowing users to have a local copy.

        Args:
            progress_callback: Function(percent) called with download progress 0-100
            status_callback: Function(message) called with status messages

        Returns:
            True if download and extraction succeeded.
        """
        if not REQUESTS_AVAILABLE:
            if status_callback:
                status_callback("Error: 'requests' library not installed. Run: pip install requests")
            return False

        self._download_cancelled = False

        # Always download to app folder (not system location)
        local_ffmpeg = self.ffmpeg_dir / ("ffmpeg.exe" if sys.platform == 'win32' else "ffmpeg")
        local_ffprobe = self.ffmpeg_dir / ("ffprobe.exe" if sys.platform == 'win32' else "ffprobe")

        try:
            # Create ffmpeg directory
            self.ffmpeg_dir.mkdir(parents=True, exist_ok=True)

            if status_callback:
                status_callback("Connecting to download server...")

            # Start download with streaming
            response = requests.get(self.FFMPEG_URL, stream=True, timeout=30)
            response.raise_for_status()

            # Get total size
            total_size = int(response.headers.get('content-length', 0))
            if status_callback:
                size_mb = total_size / (1024 * 1024)
                status_callback(f"Downloading FFmpeg ({size_mb:.1f} MB)...")

            # Download to temp file
            temp_zip = self.ffmpeg_dir / "ffmpeg_temp.zip"
            downloaded = 0

            with open(temp_zip, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if self._download_cancelled:
                        if status_callback:
                            status_callback("Download cancelled")
                        temp_zip.unlink(missing_ok=True)
                        return False

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total_size > 0:
                            percent = (downloaded / total_size) * 100
                            progress_callback(percent)

            if status_callback:
                status_callback("Extracting FFmpeg...")

            # Extract only ffmpeg.exe and ffprobe.exe to app folder
            with zipfile.ZipFile(temp_zip, 'r') as zf:
                # Find the bin folder inside the archive
                for name in zf.namelist():
                    if name.endswith('bin/ffmpeg.exe'):
                        # Extract ffmpeg.exe
                        with zf.open(name) as src, open(local_ffmpeg, 'wb') as dst:
                            dst.write(src.read())
                    elif name.endswith('bin/ffprobe.exe'):
                        # Extract ffprobe.exe
                        with zf.open(name) as src, open(local_ffprobe, 'wb') as dst:
                            dst.write(src.read())

            # Cleanup temp file
            temp_zip.unlink(missing_ok=True)

            # Re-detect to update paths (will now find app folder version)
            self._detect_ffmpeg()

            # Verify extraction
            if local_ffmpeg.exists() and local_ffprobe.exists():
                if status_callback:
                    status_callback("FFmpeg installed successfully!")
                return True
            else:
                if status_callback:
                    status_callback("Error: FFmpeg extraction failed")
                return False

        except requests.exceptions.RequestException as e:
            if status_callback:
                status_callback(f"Download error: {str(e)}")
            return False
        except zipfile.BadZipFile:
            if status_callback:
                status_callback("Error: Downloaded file is corrupted")
            return False
        except Exception as e:
            if status_callback:
                status_callback(f"Error: {str(e)}")
            return False

    def get_audio_info(self, file_path: str) -> dict:
        """Get audio stream information from a file using ffprobe.

        Args:
            file_path: Path to the media file

        Returns:
            Dictionary with audio info: sample_rate, channels, duration, codec
        """
        if not self.is_available():
            raise RuntimeError("FFmpeg not available. Call download_ffmpeg() first.")

        try:
            cmd = [
                str(self.ffprobe_path),
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                '-select_streams', 'a',
                file_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {'error': 'Failed to probe file'}

            import json
            data = json.loads(result.stdout)

            if not data.get('streams'):
                return {'error': 'No audio streams found'}

            # Get info from first audio stream
            stream = data['streams'][0]

            return {
                'sample_rate': int(stream.get('sample_rate', 48000)),
                'channels': int(stream.get('channels', 2)),
                'duration': float(stream.get('duration', 0)),
                'codec': stream.get('codec_name', 'unknown'),
                'stream_count': len(data['streams'])
            }

        except subprocess.TimeoutExpired:
            return {'error': 'Timeout reading file'}
        except Exception as e:
            return {'error': str(e)}

    def extract_audio(self, video_path: str, output_wav: str = None,
                      channel: int = None, progress_callback=None) -> str:
        """Extract audio from video file to WAV format.

        Args:
            video_path: Path to the video file
            output_wav: Output WAV path. If None, creates temp file.
            channel: Specific channel to extract (0-indexed). None extracts all channels.
            progress_callback: Function(percent) for progress updates

        Returns:
            Path to the extracted WAV file
        """
        if not self.is_available():
            raise RuntimeError("FFmpeg not available. Call download_ffmpeg() first.")

        # Create temp file if no output specified
        if output_wav is None:
            temp_dir = tempfile.gettempdir()
            output_wav = os.path.join(temp_dir, f"ltc_extract_{os.getpid()}.wav")

        try:
            cmd = [
                str(self.ffmpeg_path),
                '-y',  # Overwrite output
                '-i', video_path,
                '-vn',  # No video
            ]

            # Extract specific channel if specified
            if channel is not None:
                cmd.extend(['-af', f'pan=mono|c0=c{channel}'])

            cmd.extend([
                '-acodec', 'pcm_s16le',  # 16-bit PCM
                '-ar', '48000',  # 48kHz sample rate
                output_wav
            ])

            # Run extraction
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            stdout, stderr = process.communicate(timeout=300)  # 5 minute timeout

            if process.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore')
                raise RuntimeError(f"FFmpeg extraction failed: {error_msg}")

            if not os.path.exists(output_wav):
                raise RuntimeError("FFmpeg did not create output file")

            return output_wav

        except subprocess.TimeoutExpired:
            process.kill()
            raise RuntimeError("Audio extraction timed out")
        except Exception as e:
            # Clean up partial file
            if output_wav and os.path.exists(output_wav):
                try:
                    os.remove(output_wav)
                except:
                    pass
            raise

    def extract_all_channels(self, video_path: str, output_wav: str = None) -> str:
        """Extract all audio channels from video to multi-channel WAV.

        Args:
            video_path: Path to the video file
            output_wav: Output WAV path. If None, creates temp file.

        Returns:
            Path to the extracted WAV file
        """
        return self.extract_audio(video_path, output_wav, channel=None)
