"""
Report Generator - Generates analysis reports in text and CSV formats.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from file_analyzer import AnalysisResult, IssueRecord


class ReportGenerator:
    """Generates analysis reports in various formats."""

    @staticmethod
    def get_default_report_path(input_file: str, format: str = 'txt') -> str:
        """Generate default report filename based on input file.

        Args:
            input_file: Path to the analyzed file
            format: Report format ('txt' or 'csv')

        Returns:
            Default report path
        """
        base = Path(input_file).stem
        directory = Path(input_file).parent
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return str(directory / f"{base}_ltc_report_{timestamp}.{format}")

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format duration in HH:MM:SS.mmm format.

        Args:
            seconds: Duration in seconds

        Returns:
            Formatted duration string
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        """Format timestamp in HH:MM:SS.mmm format.

        Args:
            seconds: Timestamp in seconds

        Returns:
            Formatted timestamp string
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

    def generate_text_report(self, result: AnalysisResult, output_path: str) -> None:
        """Generate detailed text report.

        Args:
            result: Analysis result to report
            output_path: Path to save the report
        """
        lines = []

        # Header
        lines.append("=" * 80)
        lines.append("LTC TIMECODE ANALYSIS REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"File: {result.file_path}")
        lines.append("")

        # File Information
        lines.append("FILE INFORMATION")
        lines.append("-" * 80)
        lines.append(f"File Type:          {result.file_type.upper()}")
        lines.append(f"Duration:           {self.format_duration(result.duration_seconds)} ({result.duration_seconds:.3f} seconds)")
        lines.append(f"Audio Sample Rate:  {result.sample_rate} Hz")
        lines.append(f"Audio Channels:     {result.audio_channels}")
        lines.append(f"LTC Channel:        {result.ltc_channel + 1} ({result.channel_detection_method})")
        lines.append("")

        # Timecode Information
        lines.append("TIMECODE INFORMATION")
        lines.append("-" * 80)
        lines.append(f"First Timecode:     {result.first_timecode}")
        lines.append(f"Last Timecode:      {result.last_timecode}")
        lines.append(f"Detected FPS:       {result.detected_fps}")
        lines.append(f"Total Frames:       {result.total_frames:,}")
        lines.append("")

        # Analysis Results Summary
        lines.append("ANALYSIS RESULTS")
        lines.append("-" * 80)
        total_issues = result.skip_count + result.duplicate_count + result.error_count
        clean_frames = result.total_frames - total_issues
        lines.append(f"Clean Frames:       {clean_frames:,} ({result.clean_percentage:.2f}%)")
        lines.append(f"Issues Found:       {total_issues}")
        lines.append(f"  - Frame Skips:    {result.skip_count} ({result.skipped_frames_total} frames total)")
        lines.append(f"  - Duplicates:     {result.duplicate_count}")
        lines.append(f"  - Errors:         {result.error_count}")
        lines.append("")

        # Issues Detail
        if result.issues:
            lines.append("ISSUES DETAIL")
            lines.append("-" * 80)
            lines.append(f"{'#':<5} {'Position':<16} {'Timecode':<14} {'Type':<12} {'Description'}")
            lines.append("-" * 80)

            for i, issue in enumerate(result.issues, 1):
                pos = self.format_timestamp(issue.timestamp_seconds)
                lines.append(f"{i:<5} {pos:<16} {issue.timecode:<14} {issue.issue_type:<12} {issue.description}")

            lines.append("")

        # Summary and Rating
        lines.append("SUMMARY")
        lines.append("-" * 80)

        # Quality rating
        if result.total_frames == 0:
            rating = "NO DATA"
            recommendation = "No timecode frames were detected in this file."
        elif result.clean_percentage >= 99.99:
            rating = "EXCELLENT"
            recommendation = "File is suitable for broadcast use. No timecode issues detected."
        elif result.clean_percentage >= 99.9:
            rating = "VERY GOOD"
            recommendation = "File has minor timecode issues but is generally suitable for professional use."
        elif result.clean_percentage >= 99.0:
            rating = "GOOD"
            recommendation = "File has some timecode issues. Review issues before use in critical applications."
        elif result.clean_percentage >= 95.0:
            rating = "ACCEPTABLE"
            recommendation = "File has noticeable timecode issues. May cause sync problems in long-form content."
        elif result.clean_percentage >= 90.0:
            rating = "POOR"
            recommendation = "File has significant timecode issues. Consider re-recording or repairing timecode."
        else:
            rating = "BAD"
            recommendation = "File has severe timecode issues. Not recommended for professional use."

        lines.append(f"Quality Rating:     {rating} ({result.clean_percentage:.2f}% clean)")
        lines.append(f"Recommendation:     {recommendation}")
        lines.append("")

        # Footer
        lines.append("=" * 80)
        lines.append("Generated by LTC Timecode Analyzer")
        lines.append("=" * 80)

        # Write to file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

    def generate_csv_report(self, result: AnalysisResult, output_path: str,
                            include_all_frames: bool = False) -> None:
        """Generate CSV report with issues.

        Args:
            result: Analysis result to report
            output_path: Path to save the report
            include_all_frames: If True, include all frames (not just issues)
        """
        lines = []

        # Header
        lines.append("Position_Seconds,Position_TC,Timecode,Status,Issue_Type,Description,Frame_Number,Skipped_Count")

        # Write issues
        for issue in result.issues:
            pos_tc = self.format_timestamp(issue.timestamp_seconds)
            # Escape description for CSV
            desc = issue.description.replace('"', '""')
            if ',' in desc:
                desc = f'"{desc}"'

            line = f"{issue.timestamp_seconds:.3f},{pos_tc},{issue.timecode},{issue.issue_type},{issue.issue_type},{desc},{issue.frame_number},{issue.skipped_count}"
            lines.append(line)

        # Write to file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

    def format_summary(self, result: AnalysisResult) -> str:
        """Format a brief summary for GUI display.

        Args:
            result: Analysis result

        Returns:
            Formatted summary string
        """
        lines = []

        lines.append(f"File: {os.path.basename(result.file_path)}")
        lines.append(f"Duration: {self.format_duration(result.duration_seconds)}")
        lines.append(f"Total Frames: {result.total_frames:,}")
        lines.append(f"FPS: {result.detected_fps}")
        lines.append(f"LTC Channel: {result.ltc_channel + 1} ({result.channel_detection_method})")
        lines.append("")
        lines.append(f"Timecode Range: {result.first_timecode} - {result.last_timecode}")
        lines.append("")

        total_issues = result.skip_count + result.duplicate_count + result.error_count

        if total_issues == 0:
            lines.append("STATUS: CLEAN - No issues found!")
        else:
            lines.append(f"ISSUES FOUND: {total_issues}")
            if result.skip_count > 0:
                lines.append(f"  Frame Skips: {result.skip_count} ({result.skipped_frames_total} frames)")
            if result.duplicate_count > 0:
                lines.append(f"  Duplicates: {result.duplicate_count}")
            if result.error_count > 0:
                lines.append(f"  Errors: {result.error_count}")

        lines.append("")
        lines.append(f"Quality: {result.clean_percentage:.2f}% clean")

        return '\n'.join(lines)

    def format_issues_list(self, result: AnalysisResult, max_issues: int = 100) -> str:
        """Format list of issues for GUI display.

        Args:
            result: Analysis result
            max_issues: Maximum number of issues to display

        Returns:
            Formatted issues list
        """
        if not result.issues:
            return "No issues found - timecode is clean!"

        lines = []
        lines.append(f"{'Position':<12} {'Timecode':<14} {'Type':<10} {'Details'}")
        lines.append("-" * 60)

        for i, issue in enumerate(result.issues[:max_issues]):
            pos = self.format_timestamp(issue.timestamp_seconds)
            # Truncate position for display
            pos_short = pos[3:]  # Remove hours if they're 00
            lines.append(f"{pos_short:<12} {issue.timecode:<14} {issue.issue_type:<10} {issue.description}")

        if len(result.issues) > max_issues:
            lines.append(f"... and {len(result.issues) - max_issues} more issues")

        return '\n'.join(lines)
