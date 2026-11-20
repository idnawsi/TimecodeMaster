"""
File Analyzer - Analyzes audio and video files for LTC timecode errors.
Detects frame skips, duplicates, and errors in recorded timecode.
"""

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Callable
from collections import deque

import numpy as np

try:
    from scipy.io import wavfile
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from ffmpeg_manager import FFmpegManager


@dataclass
class IssueRecord:
    """Single issue detected during analysis."""
    timestamp_seconds: float
    timecode: str
    issue_type: str  # 'SKIP', 'DUPLICATE', 'ERROR'
    description: str
    frame_number: int
    skipped_count: int = 0  # Number of frames skipped (for SKIP type)


@dataclass
class OverlapRecord:
    """Record of overlapping timecode between two files."""
    file1: str
    file2: str
    file1_range: str  # "HH:MM:SS:FF - HH:MM:SS:FF"
    file2_range: str
    overlap_start: str  # Timecode where overlap starts
    overlap_end: str    # Timecode where overlap ends
    overlap_frames: int  # Number of overlapping frames
    overlap_duration: float  # Duration in seconds


@dataclass
class AnalysisResult:
    """Results from file analysis."""
    file_path: str
    file_type: str  # 'wav' or 'video'
    duration_seconds: float
    total_frames: int
    detected_fps: float
    ltc_channel: int
    channel_detection_method: str  # 'auto' or 'manual'

    # Issues found
    issues: List[IssueRecord] = field(default_factory=list)
    skip_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    skipped_frames_total: int = 0  # Total number of frames skipped

    # Timecode range
    first_timecode: str = ""
    last_timecode: str = ""

    # Quality metrics
    clean_percentage: float = 0.0

    # Analysis metadata
    sample_rate: int = 48000
    audio_channels: int = 1


class LTCChannelDetector:
    """Detects which audio channel contains LTC timecode."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate

    def detect_ltc_channel(self, audio_data: np.ndarray) -> Tuple[int, float]:
        """Auto-detect which channel contains LTC audio.

        Args:
            audio_data: Audio data array, shape (samples,) or (samples, channels)

        Returns:
            Tuple of (channel_index, confidence_score)
        """
        # Handle mono audio
        if len(audio_data.shape) == 1:
            score = self._score_ltc_likelihood(audio_data)
            return (0, score)

        num_channels = audio_data.shape[1]
        scores = []

        # Sample first 5 seconds of each channel
        sample_length = min(int(self.sample_rate * 5), len(audio_data))

        for ch in range(num_channels):
            samples = audio_data[:sample_length, ch]
            score = self._score_ltc_likelihood(samples)
            scores.append((ch, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        best_channel, best_score = scores[0]

        return (best_channel, best_score)

    def _score_ltc_likelihood(self, samples: np.ndarray) -> float:
        """Score how likely these samples contain LTC.

        Scoring criteria:
        - Signal level (RMS above threshold)
        - Zero crossing rate in expected range
        - Successful frame decode attempts

        Args:
            samples: Audio samples to analyze

        Returns:
            Score indicating LTC likelihood (higher = more likely)
        """
        score = 0.0

        # Normalize samples to float if needed
        if samples.dtype in [np.int16, np.int32]:
            samples = samples.astype(np.float32) / np.iinfo(samples.dtype).max

        # Check signal level
        rms = np.sqrt(np.mean(samples ** 2))
        if rms < 0.005:  # Too quiet
            return 0.0
        score += 10  # Has signal

        # Check for clipping
        peak = np.max(np.abs(samples))
        if peak < 0.99:
            score += 5  # Not clipped

        # Count zero crossings
        signs = np.sign(samples)
        # Handle zeros - replace with previous non-zero sign
        for i in range(1, len(signs)):
            if signs[i] == 0:
                signs[i] = signs[i - 1]

        crossings = np.sum(np.diff(signs) != 0)
        duration = len(samples) / self.sample_rate
        crossing_rate = crossings / duration if duration > 0 else 0

        # LTC at 30fps has 80 bits * 30 = 2400 transitions/sec minimum
        # With bi-phase mark encoding, roughly 2400-4800 crossings/sec
        if 2000 < crossing_rate < 6000:
            score += 20  # Crossing rate in LTC range
        elif 1500 < crossing_rate < 7000:
            score += 10  # Close to LTC range

        # Try to decode frames using a simple sync word search
        # Import LTCDecoder here to avoid circular imports
        try:
            from ltc_analyzer import LTCDecoder
            decoder = LTCDecoder(self.sample_rate)
            frames = decoder.process_samples_fast(samples)

            if len(frames) > 0:
                score += 50  # Successfully decoded frames!
                score += min(len(frames), 50)  # Bonus for more frames (cap at 50)
        except:
            pass  # If decoder fails, just use other metrics

        return score


class FileAnalyzer:
    """Analyzes audio/video files for LTC timecode errors."""

    # Process audio in chunks for memory efficiency
    CHUNK_SECONDS = 10

    def __init__(self):
        self.ffmpeg = FFmpegManager()
        self.channel_detector = LTCChannelDetector()
        self.cancelled = False
        self._temp_files = []

    def cancel(self):
        """Cancel ongoing analysis."""
        self.cancelled = True

    def cleanup(self):
        """Clean up temporary files."""
        for temp_file in self._temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except:
                pass
        self._temp_files.clear()

    def analyze_file(self, file_path: str, channel: str = 'auto',
                     fps: str = 'auto',
                     progress_callback: Callable[[float], None] = None,
                     status_callback: Callable[[str], None] = None) -> AnalysisResult:
        """Analyze a file for LTC timecode errors.

        Args:
            file_path: Path to audio or video file
            channel: 'auto' for auto-detection, or channel number (0-indexed as string)
            fps: 'auto' for auto-detection, or specific fps value (e.g., '29.97', '30', '29.97DF')
            progress_callback: Function(percent) for progress updates
            status_callback: Function(message) for status messages

        Returns:
            AnalysisResult with all findings
        """
        self.cancelled = False

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        # Determine file type
        ext = os.path.splitext(file_path)[1].lower()
        audio_extensions = ['.wav', '.wave']
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm']

        if ext in audio_extensions:
            file_type = 'wav'
            audio_path = file_path
        elif ext in video_extensions:
            file_type = 'video'
            # Need to extract audio
            if status_callback:
                status_callback("Extracting audio from video...")
            audio_path = self._extract_video_audio(file_path, status_callback)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        # Load audio file
        if status_callback:
            status_callback("Loading audio file...")

        audio_data, sample_rate = self._load_audio_file(audio_path)

        # Detect or select channel
        if channel == 'auto':
            if status_callback:
                status_callback("Auto-detecting LTC channel...")
            ltc_channel, confidence = self.channel_detector.detect_ltc_channel(audio_data)
            detection_method = 'auto'

            if confidence < 10:
                raise ValueError("No LTC signal detected in any channel. Try selecting channel manually.")
        else:
            ltc_channel = int(channel)
            detection_method = 'manual'

        # Extract single channel if multi-channel
        if len(audio_data.shape) > 1:
            num_channels = audio_data.shape[1]
            if ltc_channel >= num_channels:
                raise ValueError(f"Channel {ltc_channel} not available. File has {num_channels} channels.")
            channel_data = audio_data[:, ltc_channel]
        else:
            num_channels = 1
            channel_data = audio_data

        # Normalize to float
        if channel_data.dtype in [np.int16, np.int32]:
            channel_data = channel_data.astype(np.float32) / np.iinfo(channel_data.dtype).max

        # Process audio through LTC decoder
        if status_callback:
            status_callback("Analyzing timecode...")

        result = self._process_audio_data(
            file_path, file_type, channel_data, sample_rate,
            ltc_channel, detection_method, num_channels,
            fps, progress_callback, status_callback
        )

        # Cleanup temp files
        self.cleanup()

        return result

    def _load_audio_file(self, file_path: str) -> Tuple[np.ndarray, int]:
        """Load WAV audio file.

        Args:
            file_path: Path to WAV file

        Returns:
            Tuple of (audio_data, sample_rate)
        """
        if not SCIPY_AVAILABLE:
            raise RuntimeError("scipy not installed. Run: pip install scipy")

        sample_rate, audio_data = wavfile.read(file_path)
        return audio_data, sample_rate

    def _extract_video_audio(self, video_path: str,
                             status_callback: Callable[[str], None] = None) -> str:
        """Extract audio from video file.

        Args:
            video_path: Path to video file
            status_callback: Status callback function

        Returns:
            Path to extracted WAV file
        """
        if not self.ffmpeg.is_available():
            raise RuntimeError("FFmpeg not available. Please download FFmpeg first.")

        # Create temp file for extracted audio (unique per thread to support parallel processing)
        temp_dir = tempfile.gettempdir()
        unique_id = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        temp_wav = os.path.join(temp_dir, f"ltc_extract_{unique_id}.wav")
        self._temp_files.append(temp_wav)

        # Extract all channels
        return self.ffmpeg.extract_all_channels(video_path, temp_wav)

    def _process_audio_data(self, file_path: str, file_type: str,
                            audio_data: np.ndarray, sample_rate: int,
                            ltc_channel: int, detection_method: str,
                            num_channels: int, fps: str = 'auto',
                            progress_callback: Callable[[float], None] = None,
                            status_callback: Callable[[str], None] = None) -> AnalysisResult:
        """Process audio data through LTC decoder and analyzer.

        Args:
            file_path: Original file path
            file_type: 'wav' or 'video'
            audio_data: Single channel audio data
            sample_rate: Audio sample rate
            ltc_channel: Channel being analyzed
            detection_method: 'auto' or 'manual'
            num_channels: Total number of channels in file
            fps: 'auto' for auto-detection, or specific fps value
            progress_callback: Progress callback
            status_callback: Status callback

        Returns:
            AnalysisResult
        """
        # Import here to avoid circular imports
        from ltc_analyzer import LTCDecoder, LTCAnalyzer

        decoder = LTCDecoder(sample_rate)
        analyzer = LTCAnalyzer()
        analyzer.set_sample_rate(sample_rate)

        # Set expected FPS if not auto
        if fps and fps.lower() != 'auto':
            if fps == '29.97DF':
                fps_value = 29.97
            else:
                fps_value = float(fps)
            analyzer.set_fps(fps_value)
            decoder.set_expected_fps(fps_value)

        # Initialize result
        issues = []
        total_frames = 0
        first_tc = None
        last_tc = None
        has_drop_frame = False

        # Process in chunks
        chunk_size = int(sample_rate * self.CHUNK_SECONDS)
        total_samples = len(audio_data)
        processed = 0

        # Early detection settings - check first N seconds for LTC
        early_detect_seconds = 10  # Check first 10 seconds
        early_detect_samples = int(sample_rate * early_detect_seconds)
        ltc_detected = False

        while processed < total_samples and not self.cancelled:
            chunk_end = min(processed + chunk_size, total_samples)
            chunk = audio_data[processed:chunk_end]

            # Decode LTC frames from chunk
            frames = decoder.process_samples_fast(chunk)

            # Track if we've found any LTC
            if frames:
                ltc_detected = True

            # Early skip: if no LTC found in first N seconds, skip this file
            if not ltc_detected and processed >= early_detect_samples:
                raise ValueError(f"No LTC timecode detected in first {early_detect_seconds} seconds - skipping file")

            # Analyze each frame
            for frame in frames:
                result = analyzer.analyze_frame(frame)
                total_frames += 1

                # Track drop frame flag
                if frame.get('drop_frame', False):
                    has_drop_frame = True

                # Record timecode
                tc = result['timecode']
                if first_tc is None:
                    first_tc = tc
                last_tc = tc

                # Record issues
                if result['status'] != 'OK':
                    timestamp = processed / sample_rate
                    issue = IssueRecord(
                        timestamp_seconds=timestamp,
                        timecode=tc,
                        issue_type=result['status'],
                        description=result.get('issue', ''),
                        frame_number=total_frames,
                        skipped_count=result.get('diff', 1) - 1 if result['status'] == 'SKIP' else 0
                    )
                    issues.append(issue)

            processed = chunk_end

            if progress_callback:
                progress_callback((processed / total_samples) * 100)

        # Calculate statistics
        duration = len(audio_data) / sample_rate
        skip_count = sum(1 for i in issues if i.issue_type == 'SKIP')
        duplicate_count = sum(1 for i in issues if i.issue_type == 'DUPLICATE')
        error_count = sum(1 for i in issues if i.issue_type == 'ERROR')
        skipped_frames = sum(i.skipped_count for i in issues if i.issue_type == 'SKIP')

        # Calculate clean percentage
        if total_frames > 0:
            total_issues = skip_count + duplicate_count + error_count
            clean_percentage = ((total_frames - total_issues) / total_frames) * 100
        else:
            clean_percentage = 0.0

        # Get detected FPS - calculate from actual frame count and duration for accuracy
        nominal_fps = analyzer.get_nominal_fps()

        # Calculate actual measured FPS from frame count and audio duration
        if total_frames > 0 and duration > 1.0:
            measured_fps = total_frames / duration

            # Determine precise FPS based on measurement and drop frame flag
            if has_drop_frame:
                # Drop frame is always 29.97
                detected_fps = 29.97
            elif nominal_fps == 30 or (29 < measured_fps < 31):
                # Distinguish 29.97 vs 30: threshold at 29.985
                if measured_fps < 29.985:
                    detected_fps = 29.97
                else:
                    detected_fps = 30
            elif nominal_fps == 24 or (23 < measured_fps < 25):
                # Distinguish 23.976 vs 24: threshold at 23.988
                if measured_fps < 23.988:
                    detected_fps = 23.976
                else:
                    detected_fps = 24
            elif nominal_fps == 25 or (24.5 < measured_fps < 25.5):
                detected_fps = 25
            else:
                detected_fps = nominal_fps
        else:
            detected_fps = nominal_fps

        return AnalysisResult(
            file_path=file_path,
            file_type=file_type,
            duration_seconds=duration,
            total_frames=total_frames,
            detected_fps=detected_fps,
            ltc_channel=ltc_channel,
            channel_detection_method=detection_method,
            issues=issues,
            skip_count=skip_count,
            duplicate_count=duplicate_count,
            error_count=error_count,
            skipped_frames_total=skipped_frames,
            first_timecode=first_tc or "--:--:--:--",
            last_timecode=last_tc or "--:--:--:--",
            clean_percentage=clean_percentage,
            sample_rate=sample_rate,
            audio_channels=num_channels
        )

    def get_file_info(self, file_path: str) -> dict:
        """Get basic information about a file.

        Args:
            file_path: Path to audio or video file

        Returns:
            Dictionary with file info
        """
        ext = os.path.splitext(file_path)[1].lower()
        audio_extensions = ['.wav', '.wave']
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm']

        info = {
            'path': file_path,
            'name': os.path.basename(file_path),
            'extension': ext,
            'size_bytes': os.path.getsize(file_path) if os.path.exists(file_path) else 0
        }

        if ext in audio_extensions:
            info['type'] = 'audio'
            try:
                sample_rate, audio_data = wavfile.read(file_path)
                info['sample_rate'] = sample_rate
                info['channels'] = audio_data.shape[1] if len(audio_data.shape) > 1 else 1
                info['duration'] = len(audio_data) / sample_rate
            except:
                pass

        elif ext in video_extensions:
            info['type'] = 'video'
            if self.ffmpeg.is_available():
                audio_info = self.ffmpeg.get_audio_info(file_path)
                info.update(audio_info)
        else:
            info['type'] = 'unknown'

        return info


def timecode_to_frames(tc_str: str, fps: float = 30.0) -> int:
    """Convert timecode string to total frame count.

    Args:
        tc_str: Timecode string in format "HH:MM:SS:FF"
        fps: Frame rate (default 30)

    Returns:
        Total frame count
    """
    if not tc_str or tc_str == "--:--:--:--":
        return -1

    try:
        parts = tc_str.replace(';', ':').split(':')
        if len(parts) != 4:
            return -1

        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
        frames = int(parts[3])

        nominal_fps = int(round(fps))
        total = frames
        total += seconds * nominal_fps
        total += minutes * 60 * nominal_fps
        total += hours * 3600 * nominal_fps

        return total
    except (ValueError, IndexError):
        return -1


def frames_to_timecode(total_frames: int, fps: float = 30.0) -> str:
    """Convert total frames to timecode string.

    Args:
        total_frames: Total frame count
        fps: Frame rate (default 30)

    Returns:
        Timecode string in format "HH:MM:SS:FF"
    """
    if total_frames < 0:
        return "--:--:--:--"

    nominal_fps = int(round(fps))

    frames = total_frames % nominal_fps
    total_seconds = total_frames // nominal_fps
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def find_timecode_overlaps(results: List[AnalysisResult]) -> List[OverlapRecord]:
    """Find overlapping timecode ranges between analyzed files.

    Args:
        results: List of AnalysisResult from batch analysis

    Returns:
        List of OverlapRecord describing each overlap found
    """
    overlaps = []

    # Filter results that have valid timecode ranges
    valid_results = []
    for r in results:
        if r and r.first_timecode and r.last_timecode:
            if r.first_timecode != "--:--:--:--" and r.last_timecode != "--:--:--:--":
                valid_results.append(r)

    # Compare each pair of files
    for i in range(len(valid_results)):
        for j in range(i + 1, len(valid_results)):
            r1 = valid_results[i]
            r2 = valid_results[j]

            # Use the average FPS of both files
            fps = (r1.detected_fps + r2.detected_fps) / 2 if r1.detected_fps and r2.detected_fps else 30.0

            # Convert timecodes to frames
            r1_start = timecode_to_frames(r1.first_timecode, fps)
            r1_end = timecode_to_frames(r1.last_timecode, fps)
            r2_start = timecode_to_frames(r2.first_timecode, fps)
            r2_end = timecode_to_frames(r2.last_timecode, fps)

            # Skip invalid timecodes
            if r1_start < 0 or r1_end < 0 or r2_start < 0 or r2_end < 0:
                continue

            # Handle case where end < start (shouldn't happen but be safe)
            if r1_end < r1_start:
                r1_start, r1_end = r1_end, r1_start
            if r2_end < r2_start:
                r2_start, r2_end = r2_end, r2_start

            # Check for overlap: ranges overlap if start1 <= end2 AND start2 <= end1
            if r1_start <= r2_end and r2_start <= r1_end:
                # Calculate overlap range
                overlap_start_frames = max(r1_start, r2_start)
                overlap_end_frames = min(r1_end, r2_end)
                overlap_frames = overlap_end_frames - overlap_start_frames + 1

                if overlap_frames > 0:
                    overlap_duration = overlap_frames / fps

                    overlap = OverlapRecord(
                        file1=os.path.basename(r1.file_path),
                        file2=os.path.basename(r2.file_path),
                        file1_range=f"{r1.first_timecode} - {r1.last_timecode}",
                        file2_range=f"{r2.first_timecode} - {r2.last_timecode}",
                        overlap_start=frames_to_timecode(overlap_start_frames, fps),
                        overlap_end=frames_to_timecode(overlap_end_frames, fps),
                        overlap_frames=overlap_frames,
                        overlap_duration=overlap_duration
                    )
                    overlaps.append(overlap)

    return overlaps
