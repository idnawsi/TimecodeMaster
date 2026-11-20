"""
LTC Timecode Analyzer
Reads LTC audio input and analyzes timecode accuracy, stability, and detects frame skips/duplicates.
Supports both live audio analysis and file-based analysis (audio and video files).
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import queue
import time
import os
import json
from datetime import datetime
from collections import deque, Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import sounddevice as sd
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    print("Please install: pip install sounddevice numpy")

# Import file analysis modules (optional - only needed for file analysis)
try:
    from file_analyzer import FileAnalyzer, AnalysisResult, find_timecode_overlaps, OverlapRecord
    from report_generator import ReportGenerator
    from ffmpeg_manager import FFmpegManager
    FILE_ANALYSIS_AVAILABLE = True
except ImportError:
    FILE_ANALYSIS_AVAILABLE = False


class LTCDecoder:
    """Decodes LTC (Linear Timecode) from audio samples using robust sync detection."""

    # LTC sync word: 0011111111111101 (bits 64-79)
    SYNC_WORD = [0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,1]
    SYNC_WORD_REV = [1,0,1,1,1,1,1,1,1,1,1,1,1,1,0,0]  # Reversed for backwards LTC

    def __init__(self, sample_rate=48000):
        self.sample_rate = sample_rate
        self.reset()

    def reset(self):
        self.last_sample = 0
        self.samples_since_edge = 0
        self.edge_intervals = deque(maxlen=500)
        self.bit_buffer = deque(maxlen=200)
        self.synced = False
        # Start with a default timing estimate for ~27fps (works for 24-30fps range)
        # This allows decoding to start immediately while we refine the timing
        default_fps = 27  # Middle ground between 24 and 30
        bits_per_second = default_fps * 80
        self.samples_per_bit = self.sample_rate / bits_per_second
        self.half_bit_samples = self.samples_per_bit / 2
        self.timing_refined = False  # Track if we've refined from default
        self.last_edge_was_half = False
        self.direction = 1  # 1 = forward, -1 = reverse
        self.frames_decoded = 0

    def set_sample_rate(self, sample_rate):
        self.sample_rate = sample_rate
        self.reset()

    def set_expected_fps(self, fps):
        """Set expected FPS to calculate bit timing."""
        if fps and fps > 0:
            bits_per_second = fps * 80
            self.samples_per_bit = self.sample_rate / bits_per_second
            self.half_bit_samples = self.samples_per_bit / 2
            self.timing_refined = True  # Manual FPS is considered refined
        else:
            # Keep default timing estimate (set in reset()) for auto mode
            # This allows decoding to start immediately
            self.timing_refined = False  # Will refine from edge intervals

    def auto_detect_timing(self):
        """Auto-detect bit timing from edge intervals."""
        if len(self.edge_intervals) < 40:
            return False

        intervals = np.array(list(self.edge_intervals))

        # Calculate expected range for valid LTC timing
        # At 48kHz: half-bit is 10 samples (30fps) to 12.5 samples (24fps)
        min_half_bit = self.sample_rate / (30 * 80 * 2) * 0.7  # 30fps with 30% tolerance = ~7
        max_half_bit = self.sample_rate / (23.976 * 80 * 2) * 1.3  # 23.976fps with 30% tolerance = ~16

        # Filter out intervals that are too small (noise) or too large (dropouts)
        # Only keep intervals that could plausibly be half-bit or full-bit
        min_valid = min_half_bit * 0.5  # Allow some margin
        max_valid = max_half_bit * 3  # Full bit could be 2x half bit, plus margin
        filtered = intervals[(intervals >= min_valid) & (intervals <= max_valid)]

        if len(filtered) < 20:
            return False

        # Sort filtered intervals
        sorted_intervals = np.sort(filtered)

        # For LTC biphase mark encoding:
        # - '1' bits have two transitions (half-bit intervals)
        # - '0' bits have one transition (full-bit intervals)
        # The shorter intervals are half-bits

        # Use the 25th percentile as half-bit estimate (lower quarter should be half-bits)
        half_bit_estimate = np.percentile(sorted_intervals, 25)

        # Validate the estimate is in expected range
        if min_half_bit <= half_bit_estimate <= max_half_bit:
            self.half_bit_samples = half_bit_estimate
            self.samples_per_bit = self.half_bit_samples * 2
            return True

        # Fallback: try median of lower third (more aggressive half-bit selection)
        lower_third = sorted_intervals[:len(sorted_intervals)//3]
        if len(lower_third) > 5:
            half_bit_fallback = np.median(lower_third)
            if min_half_bit <= half_bit_fallback <= max_half_bit:
                self.half_bit_samples = half_bit_fallback
                self.samples_per_bit = self.half_bit_samples * 2
                return True

        # Don't update timing if we can't find a valid value
        # Keep using the default timing estimate instead
        return False

    def process_samples(self, samples):
        """Process audio samples and return decoded frames."""
        return self.process_samples_fast(np.array(samples))

    def process_samples_fast(self, samples):
        """Process audio samples using numpy for speed."""
        frames = []

        if len(samples) == 0:
            return frames

        # Find zero crossings
        extended = np.concatenate([[self.last_sample], samples])
        signs = np.sign(extended)
        sign_changes = np.diff(signs)
        crossings = np.where(sign_changes != 0)[0]

        if len(crossings) == 0:
            self.samples_since_edge += len(samples)
            self.last_sample = samples[-1]
            return frames

        # Process edges
        prev_pos = 0
        for crossing_pos in crossings:
            interval = self.samples_since_edge + (crossing_pos - prev_pos)
            self.edge_intervals.append(interval)

            # Note: Auto timing refinement disabled - default 27fps timing works for 24-30fps range
            # The biphase mark decoder is tolerant of timing variations
            # If manual FPS is set, that timing is used instead

            if self.half_bit_samples > 0:
                # Decode bit using biphase mark
                bit = self.decode_edge(interval)
                if bit is not None:
                    self.bit_buffer.append(bit)

                    # Try to find and decode frames
                    frame = self.try_find_frame()
                    if frame:
                        frames.append(frame)

            self.samples_since_edge = 0
            prev_pos = crossing_pos

        self.samples_since_edge = crossing_pos if len(crossings) > 0 else self.samples_since_edge
        self.samples_since_edge = len(samples) - crossings[-1] if len(crossings) > 0 else self.samples_since_edge + len(samples)
        self.last_sample = samples[-1]

        return frames

    def decode_edge(self, interval):
        """Decode an edge interval into a bit value."""
        threshold = self.half_bit_samples * 1.5

        if interval < self.half_bit_samples * 0.5:
            # Too short - noise, skip
            return None
        elif interval < threshold:
            # Short interval = half-bit
            if self.last_edge_was_half:
                # Second half of a '1' bit
                self.last_edge_was_half = False
                return 1
            else:
                # First half of a '1' bit
                self.last_edge_was_half = True
                return None
        elif interval < self.samples_per_bit * 1.5:
            # Full-bit interval = '0' bit
            self.last_edge_was_half = False
            return 0
        else:
            # Too long - lost sync, reset
            self.last_edge_was_half = False
            return None

    def try_find_frame(self):
        """Try to find a complete LTC frame in the bit buffer."""
        if len(self.bit_buffer) < 80:
            return None

        bits = list(self.bit_buffer)

        # Only check the most recent 80 bits for sync word
        # This is more efficient and avoids re-processing old data
        if len(bits) >= 80:
            candidate = bits[-80:]
            sync_word = candidate[64:80]

            # Check forward sync word
            if sync_word == self.SYNC_WORD:
                frame = self.decode_frame(candidate, forward=True)
                if frame:
                    # Remove the 80 bits we just processed
                    for _ in range(80):
                        if self.bit_buffer:
                            self.bit_buffer.popleft()
                    return frame

            # Check reverse sync word
            if sync_word == self.SYNC_WORD_REV:
                candidate_rev = candidate[::-1]
                frame = self.decode_frame(candidate_rev, forward=False)
                if frame:
                    for _ in range(80):
                        if self.bit_buffer:
                            self.bit_buffer.popleft()
                    return frame

        # Keep buffer from growing too large
        while len(self.bit_buffer) > 160:
            self.bit_buffer.popleft()

        return None

    def decode_frame(self, bits, forward=True):
        """Decode an 80-bit LTC frame."""
        if len(bits) != 80:
            return None

        try:
            # Verify sync word
            sync_word = bits[64:80]
            if sync_word != self.SYNC_WORD:
                return None

            # Decode BCD values
            frame_units = bits[0] + bits[1]*2 + bits[2]*4 + bits[3]*8
            frame_tens = bits[8] + bits[9]*2
            drop_frame = bits[10] == 1

            sec_units = bits[16] + bits[17]*2 + bits[18]*4 + bits[19]*8
            sec_tens = bits[24] + bits[25]*2 + bits[26]*4

            min_units = bits[32] + bits[33]*2 + bits[34]*4 + bits[35]*8
            min_tens = bits[40] + bits[41]*2 + bits[42]*4

            hour_units = bits[48] + bits[49]*2 + bits[50]*4 + bits[51]*8
            hour_tens = bits[56] + bits[57]*2

            frames = frame_tens * 10 + frame_units
            seconds = sec_tens * 10 + sec_units
            minutes = min_tens * 10 + min_units
            hours = hour_tens * 10 + hour_units

            # Validate ranges
            if frames > 30 or seconds > 59 or minutes > 59 or hours > 23:
                return None

            if frame_units > 9 or sec_units > 9 or min_units > 9 or hour_units > 9:
                return None

            self.frames_decoded += 1
            self.synced = True
            self.direction = 1 if forward else -1

            return {
                'hours': hours,
                'minutes': minutes,
                'seconds': seconds,
                'frames': frames,
                'drop_frame': drop_frame,
                'timestamp': time.time()
            }
        except (IndexError, ValueError):
            return None


class LTCAnalyzer:
    """Main analyzer that tracks timecode accuracy and detects issues."""

    def __init__(self):
        self.last_frame = None
        self.frame_count = 0
        self.skip_count = 0
        self.duplicate_count = 0
        self.error_count = 0
        self.start_time = None
        self.fps_setting = None  # None = auto
        self.detected_fps = None
        self.frame_times = deque(maxlen=100)
        self.logs = []
        # For accurate FPS measurement based on timecode progression
        self.first_tc_frame = None
        self.first_tc_time = None
        self.last_tc_frame = None
        self.last_tc_time = None
        self.total_tc_frames_elapsed = 0
        # For stable FPS measurement - use longer window for accuracy
        self.fps_update_interval = 5.0  # Start showing FPS after 5 seconds
        self.fps_min_time_for_accuracy = 10.0  # Need at least 10 seconds for accurate drift
        self.stable_measured_fps = 0
        # Warmup period to let audio buffer stabilize before measuring
        self.warmup_frames = 90  # ~3 seconds at 30fps
        self.measurement_start_time = None
        self.measurement_start_tc_frame = None
        # Audio sample counting - measures time using audio clock instead of wall clock
        # This eliminates sound card clock drift from the measurement
        self.sample_rate = 48000
        self.total_audio_samples = 0
        self.audio_samples_at_measurement_start = 0
        self.audio_clock_ratio = 1.0  # Ratio of audio clock to wall clock
        # Track consecutive normal frames for robust skip detection
        # Only report skips after a stable period to avoid false positives
        self.consecutive_normal_frames = 0
        self.stable_threshold = 10  # Need 10 consecutive normal frames before reporting issues

    def reset(self):
        self.last_frame = None
        self.frame_count = 0
        self.skip_count = 0
        self.duplicate_count = 0
        self.error_count = 0
        self.start_time = None
        self.detected_fps = None
        self.frame_times.clear()
        self.logs = []
        self.first_tc_frame = None
        self.first_tc_time = None
        self.last_tc_frame = None
        self.last_tc_time = None
        self.consecutive_normal_frames = 0
        self._max_frame_seen = 0
        self._frame_value_counts = {}  # Track frequency of frame values for robust detection
        self.total_tc_frames_elapsed = 0
        self.stable_measured_fps = 0
        self.measurement_start_time = None
        self.measurement_start_tc_frame = None
        self.total_audio_samples = 0
        self.audio_samples_at_measurement_start = 0
        self.audio_clock_ratio = 1.0

    def set_sample_rate(self, sample_rate):
        """Set audio sample rate for timing calculations."""
        self.sample_rate = sample_rate

    def add_audio_samples(self, count):
        """Track audio samples received for accurate timing."""
        self.total_audio_samples += count

    def set_fps(self, fps):
        """Set expected FPS (None for auto-detect)."""
        self.fps_setting = fps

    def get_nominal_fps(self, drop_frame=False):
        """Get the nominal FPS based on settings or detection."""
        if self.fps_setting:
            return self.fps_setting
        if self.detected_fps:
            return self.detected_fps
        return 30  # Default

    def timecode_to_frames(self, tc, fps):
        """Convert timecode to total frame count."""
        nominal_fps = int(round(fps))
        total = tc['frames']
        total += tc['seconds'] * nominal_fps
        total += tc['minutes'] * 60 * nominal_fps
        total += tc['hours'] * 3600 * nominal_fps

        # Account for drop frame
        if tc['drop_frame'] and fps in [29.97, 30]:
            # Drop frame skips frames 0 and 1 at each minute except every 10th
            total_minutes = tc['hours'] * 60 + tc['minutes']
            drops = 2 * (total_minutes - total_minutes // 10)
            total -= drops

        return total

    def frames_to_timecode(self, total_frames, fps, drop_frame=False):
        """Convert total frames back to timecode."""
        nominal_fps = int(round(fps))

        if drop_frame and fps in [29.97, 30]:
            # Reverse drop frame calculation
            # This is approximate for display
            drop_frames = 2
            frames_per_10min = 10 * 60 * nominal_fps - 9 * drop_frames

            d = total_frames // frames_per_10min
            m = total_frames % frames_per_10min

            if m < drop_frames:
                m += drop_frames
            else:
                m += drop_frames * ((m - drop_frames) // (60 * nominal_fps - drop_frames) + 1)

            total_frames = d * 10 * 60 * nominal_fps + m

        frames = total_frames % nominal_fps
        total_seconds = total_frames // nominal_fps
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    def detect_fps_from_frames(self, frame):
        """Try to detect FPS from frame values and timing."""
        frame_value = frame['frames']

        # Track frame value frequency for robust detection
        # This prevents a single glitchy frame from wrong FPS detection
        if not hasattr(self, '_frame_value_counts'):
            self._frame_value_counts = {}
        if not hasattr(self, '_max_frame_seen'):
            self._max_frame_seen = 0

        self._frame_value_counts[frame_value] = self._frame_value_counts.get(frame_value, 0) + 1

        # Determine the reliable max frame value
        # Only consider a frame value as valid if seen multiple times (at least 2)
        # This filters out decoder glitches that produce invalid frame values
        reliable_max = 0
        for fv, count in self._frame_value_counts.items():
            if count >= 2 and fv > reliable_max:
                reliable_max = fv

        # Fall back to single-occurrence max only if no reliable max found yet
        if reliable_max == 0:
            reliable_max = max(self._frame_value_counts.keys()) if self._frame_value_counts else 0

        self._max_frame_seen = reliable_max

        # First, determine nominal FPS from frame numbers
        if self._max_frame_seen >= 29:
            nominal = 30  # Could be 30 or 29.97
        elif self._max_frame_seen >= 25:
            return None  # Wait for more data
        elif self._max_frame_seen >= 24:
            nominal = 25
        elif self._max_frame_seen >= 23:
            nominal = 24  # Could be 24 or 23.976
        else:
            return None

        # If drop frame flag is set, it's definitely 29.97
        if frame['drop_frame'] and nominal == 30:
            return 29.97

        # For 30/29.97 detection, use timing after warmup
        # Use stable_measured_fps which is calculated after warmup period
        if nominal == 30 and self.stable_measured_fps > 0:
            # Use threshold of 29.985 (midpoint between 29.97 and 30)
            if self.stable_measured_fps < 29.985:
                return 29.97
            else:
                return 30

        # For 24/23.976 detection
        if nominal == 24 and self.stable_measured_fps > 0:
            # 23.976 vs 24 - threshold at 23.988
            if self.stable_measured_fps < 23.988:
                return 23.976
            else:
                return 24

        return nominal

    def analyze_frame(self, frame):
        """Analyze a decoded frame and return status."""
        now = time.time()

        if self.start_time is None:
            self.start_time = now

        # Record frame time for FPS calculation
        self.frame_times.append(now)

        # Try to detect FPS from frame values
        if not self.fps_setting:
            detected = self.detect_fps_from_frames(frame)
            if detected:
                self.detected_fps = detected

        fps = self.get_nominal_fps(frame['drop_frame'])
        nominal_fps = int(round(fps))

        result = {
            'timecode': f"{frame['hours']:02d}:{frame['minutes']:02d}:{frame['seconds']:02d}:{frame['frames']:02d}",
            'drop_frame': frame['drop_frame'],
            'status': 'OK',
            'fps': fps,
            'issue': None
        }

        if self.last_frame:
            # Calculate total frames for comparison
            last_total = self.timecode_to_frames(self.last_frame, fps)
            current_total = self.timecode_to_frames(frame, fps)

            diff = current_total - last_total
            result['diff'] = diff  # Store for debug output

            # Handle midnight wrap (24:00:00:00 -> 00:00:00:00)
            max_daily_frames = 24 * 3600 * nominal_fps
            if diff < -max_daily_frames // 2:
                diff += max_daily_frames
            elif diff > max_daily_frames // 2:
                diff -= max_daily_frames

            # Warmup period: ignore skip/dup detection for first 30 frames
            # This avoids false reports from audio buffer startup glitches
            # Matches the comparator warmup period for consistency
            in_warmup = self.frame_count < 30

            # Also require FPS to be detected before reporting skips
            # Without proper FPS detection, timecode_to_frames uses wrong math
            # (e.g., 24fps source using 30fps default causes false "6 skipped" every second)
            fps_detected = self.detected_fps is not None

            # Also require stability period: need consecutive normal frames after warmup
            # before reporting issues. This prevents false positives from decoder settling.
            is_stable = self.consecutive_normal_frames >= self.stable_threshold

            # Conditions for reporting issues:
            # - Not in warmup period (first 30 frames)
            # - FPS has been detected (prevents wrong math with default 30fps)
            # - Stable period achieved (10 consecutive normal frames)
            can_report_issues = not in_warmup and fps_detected and is_stable

            if diff == 1:
                # Normal progression
                result['status'] = 'OK'
                self.consecutive_normal_frames += 1
            elif diff == 0 and can_report_issues:
                # Duplicate frame (only report when conditions met)
                self.duplicate_count += 1
                result['status'] = 'DUPLICATE'
                result['issue'] = f"Duplicate frame at {result['timecode']}"
                self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] DUPLICATE: {result['timecode']}")
                self.consecutive_normal_frames = 0
            elif diff == 0:
                # Duplicate during warmup/settling/detection - ignore but reset stability
                result['status'] = 'OK'
                self.consecutive_normal_frames = 0
            elif diff > 1 and diff <= nominal_fps * 2 and can_report_issues:
                # Skipped frames (only report when conditions met)
                self.skip_count += diff - 1
                result['status'] = 'SKIP'
                result['issue'] = f"Skipped {diff-1} frame(s) before {result['timecode']}"
                self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] SKIP: {diff-1} frame(s) before {result['timecode']}")
                self.consecutive_normal_frames = 0
            elif diff > 1 and diff <= nominal_fps * 2:
                # Skip during warmup/settling/detection - ignore but reset stability
                result['status'] = 'OK'
                self.consecutive_normal_frames = 0
            elif diff > nominal_fps * 2 or (in_warmup and diff > 1):
                # Large jump - probably just started or reconnected
                result['status'] = 'OK'
                result['issue'] = f"Timecode jump detected (resynced)"
                self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] RESYNC: Jumped to {result['timecode']}")
                self.consecutive_normal_frames = 0
            elif diff < 0 and diff > -nominal_fps:
                # Small backwards jump - likely decoder error, treat as skip
                result['status'] = 'OK'  # Don't count small glitches as errors
                self.consecutive_normal_frames = 0
            elif diff < 0:
                # Significant backwards jump (only report when conditions met)
                if can_report_issues:
                    self.error_count += 1
                    result['status'] = 'ERROR'
                    result['issue'] = f"Timecode went backwards by {-diff} frames to {result['timecode']}"
                    self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Backwards {-diff} frames to {result['timecode']}")
                else:
                    result['status'] = 'OK'
                self.consecutive_normal_frames = 0

        self.last_frame = frame.copy()  # Make a copy to avoid reference issues
        self.frame_count += 1

        # Track timecode progression for accurate FPS measurement
        current_tc_total = self.timecode_to_frames(frame, fps)
        if self.first_tc_frame is None:
            self.first_tc_frame = current_tc_total
            self.first_tc_time = now
        self.last_tc_frame = current_tc_total
        self.last_tc_time = now
        self.total_tc_frames_elapsed = current_tc_total - self.first_tc_frame

        # After warmup period, start accurate measurement
        # This allows audio buffer to stabilize before we begin timing
        if self.frame_count >= self.warmup_frames and self.measurement_start_time is None:
            self.measurement_start_time = now
            self.measurement_start_tc_frame = current_tc_total
            self.audio_samples_at_measurement_start = self.total_audio_samples

        # Calculate measured FPS using wall clock, with audio sample smoothing
        if self.measurement_start_time is not None:
            # Use audio sample count for smooth timing (eliminates jitter)
            audio_samples_elapsed = self.total_audio_samples - self.audio_samples_at_measurement_start
            audio_time_elapsed = audio_samples_elapsed / self.sample_rate
            tc_frames_since_measurement = current_tc_total - self.measurement_start_tc_frame

            # Track wall clock for calibrating audio clock rate
            wall_clock_elapsed = now - self.measurement_start_time

            if audio_time_elapsed >= self.fps_update_interval and tc_frames_since_measurement > 0:
                # Calculate audio clock ratio (how fast/slow is sound card vs wall clock)
                if wall_clock_elapsed > 1.0:
                    self.audio_clock_ratio = audio_time_elapsed / wall_clock_elapsed

                # Calculate actual sample rate based on wall clock calibration
                # This corrects for sound card clock drift
                actual_sample_rate = self.sample_rate * self.audio_clock_ratio

                # Corrected audio time using actual sample rate
                corrected_audio_time = audio_samples_elapsed / actual_sample_rate

                # FPS measured relative to real time (wall clock), using audio samples for smoothness
                self.stable_measured_fps = tc_frames_since_measurement / corrected_audio_time

            measurement_elapsed = wall_clock_elapsed  # Show wall clock time
        else:
            measurement_elapsed = 0

        result['measured_fps'] = self.stable_measured_fps
        result['measurement_time'] = measurement_elapsed
        result['audio_clock_ratio'] = self.audio_clock_ratio
        result['total_frames'] = current_tc_total  # Absolute frame position from timecode

        return result

    def get_summary(self):
        """Get a summary of the analysis session."""
        fps = self.get_nominal_fps()

        # Use wall clock calibrated measurement from after warmup
        audio_samples_elapsed = self.total_audio_samples - self.audio_samples_at_measurement_start
        tc_frames_measured = self.last_tc_frame - self.measurement_start_tc_frame if self.measurement_start_tc_frame else 0

        # Calculate corrected time using calibrated sample rate
        actual_sample_rate = self.sample_rate * self.audio_clock_ratio if self.audio_clock_ratio > 0 else self.sample_rate
        corrected_time_elapsed = audio_samples_elapsed / actual_sample_rate if actual_sample_rate > 0 else 0

        # Calculate final measured FPS using corrected time (relative to wall clock)
        if corrected_time_elapsed > 1.0 and tc_frames_measured > 0:
            final_measured_fps = tc_frames_measured / corrected_time_elapsed
        else:
            final_measured_fps = self.stable_measured_fps

        summary = {
            'frames_decoded': self.frame_count,
            'tc_frames_elapsed': self.total_tc_frames_elapsed,
            'real_time_elapsed': corrected_time_elapsed,
            'expected_fps': fps,
            'measured_fps': final_measured_fps,
            'frame_skips': self.skip_count,
            'duplicates': self.duplicate_count,
            'errors': self.error_count,
            'drift_ppm': 0,
            'drift_seconds_per_hour': 0,
            'measurement_reliable': corrected_time_elapsed >= 30,  # Need 30+ seconds for reliable drift
            'audio_clock_ratio': self.audio_clock_ratio
        }

        # Calculate drift based on measured vs expected FPS
        # Only calculate if we have enough data (30+ seconds recommended)
        if final_measured_fps > 0 and fps > 0:
            drift_ratio = (final_measured_fps - fps) / fps
            summary['drift_ppm'] = drift_ratio * 1_000_000
            summary['drift_seconds_per_hour'] = drift_ratio * 3600

        return summary


class LTCAnalyzerApp:
    """Main application GUI."""

    def __init__(self, root):
        self.root = root
        self.root.title("LTC Timecode Analyzer")
        self.root.geometry("850x650")
        self.root.minsize(700, 500)  # Minimum size
        self.root.configure(bg='#1a1a2e')

        # Live analysis components
        self.decoder = LTCDecoder()
        self.analyzer = LTCAnalyzer()
        self.running = False
        self.stream = None
        self.frame_queue = queue.Queue()
        self.device_channels = {}
        self.selected_channel = 0
        self.audio_level = 0.0
        self.peak_level = 0.0
        self.peak_hold_time = 0
        self.log_file = None
        self.log_file_path = "ltc_log.txt"
        self.max_log_lines = 150  # Keep last 150 frame entries (~10KB)
        self.log_buffer = deque(maxlen=150)  # Rolling buffer for frame logs

        # File analysis components
        self.file_analyzer = None
        self.file_analysis_thread = None
        self.file_analysis_cancelled = False
        self.file_result_queue = queue.Queue()
        if FILE_ANALYSIS_AVAILABLE:
            self.file_analyzer = FileAnalyzer()
            self.report_generator = ReportGenerator()
            self.ffmpeg_manager = FFmpegManager()

        # Settings file path
        self.settings_file = Path(__file__).parent / "ltc_analyzer_settings.json"

        # Comparator components
        self.comparator_sources = []  # List of LTC source objects
        self.comparator_running = False
        self.comparator_streams = []
        self.comparator_queue = queue.Queue()
        self.comparator_threshold = 3  # Default frame threshold
        self.comparator_reference = 0  # Index of reference source (0 = first source)
        self.sync_history = []  # Track sync status over time

        # Calibration state (for different audio interfaces mode)
        self.comp_use_calibration = False
        self.comp_calibration_done = False
        self.comp_calibration_offsets = []
        self.comp_calibration_samples = []
        self.comp_calibration_frames_needed = 60

        self.setup_ui()
        self.populate_devices()
        self.load_settings()  # Load saved settings after UI is ready

    def setup_ui(self):
        # Style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#1a1a2e')
        style.configure('TLabel', background='#1a1a2e', foreground='#eee', font=('Consolas', 10))
        style.configure('TButton', font=('Consolas', 10))
        style.configure('Header.TLabel', font=('Consolas', 14, 'bold'), foreground='#e94560')
        style.configure('Timecode.TLabel', font=('Consolas', 36, 'bold'), foreground='#00ff88')
        style.configure('Status.TLabel', font=('Consolas', 12))
        style.configure('TCombobox', font=('Consolas', 10))
        style.configure('TNotebook', background='#1a1a2e')
        style.configure('TNotebook.Tab', font=('Consolas', 11), padding=[10, 5])

        # Main container
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header
        header = ttk.Label(main_frame, text="LTC Timecode Analyzer", style='Header.TLabel')
        header.pack(pady=(0, 10))

        # Create notebook (tabs)
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Live Analysis tab
        self.live_frame = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.live_frame, text="Live Analysis")

        # File Analysis tab
        self.file_frame = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.file_frame, text="File Analysis")

        # Comparator tab
        self.comparator_frame = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.comparator_frame, text="Comparator")

        # Setup all tabs
        self.setup_live_tab()
        self.setup_file_tab()
        self.setup_comparator_tab()

    def setup_live_tab(self):
        """Setup the live analysis tab UI."""
        main_frame = self.live_frame

        # Settings frame
        settings_frame = ttk.Frame(main_frame)
        settings_frame.pack(fill=tk.X, pady=5)

        # Audio device selection
        ttk.Label(settings_frame, text="Audio Input:").grid(row=0, column=0, sticky='w', padx=5)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(settings_frame, textvariable=self.device_var, width=40, state='readonly')
        self.device_combo.grid(row=0, column=1, padx=5, pady=2)

        # Channel selection
        ttk.Label(settings_frame, text="Channel:").grid(row=0, column=2, sticky='w', padx=5)
        self.channel_var = tk.StringVar(value="1")
        self.channel_combo = ttk.Combobox(settings_frame, textvariable=self.channel_var,
                                           values=["1", "2", "3", "4", "5", "6", "7", "8"],
                                           width=5, state='readonly')
        self.channel_combo.grid(row=0, column=3, padx=5, pady=2)
        self.device_combo.bind('<<ComboboxSelected>>', self.on_device_changed)

        # Sample rate selection
        ttk.Label(settings_frame, text="Sample Rate:").grid(row=0, column=4, sticky='w', padx=5)
        self.sample_rate_var = tk.StringVar(value="48000")
        self.sample_rate_combo = ttk.Combobox(settings_frame, textvariable=self.sample_rate_var,
                                               values=["44100", "48000"], width=10, state='readonly')
        self.sample_rate_combo.grid(row=0, column=5, padx=5, pady=2)

        # Frame rate selection
        ttk.Label(settings_frame, text="Frame Rate:").grid(row=1, column=0, sticky='w', padx=5)
        self.fps_var = tk.StringVar(value="Auto")
        self.fps_combo = ttk.Combobox(settings_frame, textvariable=self.fps_var,
                                       values=["Auto", "23.976", "24", "25", "29.97", "29.97DF", "30"],
                                       width=10, state='readonly')
        self.fps_combo.grid(row=1, column=1, sticky='w', padx=5, pady=2)

        # Buttons
        btn_frame = ttk.Frame(settings_frame)
        btn_frame.grid(row=1, column=2, columnspan=4, padx=5, pady=2)

        self.start_btn = ttk.Button(btn_frame, text="▶ Start", command=self.toggle_capture)
        self.start_btn.pack(side=tk.LEFT, padx=2)

        self.reset_btn = ttk.Button(btn_frame, text="↺ Reset", command=self.reset_stats)
        self.reset_btn.pack(side=tk.LEFT, padx=2)

        # Debug checkbox
        self.debug_var = tk.BooleanVar(value=False)
        self.debug_check = ttk.Checkbutton(btn_frame, text="Debug", variable=self.debug_var)
        self.debug_check.pack(side=tk.LEFT, padx=10)

        # Audio level meter frame
        level_frame = ttk.Frame(main_frame)
        level_frame.pack(fill=tk.X, pady=10)

        ttk.Label(level_frame, text="Audio Level:").pack(side=tk.LEFT, padx=5)

        # Level meter canvas
        self.level_canvas = tk.Canvas(level_frame, width=400, height=25, bg='#0a0a15', highlightthickness=1, highlightbackground='#333')
        self.level_canvas.pack(side=tk.LEFT, padx=5)

        # Draw meter background segments
        self.level_canvas.create_rectangle(0, 0, 400, 25, fill='#0a0a15', outline='')

        # Level bar (will be updated)
        self.level_bar = self.level_canvas.create_rectangle(0, 2, 0, 23, fill='#00ff88', outline='')
        self.peak_bar = self.level_canvas.create_line(0, 0, 0, 25, fill='#ff4444', width=2)

        # dB label
        self.db_label = ttk.Label(level_frame, text="-∞ dB", width=10)
        self.db_label.pack(side=tk.LEFT, padx=5)

        # Timecode display
        tc_frame = ttk.Frame(main_frame)
        tc_frame.pack(fill=tk.X, pady=10)

        self.timecode_label = ttk.Label(tc_frame, text="--:--:--:--", style='Timecode.TLabel')
        self.timecode_label.pack()

        self.status_label = ttk.Label(tc_frame, text="Not running", style='Status.TLabel')
        self.status_label.pack()

        # Sync status frame
        sync_frame = ttk.Frame(main_frame)
        sync_frame.pack(fill=tk.X, pady=5)

        self.sync_label = ttk.Label(sync_frame, text="Sync: --", font=('Consolas', 10))
        self.sync_label.pack(side=tk.LEFT, padx=10)

        self.timing_label = ttk.Label(sync_frame, text="Bit timing: --", font=('Consolas', 10))
        self.timing_label.pack(side=tk.LEFT, padx=10)

        self.direction_label = ttk.Label(sync_frame, text="Direction: --", font=('Consolas', 10))
        self.direction_label.pack(side=tk.LEFT, padx=10)

        # Stats frame
        stats_frame = ttk.Frame(main_frame)
        stats_frame.pack(fill=tk.X, pady=10)

        # Left stats
        left_stats = ttk.Frame(stats_frame)
        left_stats.pack(side=tk.LEFT, expand=True, fill=tk.X)

        self.fps_label = ttk.Label(left_stats, text="Detected FPS: --")
        self.fps_label.pack(anchor='w')

        self.measured_fps_label = ttk.Label(left_stats, text="Measured FPS: --")
        self.measured_fps_label.pack(anchor='w')

        self.frames_label = ttk.Label(left_stats, text="Frames decoded: 0")
        self.frames_label.pack(anchor='w')

        # Right stats
        right_stats = ttk.Frame(stats_frame)
        right_stats.pack(side=tk.RIGHT, expand=True, fill=tk.X)

        self.skip_label = ttk.Label(right_stats, text="Frame skips: 0", foreground='#ffaa00')
        self.skip_label.pack(anchor='w')

        self.dup_label = ttk.Label(right_stats, text="Duplicates: 0", foreground='#ffaa00')
        self.dup_label.pack(anchor='w')

        self.error_label = ttk.Label(right_stats, text="Errors: 0", foreground='#ff4444')
        self.error_label.pack(anchor='w')

        # Accuracy indicator
        accuracy_frame = ttk.Frame(main_frame)
        accuracy_frame.pack(fill=tk.X, pady=10)

        ttk.Label(accuracy_frame, text="Accuracy:").pack(side=tk.LEFT)
        self.accuracy_label = ttk.Label(accuracy_frame, text="--", font=('Consolas', 12, 'bold'))
        self.accuracy_label.pack(side=tk.LEFT, padx=10)

        self.drift_label = ttk.Label(accuracy_frame, text="Drift: -- ppm")
        self.drift_label.pack(side=tk.LEFT, padx=20)

        # Log frame
        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        ttk.Label(log_frame, text="Event Log:").pack(anchor='w')

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, font=('Consolas', 9),
                                                   bg='#0f0f1a', fg='#aaa', insertbackground='#fff')
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def setup_file_tab(self):
        """Setup the file analysis tab UI."""
        main_frame = self.file_frame

        # File selection frame
        file_frame = ttk.Frame(main_frame)
        file_frame.pack(fill=tk.X, pady=10)

        ttk.Label(file_frame, text="Files:").grid(row=0, column=0, sticky='w', padx=5)
        self.file_path_var = tk.StringVar()
        self.file_entry = ttk.Entry(file_frame, textvariable=self.file_path_var, width=50)
        self.file_entry.grid(row=0, column=1, padx=5, pady=2, sticky='ew')

        self.browse_btn = ttk.Button(file_frame, text="Files...", command=self.browse_files)
        self.browse_btn.grid(row=0, column=2, padx=2, pady=2)

        self.browse_folder_btn = ttk.Button(file_frame, text="Folder...", command=self.browse_folder)
        self.browse_folder_btn.grid(row=0, column=3, padx=2, pady=2)

        file_frame.columnconfigure(1, weight=1)

        # Store selected files list
        self.selected_files = []

        # Options frame
        options_frame = ttk.Frame(main_frame)
        options_frame.pack(fill=tk.X, pady=5)

        # LTC Channel selection
        ttk.Label(options_frame, text="LTC Channel:").grid(row=0, column=0, sticky='w', padx=5)
        self.file_channel_var = tk.StringVar(value="Auto-detect")
        self.file_channel_combo = ttk.Combobox(options_frame, textvariable=self.file_channel_var,
                                                values=["Auto-detect", "1", "2", "3", "4", "5", "6", "7", "8"],
                                                width=11, state='readonly')
        self.file_channel_combo.grid(row=0, column=1, padx=5, pady=2, sticky='w')

        # FPS selection
        ttk.Label(options_frame, text="Frame Rate:").grid(row=0, column=2, sticky='w', padx=5)
        self.file_fps_var = tk.StringVar(value="Auto")
        self.file_fps_combo = ttk.Combobox(options_frame, textvariable=self.file_fps_var,
                                            values=["Auto", "23.976", "24", "25", "29.97", "29.97DF", "30"],
                                            width=9, state='readonly')
        self.file_fps_combo.grid(row=0, column=3, padx=5, pady=2, sticky='w')

        # Report options - second row
        self.save_report_var = tk.BooleanVar(value=True)
        self.save_report_check = ttk.Checkbutton(options_frame, text="Save report",
                                                  variable=self.save_report_var)
        self.save_report_check.grid(row=0, column=4, padx=10, pady=2)

        ttk.Label(options_frame, text="Format:").grid(row=0, column=5, sticky='w', padx=2)
        self.report_format_var = tk.StringVar(value="Both")
        self.report_format_combo = ttk.Combobox(options_frame, textvariable=self.report_format_var,
                                                 values=["Text", "CSV", "Both"], width=6, state='readonly')
        self.report_format_combo.grid(row=0, column=6, padx=2, pady=2)

        # Parallel workers option
        ttk.Label(options_frame, text="Parallel:").grid(row=0, column=7, sticky='w', padx=5)
        self.parallel_workers_var = tk.StringVar(value="4")
        self.parallel_workers_combo = ttk.Combobox(options_frame, textvariable=self.parallel_workers_var,
                                                    values=["1", "2", "4", "6", "8"], width=3, state='readonly')
        self.parallel_workers_combo.grid(row=0, column=8, padx=2, pady=2)

        # Buttons frame
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        self.analyze_btn = ttk.Button(btn_frame, text="Analyze", command=self.start_file_analysis)
        self.analyze_btn.pack(side=tk.LEFT, padx=5)

        self.cancel_analysis_btn = ttk.Button(btn_frame, text="Cancel", command=self.cancel_file_analysis,
                                               state=tk.DISABLED)
        self.cancel_analysis_btn.pack(side=tk.LEFT, padx=5)

        # FFmpeg status
        self.ffmpeg_status_label = ttk.Label(btn_frame, text="", foreground='#888')
        self.ffmpeg_status_label.pack(side=tk.RIGHT, padx=10)
        self.update_ffmpeg_status()

        # Progress frame
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=10)

        ttk.Label(progress_frame, text="Progress:").pack(side=tk.LEFT, padx=5)
        self.file_progress = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.file_progress.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.file_status_label = ttk.Label(progress_frame, text="Ready", width=30)
        self.file_status_label.pack(side=tk.LEFT, padx=10)

        # Results frame with two sections
        results_container = ttk.Frame(main_frame)
        results_container.pack(fill=tk.BOTH, expand=True, pady=10)

        # Summary section (left)
        summary_frame = ttk.LabelFrame(results_container, text="Summary", padding=5)
        summary_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        self.file_summary_text = scrolledtext.ScrolledText(summary_frame, height=15, width=40,
                                                            font=('Consolas', 10),
                                                            bg='#0f0f1a', fg='#aaa',
                                                            insertbackground='#fff')
        self.file_summary_text.pack(fill=tk.BOTH, expand=True)

        # Issues section (right)
        issues_frame = ttk.LabelFrame(results_container, text="Issues Found", padding=5)
        issues_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        self.file_issues_text = scrolledtext.ScrolledText(issues_frame, height=15, width=50,
                                                           font=('Consolas', 9),
                                                           bg='#0f0f1a', fg='#aaa',
                                                           insertbackground='#fff')
        self.file_issues_text.pack(fill=tk.BOTH, expand=True)

        # Check if file analysis is available
        if not FILE_ANALYSIS_AVAILABLE:
            self.file_summary_text.insert(tk.END, "File analysis not available.\n\n")
            self.file_summary_text.insert(tk.END, "Please install required dependencies:\n")
            self.file_summary_text.insert(tk.END, "  pip install scipy requests\n")
            self.analyze_btn.configure(state=tk.DISABLED)

    def setup_comparator_tab(self):
        """Setup the LTC comparator tab UI."""
        main_frame = self.comparator_frame

        # Description
        desc_label = ttk.Label(main_frame, text="Compare multiple LTC sources to check sync accuracy",
                               font=('Consolas', 10))
        desc_label.pack(anchor='w', pady=(0, 10))

        # Settings frame
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding=5)
        settings_frame.pack(fill=tk.X, pady=5)

        ttk.Label(settings_frame, text="Sync Threshold (frames):").grid(row=0, column=0, sticky='w', padx=5)
        self.threshold_var = tk.IntVar(value=3)
        self.threshold_spin = ttk.Spinbox(settings_frame, from_=1, to=30, width=5,
                                           textvariable=self.threshold_var)
        self.threshold_spin.grid(row=0, column=1, padx=5, pady=2, sticky='w')

        ttk.Label(settings_frame, text="Sample Rate:").grid(row=0, column=2, sticky='w', padx=15)
        self.comp_sample_rate_var = tk.StringVar(value="48000")
        self.comp_sample_rate_combo = ttk.Combobox(settings_frame, textvariable=self.comp_sample_rate_var,
                                                    values=["44100", "48000"], width=8, state='readonly')
        self.comp_sample_rate_combo.grid(row=0, column=3, padx=5, pady=2, sticky='w')

        # FPS setting
        ttk.Label(settings_frame, text="FPS:").grid(row=0, column=4, sticky='w', padx=15)
        self.comp_fps_var = tk.StringVar(value="Auto")
        self.comp_fps_combo = ttk.Combobox(settings_frame, textvariable=self.comp_fps_var,
                                            values=["Auto", "23.976", "24", "25", "29.97 DF", "29.97", "30"],
                                            width=10, state='readonly')
        self.comp_fps_combo.grid(row=0, column=5, padx=5, pady=2, sticky='w')

        # Interface mode setting (row 1)
        ttk.Label(settings_frame, text="Audio Interface:").grid(row=1, column=0, sticky='w', padx=5)
        self.comp_interface_mode_var = tk.StringVar(value="same")
        self.comp_interface_mode_combo = ttk.Combobox(settings_frame, textvariable=self.comp_interface_mode_var,
                                                       values=["Same Interface", "Different Interfaces"],
                                                       width=18, state='readonly')
        self.comp_interface_mode_combo.grid(row=1, column=1, columnspan=2, padx=5, pady=2, sticky='w')
        self.comp_interface_mode_combo.current(0)
        self.comp_interface_mode_combo.bind('<<ComboboxSelected>>', self.on_interface_mode_changed)

        # Calibration status label
        self.comp_calibration_label = ttk.Label(settings_frame, text="", font=('Consolas', 9), foreground='#888')
        self.comp_calibration_label.grid(row=1, column=3, padx=5, pady=2, sticky='w')

        # Sources frame
        sources_outer_frame = ttk.LabelFrame(main_frame, text="LTC Sources (2-4)", padding=5)
        sources_outer_frame.pack(fill=tk.X, pady=5)

        # Container for dynamic source rows
        self.comp_sources_container = ttk.Frame(sources_outer_frame)
        self.comp_sources_container.pack(fill=tk.X)

        # Source management buttons
        src_btn_frame = ttk.Frame(sources_outer_frame)
        src_btn_frame.pack(fill=tk.X, pady=5)

        self.add_source_btn = ttk.Button(src_btn_frame, text="+ Add Source", command=self.add_comparator_source)
        self.add_source_btn.pack(side=tk.LEFT, padx=5)

        self.remove_source_btn = ttk.Button(src_btn_frame, text="- Remove Source", command=self.remove_comparator_source)
        self.remove_source_btn.pack(side=tk.LEFT, padx=5)

        # Initialize source list and device channels dict
        self.comp_source_widgets = []  # List of dicts with widgets for each source
        self.comp_device_channels = {}  # Device ID -> max input channels
        self.add_comparator_source()  # Source 1 (Reference)
        self.add_comparator_source()  # Source 2

        # Buttons frame
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        self.comp_start_btn = ttk.Button(btn_frame, text="▶ Start Comparison",
                                          command=self.toggle_comparator)
        self.comp_start_btn.pack(side=tk.LEFT, padx=5)

        self.comp_reset_btn = ttk.Button(btn_frame, text="↺ Reset Stats",
                                          command=self.reset_comparator_stats)
        self.comp_reset_btn.pack(side=tk.LEFT, padx=5)

        # Sync status display
        status_frame = ttk.LabelFrame(main_frame, text="Sync Status", padding=10)
        status_frame.pack(fill=tk.X, pady=5)

        self.sync_status_label = ttk.Label(status_frame, text="NOT RUNNING",
                                            font=('Consolas', 24, 'bold'), foreground='#888')
        self.sync_status_label.pack()

        self.sync_offset_label = ttk.Label(status_frame, text="Offset: --",
                                            font=('Consolas', 12))
        self.sync_offset_label.pack()

        # Statistics frame
        stats_frame = ttk.Frame(main_frame)
        stats_frame.pack(fill=tk.X, pady=5)

        left_stats = ttk.Frame(stats_frame)
        left_stats.pack(side=tk.LEFT, expand=True, fill=tk.X)

        self.comp_frames_label = ttk.Label(left_stats, text="Frames compared: 0")
        self.comp_frames_label.pack(anchor='w')

        self.comp_insync_label = ttk.Label(left_stats, text="In sync: 0 (0%)", foreground='#00ff88')
        self.comp_insync_label.pack(anchor='w')

        right_stats = ttk.Frame(stats_frame)
        right_stats.pack(side=tk.RIGHT, expand=True, fill=tk.X)

        self.comp_outsync_label = ttk.Label(right_stats, text="Out of sync: 0", foreground='#ffaa00')
        self.comp_outsync_label.pack(anchor='w')

        self.comp_errors_label = ttk.Label(right_stats, text="Skips: 0  Dups: 0  Errors: 0", foreground='#ff4444')
        self.comp_errors_label.pack(anchor='w')

        # Log frame
        log_frame = ttk.LabelFrame(main_frame, text="Comparison Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.comp_log_text = scrolledtext.ScrolledText(log_frame, height=10, font=('Consolas', 9),
                                                        bg='#0f0f1a', fg='#aaa',
                                                        insertbackground='#fff')
        self.comp_log_text.pack(fill=tk.BOTH, expand=True)

        # Populate device combos
        self.populate_comparator_devices()

    def add_comparator_source(self):
        """Add a new LTC source to the comparator."""
        if len(self.comp_source_widgets) >= 4:
            return  # Max 4 sources

        source_idx = len(self.comp_source_widgets)
        src_frame = ttk.Frame(self.comp_sources_container)
        src_frame.pack(fill=tk.X, pady=2)

        # Label
        if source_idx == 0:
            label_text = "Source 1 (Reference):"
        else:
            label_text = f"Source {source_idx + 1}:"

        label = ttk.Label(src_frame, text=label_text, width=18)
        label.pack(side=tk.LEFT, padx=5)

        # Device combo
        device_var = tk.StringVar()
        device_combo = ttk.Combobox(src_frame, textvariable=device_var, width=35, state='readonly')
        device_combo.pack(side=tk.LEFT, padx=5)
        device_combo.bind('<<ComboboxSelected>>', lambda e, idx=source_idx: self.on_comp_device_changed(idx))

        # Channel
        ttk.Label(src_frame, text="Ch:").pack(side=tk.LEFT, padx=5)
        channel_var = tk.StringVar(value="1")
        channel_combo = ttk.Combobox(src_frame, textvariable=channel_var,
                                      values=["1", "2", "3", "4", "5", "6", "7", "8"],
                                      width=3, state='readonly')
        channel_combo.pack(side=tk.LEFT, padx=2)

        # Timecode display
        tc_label = ttk.Label(src_frame, text="--:--:--:--", font=('Consolas', 12, 'bold'),
                              foreground='#888')
        tc_label.pack(side=tk.RIGHT, padx=10)

        # FPS display
        fps_label = ttk.Label(src_frame, text="", font=('Consolas', 9), width=10, foreground='#888')
        fps_label.pack(side=tk.RIGHT, padx=2)

        # Offset display (not for reference)
        offset_label = ttk.Label(src_frame, text="", font=('Consolas', 10), width=12)
        offset_label.pack(side=tk.RIGHT, padx=5)

        # Store widgets
        source_widgets = {
            'frame': src_frame,
            'label': label,
            'device_var': device_var,
            'device_combo': device_combo,
            'channel_var': channel_var,
            'channel_combo': channel_combo,
            'tc_label': tc_label,
            'fps_label': fps_label,
            'offset_label': offset_label
        }
        self.comp_source_widgets.append(source_widgets)

        # Populate device list
        self.populate_source_devices(source_idx)

        # Update button states
        self.update_source_buttons()

    def remove_comparator_source(self):
        """Remove the last LTC source from the comparator."""
        if len(self.comp_source_widgets) <= 2:
            return  # Min 2 sources

        # Remove last source
        source = self.comp_source_widgets.pop()
        source['frame'].destroy()

        # Update button states
        self.update_source_buttons()

    def update_source_buttons(self):
        """Update add/remove button states based on source count."""
        num_sources = len(self.comp_source_widgets)

        if num_sources >= 4:
            self.add_source_btn.configure(state=tk.DISABLED)
        else:
            self.add_source_btn.configure(state=tk.NORMAL)

        if num_sources <= 2:
            self.remove_source_btn.configure(state=tk.DISABLED)
        else:
            self.remove_source_btn.configure(state=tk.NORMAL)

    def populate_source_devices(self, source_idx):
        """Populate device list for a specific source."""
        if not AUDIO_AVAILABLE:
            return

        if source_idx >= len(self.comp_source_widgets):
            return

        devices = sd.query_devices()
        input_devices = []

        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0:
                input_devices.append(f"{i}: {dev['name']}")
                self.comp_device_channels[i] = dev['max_input_channels']

        source = self.comp_source_widgets[source_idx]
        source['device_combo']['values'] = input_devices

        # Auto-select a device
        if len(input_devices) > source_idx:
            source['device_combo'].current(source_idx)
        elif len(input_devices) > 0:
            source['device_combo'].current(0)

        self.on_comp_device_changed(source_idx)

    def populate_comparator_devices(self):
        """Populate audio device lists for all comparator sources."""
        if not AUDIO_AVAILABLE:
            return

        devices = sd.query_devices()
        self.comp_device_channels = {}

        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0:
                self.comp_device_channels[i] = dev['max_input_channels']

        # Populate each source
        for idx in range(len(self.comp_source_widgets)):
            self.populate_source_devices(idx)

    def on_comp_device_changed(self, source_index):
        """Update channel list when comparator device changes."""
        if source_index >= len(self.comp_source_widgets):
            return

        source = self.comp_source_widgets[source_index]
        device_var = source['device_var']
        channel_combo = source['channel_combo']
        channel_var = source['channel_var']

        selection = device_var.get()
        if selection:
            device_id = int(selection.split(':')[0])
            if device_id in self.comp_device_channels:
                num_channels = self.comp_device_channels[device_id]
                channels = [str(i+1) for i in range(num_channels)]
                channel_combo['values'] = channels
                if channel_var.get() not in channels:
                    channel_var.set("1")

        # Auto-detect interface mode based on device selections
        self.auto_detect_interface_mode()

        # If in "Same Interface" mode and Source 1 changed, update all other sources
        if source_index == 0 and self.comp_interface_mode_var.get() == "Same Interface":
            self.sync_sources_to_reference()

    def auto_detect_interface_mode(self):
        """Auto-detect whether all sources use the same interface."""
        if len(self.comp_source_widgets) < 2:
            return

        # Get reference device (Source 1)
        ref_device = self.comp_source_widgets[0]['device_var'].get()
        if not ref_device:
            return

        ref_device_id = ref_device.split(':')[0]

        # Check if all other sources use the same device
        all_same = True
        for idx in range(1, len(self.comp_source_widgets)):
            other_device = self.comp_source_widgets[idx]['device_var'].get()
            if other_device:
                other_device_id = other_device.split(':')[0]
                if other_device_id != ref_device_id:
                    all_same = False
                    break

        # Update interface mode combo
        if all_same:
            if self.comp_interface_mode_var.get() != "Same Interface":
                self.comp_interface_mode_combo.current(0)  # Same Interface
                self.comp_calibration_label.configure(text="")
        else:
            if self.comp_interface_mode_var.get() != "Different Interfaces":
                self.comp_interface_mode_combo.current(1)  # Different Interfaces
                self.comp_calibration_label.configure(text="(Auto-calibration enabled)")

    def on_interface_mode_changed(self, event=None):
        """Handle interface mode selection change."""
        mode = self.comp_interface_mode_var.get()

        if mode == "Same Interface":
            # Sync all sources to reference device
            self.sync_sources_to_reference()
            self.comp_calibration_label.configure(text="")
        else:
            # Different interfaces - enable calibration
            self.comp_calibration_label.configure(text="(Auto-calibration enabled)")

    def sync_sources_to_reference(self):
        """Sync all source devices to match Source 1 (reference)."""
        if len(self.comp_source_widgets) < 2:
            return

        ref_device = self.comp_source_widgets[0]['device_var'].get()
        if not ref_device:
            return

        # Update all other sources to use the same device
        for idx in range(1, len(self.comp_source_widgets)):
            source = self.comp_source_widgets[idx]
            current_device = source['device_var'].get()

            # Only update if different
            if current_device != ref_device:
                source['device_var'].set(ref_device)

                # Update channel list for new device
                device_id = int(ref_device.split(':')[0])
                if device_id in self.comp_device_channels:
                    num_channels = self.comp_device_channels[device_id]
                    channels = [str(i+1) for i in range(num_channels)]
                    source['channel_combo']['values'] = channels
                    # Keep channel if valid, otherwise reset
                    if source['channel_var'].get() not in channels:
                        source['channel_var'].set("1")

    def toggle_comparator(self):
        """Toggle comparator on/off."""
        if self.comparator_running:
            self.stop_comparator()
        else:
            self.start_comparator()

    def start_comparator(self):
        """Start the LTC comparator."""
        if not AUDIO_AVAILABLE:
            self.comp_log("ERROR: sounddevice not available")
            return

        num_sources = len(self.comp_source_widgets)
        if num_sources < 2:
            self.comp_log("ERROR: Need at least 2 sources")
            return

        # Validate all sources have devices selected
        source_configs = []
        for idx, source in enumerate(self.comp_source_widgets):
            device_str = source['device_var'].get()
            if not device_str:
                self.comp_log(f"ERROR: Please select device for Source {idx + 1}")
                return
            device_id = int(device_str.split(':')[0])
            channel = int(source['channel_var'].get()) - 1  # 0-indexed
            source_configs.append({'device_id': device_id, 'channel': channel})

        sample_rate = int(self.comp_sample_rate_var.get())

        # Parse FPS setting
        fps_str = self.comp_fps_var.get()
        if fps_str == "Auto":
            fps_value = None
        elif fps_str == "29.97 DF":
            fps_value = 29.97
        else:
            fps_value = float(fps_str)

        # Initialize comparator state for all sources
        self.comp_decoders = [LTCDecoder(sample_rate) for _ in range(num_sources)]
        self.comp_analyzers = [LTCAnalyzer() for _ in range(num_sources)]
        for analyzer in self.comp_analyzers:
            analyzer.set_sample_rate(sample_rate)
            analyzer.set_fps(fps_value)  # Apply manual FPS setting (None = auto)

        self.comp_current_tc = [None] * num_sources
        self.comp_current_frames = [None] * num_sources
        self.comp_source_updated = [False] * num_sources  # Track which sources have new frames
        self.comp_last_update_time = [None] * num_sources  # Track when each source last received a frame
        self.comp_display_offsets = [None] * (num_sources - 1)  # Stabilized display offsets
        self.comp_offset_history = [[] for _ in range(num_sources - 1)]  # Recent offsets for stabilization
        self.comp_stats = {
            'frames_compared': 0,
            'in_sync': [0] * (num_sources - 1),  # One count per non-reference source
            'out_of_sync': [0] * (num_sources - 1),
            'skips': 0,
            'duplicates': 0,
            'errors': 0,
            'start_time': time.time(),
            'offset_history': [[] for _ in range(num_sources - 1)],  # One history per pair
            'initial_tc_diff': [None] * (num_sources - 1),  # Initial raw TC difference per source
            'tc_mismatch': [False] * (num_sources - 1),  # True if timecodes don't match (>1 sec diff)
            'mismatch_count': [0] * (num_sources - 1),  # Consecutive mismatch frame counter (for glitch filtering)
            'match_count': [0] * (num_sources - 1),  # Consecutive match frame counter (for glitch filtering)
            'stalled': [False] * num_sources,  # True if source has stopped outputting
            'stall_check_count': [0] * num_sources,  # Consecutive stall checks counter (for glitch filtering)
            'active_check_count': [0] * num_sources  # Consecutive active checks counter (for glitch filtering)
        }

        # Reset warmup counter
        self._comp_warmup_count = 0

        # Calibration state for different interfaces mode
        self.comp_calibration_offsets = [0] * (num_sources - 1)  # Latency offset per non-ref source
        self.comp_calibration_samples = [[] for _ in range(num_sources - 1)]  # Samples during calibration
        self.comp_calibration_done = False
        self.comp_calibration_frames_needed = 60  # ~2 seconds at 30fps for calibration
        self.comp_use_calibration = (self.comp_interface_mode_var.get() == "Different Interfaces")

        if self.comp_use_calibration:
            self.comp_calibration_label.configure(text="Calibrating...", foreground='#ffaa00')
        else:
            self.comp_calibration_label.configure(text="")

        try:
            self.comparator_streams = []

            if not self.comp_use_calibration:
                # Same Interface mode: Use ONE stream, process all channels together
                # This ensures all sources are decoded from the exact same audio buffer
                device_id = source_configs[0]['device_id']
                num_channels = self.comp_device_channels.get(device_id, 2)
                channel_list = [c['channel'] for c in source_configs]

                def unified_callback(indata, frames, time_info, status):
                    if status:
                        self.comparator_queue.put(('error', 0, str(status)))

                    # Process ALL channels from the same audio buffer simultaneously
                    for source_idx, channel_idx in enumerate(channel_list):
                        if len(indata.shape) > 1 and channel_idx < indata.shape[1]:
                            samples = indata[:, channel_idx].flatten()
                        else:
                            samples = indata.flatten()

                        # Track audio samples for accurate FPS measurement
                        self.comp_analyzers[source_idx].add_audio_samples(len(samples))

                        # Decode LTC for this source
                        decoded_frames = self.comp_decoders[source_idx].process_samples_fast(samples)
                        for frame in decoded_frames:
                            result = self.comp_analyzers[source_idx].analyze_frame(frame)
                            self.comparator_queue.put(('frame', source_idx, result))

                stream = sd.InputStream(
                    device=device_id,
                    channels=num_channels,
                    samplerate=sample_rate,
                    blocksize=int(sample_rate * 0.01),  # 10ms blocks for faster response
                    callback=unified_callback
                )
                stream.start()
                self.comparator_streams.append(stream)

            else:
                # Different Interfaces mode: Separate streams for each device
                def make_callback(source_idx, channel_idx):
                    def callback(indata, frames, time_info, status):
                        if status:
                            self.comparator_queue.put(('error', source_idx, str(status)))

                        # Extract selected channel
                        if len(indata.shape) > 1 and channel_idx < indata.shape[1]:
                            samples = indata[:, channel_idx].flatten()
                        else:
                            samples = indata.flatten()

                        # Track audio samples for accurate FPS measurement
                        self.comp_analyzers[source_idx].add_audio_samples(len(samples))

                        # Decode LTC
                        decoded_frames = self.comp_decoders[source_idx].process_samples_fast(samples)
                        for frame in decoded_frames:
                            result = self.comp_analyzers[source_idx].analyze_frame(frame)
                            self.comparator_queue.put(('frame', source_idx, result))

                    return callback

                for idx, config in enumerate(source_configs):
                    stream = sd.InputStream(
                        device=config['device_id'],
                        channels=self.comp_device_channels.get(config['device_id'], 2),
                        samplerate=sample_rate,
                        blocksize=int(sample_rate * 0.01),  # 10ms blocks for faster response
                        callback=make_callback(idx, config['channel'])
                    )
                    stream.start()
                    self.comparator_streams.append(stream)

            self.comparator_running = True
            self.comp_start_btn.configure(text="■ Stop Comparison")
            self.sync_status_label.configure(text="WAITING...", foreground='#888')

            # Disable add/remove while running
            self.add_source_btn.configure(state=tk.DISABLED)
            self.remove_source_btn.configure(state=tk.DISABLED)

            # Log start
            source_desc = ", ".join([f"Src{i+1}(Dev{c['device_id']} ch{c['channel']+1})"
                                      for i, c in enumerate(source_configs)])
            self.comp_log(f"Started comparison: {source_desc}")

            # Start queue processing thread (event-driven, not polling)
            self.start_comparator_queue_thread()

        except Exception as e:
            self.comp_log(f"ERROR starting comparator: {e}")
            self.stop_comparator()

    def stop_comparator(self):
        """Stop the LTC comparator."""
        self.comparator_running = False

        # Stop and close streams
        for stream in self.comparator_streams:
            try:
                stream.stop()
                stream.close()
            except:
                pass
        self.comparator_streams = []

        self.comp_start_btn.configure(text="▶ Start Comparison")
        self.sync_status_label.configure(text="STOPPED", foreground='#888')

        # Re-enable add/remove buttons
        self.update_source_buttons()

        self.comp_log("Comparison stopped")

    def reset_comparator_stats(self):
        """Reset comparator statistics."""
        num_sources = len(self.comp_source_widgets)

        if hasattr(self, 'comp_stats'):
            self.comp_stats = {
                'frames_compared': 0,
                'in_sync': [0] * (num_sources - 1),
                'out_of_sync': [0] * (num_sources - 1),
                'skips': 0,
                'duplicates': 0,
                'errors': 0,
                'start_time': time.time(),
                'offset_history': [[] for _ in range(num_sources - 1)],
                'initial_tc_diff': [None] * (num_sources - 1),
                'tc_mismatch': [False] * (num_sources - 1),
                'stalled': [False] * num_sources
            }

        # Reset warmup counter
        self._comp_warmup_count = 0

        # Reset calibration state
        if hasattr(self, 'comp_calibration_offsets'):
            self.comp_calibration_offsets = [0] * (num_sources - 1)
            self.comp_calibration_samples = [[] for _ in range(num_sources - 1)]
            self.comp_calibration_done = False
            if self.comp_use_calibration:
                self.comp_calibration_label.configure(text="Calibrating...", foreground='#ffaa00')

        if hasattr(self, 'comp_decoders'):
            for decoder in self.comp_decoders:
                decoder.reset()
        if hasattr(self, 'comp_analyzers'):
            for analyzer in self.comp_analyzers:
                analyzer.reset()

        self.comp_current_tc = [None] * num_sources
        self.comp_current_frames = [None] * num_sources
        self.comp_source_updated = [False] * num_sources
        self.comp_last_update_time = [None] * num_sources
        self.comp_offset_history = [[] for _ in range(num_sources - 1)]
        self.comp_display_offsets = [None] * (num_sources - 1)  # Reset stabilized offsets

        # Reset offset displays
        for idx, source in enumerate(self.comp_source_widgets):
            source['tc_label'].configure(text="--:--:--:--", foreground='#888')
            source['fps_label'].configure(text="")
            source['offset_label'].configure(text="")

        self.comp_frames_label.configure(text="Frames compared: 0")
        self.comp_insync_label.configure(text="In sync: 0 (0%)")
        self.comp_outsync_label.configure(text="Out of sync: 0")
        self.comp_errors_label.configure(text="Skips: 0  Dups: 0  Errors: 0")
        self.sync_offset_label.configure(text="Offset: --")
        self.comp_log("Statistics reset")

    def start_comparator_queue_thread(self):
        """Start background thread that waits for queue data (interrupt-like)."""
        def queue_processor():
            last_stall_check = time.time()
            stall_check_interval = 0.5  # Check for stalls every 0.5 seconds

            while self.comparator_running:
                try:
                    # Block until first message arrives (with timeout for clean shutdown)
                    msg = self.comparator_queue.get(timeout=0.1)

                    # Collect all available messages in a batch
                    # This ensures we process all frames from the same audio callback together
                    messages = [msg]
                    while True:
                        try:
                            messages.append(self.comparator_queue.get_nowait())
                        except queue.Empty:
                            break

                    # Process entire batch on main thread
                    self.root.after(0, lambda msgs=messages: self.process_comparator_batch(msgs))

                except queue.Empty:
                    pass  # No message, continue to stall check

                # Always check for stalled sources periodically (not just when queue empty)
                current_time = time.time()
                if current_time - last_stall_check >= stall_check_interval:
                    self.root.after(0, self.check_stalled_sources)
                    last_stall_check = current_time

        self._comp_queue_thread = threading.Thread(target=queue_processor, daemon=True)
        self._comp_queue_thread.start()

    def process_comparator_batch(self, messages):
        """Process a batch of messages from the comparator queue (runs on main thread).

        This batches all messages from the same audio callback to ensure comparison
        uses the most recent frame from each source, not intermediate frames.
        """
        if not self.comparator_running:
            return

        # Process all messages in the batch (update displays, track issues)
        for msg in messages:
            self.process_comparator_message(msg, defer_compare=True)

        # Now compare using the final (most recent) state from each source
        if all(self.comp_source_updated):
            self.compare_timecodes()
            self.comp_source_updated = [False] * len(self.comp_source_updated)

    def process_comparator_message(self, msg, defer_compare=False):
        """Process a single message from the comparator queue (runs on main thread).

        Args:
            msg: The message tuple from the queue
            defer_compare: If True, don't trigger comparison (batch mode)
        """
        if not self.comparator_running:
            return

        msg_type = msg[0]

        if msg_type == 'frame':
            source_idx = msg[1]
            result = msg[2]
            tc = result['timecode']
            total_frames = result.get('total_frames', 0)

            self.comp_current_tc[source_idx] = tc
            self.comp_current_frames[source_idx] = total_frames
            self.comp_source_updated[source_idx] = True
            self.comp_last_update_time[source_idx] = time.time()

            # Update timecode and FPS display immediately
            if source_idx < len(self.comp_source_widgets):
                self.comp_source_widgets[source_idx]['tc_label'].configure(text=tc)
                # Show detected FPS with drop frame indicator
                fps = result.get('fps')
                drop_frame = result.get('drop_frame', False)
                measured_fps = result.get('measured_fps', 0)
                measurement_time = result.get('measurement_time', 0)

                if fps:
                    # Check if FPS is manually set (no ? needed) or auto-detecting
                    is_manual_fps = self.comp_fps_var.get() != "Auto"

                    # Check if FPS measurement is still stabilizing (only for auto mode)
                    # Need at least 3 seconds of measurement for accurate 23.976/24 or 29.97/30 detection
                    is_stabilizing = not is_manual_fps and measurement_time < 3.0

                    # Format FPS nicely
                    if fps == 23.976:
                        fps_text = "23.976"
                    elif fps == 29.97:
                        fps_text = "29.97DF" if drop_frame else "29.97"
                    elif fps == 24:
                        # Could still be 23.976, show ? if stabilizing in auto mode
                        fps_text = "24?" if is_stabilizing else "24"
                    elif fps == 25:
                        fps_text = "25"
                    elif fps == 30:
                        if drop_frame:
                            fps_text = "29.97DF"  # DF flag means definitely 29.97
                        elif is_stabilizing:
                            fps_text = "30?"  # Could be 29.97, still measuring
                        else:
                            fps_text = "30"
                    else:
                        fps_text = f"{fps:.2f}" if fps != int(fps) else f"{int(fps)}"
                    self.comp_source_widgets[source_idx]['fps_label'].configure(text=fps_text)

            # Check for issues on this source
            status = result['status']
            if status == 'SKIP':
                self.comp_stats['skips'] += 1
                self.comp_log(f"Source {source_idx+1}: SKIP - {result.get('issue', tc)}")
            elif status == 'DUPLICATE':
                self.comp_stats['duplicates'] += 1
                self.comp_log(f"Source {source_idx+1}: DUPLICATE - {result.get('issue', tc)}")
            elif status != 'OK':
                self.comp_stats['errors'] += 1
                self.comp_log(f"Source {source_idx+1}: {status} - {result.get('issue', tc)}")

            # Compare when ALL sources have new frames (unless deferred for batch processing)
            if not defer_compare and all(self.comp_source_updated):
                self.compare_timecodes()
                self.comp_source_updated = [False] * len(self.comp_source_updated)

        elif msg_type == 'error':
            source_idx = msg[1]
            error = msg[2]
            self.comp_log(f"Source {source_idx+1} audio error: {error}")

    def check_stalled_sources(self):
        """Check for stalled sources (called during idle time)."""
        if not self.comparator_running:
            return

        current_time = time.time()
        any_stalled = False

        # Stall timeout - use longer timeout to avoid false positives during audio buffer issues
        stall_timeout = 3.0  # seconds

        # Glitch filtering - require multiple consecutive checks to confirm state change
        # With 0.5s check interval, 4 checks = 2 seconds of confirmed state
        STALL_FILTER_CHECKS = 4

        for idx, source in enumerate(self.comp_source_widgets):
            if idx < len(self.comp_last_update_time):
                last_update = self.comp_last_update_time[idx]
                was_stalled = self.comp_stats['stalled'][idx] if idx < len(self.comp_stats['stalled']) else False
                appears_stalled = last_update is not None and (current_time - last_update) > stall_timeout

                # Update consecutive check counters
                if appears_stalled:
                    self.comp_stats['stall_check_count'][idx] += 1
                    self.comp_stats['active_check_count'][idx] = 0
                else:
                    self.comp_stats['active_check_count'][idx] += 1
                    self.comp_stats['stall_check_count'][idx] = 0

                # Only change state after enough consecutive checks (glitch filtering)
                if not was_stalled and self.comp_stats['stall_check_count'][idx] >= STALL_FILTER_CHECKS:
                    # Confirmed stall
                    self.comp_stats['stalled'][idx] = True
                    self.comp_log(f"SOURCE STALLED: Src{idx + 1} stopped outputting timecode")
                    # Show STALLED on offset label (for non-reference sources)
                    if idx > 0 and 'offset_label' in source:
                        source['offset_label'].configure(text="STALLED", foreground='#ff4444')
                    # Gray out the timecode display and show stalled indicator
                    source['tc_label'].configure(foreground='#ff4444')
                    source['fps_label'].configure(text="STALLED", foreground='#ff4444')

                elif was_stalled and self.comp_stats['active_check_count'][idx] >= STALL_FILTER_CHECKS:
                    # Confirmed recovery
                    self.comp_stats['stalled'][idx] = False
                    self.comp_log(f"SOURCE RECOVERED: Src{idx + 1} resumed outputting timecode")
                    # Restore normal colors
                    source['tc_label'].configure(foreground='#00ff88')
                    source['fps_label'].configure(foreground='#888888')

                # Track current stall state for UI update
                if self.comp_stats['stalled'][idx]:
                    any_stalled = True

        # Update main sync status if any source is stalled
        if any_stalled:
            self.sync_status_label.configure(text="SOURCE STALLED", foreground='#ff4444')
            self.sync_offset_label.configure(text="Check sources")

    def compare_timecodes(self):
        """Compare all timecode sources against reference (Source 1)."""
        ref_frames = self.comp_current_frames[0]
        if ref_frames is None:
            return

        threshold = self.threshold_var.get()
        num_sources = len(self.comp_source_widgets)
        current_time = time.time()

        # Track warmup separately from actual comparisons
        if not hasattr(self, '_comp_warmup_count'):
            self._comp_warmup_count = 0
        self._comp_warmup_count += 1

        # Skip first 30 frames to allow decoders to stabilize
        # (about 1 second at 30fps - needed to avoid garbage from decoder init)
        if self._comp_warmup_count <= 30:
            return

        # Only count frames that are actually compared
        self.comp_stats['frames_compared'] += 1

        # Compare each source (1+) against reference
        offsets = []
        raw_offsets = []  # Before calibration adjustment
        all_in_sync = True
        drifts = []

        for src_idx in range(1, num_sources):
            pair_idx = src_idx - 1  # Index into per-pair arrays

            src_frames = self.comp_current_frames[src_idx]
            if src_frames is None:
                offsets.append(None)
                raw_offsets.append(None)
                drifts.append(0)
                continue

            # Calculate raw offset for this source
            # When both sources show the same timecode string, treat as 0 offset
            # This handles the case where LTC frame boundaries aren't perfectly aligned
            # between channels, causing one decoder to be 1 frame ahead momentarily
            ref_tc = self.comp_current_tc[0]
            src_tc = self.comp_current_tc[src_idx]
            if ref_tc == src_tc:
                raw_offset = 0
            else:
                raw_offset = src_frames - ref_frames

                # Handle midnight rollover (23:59:59:xx -> 00:00:00:00)
                # Get FPS from the comparator setting
                fps_str = self.comp_fps_var.get()
                if fps_str == "Auto":
                    nominal_fps = 30  # Default assumption
                elif fps_str == "29.97 DF":
                    nominal_fps = 30
                else:
                    nominal_fps = int(round(float(fps_str)))

                # Calculate frames in a day
                frames_per_day = 24 * 3600 * nominal_fps

                # If offset is more than half a day, it's likely a midnight rollover
                if raw_offset > frames_per_day // 2:
                    raw_offset -= frames_per_day
                elif raw_offset < -frames_per_day // 2:
                    raw_offset += frames_per_day

            raw_offsets.append(raw_offset)

            # Check for TC mismatch based on current raw offset (continuous check)
            # TC mismatch means the absolute timecodes differ by more than threshold
            # Use glitch filtering - require multiple consecutive frames before changing state
            GLITCH_FILTER_FRAMES = 5  # Require 5 consecutive frames to confirm state change

            was_mismatch = self.comp_stats['tc_mismatch'][pair_idx]
            current_frame_mismatch = abs(raw_offset) > threshold

            if current_frame_mismatch:
                self.comp_stats['mismatch_count'][pair_idx] += 1
                self.comp_stats['match_count'][pair_idx] = 0
            else:
                self.comp_stats['match_count'][pair_idx] += 1
                self.comp_stats['mismatch_count'][pair_idx] = 0

            # Only change state after enough consecutive frames (glitch filtering)
            if not was_mismatch and self.comp_stats['mismatch_count'][pair_idx] >= GLITCH_FILTER_FRAMES:
                # Confirmed mismatch after filtering
                self.comp_stats['tc_mismatch'][pair_idx] = True
                self.comp_log(f"TC MISMATCH: Src{src_idx+1} differs by {raw_offset:+d} frames "
                              f"({self.comp_current_tc[0]} vs {self.comp_current_tc[src_idx]})")
            elif was_mismatch and self.comp_stats['match_count'][pair_idx] >= GLITCH_FILTER_FRAMES:
                # Confirmed recovery after filtering
                self.comp_stats['tc_mismatch'][pair_idx] = False
                self.comp_log(f"TC MATCHED: Src{src_idx+1} now in sync "
                              f"({self.comp_current_tc[0]} vs {self.comp_current_tc[src_idx]})")

            # Handle calibration for different interfaces mode
            if self.comp_use_calibration:
                if not self.comp_calibration_done:
                    # Collect calibration samples
                    self.comp_calibration_samples[pair_idx].append(raw_offset)

                    # Check if we have enough samples for this source
                    if len(self.comp_calibration_samples[pair_idx]) >= self.comp_calibration_frames_needed:
                        # Calculate median offset as calibration value (more robust than mean)
                        sorted_samples = sorted(self.comp_calibration_samples[pair_idx])
                        mid = len(sorted_samples) // 2
                        self.comp_calibration_offsets[pair_idx] = sorted_samples[mid]

                        # Check if all sources are calibrated
                        all_calibrated = all(
                            len(samples) >= self.comp_calibration_frames_needed
                            for samples in self.comp_calibration_samples
                        )
                        if all_calibrated:
                            self.comp_calibration_done = True
                            offset_strs = [f"Src{i+2}:{o:+d}f" for i, o in enumerate(self.comp_calibration_offsets)]
                            self.comp_log(f"Calibration complete. Latency offsets: {', '.join(offset_strs)}")
                            self.comp_calibration_label.configure(
                                text=f"Calibrated ({', '.join(offset_strs)})",
                                foreground='#00ff88'
                            )

                # Apply calibration offset
                offset = raw_offset - self.comp_calibration_offsets[pair_idx]
            else:
                # No calibration needed (same interface)
                offset = raw_offset

            offsets.append(offset)

            # Check if in sync
            if abs(offset) <= threshold:
                self.comp_stats['in_sync'][pair_idx] += 1
                src_in_sync = True
            else:
                self.comp_stats['out_of_sync'][pair_idx] += 1
                src_in_sync = False
                all_in_sync = False

                # Log out of sync events (but not every frame)
                total_out_of_sync = sum(self.comp_stats['out_of_sync'])
                if total_out_of_sync == 1 or (self.comp_stats['out_of_sync'][pair_idx] % 30 == 0):
                    self.comp_log(f"OUT OF SYNC: Src{src_idx+1} offset {offset:+d} frames "
                                  f"({self.comp_current_tc[0]} vs {self.comp_current_tc[src_idx]})")

            # Track offset for drift detection
            self.comp_stats['offset_history'][pair_idx].append((current_time, offset))

            # Keep only last 60 seconds of history
            cutoff_time = current_time - 60
            self.comp_stats['offset_history'][pair_idx] = [
                (t, o) for t, o in self.comp_stats['offset_history'][pair_idx] if t > cutoff_time
            ]

            # Calculate drift (frames per minute)
            # Only calculate after warmup period to avoid startup transients
            drift_per_min = 0
            elapsed_since_start = current_time - self.comp_stats['start_time']
            history = self.comp_stats['offset_history'][pair_idx]
            if len(history) >= 2 and elapsed_since_start > 10:  # 10 second warmup
                first_time, first_offset = history[0]
                last_time, last_offset = history[-1]
                time_diff = last_time - first_time
                if time_diff > 5:  # Need at least 5 seconds of data
                    offset_diff = last_offset - first_offset
                    drift_per_min = (offset_diff / time_diff) * 60
                    # Cap drift to reasonable range (real drift is fractions of f/min)
                    drift_per_min = max(-100, min(100, drift_per_min))

            drifts.append(drift_per_min)

            # Detect significant drift changes
            # Use fixed threshold - real clock drift is very small, anything over 10 f/min is noise
            # Don't log if drift is at cap (±100) since that means invalid data
            last_drift_attr = f'_last_logged_drift_{src_idx}'
            last_drift = getattr(self, last_drift_attr, 0)
            if abs(drift_per_min) > 10 and abs(drift_per_min) < 100 and abs(drift_per_min - last_drift) > 5:
                self.comp_log(f"Drift detected Src{src_idx+1}: {drift_per_min:+.2f} frames/min")
                setattr(self, last_drift_attr, drift_per_min)

        # Update display
        self.update_comparator_display(all_in_sync, offsets, drifts, raw_offsets)

    def update_comparator_display(self, all_in_sync, offsets, drifts, raw_offsets=None):
        """Update the comparator display.

        Args:
            all_in_sync: True if all sources are in sync with reference
            offsets: List of offsets for each non-reference source (None if no data)
            drifts: List of drift rates (frames/min) for each non-reference source
        """
        threshold = self.threshold_var.get()

        # Check if any source is stalled (including reference)
        any_stalled = any(self.comp_stats['stalled']) if 'stalled' in self.comp_stats else False
        ref_stalled = self.comp_stats['stalled'][0] if 'stalled' in self.comp_stats and len(self.comp_stats['stalled']) > 0 else False

        # Overall sync status - show calibration progress if applicable
        if ref_stalled:
            self.sync_status_label.configure(text="REF STALLED", foreground='#ff4444')
        elif any_stalled:
            self.sync_status_label.configure(text="SOURCE STALLED", foreground='#ff4444')
        elif self.comp_use_calibration and not self.comp_calibration_done:
            # Calculate calibration progress
            min_samples = min(len(s) for s in self.comp_calibration_samples) if self.comp_calibration_samples else 0
            progress_pct = min(100, int(min_samples / self.comp_calibration_frames_needed * 100))
            self.sync_status_label.configure(text=f"CALIBRATING {progress_pct}%", foreground='#ffaa00')
        elif all_in_sync:
            self.sync_status_label.configure(text="ALL IN SYNC", foreground='#00ff88')
        else:
            self.sync_status_label.configure(text="OUT OF SYNC", foreground='#ff4444')

        # Update per-source offset displays with stabilization
        offset_parts = []
        for src_idx, offset in enumerate(offsets, start=1):
            pair_idx = src_idx - 1
            # Update offset label for this source
            if src_idx < len(self.comp_source_widgets):
                source = self.comp_source_widgets[src_idx]
                # Don't update offset display if this source is stalled (keep showing STALLED)
                is_src_stalled = self.comp_stats['stalled'][src_idx] if src_idx < len(self.comp_stats['stalled']) else False
                if is_src_stalled:
                    offset_parts.append(f"S{src_idx+1}:STALLED")
                    continue
                if offset is not None:
                    # Add to offset history for stabilization
                    if pair_idx < len(self.comp_offset_history):
                        self.comp_offset_history[pair_idx].append(offset)
                        # Keep only last 15 samples
                        if len(self.comp_offset_history[pair_idx]) > 15:
                            self.comp_offset_history[pair_idx].pop(0)

                        # Stabilize display offset using hysteresis
                        # This eliminates ±1 frame flicker from decoder phase jitter
                        history = self.comp_offset_history[pair_idx]
                        current_display = self.comp_display_offsets[pair_idx] if pair_idx < len(self.comp_display_offsets) else None

                        if len(history) >= 5:
                            # Calculate statistics from recent offsets
                            recent = history[-10:]
                            counter = Counter(recent)

                            # Check if offsets are oscillating in ±1 range (decoder phase jitter)
                            # In this case, favor 0 as it represents "in sync"
                            all_within_one = all(abs(o) <= 1 for o in recent)
                            has_zero = 0 in counter

                            if all_within_one and has_zero:
                                # Phase jitter - sources are in sync, show 0
                                display_offset = 0
                            else:
                                # Real offset - use mode with hysteresis
                                mode_offset = counter.most_common(1)[0][0]
                                mode_count = counter.most_common(1)[0][1]

                                if current_display is None:
                                    display_offset = mode_offset
                                elif mode_count >= 6:  # Mode appears in 60%+ of samples
                                    display_offset = mode_offset
                                elif abs(mode_offset - current_display) > 1:
                                    # Significant drift detected
                                    display_offset = mode_offset
                                else:
                                    display_offset = current_display
                        else:
                            # Not enough samples yet - show 0 if current offset is small
                            if current_display is None:
                                display_offset = 0 if abs(offset) <= 1 else offset
                            else:
                                display_offset = current_display

                        # Update stored display offset
                        if pair_idx < len(self.comp_display_offsets):
                            self.comp_display_offsets[pair_idx] = display_offset
                    else:
                        display_offset = offset

                    offset_text = f"{display_offset:+d}f"
                    if abs(display_offset) <= threshold:
                        source['offset_label'].configure(text=offset_text, foreground='#00ff88')
                    else:
                        source['offset_label'].configure(text=offset_text, foreground='#ff4444')
                    offset_parts.append(f"S{src_idx+1}:{display_offset:+d}")
                else:
                    source['offset_label'].configure(text="--", foreground='#888')

        # Show offsets summary with stabilization (use mode of recent offsets)
        if offset_parts:
            self.sync_offset_label.configure(text=f"Offsets: {', '.join(offset_parts)}")
        else:
            self.sync_offset_label.configure(text="Offsets: --")

        # Statistics - sum across all pairs
        stats = self.comp_stats
        total_in_sync = sum(stats['in_sync'])
        total_out_of_sync = sum(stats['out_of_sync'])

        self.comp_frames_label.configure(text=f"Frames compared: {stats['frames_compared']:,}")

        # Calculate sync percentage based on total comparisons
        num_pairs = len(offsets)
        total_comparisons = stats['frames_compared'] * num_pairs
        if total_comparisons > 0:
            sync_pct = (total_in_sync / total_comparisons) * 100
            self.comp_insync_label.configure(text=f"In sync: {total_in_sync:,} ({sync_pct:.1f}%)")
        else:
            self.comp_insync_label.configure(text="In sync: 0 (0%)")

        self.comp_outsync_label.configure(text=f"Out of sync: {total_out_of_sync:,}")
        self.comp_errors_label.configure(text=f"Skips: {stats['skips']}  Dups: {stats['duplicates']}  Errors: {stats['errors']}")

        # Show TC mismatch warning
        has_mismatch = any(stats['tc_mismatch'])
        if has_mismatch and raw_offsets:
            # Show TC mismatch in status with time difference
            if not (self.comp_use_calibration and not self.comp_calibration_done):
                # Find the largest mismatch and show it
                for pair_idx, raw_off in enumerate(raw_offsets):
                    if raw_off is not None and stats['tc_mismatch'][pair_idx]:
                        abs_frames = abs(raw_off)
                        sign = "+" if raw_off >= 0 else "-"
                        fps = 30  # Approximate
                        total_secs = abs_frames // fps
                        frames = abs_frames % fps
                        hours = total_secs // 3600
                        mins = (total_secs % 3600) // 60
                        secs = total_secs % 60

                        if hours > 0:
                            diff_str = f"{sign}{hours:d}h{mins:02d}m{secs:02d}s"
                        elif mins > 0:
                            diff_str = f"{sign}{mins:d}m{secs:02d}s"
                        else:
                            diff_str = f"{sign}{secs:d}s{frames:02d}f"
                        self.sync_status_label.configure(text=f"TC MISMATCH ({diff_str})", foreground='#ff4444')
                        break

    def comp_log(self, message):
        """Add message to comparator log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.comp_log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.comp_log_text.see(tk.END)

        # Limit log size
        line_count = int(self.comp_log_text.index('end-1c').split('.')[0])
        if line_count > 500:
            self.comp_log_text.delete('1.0', '100.0')

    def update_ffmpeg_status(self):
        """Update FFmpeg availability status."""
        if not FILE_ANALYSIS_AVAILABLE:
            self.ffmpeg_status_label.configure(text="")
            return

        if self.ffmpeg_manager.is_available():
            status = self.ffmpeg_manager.get_status()
            self.ffmpeg_status_label.configure(text=status, foreground='#00ff88')
        else:
            self.ffmpeg_status_label.configure(text="FFmpeg: Not installed (needed for video)", foreground='#ffaa00')

    def browse_files(self):
        """Open file browser to select multiple audio/video files."""
        filetypes = [
            ("All supported", "*.wav *.wave *.mp4 *.mov *.avi *.mkv *.mxf *.m4v"),
            ("Audio files", "*.wav *.wave"),
            ("Video files", "*.mp4 *.mov *.avi *.mkv *.mxf *.m4v"),
            ("All files", "*.*")
        ]

        # Get initial directory
        initial_dir = None
        current_path = self.file_path_var.get()
        if current_path and os.path.exists(os.path.dirname(current_path)):
            initial_dir = os.path.dirname(current_path)

        filenames = filedialog.askopenfilenames(
            parent=self.root,
            title="Select Files",
            filetypes=filetypes,
            initialdir=initial_dir
        )

        if filenames:
            self.selected_files = list(filenames)
            self._update_file_selection_display()

    def browse_folder(self):
        """Open folder browser to select all supported files in a folder."""
        initial_dir = None
        current_path = self.file_path_var.get()
        if current_path and os.path.exists(os.path.dirname(current_path)):
            initial_dir = os.path.dirname(current_path)

        folder = filedialog.askdirectory(
            parent=self.root,
            title="Select Folder",
            initialdir=initial_dir
        )

        if folder:
            # Find all supported files in folder and subfolders recursively
            supported_ext = ['.wav', '.wave', '.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v']
            files = []
            for root, dirs, filenames in os.walk(folder):
                for f in filenames:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in supported_ext:
                        files.append(os.path.join(root, f))

            if files:
                self.selected_files = sorted(files)
                self._update_file_selection_display()
            else:
                messagebox.showinfo("No Files", "No supported audio/video files found in folder.")

    def _update_file_selection_display(self):
        """Update the display after file selection."""
        # Clear previous results
        self.file_summary_text.delete(1.0, tk.END)
        self.file_issues_text.delete(1.0, tk.END)

        count = len(self.selected_files)
        if count == 1:
            self.file_path_var.set(self.selected_files[0])
            # Show single file info
            if FILE_ANALYSIS_AVAILABLE:
                try:
                    info = self.file_analyzer.get_file_info(self.selected_files[0])
                    self.file_summary_text.insert(tk.END, f"File: {info['name']}\n")
                    self.file_summary_text.insert(tk.END, f"Type: {info.get('type', 'unknown')}\n")
                    size_mb = info.get('size_bytes', 0) / (1024 * 1024)
                    self.file_summary_text.insert(tk.END, f"Size: {size_mb:.1f} MB\n")
                except Exception as e:
                    self.file_summary_text.insert(tk.END, f"Error: {e}\n")
        else:
            self.file_path_var.set(f"{count} files selected")
            self.file_summary_text.insert(tk.END, f"Selected {count} files for batch analysis:\n\n")
            for i, f in enumerate(self.selected_files[:20], 1):
                self.file_summary_text.insert(tk.END, f"  {i}. {os.path.basename(f)}\n")
            if count > 20:
                self.file_summary_text.insert(tk.END, f"  ... and {count - 20} more\n")

    def start_file_analysis(self):
        """Start file analysis in background thread."""
        if not FILE_ANALYSIS_AVAILABLE:
            messagebox.showerror("Error", "File analysis not available. Install scipy and requests.")
            return

        # Check if we have files selected
        if not self.selected_files:
            # Try to use the path from entry
            file_path = self.file_path_var.get()
            if file_path and os.path.exists(file_path):
                self.selected_files = [file_path]
            else:
                messagebox.showwarning("Warning", "Please select file(s) first.")
                return

        # Check for video files and FFmpeg
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm']
        has_video = any(os.path.splitext(f)[1].lower() in video_extensions for f in self.selected_files)

        if has_video and not self.ffmpeg_manager.is_available():
            result = messagebox.askyesno(
                "FFmpeg Required",
                "FFmpeg is required to analyze video files.\n\n"
                "Would you like to download FFmpeg now?\n"
                "(This will download ~80MB to the app folder)"
            )
            if result:
                self.download_ffmpeg()
            return

        # Clear previous results
        self.file_summary_text.delete(1.0, tk.END)
        self.file_issues_text.delete(1.0, tk.END)

        # Update UI state
        self.analyze_btn.configure(state=tk.DISABLED)
        self.cancel_analysis_btn.configure(state=tk.NORMAL)
        self.file_progress['value'] = 0
        self.file_analysis_cancelled = False

        # Get channel selection
        channel_str = self.file_channel_var.get()
        if channel_str == "Auto-detect":
            channel = 'auto'
        else:
            channel = str(int(channel_str) - 1)  # Convert to 0-indexed

        # Get FPS selection
        fps_str = self.file_fps_var.get()
        if fps_str == "Auto":
            fps = 'auto'
        else:
            fps = fps_str  # Pass as-is ('23.976', '24', '25', '29.97', '29.97DF', '30')

        # Store batch results
        self.batch_results = []

        # Get parallel workers count
        workers = int(self.parallel_workers_var.get())

        # Start analysis thread
        self.file_analysis_thread = threading.Thread(
            target=self._run_batch_analysis,
            args=(self.selected_files, channel, fps, workers),
            daemon=True
        )
        self.file_analysis_thread.start()

        # Start checking for results
        self.root.after(100, self.check_file_analysis_results)

    def _run_batch_analysis(self, files, channel, fps='auto', workers=4):
        """Run batch file analysis in background thread with parallel processing."""
        total_files = len(files)
        results = []
        completed_count = 0
        results_lock = threading.Lock()

        def analyze_single_file(file_path):
            """Analyze a single file - called by thread pool."""
            nonlocal completed_count

            if self.file_analysis_cancelled:
                return None

            # Create a new FileAnalyzer instance for each file to avoid thread-safety issues
            file_analyzer = FileAnalyzer()

            try:
                result = file_analyzer.analyze_file(
                    file_path,
                    channel=channel,
                    fps=fps,
                    progress_callback=None,  # Disable per-file progress for parallel
                    status_callback=None
                )

                return {
                    'file': file_path,
                    'result': result,
                    'error': None
                }

            except Exception as e:
                return {
                    'file': file_path,
                    'result': None,
                    'error': str(e)
                }
            finally:
                # Clean up temp files for this analyzer
                file_analyzer.cleanup()

        # Use ThreadPoolExecutor for parallel analysis
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Submit all files
            future_to_file = {executor.submit(analyze_single_file, f): f for f in files}

            # Process completed files as they finish
            for future in as_completed(future_to_file):
                if self.file_analysis_cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.file_result_queue.put(('cancelled', results))
                    return

                result = future.result()
                if result:
                    with results_lock:
                        results.append(result)
                        completed_count += 1

                    # Update progress
                    progress = (completed_count / total_files) * 100
                    self.file_result_queue.put(('progress', progress))
                    self.file_result_queue.put(('status', f"Analyzed {completed_count}/{total_files}: {os.path.basename(result['file'])}"))

        self.file_result_queue.put(('batch_complete', results))

    def check_file_analysis_results(self):
        """Check for file analysis results from background thread."""
        try:
            while True:
                msg_type, data = self.file_result_queue.get_nowait()

                if msg_type == 'progress':
                    self.file_progress['value'] = data

                elif msg_type == 'status':
                    self.file_status_label.configure(text=data)

                elif msg_type == 'complete':
                    self.on_file_analysis_complete(data)
                    return  # Stop checking

                elif msg_type == 'batch_complete':
                    self.on_batch_analysis_complete(data)
                    return  # Stop checking

                elif msg_type == 'cancelled':
                    self.on_batch_analysis_complete(data, cancelled=True)
                    return  # Stop checking

                elif msg_type == 'error':
                    self.on_file_analysis_error(data)
                    return  # Stop checking

        except queue.Empty:
            pass

        # Continue checking if analysis is still running
        if self.file_analysis_thread and self.file_analysis_thread.is_alive():
            self.root.after(100, self.check_file_analysis_results)

    def on_batch_analysis_complete(self, results, cancelled=False):
        """Handle completed batch analysis."""
        # Update UI state
        self.analyze_btn.configure(state=tk.NORMAL)
        self.cancel_analysis_btn.configure(state=tk.DISABLED)
        self.file_progress['value'] = 100

        if cancelled:
            self.file_status_label.configure(text=f"Cancelled - {len(results)} files analyzed")
        else:
            self.file_status_label.configure(text=f"Complete - {len(results)} files analyzed")

        # Categorize results
        files_with_issues = []
        files_clean = []
        files_error = []

        for item in results:
            filename = os.path.basename(item['file'])
            if item['error']:
                files_error.append((filename, item['error']))
            elif item['result']:
                r = item['result']
                total_issues = r.skip_count + r.duplicate_count + r.error_count
                if total_issues > 0:
                    files_with_issues.append((filename, r))
                else:
                    files_clean.append((filename, r))

        # Display summary
        self.file_summary_text.delete(1.0, tk.END)
        self.file_summary_text.insert(tk.END, "BATCH ANALYSIS SUMMARY\n")
        self.file_summary_text.insert(tk.END, "=" * 40 + "\n\n")
        self.file_summary_text.insert(tk.END, f"Total files analyzed: {len(results)}\n")
        self.file_summary_text.insert(tk.END, f"Files with issues:    {len(files_with_issues)}\n")
        self.file_summary_text.insert(tk.END, f"Clean files:          {len(files_clean)}\n")
        self.file_summary_text.insert(tk.END, f"Errors:               {len(files_error)}\n")
        self.file_summary_text.insert(tk.END, "\n")

        if files_clean:
            self.file_summary_text.insert(tk.END, "CLEAN FILES:\n")
            for name, r in files_clean:
                fps_str = f"{r.detected_fps:.3f}" if r.detected_fps else "?"
                start_tc = r.first_timecode if r.first_timecode else "?"
                self.file_summary_text.insert(tk.END, f"  [OK] {name}\n")
                self.file_summary_text.insert(tk.END, f"       FPS: {fps_str}  Start TC: {start_tc}\n")
            self.file_summary_text.insert(tk.END, "\n")

        # Display issues detail
        self.file_issues_text.delete(1.0, tk.END)

        if files_with_issues:
            self.file_issues_text.insert(tk.END, "FILES WITH TIMECODE ISSUES:\n")
            self.file_issues_text.insert(tk.END, "=" * 50 + "\n\n")

            for name, r in files_with_issues:
                fps_str = f"{r.detected_fps:.3f}" if r.detected_fps else "?"
                start_tc = r.first_timecode if r.first_timecode else "?"
                self.file_issues_text.insert(tk.END, f"[!] {name}\n")
                self.file_issues_text.insert(tk.END, f"    FPS: {fps_str}  Start TC: {start_tc}\n")
                self.file_issues_text.insert(tk.END, f"    Skips: {r.skip_count}")
                if r.skipped_frames_total > 0:
                    self.file_issues_text.insert(tk.END, f" ({r.skipped_frames_total} frames)")
                self.file_issues_text.insert(tk.END, f"  Dups: {r.duplicate_count}  Errors: {r.error_count}\n")

                # Show first few issues
                if r.issues:
                    for issue in r.issues[:3]:
                        self.file_issues_text.insert(tk.END, f"      [{issue.timecode}] {issue.issue_type}: {issue.description}\n")
                    if len(r.issues) > 3:
                        self.file_issues_text.insert(tk.END, f"      ... and {len(r.issues) - 3} more issues\n")
                self.file_issues_text.insert(tk.END, "\n")
        else:
            self.file_issues_text.insert(tk.END, "No timecode issues found in any file!\n")

        if files_error:
            self.file_issues_text.insert(tk.END, "\nFILES WITH ERRORS:\n")
            self.file_issues_text.insert(tk.END, "-" * 50 + "\n")
            for name, error in files_error:
                self.file_issues_text.insert(tk.END, f"[X] {name}\n")
                self.file_issues_text.insert(tk.END, f"    Error: {error}\n\n")

        # Check for overlapping timecodes
        all_results = [item['result'] for item in results if item['result']]
        overlaps = find_timecode_overlaps(all_results)

        if overlaps:
            self.file_issues_text.insert(tk.END, "\nOVERLAPPING TIMECODES:\n")
            self.file_issues_text.insert(tk.END, "=" * 50 + "\n\n")
            for overlap in overlaps:
                self.file_issues_text.insert(tk.END, f"[OVERLAP] {overlap.file1} <-> {overlap.file2}\n")
                self.file_issues_text.insert(tk.END, f"    File 1: {overlap.file1_range}\n")
                self.file_issues_text.insert(tk.END, f"    File 2: {overlap.file2_range}\n")
                self.file_issues_text.insert(tk.END, f"    Overlap: {overlap.overlap_start} - {overlap.overlap_end}\n")
                self.file_issues_text.insert(tk.END, f"    Duration: {overlap.overlap_frames} frames ({overlap.overlap_duration:.2f}s)\n\n")

            # Update summary with overlap count
            self.file_summary_text.insert(tk.END, f"Overlapping files:    {len(overlaps)}\n")

        # Save batch report if enabled
        if self.save_report_var.get() and results:
            self.save_batch_report(results, overlaps)

    def save_batch_report(self, results, overlaps=None):
        """Save batch analysis report."""
        if overlaps is None:
            overlaps = []
        # Get output directory from first file
        if results:
            output_dir = os.path.dirname(results[0]['file'])
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            report_path = os.path.join(output_dir, f"batch_ltc_report_{timestamp}.txt")

            try:
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write("=" * 80 + "\n")
                    f.write("LTC TIMECODE BATCH ANALYSIS REPORT\n")
                    f.write("=" * 80 + "\n")
                    f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Files analyzed: {len(results)}\n\n")

                    # Categorize
                    files_with_issues = []
                    files_clean = []
                    files_error = []

                    for item in results:
                        filename = os.path.basename(item['file'])
                        if item['error']:
                            files_error.append((filename, item['file'], item['error']))
                        elif item['result']:
                            r = item['result']
                            total_issues = r.skip_count + r.duplicate_count + r.error_count
                            if total_issues > 0:
                                files_with_issues.append((filename, item['file'], r))
                            else:
                                files_clean.append((filename, item['file'], r))

                    # Summary
                    f.write("SUMMARY\n")
                    f.write("-" * 80 + "\n")
                    f.write(f"Files with issues: {len(files_with_issues)}\n")
                    f.write(f"Clean files:       {len(files_clean)}\n")
                    f.write(f"Errors:            {len(files_error)}\n\n")

                    # Files with issues
                    if files_with_issues:
                        f.write("FILES WITH TIMECODE ISSUES\n")
                        f.write("=" * 80 + "\n\n")

                        for name, path, r in files_with_issues:
                            f.write(f"File: {name}\n")
                            f.write(f"Path: {path}\n")
                            f.write(f"Duration: {r.duration_seconds:.1f}s | Frames: {r.total_frames} | FPS: {r.detected_fps}\n")
                            f.write(f"Issues: Skips={r.skip_count} ({r.skipped_frames_total} frames), Dups={r.duplicate_count}, Errors={r.error_count}\n")
                            f.write(f"Quality: {r.clean_percentage:.2f}% clean\n")
                            f.write("\nIssues detail:\n")
                            for issue in r.issues:
                                f.write(f"  [{issue.timecode}] {issue.issue_type}: {issue.description}\n")
                            f.write("\n" + "-" * 40 + "\n\n")

                    # Clean files
                    if files_clean:
                        f.write("CLEAN FILES\n")
                        f.write("=" * 80 + "\n")
                        for name, path, r in files_clean:
                            f.write(f"[OK] {name} ({r.total_frames} frames, {r.detected_fps} fps)\n")
                        f.write("\n")

                    # Errors
                    if files_error:
                        f.write("FILES WITH ERRORS\n")
                        f.write("=" * 80 + "\n")
                        for name, path, error in files_error:
                            f.write(f"[ERROR] {name}\n")
                            f.write(f"        {error}\n")
                        f.write("\n")

                    # Overlapping timecodes
                    if overlaps:
                        f.write("OVERLAPPING TIMECODES\n")
                        f.write("=" * 80 + "\n\n")
                        for overlap in overlaps:
                            f.write(f"[OVERLAP] {overlap.file1} <-> {overlap.file2}\n")
                            f.write(f"    File 1: {overlap.file1_range}\n")
                            f.write(f"    File 2: {overlap.file2_range}\n")
                            f.write(f"    Overlap: {overlap.overlap_start} - {overlap.overlap_end}\n")
                            f.write(f"    Duration: {overlap.overlap_frames} frames ({overlap.overlap_duration:.2f}s)\n\n")

                    f.write("=" * 80 + "\n")
                    f.write("Generated by LTC Timecode Analyzer\n")

                self.file_issues_text.insert(tk.END, f"\n\nReport saved: {report_path}")

            except Exception as e:
                messagebox.showwarning("Report Error", f"Failed to save report: {e}")

    def on_file_analysis_complete(self, result: 'AnalysisResult'):
        """Handle completed single file analysis."""
        # Update UI state
        self.analyze_btn.configure(state=tk.NORMAL)
        self.cancel_analysis_btn.configure(state=tk.DISABLED)
        self.file_progress['value'] = 100
        self.file_status_label.configure(text="Analysis complete!")

        # Display summary
        self.file_summary_text.delete(1.0, tk.END)
        summary = self.report_generator.format_summary(result)
        self.file_summary_text.insert(tk.END, summary)

        # Display issues
        self.file_issues_text.delete(1.0, tk.END)
        issues_text = self.report_generator.format_issues_list(result)
        self.file_issues_text.insert(tk.END, issues_text)

        # Save report if enabled
        if self.save_report_var.get():
            self.save_analysis_report(result)

    def on_file_analysis_error(self, error_msg):
        """Handle file analysis error."""
        # Update UI state
        self.analyze_btn.configure(state=tk.NORMAL)
        self.cancel_analysis_btn.configure(state=tk.DISABLED)
        self.file_progress['value'] = 0
        self.file_status_label.configure(text="Error!")

        # Display error
        self.file_summary_text.delete(1.0, tk.END)
        self.file_summary_text.insert(tk.END, f"Analysis failed:\n\n{error_msg}")

        messagebox.showerror("Analysis Error", error_msg)

    def cancel_file_analysis(self):
        """Cancel ongoing file analysis."""
        if self.file_analyzer:
            self.file_analyzer.cancel()
        self.file_analysis_cancelled = True
        self.file_status_label.configure(text="Cancelling...")

    def save_analysis_report(self, result: 'AnalysisResult'):
        """Save analysis report to file."""
        format_choice = self.report_format_var.get()

        try:
            if format_choice in ["Text", "Both"]:
                txt_path = self.report_generator.get_default_report_path(result.file_path, 'txt')
                self.report_generator.generate_text_report(result, txt_path)
                self.file_issues_text.insert(tk.END, f"\n\nReport saved: {txt_path}")

            if format_choice in ["CSV", "Both"]:
                csv_path = self.report_generator.get_default_report_path(result.file_path, 'csv')
                self.report_generator.generate_csv_report(result, csv_path)
                self.file_issues_text.insert(tk.END, f"\nCSV saved: {csv_path}")

        except Exception as e:
            messagebox.showwarning("Report Error", f"Failed to save report: {e}")

    def download_ffmpeg(self):
        """Download FFmpeg with progress dialog."""
        # Create progress dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Downloading FFmpeg")
        dialog.geometry("400x150")
        dialog.transient(self.root)
        dialog.grab_set()

        # Center on parent
        dialog.geometry(f"+{self.root.winfo_x() + 250}+{self.root.winfo_y() + 200}")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        status_label = ttk.Label(frame, text="Starting download...")
        status_label.pack(pady=10)

        progress = ttk.Progressbar(frame, length=300, mode='determinate')
        progress.pack(pady=10)

        cancel_var = {'cancelled': False}

        def cancel():
            cancel_var['cancelled'] = True
            self.ffmpeg_manager.cancel_download()
            dialog.destroy()

        cancel_btn = ttk.Button(frame, text="Cancel", command=cancel)
        cancel_btn.pack(pady=10)

        def do_download():
            def progress_cb(percent):
                if not cancel_var['cancelled']:
                    dialog.after(0, lambda: progress.configure(value=percent))

            def status_cb(message):
                if not cancel_var['cancelled']:
                    dialog.after(0, lambda: status_label.configure(text=message))

            success = self.ffmpeg_manager.download_ffmpeg(
                progress_callback=progress_cb,
                status_callback=status_cb
            )

            if not cancel_var['cancelled']:
                dialog.after(0, dialog.destroy)
                if success:
                    self.root.after(0, lambda: messagebox.showinfo("Success", "FFmpeg downloaded successfully!"))
                    self.root.after(0, self.update_ffmpeg_status)
                else:
                    self.root.after(0, lambda: messagebox.showerror("Error", "FFmpeg download failed."))

        # Run download in thread
        thread = threading.Thread(target=do_download, daemon=True)
        thread.start()

    def populate_devices(self):
        """Populate audio device list."""
        if not AUDIO_AVAILABLE:
            self.device_combo['values'] = ["sounddevice not installed"]
            return

        devices = sd.query_devices()
        input_devices = []
        self.device_channels = {}  # Store channel count for each device

        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0:
                input_devices.append(f"{i}: {dev['name']}")
                self.device_channels[i] = dev['max_input_channels']

        self.device_combo['values'] = input_devices
        if input_devices:
            self.device_combo.current(0)
            self.on_device_changed(None)

    def on_device_changed(self, event):
        """Update channel list when device changes."""
        device = self.get_selected_device()
        if device is not None and device in self.device_channels:
            num_channels = self.device_channels[device]
            channels = [str(i+1) for i in range(num_channels)]
            self.channel_combo['values'] = channels
            if self.channel_var.get() not in channels:
                self.channel_var.set("1")
            self.log(f"Device has {num_channels} input channel(s)")

    def get_selected_device(self):
        """Get selected device index."""
        selection = self.device_var.get()
        if selection:
            return int(selection.split(':')[0])
        return None

    def get_selected_channel(self):
        """Get selected channel (0-indexed)."""
        try:
            return int(self.channel_var.get()) - 1  # Convert to 0-indexed
        except ValueError:
            return 0

    def toggle_capture(self):
        """Start or stop capture."""
        if self.running:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self):
        """Start audio capture."""
        if not AUDIO_AVAILABLE:
            self.log("ERROR: sounddevice not installed. Run: pip install sounddevice numpy")
            return

        device = self.get_selected_device()
        if device is None:
            self.log("ERROR: No audio device selected")
            return

        sample_rate = int(self.sample_rate_var.get())
        self.decoder.set_sample_rate(sample_rate)
        self.decoder.reset()  # Ensure clean decoder state
        self.analyzer.set_sample_rate(sample_rate)
        self.analyzer.reset()  # Ensure clean analyzer state
        if hasattr(self.analyzer, '_max_frame_seen'):
            delattr(self.analyzer, '_max_frame_seen')

        # Set FPS if not auto
        fps_str = self.fps_var.get()
        if fps_str == "Auto":
            self.analyzer.set_fps(None)
            self.decoder.set_expected_fps(None)
        elif fps_str == "29.97DF":
            self.analyzer.set_fps(29.97)
            self.decoder.set_expected_fps(29.97)
        else:
            fps = float(fps_str)
            self.analyzer.set_fps(fps)
            self.decoder.set_expected_fps(fps)

        # Get number of channels for this device
        num_channels = self.device_channels.get(device, 1)
        self.selected_channel = self.get_selected_channel()

        # Make sure selected channel is valid
        if self.selected_channel >= num_channels:
            self.selected_channel = 0
            self.channel_var.set("1")

        try:
            self.stream = sd.InputStream(
                device=device,
                channels=num_channels,  # Record all channels
                samplerate=sample_rate,
                blocksize=4096,  # Larger buffer to prevent overflow
                latency='high',  # Higher latency = more stable
                callback=self.audio_callback
            )
            self.stream.start()
            self.running = True
            self.start_btn.configure(text="■ Stop")
            self.status_label.configure(text="Running...", foreground='#00ff88')
            self.log(f"Started capture on device {device}, channel {self.selected_channel + 1} at {sample_rate}Hz")

            # Open log file
            self.open_log_file()

            # Start queue processing thread (event-driven, not polling)
            self.start_display_queue_thread()
        except Exception as e:
            self.log(f"ERROR: {str(e)}")

    def stop_capture(self):
        """Stop audio capture."""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False
        self.start_btn.configure(text="▶ Start")
        self.status_label.configure(text="Stopped", foreground='#aaa')

        # Write summary and close log file
        self.write_summary_and_close_log()

        self.log("Capture stopped")

        # Reset level meter
        self.level_canvas.coords(self.level_bar, 0, 2, 0, 23)
        self.level_canvas.coords(self.peak_bar, 0, 0, 0, 25)
        self.db_label.configure(text="-∞ dB")
        self.peak_level = 0

        # Reset sync status
        self.sync_label.configure(text="Sync: --", foreground='#aaa')
        self.timing_label.configure(text="Bit timing: --")
        self.direction_label.configure(text="Direction: --")

    def audio_callback(self, indata, frames, time_info, status):
        """Audio input callback."""
        if status:
            # Only log overflow occasionally to reduce spam
            if not hasattr(self, '_last_overflow') or time.time() - self._last_overflow > 2:
                self.frame_queue.put(('status', str(status)))
                self._last_overflow = time.time()

        # Extract selected channel from multi-channel input
        channel = getattr(self, 'selected_channel', 0)
        if channel < indata.shape[1]:
            samples = indata[:, channel]
        else:
            samples = indata[:, 0]

        # Track audio samples received for accurate timing
        self.analyzer.add_audio_samples(len(samples))

        # Calculate audio level (RMS and peak)
        if len(samples) > 0:
            peak = np.max(np.abs(samples))
            rms = np.sqrt(np.mean(samples**2))
            self.frame_queue.put(('level', (rms, peak)))

        # Decode LTC frames
        decoded_frames = self.decoder.process_samples_fast(samples)

        for frame in decoded_frames:
            self.frame_queue.put(('frame', frame))

    def reset_stats(self):
        """Reset statistics."""
        self.analyzer.reset()
        self.decoder.reset()
        if hasattr(self.analyzer, '_max_frame_seen'):
            delattr(self.analyzer, '_max_frame_seen')
        self.log_text.delete(1.0, tk.END)
        self.log("Statistics reset")

    def log(self, message):
        """Add message to log."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def update_sync_status(self):
        """Update sync status display from decoder state."""
        if self.decoder.synced:
            self.sync_label.configure(text="Sync: LOCKED", foreground='#00ff88')
        elif self.decoder.half_bit_samples > 0:
            self.sync_label.configure(text="Sync: SEARCHING", foreground='#ffaa00')
        else:
            self.sync_label.configure(text="Sync: NO SIGNAL", foreground='#ff4444')

        # Show bit timing
        if self.decoder.half_bit_samples > 0:
            samples_per_bit = self.decoder.half_bit_samples * 2
            detected_fps = self.decoder.sample_rate / (samples_per_bit * 80)
            self.timing_label.configure(text=f"Detected: {detected_fps:.3f} fps ({samples_per_bit:.1f} samp/bit)")
        else:
            self.timing_label.configure(text="Bit timing: detecting...")

        # Show direction
        if self.decoder.synced:
            direction = "Forward" if self.decoder.direction == 1 else "Reverse"
            self.direction_label.configure(text=f"Direction: {direction}")
        else:
            self.direction_label.configure(text="Direction: --")

    def update_level_meter(self, rms, peak):
        """Update the audio level meter."""
        # Convert to dB (avoid log of zero)
        if rms > 0:
            db = 20 * np.log10(rms)
        else:
            db = -60

        if peak > 0:
            peak_db = 20 * np.log10(peak)
        else:
            peak_db = -60

        # Clamp values
        db = max(-60, min(0, db))
        peak_db = max(-60, min(0, peak_db))

        # Convert to pixel position (0-400 for -60 to 0 dB)
        level_width = int((db + 60) / 60 * 400)
        peak_pos = int((peak_db + 60) / 60 * 400)

        # Color based on level
        if db > -6:
            color = '#ff4444'  # Red - too hot
        elif db > -12:
            color = '#ffaa00'  # Orange - warm
        else:
            color = '#00ff88'  # Green - good

        # Update meter bar
        self.level_canvas.coords(self.level_bar, 0, 2, level_width, 23)
        self.level_canvas.itemconfig(self.level_bar, fill=color)

        # Update peak indicator with hold
        current_time = time.time()
        if peak_pos > self.peak_level or current_time - self.peak_hold_time > 1.5:
            self.peak_level = peak_pos
            self.peak_hold_time = current_time

        self.level_canvas.coords(self.peak_bar, self.peak_level, 0, self.peak_level, 25)

        # Update dB label
        if db <= -60:
            self.db_label.configure(text="-∞ dB")
        else:
            self.db_label.configure(text=f"{db:.1f} dB")

    def start_display_queue_thread(self):
        """Start background thread that waits for queue data (interrupt-like)."""
        def queue_processor():
            while self.running:
                try:
                    # Block until data arrives (with timeout for clean shutdown)
                    msg = self.frame_queue.get(timeout=0.1)
                    # Process on main thread for thread-safe UI updates
                    self.root.after(0, lambda m=msg: self.process_display_message(m))
                except queue.Empty:
                    pass

        self._display_queue_thread = threading.Thread(target=queue_processor, daemon=True)
        self._display_queue_thread.start()

    def process_display_message(self, msg):
        """Process a single message from the frame queue (runs on main thread)."""
        if not self.running:
            return

        msg_type, data = msg

        if msg_type == 'level':
            rms, peak = data
            self.update_level_meter(rms, peak)
            self.update_sync_status()
        elif msg_type == 'status':
            self.log(f"Audio status: {data}")
        elif msg_type == 'debug':
            if self.debug_var.get():
                self.log(f"DEBUG: {data}")
        elif msg_type == 'frame':
            result = self.analyzer.analyze_frame(data)
            if self.debug_var.get():
                self.log(f"FRAME: {result['timecode']} (diff={result.get('diff', '?')})")

            # Log frame to file
            self.write_frame_to_log(result)

            # Update timecode display
            tc_text = result['timecode']
            if result['drop_frame']:
                tc_text += " DF"
            self.timecode_label.configure(text=tc_text)

            # Update status
            if result['status'] == 'OK':
                self.status_label.configure(text="● Receiving LTC", foreground='#00ff88')
            elif result['status'] == 'SKIP':
                self.status_label.configure(text=f"⚠ {result['issue']}", foreground='#ffaa00')
            elif result['status'] == 'DUPLICATE':
                self.status_label.configure(text=f"⚠ {result['issue']}", foreground='#ffaa00')
            else:
                self.status_label.configure(text=f"✗ {result['issue']}", foreground='#ff4444')

            # Update stats
            measurement_time = result.get('measurement_time', 0)
            if self.fps_var.get() == "Auto":
                if measurement_time < 10:
                    self.fps_label.configure(text=f"Detected FPS: {result['fps']} (detecting...)")
                else:
                    self.fps_label.configure(text=f"Detected FPS: {result['fps']}")
            else:
                self.fps_label.configure(text=f"Expected FPS: {result['fps']}")
            if result['measured_fps'] > 0:
                if measurement_time < 10:
                    self.measured_fps_label.configure(
                        text=f"Measured FPS: {result['measured_fps']:.4f} (stabilizing {measurement_time:.0f}s)",
                        foreground='#ffaa00')
                else:
                    self.measured_fps_label.configure(
                        text=f"Measured FPS: {result['measured_fps']:.6f} ({measurement_time:.0f}s)",
                        foreground='#00ff88')

                # Calculate drift - only show meaningful values after 10 seconds
                if measurement_time >= 10:
                    expected = result['fps']
                    measured = result['measured_fps']
                    drift_ppm = ((measured - expected) / expected) * 1000000

                    # Show audio clock ratio if significantly off (helps diagnose sound card issues)
                    audio_ratio = result.get('audio_clock_ratio', 1.0)
                    soundcard_ppm = (audio_ratio - 1.0) * 1000000
                    if abs(soundcard_ppm) > 100:
                        self.drift_label.configure(text=f"Drift: {drift_ppm:+.1f} ppm (SC: {soundcard_ppm:+.0f})")
                    else:
                        self.drift_label.configure(text=f"Drift: {drift_ppm:+.1f} ppm")

                    # Update accuracy indicator
                    if abs(drift_ppm) < 10:
                        self.accuracy_label.configure(text="EXCELLENT", foreground='#00ff88')
                    elif abs(drift_ppm) < 50:
                        self.accuracy_label.configure(text="GOOD", foreground='#88ff00')
                    elif abs(drift_ppm) < 100:
                        self.accuracy_label.configure(text="ACCEPTABLE", foreground='#ffff00')
                    elif abs(drift_ppm) < 500:
                        self.accuracy_label.configure(text="POOR", foreground='#ffaa00')
                    else:
                        self.accuracy_label.configure(text="BAD", foreground='#ff4444')
                else:
                    self.drift_label.configure(text="Drift: (stabilizing...)")
                    self.accuracy_label.configure(text="MEASURING", foreground='#ffaa00')

            self.frames_label.configure(text=f"Frames decoded: {self.analyzer.frame_count}")
            self.skip_label.configure(text=f"Frame skips: {self.analyzer.skip_count}")
            self.dup_label.configure(text=f"Duplicates: {self.analyzer.duplicate_count}")
            self.error_label.configure(text=f"Errors: {self.analyzer.error_count}")

            # Log issues
            if result['issue']:
                self.log(result['issue'])

    def open_log_file(self):
        """Initialize rolling log buffer."""
        self.log_buffer.clear()
        self.log_file_path = "ltc_log.txt"
        self.log(f"Log buffer ready: {self.log_file_path}")

    def write_frame_to_log(self, result):
        """Add a decoded frame to the rolling log buffer."""
        try:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            tc = result['timecode']
            status = result['status']
            measurement_time = result.get('measurement_time', 0)

            # Only show measured FPS after 5 seconds of data
            if measurement_time >= 5:
                measured_fps = f"{result.get('measured_fps', 0):.6f}"
            else:
                measured_fps = "---"

            # Only calculate drift after 10 seconds (when measurement is stable)
            expected = result.get('fps', 0)
            measured = result.get('measured_fps', 0)
            if measurement_time >= 10 and expected > 0 and measured > 0:
                drift_ppm = ((measured - expected) / expected) * 1000000
                drift_str = f"{drift_ppm:+.2f}"
            else:
                drift_str = "---"

            line = f"{timestamp},{tc},{status},{measured_fps},{drift_str}"
            # Add to rolling buffer (old entries automatically removed when maxlen reached)
            self.log_buffer.append(line)

        except Exception as e:
            # Silently ignore errors to not spam the GUI log
            pass

    def write_summary_and_close_log(self):
        """Write rolling buffer contents and summary to log file."""
        if len(self.log_buffer) == 0 and self.analyzer.frame_count == 0:
            return

        try:
            summary = self.analyzer.get_summary()

            # Write buffer contents and summary to file (overwrites previous)
            with open(self.log_file_path, 'w') as f:
                # Write header
                f.write("LTC Timecode Analysis Log\n")
                f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n")
                f.write("Time,Timecode,Status,Measured FPS,Drift PPM\n")
                f.write("-" * 60 + "\n")

                # Write all buffered frame entries (last 150 frames)
                for line in self.log_buffer:
                    f.write(line + "\n")

                # Write summary
                f.write("\n" + "=" * 60 + "\n")
                f.write("ANALYSIS SUMMARY\n")
                f.write("=" * 60 + "\n")

                f.write(f"Frames Decoded:        {summary['frames_decoded']}\n")
                f.write(f"TC Frames Elapsed:     {summary['tc_frames_elapsed']}\n")
                f.write(f"Real Time Elapsed:     {summary['real_time_elapsed']:.2f} seconds\n\n")

                f.write(f"Expected FPS:          {summary['expected_fps']}\n")
                f.write(f"Measured FPS:          {summary['measured_fps']:.6f}\n\n")

                f.write(f"Frame Skips:           {summary['frame_skips']}\n")
                f.write(f"Duplicate Frames:      {summary['duplicates']}\n")
                f.write(f"Errors:                {summary['errors']}\n\n")

                # Check if measurement is reliable
                if not summary.get('measurement_reliable', False):
                    f.write(f"\n** WARNING: Measurement duration too short ({summary['real_time_elapsed']:.1f}s) **\n")
                    f.write(f"** Run for at least 60 seconds for accurate drift measurement **\n")
                    f.write(f"** Audio buffer latency (~50-100ms) significantly affects short tests **\n\n")

                f.write(f"Drift:                 {summary['drift_ppm']:+.2f} ppm\n")
                f.write(f"Drift (sec/hour):      {summary['drift_seconds_per_hour']:+.4f} seconds\n")

                # Show audio clock ratio if significantly off
                audio_ratio = summary.get('audio_clock_ratio', 1.0)
                soundcard_ppm = (audio_ratio - 1.0) * 1000000
                if abs(soundcard_ppm) > 50:
                    f.write(f"\nSound Card Clock:      {soundcard_ppm:+.0f} ppm vs wall clock\n")
                    f.write(f"(Drift measurement uses audio clock, eliminating sound card error)\n")

                # Interpret drift - but warn if measurement is unreliable
                drift_ppm = abs(summary['drift_ppm'])
                if not summary.get('measurement_reliable', False):
                    accuracy = "UNRELIABLE - Test duration too short (need 30+ seconds)"
                elif drift_ppm < 10:
                    accuracy = "EXCELLENT - Professional broadcast quality"
                elif drift_ppm < 50:
                    accuracy = "GOOD - Suitable for most applications"
                elif drift_ppm < 100:
                    accuracy = "ACCEPTABLE - Minor drift over long durations"
                elif drift_ppm < 500:
                    accuracy = "POOR - Noticeable drift in extended recordings"
                else:
                    accuracy = "BAD - Significant timing issues"

                f.write(f"\nAccuracy Rating:       {accuracy}\n")

            # Also log summary to GUI
            self.log("=" * 40)
            self.log("SESSION SUMMARY:")
            self.log(f"  Frames decoded: {summary['frames_decoded']}")
            self.log(f"  Duration: {summary['real_time_elapsed']:.1f} sec")
            self.log(f"  Measured FPS: {summary['measured_fps']:.6f}")
            self.log(f"  Expected FPS: {summary['expected_fps']}")
            if summary.get('measurement_reliable', False):
                self.log(f"  Drift: {summary['drift_ppm']:+.2f} ppm ({summary['drift_seconds_per_hour']:+.4f} sec/hour)")
            else:
                self.log(f"  Drift: {summary['drift_ppm']:+.2f} ppm (UNRELIABLE - test too short!)")
                self.log(f"  ** Run for 60+ seconds for accurate results **")

            # Show sound card clock info if significantly off
            audio_ratio = summary.get('audio_clock_ratio', 1.0)
            soundcard_ppm = (audio_ratio - 1.0) * 1000000
            if abs(soundcard_ppm) > 50:
                self.log(f"  Sound card clock: {soundcard_ppm:+.0f} ppm (compensated)")

            self.log(f"  Skips: {summary['frame_skips']}, Dups: {summary['duplicates']}, Errors: {summary['errors']}")
            self.log(f"  Log saved: {self.log_file_path} ({len(self.log_buffer)} frames)")
            self.log("=" * 40)

        except Exception as e:
            self.log(f"ERROR writing summary: {e}")

    def load_settings(self):
        """Load saved settings from file."""
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)

                # Restore audio device selection
                saved_device = settings.get('audio_device', '')
                if saved_device:
                    # Find matching device in combo box
                    for i, device in enumerate(self.device_combo['values']):
                        if device == saved_device:
                            self.device_combo.current(i)
                            self.on_device_changed(None)
                            break

                # Restore channel selection
                saved_channel = settings.get('audio_channel', '1')
                if saved_channel in self.channel_combo['values']:
                    self.channel_var.set(saved_channel)

                # Restore sample rate
                saved_sample_rate = settings.get('sample_rate', '48000')
                if saved_sample_rate in self.sample_rate_combo['values']:
                    self.sample_rate_var.set(saved_sample_rate)

                # Restore frame rate
                saved_fps = settings.get('frame_rate', 'Auto')
                if saved_fps in self.fps_combo['values']:
                    self.fps_var.set(saved_fps)

                # Restore file analysis settings
                if FILE_ANALYSIS_AVAILABLE:
                    saved_file_channel = settings.get('file_channel', 'Auto-detect')
                    if saved_file_channel in self.file_channel_combo['values']:
                        self.file_channel_var.set(saved_file_channel)

                    saved_file_fps = settings.get('file_fps', 'Auto')
                    if saved_file_fps in self.file_fps_combo['values']:
                        self.file_fps_var.set(saved_file_fps)

                    self.save_report_var.set(settings.get('save_report', True))

                    saved_report_format = settings.get('report_format', 'Both')
                    if saved_report_format in self.report_format_combo['values']:
                        self.report_format_var.set(saved_report_format)

                    saved_parallel_workers = settings.get('parallel_workers', '4')
                    if saved_parallel_workers in self.parallel_workers_combo['values']:
                        self.parallel_workers_var.set(saved_parallel_workers)

                # Restore comparator settings
                self.threshold_var.set(settings.get('comp_threshold', 3))
                self.comp_sample_rate_var.set(settings.get('comp_sample_rate', '48000'))

                # Restore FPS setting
                saved_fps = settings.get('comp_fps', 'Auto')
                if saved_fps in self.comp_fps_combo['values']:
                    self.comp_fps_var.set(saved_fps)

                # Restore interface mode
                saved_interface_mode = settings.get('comp_interface_mode', 'Same Interface')
                if saved_interface_mode in self.comp_interface_mode_combo['values']:
                    self.comp_interface_mode_var.set(saved_interface_mode)

                # Restore source configurations
                comp_sources = settings.get('comp_sources', [])
                for idx, source_config in enumerate(comp_sources):
                    if idx < len(self.comp_source_widgets):
                        source = self.comp_source_widgets[idx]

                        # Restore device
                        saved_device = source_config.get('device', '')
                        if saved_device:
                            for i, device in enumerate(source['device_combo']['values']):
                                if device == saved_device:
                                    source['device_combo'].current(i)
                                    self.on_comp_device_changed(idx)
                                    break

                        # Restore channel
                        saved_channel = source_config.get('channel', '1')
                        if saved_channel in source['channel_combo']['values']:
                            source['channel_var'].set(saved_channel)

        except Exception as e:
            # Silently ignore settings load errors
            pass

    def save_settings(self):
        """Save current settings to file."""
        try:
            settings = {
                'audio_device': self.device_var.get(),
                'audio_channel': self.channel_var.get(),
                'sample_rate': self.sample_rate_var.get(),
                'frame_rate': self.fps_var.get(),
            }

            # File analysis settings
            if FILE_ANALYSIS_AVAILABLE:
                settings['file_channel'] = self.file_channel_var.get()
                settings['file_fps'] = self.file_fps_var.get()
                settings['save_report'] = self.save_report_var.get()
                settings['report_format'] = self.report_format_var.get()
                settings['parallel_workers'] = self.parallel_workers_var.get()

            # Comparator settings
            settings['comp_threshold'] = self.threshold_var.get()
            settings['comp_sample_rate'] = self.comp_sample_rate_var.get()
            settings['comp_fps'] = self.comp_fps_var.get()
            settings['comp_interface_mode'] = self.comp_interface_mode_var.get()

            # Save source configurations
            comp_sources = []
            for source in self.comp_source_widgets:
                comp_sources.append({
                    'device': source['device_var'].get(),
                    'channel': source['channel_var'].get()
                })
            settings['comp_sources'] = comp_sources

            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)

        except Exception as e:
            # Silently ignore settings save errors
            pass

    def on_closing(self):
        """Handle window close."""
        self.save_settings()  # Save settings before closing
        self.stop_capture()
        if self.comparator_running:
            self.stop_comparator()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = LTCAnalyzerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
