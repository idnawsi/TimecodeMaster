"""
LTC Timecode Sync Application
Timecode synchronization tool for multi-camera and separate-sound footage.

Features:
- Drag & drop media files
- Auto-detect LTC timecode from audio tracks
- Visual timeline showing clips aligned by timecode
- Multiple sync logics (TC Only, TC+Filename, TC+Date)
- Export XML for NLEs (Premiere Pro, DaVinci Resolve, Final Cut Pro)
- Mute LTC track / Use linked audio

Based on the LTC implementation from TimecodeMaster-OTA project.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time
import queue
import os
import json
import subprocess
import tempfile
import shutil
import wave
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from collections import deque, OrderedDict, Counter
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
import re
import math

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# Try to import tkinterdnd2 for drag-and-drop support
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

# Try to import PIL for image display
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Try to import sounddevice for audio playback
try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False

# Try to import OpenCV for fast video playback
try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

# Try to import VLC for hardware-accelerated video playback
try:
    import vlc
    VLC_AVAILABLE = True
except ImportError:
    VLC_AVAILABLE = False

# Global FFmpeg paths (will be set by find_ffmpeg)
FFMPEG_PATH = None
FFPROBE_PATH = None

def find_ffmpeg():
    """Search for FFmpeg in common installation locations (Windows, macOS, Linux)."""
    global FFMPEG_PATH, FFPROBE_PATH
    import sys

    # Determine platform and executable extension
    is_windows = os.name == 'nt'
    is_macos = sys.platform == 'darwin'
    is_linux = sys.platform.startswith('linux')

    exe_ext = '.exe' if is_windows else ''
    ffmpeg_name = f'ffmpeg{exe_ext}'
    ffprobe_name = f'ffprobe{exe_ext}'

    # Build platform-specific search paths
    search_paths = []

    if is_windows:
        # Windows paths
        search_paths = [
            # Tentacle Sync Tool (common for timecode users)
            r"C:\Program Files\Tentacle Sync\Tentacle Timecode Tool",
            # Standard FFmpeg installations
            r"C:\Program Files\ffmpeg\bin",
            r"C:\ffmpeg\bin",
            # WinGet package locations
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
            # Chocolatey
            r"C:\ProgramData\chocolatey\bin",
            # Scoop
            os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
            # ImageMagick (sometimes includes ffmpeg)
            r"C:\Program Files\ImageMagick-7.1.1-Q16-HDRI",
        ]
    elif is_macos:
        # macOS paths
        search_paths = [
            # Homebrew (Intel)
            '/usr/local/bin',
            # Homebrew (Apple Silicon)
            '/opt/homebrew/bin',
            # MacPorts
            '/opt/local/bin',
            # System
            '/usr/bin',
            # FFmpeg.app
            '/Applications/ffmpeg',
            os.path.expanduser('~/Applications/ffmpeg'),
        ]
    elif is_linux:
        # Linux paths
        search_paths = [
            # Standard system paths
            '/usr/bin',
            '/usr/local/bin',
            # Snap
            '/snap/bin',
            # Flatpak
            '/var/lib/flatpak/exports/bin',
            os.path.expanduser('~/.local/bin'),
            # AppImage location
            os.path.expanduser('~/Applications'),
        ]

    # Check if ffmpeg/ffprobe are already in PATH
    for exe_name in ['ffmpeg', 'ffprobe']:
        try:
            kwargs = {'capture_output': True, 'text': True}
            if is_windows:
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run([exe_name, '-version'], **kwargs)
            if result.returncode == 0:
                # Found in PATH
                if exe_name == 'ffmpeg':
                    FFMPEG_PATH = 'ffmpeg'
                else:
                    FFPROBE_PATH = 'ffprobe'
        except FileNotFoundError:
            pass

    # If both found in PATH, we're done
    if FFMPEG_PATH and FFPROBE_PATH:
        return True

    # Search in common paths
    for base_path in search_paths:
        if not os.path.exists(base_path):
            continue

        # Check directly in base path
        ffmpeg_exe = os.path.join(base_path, ffmpeg_name)
        ffprobe_exe = os.path.join(base_path, ffprobe_name)

        if os.path.isfile(ffmpeg_exe):
            FFMPEG_PATH = ffmpeg_exe
        if os.path.isfile(ffprobe_exe):
            FFPROBE_PATH = ffprobe_exe

        if FFMPEG_PATH and FFPROBE_PATH:
            return True

        # Search subdirectories (for WinGet packages on Windows)
        if is_windows and 'WinGet' in base_path:
            try:
                for item in os.listdir(base_path):
                    item_path = os.path.join(base_path, item)
                    if os.path.isdir(item_path) and 'ffmpeg' in item.lower():
                        # Search in this package directory
                        for root, dirs, files in os.walk(item_path):
                            if ffmpeg_name in files and not FFMPEG_PATH:
                                FFMPEG_PATH = os.path.join(root, ffmpeg_name)
                            if ffprobe_name in files and not FFPROBE_PATH:
                                FFPROBE_PATH = os.path.join(root, ffprobe_name)
                            if FFMPEG_PATH and FFPROBE_PATH:
                                return True
            except Exception:
                pass

    # Try to use ffmpeg without ffprobe if only one is found
    if FFMPEG_PATH and not FFPROBE_PATH:
        # ffmpeg can do most of what ffprobe does
        FFPROBE_PATH = FFMPEG_PATH
        return True

    return FFMPEG_PATH is not None or FFPROBE_PATH is not None

def check_ffmpeg():
    """Check if FFmpeg is available (searches common paths on first call)."""

    # If not yet searched, find FFmpeg
    if FFMPEG_PATH is None and FFPROBE_PATH is None:
        find_ffmpeg()

    if not FFPROBE_PATH and not FFMPEG_PATH:
        return False

    try:
        probe_cmd = FFPROBE_PATH if FFPROBE_PATH else FFMPEG_PATH
        kwargs = {'capture_output': True, 'text': True}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run([probe_cmd, '-version'], **kwargs)
        return result.returncode == 0
    except FileNotFoundError:
        return False

FFMPEG_AVAILABLE = check_ffmpeg()


# =============================================================================
# GPU video decoding - which GPUs exist, which actually decode, which is used
# =============================================================================
#
# Video decode device (toolbar "Decode (GPU/CPU)" + Settings). Measured on an
# RTX 3080 + Intel UHD 730, 4K HEVC 10-bit: GPU decode ~2-3x faster than CPU
# (frames kept on the GPU); single-frame thumbnails tie; OpenCV frame grabs
# are FASTER on the CPU (each frame must be copied back to RAM). So:
#   Auto - GPU for VLC + FFmpeg thumbnails, CPU for OpenCV grabs
#   GPU  - GPU everywhere it can be used
#   CPU  - no GPU decoding at all
# DECODE_GPU picks WHICH GPU ("Auto" = best working one). Sync/LTC analysis
# reads audio only - there is no video decode to offload.
#
# Dependencies: none beyond the GPU's normal display driver. On Windows every
# vendor (NVIDIA, AMD, Intel) can decode through D3D11VA, built into Windows
# and into standard FFmpeg builds; `-hwaccel_device N` is the DXGI adapter
# index, the same numbering this module enumerates. NVIDIA cards use CUDA
# instead when it works (single 4K frame: 411ms CUDA vs 590ms D3D11VA) - CUDA
# devices are numbered in PCI order (CUDA_DEVICE_ORDER below, same as
# nvidia-smi) and matched to the DXGI adapter by name. The NVIDIA driver
# includes CUDA decoding; no CUDA toolkit is needed. VLC 3 has no adapter
# option: it uses the GPU Windows assigns to the app (Settings > System >
# Display > Graphics).
DECODE_DEVICES = ("Auto", "GPU", "CPU")
DECODE_DEVICE = "Auto"
DECODE_GPU = "Auto"  # "Auto" or a GPU name from list_gpus()

_GPU_LIST = None
_GPU_LOCK = threading.Lock()
# FFmpeg child processes inherit this: CUDA device N == nvidia-smi index N
os.environ.setdefault('CUDA_DEVICE_ORDER', 'PCI_BUS_ID')
_GPU_VENDORS = {0x10DE: 'NVIDIA', 0x1002: 'AMD', 0x1022: 'AMD', 0x8086: 'Intel'}


def _enumerate_dxgi_adapters() -> List[dict]:
    """Hardware GPUs in DXGI order (= FFmpeg d3d11va / MSMF device index)."""
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

    class DESC1(ctypes.Structure):
        _fields_ = [('Description', wintypes.WCHAR * 128), ('VendorId', wintypes.UINT),
                    ('DeviceId', wintypes.UINT), ('SubSysId', wintypes.UINT), ('Revision', wintypes.UINT),
                    ('DedicatedVideoMemory', ctypes.c_size_t), ('DedicatedSystemMemory', ctypes.c_size_t),
                    ('SharedSystemMemory', ctypes.c_size_t), ('AdapterLuid', LUID), ('Flags', wintypes.UINT)]

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                    ('Data4', ctypes.c_ubyte * 8)]

    def method(obj, slot, *argtypes):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[slot])

    iid_factory1 = GUID(0x770aae78, 0xf26f, 0x4dba,
                        (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
    factory = ctypes.c_void_p()
    if ctypes.WinDLL('dxgi').CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0:
        return []
    adapters = []
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 (vtable slot 12)
            if method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                    factory, index, ctypes.byref(adapter)) != 0:
                break
            desc = DESC1()
            # IDXGIAdapter1::GetDesc1 (slot 10); Release (slot 2)
            method(adapter, 10, ctypes.POINTER(DESC1))(adapter, ctypes.byref(desc))
            method(adapter, 2)(adapter)
            if not desc.Flags & 2:  # skip DXGI_ADAPTER_FLAG_SOFTWARE (Basic Render Driver)
                adapters.append({'index': index, 'name': desc.Description.strip(),
                                 'vendor': _GPU_VENDORS.get(desc.VendorId, hex(desc.VendorId)),
                                 'vram_mb': desc.DedicatedVideoMemory // 2 ** 20})
            index += 1
    finally:
        method(factory, 2)(factory)
    return adapters


def _gpu_probe_file() -> Optional[str]:
    """Tiny H.264 clip for real decode tests (made once per run)."""
    path = os.path.join(tempfile.gettempdir(), 'ltc_sync_gpu_probe.mp4')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    kwargs = {'capture_output': True, 'timeout': 20}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    for codec in (['-c:v', 'libx264', '-pix_fmt', 'yuv420p'], ['-c:v', 'mpeg2video']):
        try:
            r = subprocess.run([FFMPEG_PATH, '-y', '-v', 'error', '-f', 'lavfi',
                                '-i', 'testsrc2=s=256x144:r=25:d=0.4', *codec, path], **kwargs)
            if r.returncode == 0:
                return path
        except Exception:
            pass
    return None


def _gpu_decodes(method: str, device: Optional[str], out_format: str, probe: str) -> bool:
    """True only if FFmpeg really decodes on this GPU. Frames are forced to
    stay on the GPU and then downloaded - if the hwaccel fails FFmpeg falls
    back to the CPU, the download has nothing to download and the run fails.
    (The old test decoded `nullsrc`, which never touches a decoder, so every
    method "passed" - an AMD/Intel PC would have picked cuda.)"""
    cmd = [FFMPEG_PATH, '-v', 'error', '-hwaccel', method]
    if device is not None:
        cmd += ['-hwaccel_device', str(device)]
    cmd += ['-hwaccel_output_format', out_format, '-i', probe,
            '-vf', 'hwdownload,format=nv12', '-f', 'null', '-']
    kwargs = {'capture_output': True, 'timeout': 20}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, **kwargs).returncode == 0
    except Exception:
        return False


def _nvidia_smi_names() -> List[str]:
    """NVIDIA GPU names in nvidia-smi (= PCI = our CUDA) order, or []."""
    kwargs = {'capture_output': True, 'text': True, 'timeout': 10}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], **kwargs)
        return [line.strip() for line in r.stdout.splitlines() if line.strip()] if r.returncode == 0 else []
    except Exception:
        return []


def list_gpus(refresh: bool = False) -> List[dict]:
    """Installed GPUs, each tested for real video decoding (cached).

    Each entry: name, vendor, vram_mb, method/device (FFmpeg -hwaccel and
    -hwaccel_device), index (DXGI adapter, Windows), ok (decode test passed).
    Takes ~0.2s per GPU the first time; call from a worker thread."""
    global _GPU_LIST
    with _GPU_LOCK:
        if _GPU_LIST is not None and not refresh:
            return _GPU_LIST
        gpus = []
        probe = _gpu_probe_file() if FFMPEG_AVAILABLE else None
        if os.name == 'nt':
            try:
                adapters = _enumerate_dxgi_adapters()
                cuda_names = _nvidia_smi_names()
                if not cuda_names and sum(a['vendor'] == 'NVIDIA' for a in adapters) == 1:
                    cuda_names = [next(a['name'] for a in adapters if a['vendor'] == 'NVIDIA')]
                taken = set()
                for a in adapters:
                    a.update(method='d3d11va', device=str(a['index']), ok=False)
                    if a['vendor'] == 'NVIDIA' and probe:
                        cuda_idx = next((i for i, n in enumerate(cuda_names)
                                         if i not in taken and n == a['name']), None)
                        if cuda_idx is not None and _gpu_decodes('cuda', str(cuda_idx), 'cuda', probe):
                            taken.add(cuda_idx)
                            a.update(method='cuda', device=str(cuda_idx), ok=True)
                    if not a['ok']:
                        a['ok'] = bool(probe) and _gpu_decodes('d3d11va', a['device'], 'd3d11', probe)
                    gpus.append(a)
            except Exception:
                gpus = []
        else:
            # Linux / macOS: no adapter list - offer whichever API decodes
            for method, device, fmt, label in (('cuda', None, 'cuda', 'NVIDIA (CUDA)'),
                                               ('vaapi', '/dev/dri/renderD128', 'vaapi', 'VA-API GPU'),
                                               ('videotoolbox', None, 'videotoolbox_vld', 'Apple VideoToolbox')):
                if probe and _gpu_decodes(method, device, fmt, probe):
                    gpus.append({'index': len(gpus), 'name': label, 'vendor': '', 'vram_mb': 0,
                                 'method': method, 'device': device, 'ok': True})
        _GPU_LIST = gpus
        return gpus


def gpu_label(gpu: dict) -> str:
    """'NVIDIA GeForce RTX 3080 - 10 GB' for menus."""
    vram = f"{gpu['vram_mb'] / 1024:.0f} GB" if gpu.get('vram_mb', 0) >= 1024 else "shared memory"
    return f"{gpu['name']} - {vram}" + ("" if gpu.get('ok') else " (can't decode video)")


def selected_gpu(ignore_cpu_mode: bool = False) -> Optional[dict]:
    """The GPU decoding should use, or None for the CPU.

    DECODE_GPU names one; "Auto" (or a GPU that is gone / failed its test)
    picks the best working one: NVIDIA, then AMD, then other dedicated cards,
    then Intel integrated - larger dedicated memory first within a vendor."""
    if DECODE_DEVICE == "CPU" and not ignore_cpu_mode:
        return None
    working = [g for g in list_gpus() if g.get('ok')]
    if not working:
        return None
    if DECODE_GPU != "Auto":
        for g in working:
            if g['name'] == DECODE_GPU:
                return g
    rank = {'NVIDIA': 0, 'AMD': 1, 'Intel': 3}
    return min(working, key=lambda g: (rank.get(g['vendor'], 2), -g.get('vram_mb', 0)))


# Legacy names still read by the About dialog / status text
HW_ACCEL = None
_HW_ACCEL_CHECKED = False


def get_hw_accel():
    """FFmpeg -hwaccel method of the selected GPU (detected on first use)."""
    global HW_ACCEL, _HW_ACCEL_CHECKED
    gpu = selected_gpu(ignore_cpu_mode=True) if FFMPEG_AVAILABLE else None
    HW_ACCEL = gpu['method'] if gpu else None
    _HW_ACCEL_CHECKED = True
    return HW_ACCEL


def ffmpeg_frame_hwaccel() -> Optional[dict]:
    """GPU for FFmpeg frame grabs, or None to decode on the CPU."""
    get_hw_accel()  # keeps HW_ACCEL / _HW_ACCEL_CHECKED current
    return selected_gpu()


def ffmpeg_frame_grab_args(gpu: Optional[dict], scale_w: str, scale_h: str) -> Tuple[List[str], List[str]]:
    """(input args, output filter args) for a scaled FFmpeg frame grab.

    CUDA keeps frames on the GPU and scales there; the download must be to
    nv12 (the old 'format=nv12|yuv420p' failed every time - "Invalid output
    format yuv420p for hwframe download" - so each grab silently ran twice).
    D3D11VA / VA-API / VideoToolbox hand the single frame back to system
    memory, so the plain software scaler works."""
    if not gpu:
        return ([], ['-vf', f'scale={scale_w}:{scale_h}'])
    hw_in = ['-hwaccel', gpu['method']]
    if gpu.get('device') is not None:
        hw_in += ['-hwaccel_device', str(gpu['device'])]
    if gpu['method'] == 'cuda':
        return (hw_in + ['-hwaccel_output_format', 'cuda'],
                ['-vf', f'scale_cuda={scale_w}:{scale_h}:format=nv12,hwdownload,format=nv12'])
    return (hw_in, ['-vf', f'scale={scale_w}:{scale_h}'])


def open_video_capture(path: str):
    """cv2.VideoCapture for PREVIEW of `path` (its proxy when one is ready),
    honouring DECODE_DEVICE / DECODE_GPU.

    FFmpeg backend, not Media Foundation: on 4K HEVC 4:2:2 10-bit MSMF took
    4.8-8.2s per seek+read (on the UI thread = the multi-second black frame
    after releasing a scrub); the FFmpeg backend 0.6-1.2s. GPU only when
    forced - OpenCV grabs measured faster on the CPU."""
    source = preview_source(path)
    params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE]
    if DECODE_DEVICE == "GPU" and os.name == 'nt':
        gpu = selected_gpu()
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_D3D11]
        if gpu:
            # OpenCV numbers GPUs by DXGI adapter, whatever FFmpeg uses
            params += [cv2.CAP_PROP_HW_DEVICE, int(gpu['index'])]
    try:
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
    except (TypeError, AttributeError, cv2.error):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)  # OpenCV < 4.5.2
    if cap.isOpened():
        return cap
    cap.release()
    return cv2.VideoCapture(source)



# =============================================================================
# Preview proxies - small copies of heavy footage for smooth preview
# =============================================================================
#
# 4K HEVC 4:2:2 10-bit (Canon R6 II / R8 and many mirrorless cameras) can't be
# decoded by NVIDIA GPUs before the RTX 50 series, nor by VLC 3 on any GPU, so
# preview fell back to the CPU: VLC played at ~22fps (< 25fps real time), took
# ~1.5s to show a clip, and OpenCV's MSMF backend needed 5-8s per frame. A
# 960px H.264 4:2:0 proxy with a keyframe every 12 frames plays and seeks
# instantly everywhere. Proxies are used ONLY for preview / playback /
# scrubbing / thumbnails; analysis, audio and every export use the originals.
# Frame timing is identical (checked: same frame count, every pts equal).
PREVIEW_PROXY_MODES = ("Auto", "Always", "Off")
PREVIEW_PROXY_MODE = "Auto"  # Auto = only footage that needs it (see _needs_proxy)
PROXY_WIDTH = 960


class ProxyManager:
    """Builds proxies one at a time in the background and maps original paths
    to finished proxies. Thread-safe; never touches Tk (progress goes through
    the `notify` callback, which must be thread-safe - the app's queue)."""

    def __init__(self):
        self.directory = None
        self.notify = None  # callable(message_tuple)
        self._lock = threading.RLock()  # re-entrant: _run calls get() while holding it
        self._queue = deque()
        self._queued = set()
        self._ready = {}      # original path -> proxy path
        self._skip = set()    # originals that don't need / couldn't get a proxy
        self._worker = None
        self._route_cache = {}  # (codec, pix_fmt) -> index of the route that worked
        self.done = 0
        self.total = 0
        self.current = None

    def set_directory(self, directory: str):
        with self._lock:
            self.directory = directory
            self._ready.clear()
            self._skip.clear()

    def _proxy_path(self, path: str) -> Optional[str]:
        if not self.directory:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        import hashlib
        key = hashlib.sha1(f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}"
                           .encode('utf-8')).hexdigest()[:20]
        return os.path.join(self.directory, f"{key}.mp4")

    def get(self, path: str) -> Optional[str]:
        """Finished proxy for `path`, or None (not needed / not built yet / Off)."""
        if PREVIEW_PROXY_MODE == "Off" or not path:
            return None
        with self._lock:
            hit = self._ready.get(path)
            if hit and os.path.exists(hit):
                return hit
        candidate = self._proxy_path(path)
        if candidate and os.path.exists(candidate):
            with self._lock:
                self._ready[path] = candidate
            return candidate
        return None

    def pending(self) -> int:
        with self._lock:
            return len(self._queue) + (1 if self.current else 0)

    def enqueue(self, paths):
        """Queue originals (first = built first). Already-built ones are skipped."""
        if PREVIEW_PROXY_MODE == "Off" or not FFMPEG_AVAILABLE:
            return
        with self._lock:
            if not self.directory:
                return
            for path in paths:
                if path in self._queued or path in self._skip or path in self._ready or path == self.current:
                    continue
                self._queue.append(path)
                self._queued.add(path)
                self.total += 1
            if self._queue and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._run, name='proxy-builder', daemon=True)
                self._worker.start()

    def cancel_all(self):
        with self._lock:
            self._queue.clear()
            self._queued.clear()
            self.total = self.done

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    self.current = None
                    self.done = self.total = 0
                    return
                path = self._queue.popleft()
                self._queued.discard(path)
                self.current = path
            built = None
            try:
                if not self.get(path):
                    built = self._build(path)
            except Exception:
                built = None
            with self._lock:
                self.done += 1
                if built:
                    self._ready[path] = built
                elif not self.get(path):
                    self._skip.add(path)
                done, total = self.done, self.total
            if self.notify:
                try:
                    self.notify(('proxy', (done, total, os.path.basename(path), path, built)))
                except Exception:
                    pass

    @staticmethod
    def _probe(path: str) -> Optional[dict]:
        kwargs = {'capture_output': True, 'text': True, 'timeout': 30}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run([FFPROBE_PATH, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                'stream=codec_name,width,height,pix_fmt:format=duration',
                                '-of', 'json', path], **kwargs)
            info = json.loads(r.stdout or '{}')
            stream = (info.get('streams') or [None])[0]
            if not stream:
                return None
            stream['duration'] = float(info.get('format', {}).get('duration') or 0)
            return stream
        except Exception:
            return None

    @staticmethod
    def _needs_proxy(info: dict) -> bool:
        if PREVIEW_PROXY_MODE == "Always":
            return info.get('width', 0) > PROXY_WIDTH
        pix = info.get('pix_fmt', '')
        heavy_format = pix not in ('yuv420p', 'yuvj420p', 'nv12')  # 4:2:2, 4:4:4, 10-bit...
        large = info.get('width', 0) * info.get('height', 0) > 1920 * 1088
        return heavy_format or large

    def _routes(self, src: str, dst: str, w: int, h: int) -> List[List[str]]:
        """FFmpeg commands, fastest first; a route that can't run fails fast
        (frames are forced onto the GPU, so no silent CPU fallback)."""
        common_out = ['-g', '12', '-bf', '0', '-b:v', '4M', '-map', '0:v:0', '-map', '0:a:0?',
                      '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', '-f', 'mp4', dst]
        base = [FFMPEG_PATH, '-v', 'error', '-y']
        routes = []
        if DECODE_DEVICE != "CPU":
            # Intel Quick Sync end to end - the only HW path for HEVC 4:2:2 here
            routes.append(base + ['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv', '-i', src,
                                  '-vf', f'vpp_qsv=w={w}:h={h}:format=nv12', '-c:v', 'h264_qsv',
                                  '-preset', 'veryfast'] + common_out)
            # NVIDIA end to end (4:2:0 footage)
            routes.append(base + ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda', '-i', src,
                                  '-vf', f'scale_cuda={w}:{h}:format=nv12', '-c:v', 'h264_nvenc',
                                  '-preset', 'p1'] + common_out)
            # CPU decode, GPU encode
            for enc in ('h264_nvenc', 'h264_qsv', 'h264_amf'):
                routes.append(base + ['-i', src, '-vf', f'scale={w}:{h},format=nv12', '-c:v', enc] + common_out)
        routes.append(base + ['-i', src, '-vf', f'scale={w}:{h},format=yuv420p', '-c:v', 'libx264',
                              '-preset', 'ultrafast'] + common_out)
        return routes

    def _build(self, path: str) -> Optional[str]:
        dst = self._proxy_path(path)
        info = self._probe(path)
        if not dst or not info or not self._needs_proxy(info):
            return None
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        w = PROXY_WIDTH
        h = max(2, int(round(PROXY_WIDTH * info['height'] / max(1, info['width']) / 2)) * 2)
        part = dst[:-4] + '.part.mp4'
        kwargs = {'capture_output': True, 'timeout': max(120, info.get('duration', 0) * 4 + 60)}
        if os.name == 'nt':
            # below-normal priority keeps the UI and playback smooth meanwhile
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | 0x00004000
        routes = self._routes(path, part, w, h)
        sig = (info.get('codec_name'), info.get('pix_fmt'))
        order = list(range(len(routes)))
        if sig in self._route_cache:
            order.remove(self._route_cache[sig])
            order.insert(0, self._route_cache[sig])
        for i in order:
            try:
                r = subprocess.run(routes[i], **kwargs)
            except Exception:
                continue
            if r.returncode == 0 and os.path.exists(part) and os.path.getsize(part) > 0:
                self._route_cache[sig] = i
                os.replace(part, dst)  # atomic: a half-written proxy is never used
                return dst
        try:
            os.remove(part)
        except OSError:
            pass
        return None

    def size_bytes(self) -> int:
        if not self.directory or not os.path.isdir(self.directory):
            return 0
        return sum(os.path.getsize(os.path.join(self.directory, f))
                   for f in os.listdir(self.directory) if f.endswith('.mp4'))

    def clear(self) -> int:
        """Delete all finished proxies (not one being written). Returns count."""
        self.cancel_all()
        removed = 0
        if self.directory and os.path.isdir(self.directory):
            for f in os.listdir(self.directory):
                if f.endswith('.mp4') and not f.endswith('.part.mp4'):
                    try:
                        os.remove(os.path.join(self.directory, f))
                        removed += 1
                    except OSError:
                        pass
        with self._lock:
            self._ready.clear()
            self._skip.clear()
        return removed


PROXIES = ProxyManager()


TEMP_PREFIX = 'ltc_sync_'


def _pid_alive(pid: int) -> bool:
    """Is a process with this id running? (Windows; elsewhere: os.kill 0)"""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _other_app_instances_running() -> bool:
    """Is another copy of this app running? (True if it can't be told.)"""
    if os.name != 'nt':
        return True
    try:
        kwargs = {'capture_output': True, 'text': True, 'timeout': 15,
                  'creationflags': subprocess.CREATE_NO_WINDOW}
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "Get-CimInstance Win32_Process -Filter \"Name like 'python%' or Name like 'LTC%'\" | "
                            "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"], **kwargs)
        for line in r.stdout.splitlines():
            pid, _, cmd = line.partition('|')
            if pid.strip().isdigit() and int(pid) != os.getpid() and (
                    'ltc_sync_app' in cmd.lower() or 'ltc sync' in cmd.lower()):
                return True
        return r.returncode != 0
    except Exception:
        return True


def cleanup_stale_temp_dirs(roots, idle_minutes: int = 60) -> Tuple[int, int]:
    """Delete leftover 'ltc_sync_*' temp folders (from crashed / killed runs).

    New folders are named ltc_sync_<pid>_... and are kept while that process
    runs. Older folders (no pid in the name) are removed only if nothing in
    them changed for `idle_minutes` - never one another open copy is using.
    Returns (folders removed, bytes freed)."""
    removed = freed = 0
    now = time.time()
    # old-style folders carry no pid: only touch them when no other copy of
    # the app is running (it could be using one of them)
    legacy_ok = not _other_app_instances_running()
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(TEMP_PREFIX):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            parts = name[len(TEMP_PREFIX):].split('_', 1)
            if parts[0].isdigit():
                pid = int(parts[0])
                if pid == os.getpid() or _pid_alive(pid):
                    continue
            else:
                if not legacy_ok:
                    continue
                newest, size = 0.0, 0
                for dirpath, _, files in os.walk(path):
                    for f in files:
                        try:
                            st = os.stat(os.path.join(dirpath, f))
                            newest = max(newest, st.st_mtime)
                            size += st.st_size
                        except OSError:
                            pass
                newest = max(newest, os.path.getmtime(path))
                if now - newest < idle_minutes * 60:
                    continue
            size = 0
            for dirpath, _, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
                freed += size
    return removed, freed


def _suspend_process(proc, suspend: bool) -> bool:
    """Pause/resume a child process (Windows NtSuspendProcess/NtResumeProcess).
    Returns False where unsupported, so callers can fall back."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        fn = ctypes.windll.ntdll.NtSuspendProcess if suspend else ctypes.windll.ntdll.NtResumeProcess
        fn.argtypes = [ctypes.c_void_p]
        return fn(int(proc._handle)) == 0
    except Exception:
        return False


def preview_source(path: str) -> str:
    """File to decode for PREVIEW of `path`: its proxy when ready, else itself."""
    return PROXIES.get(path) or path



# =============================================================================
# Direct GPU playback - hardware decoding in-process (PyAV), no VLC, no proxy
# =============================================================================
#
# Plays original files on whichever GPU can decode them IN HARDWARE, tested per
# format with software fallback forbidden: e.g. 4K HEVC 4:2:2 10-bit (Canon)
# decodes only on Intel Quick Sync (hevc_qsv) - NVIDIA before RTX 50 and every
# D3D11VA route refuse it; ordinary 4:2:0 footage works on any GPU. Measured
# on that footage: exact-frame seek 0.1-0.23s, 86 fps decode+scale (VLC: ~7fps
# and 1.5s to first picture). Hybrid with proxies: a clip whose proxy is ready
# previews from the proxy (fastest scrubbing); otherwise from here. Files no
# GPU can decode in hardware are left to proxies/VLC (no CPU fallback here).
try:
    import av
    from av.codec.hwaccel import HWAccel
    AV_AVAILABLE = True
except Exception:
    AV_AVAILABLE = False

DIRECT_GPU_MODES = ("Auto", "Off")  # ...or the name of one GPU from list_gpus()
DIRECT_GPU_MODE = "Auto"
PREVIEW_W, PREVIEW_H = 480, 270
_QSV_DECODERS = {'h264': 'h264_qsv', 'hevc': 'hevc_qsv', 'av1': 'av1_qsv', 'vp9': 'vp9_qsv',
                 'mpeg2video': 'mpeg2_qsv', 'vc1': 'vc1_qsv'}


class _HwSession:
    """One open file + hardware decoder. Used by one thread at a time (the
    player thread, or the prepare thread before handing it over)."""

    def __init__(self, path: str, route: dict):
        self.path = path
        self.route = route
        if route['kind'] == 'qsv':
            # Intel's dedicated decoder: demux with PyAV, decode on Quick Sync
            self.container = av.open(path)
            self.stream = self.container.streams.video[0]
            # (A shared QSV device via hwaccel= saves ~200ms per new decoder,
            # but PyAV creates it holding the GIL: the whole UI froze ~1.25s
            # at play start. Cold starts are hidden by pre-warming instead.)
            self.decoder = av.CodecContext.create(route['decoder'], 'r')
            self.decoder.extradata = self.stream.codec_context.extradata
        else:
            self.container = av.open(path, hwaccel=HWAccel(device_type=route['type'], device=route['device'],
                                                            allow_software_fallback=False))
            self.stream = self.container.streams.video[0]
            self.decoder = self.stream.codec_context
        self.tb = float(self.stream.time_base)
        self.start = self.stream.start_time or 0
        rate = self.stream.average_rate or self.stream.guessed_rate
        self.frame_dur = 1.0 / float(rate) if rate else 0.04
        w, h = self.stream.width or 16, self.stream.height or 9
        scale = min(PREVIEW_W / w, PREVIEW_H / h)
        self.out_w = max(2, int(w * scale) // 2 * 2)
        self.out_h = max(2, int(h * scale) // 2 * 2)

    def close(self):
        try:
            self.container.close()
        except Exception:
            pass

    def frames_from(self, seconds: float):
        """(time in file, frame) from the frame shown at `seconds` onward."""
        qsv = self.route['kind'] == 'qsv'
        if not qsv:
            try:
                self.decoder.flush_buffers()
            except Exception:
                pass
        # QSV: NO drain/flush. hevc_qsv's flush leaks ~160 MB per call in this
        # FFmpeg build (30 seeks = +4.9 GB; gc doesn't help - it's native), and
        # every scrub/still/play request seeks. Frames still queued from the
        # old position are skipped EXACTLY instead: a frame belongs to this
        # seek only if its pts is one of the packets sent since the seek.
        # Safe because the decoder restarts at the keyframe we seek to (Canon
        # HEVC keyframes are IDR, NAL type 19 - no leading pictures need old
        # references). QSV can also drop the first frame(s) after such a jump,
        # so seek a few frames EARLIER: a target right after a keyframe is then
        # decoded mid-stream from the previous one.
        lead_in = self.frame_dur * 4 if qsv else 0.0
        target = self.start + int(max(0.0, seconds - lead_in) / self.tb)
        self.container.seek(target, stream=self.stream, backward=True)
        threshold = seconds - self.frame_dur / 2
        sent = set()
        last_t = None
        fallback = None  # latest frame of THIS seek before the target
        for packet in self.container.demux(self.stream):
            if packet.pts is not None:
                sent.add(packet.pts)
            if not packet.size:
                if qsv:
                    # end of file: sending the drain packet puts hevc_qsv in
                    # EOF state, and only the leaking flush gets it out again
                    break
                packet = None
            for frame in self.decoder.decode(packet):
                if frame.pts is None:
                    continue
                if qsv and frame.pts not in sent:
                    continue  # queued from before this seek
                t = (frame.pts - self.start) * self.tb
                if t < threshold:
                    fallback = (t, frame)
                    continue
                if last_t is not None and t <= last_t + 1e-6:
                    continue
                fallback = None
                last_t = t
                yield t, frame
        if last_t is None and fallback is not None:
            # target within the last few frames (still queued in the decoder
            # at end of file): show the latest frame we have instead
            yield fallback

    def rgb(self, frame):
        return frame.reformat(width=self.out_w, height=self.out_h, format='rgb24').to_ndarray()


class HardwarePlayer:
    """Background hardware decoder for the preview.

    show(path, t) asks for one exact frame; play(path, t) decodes ahead into a
    small queue. Requests are latest-wins. The UI thread only calls the public
    methods (all lock-protected) and converts finished RGB frames to images.
    """

    def __init__(self):
        # Two threads wait on this condition (player + prepare): always
        # notify_all - notify() woke just one of them at random, so a request
        # could sit unseen until the next one (Play/stills "sometimes" did nothing)
        self._cond = threading.Condition()
        self._request = None            # {'id', 'path', 't', 'mode'}
        self._next_id = 0
        self._still = None              # (id, path, t, rgb)
        self._play_frames = deque()     # (t, rgb) for the current play request
        self._play_id = None
        self._probe_queue = deque()
        self._routes = {}               # path -> route dict | None (no hardware)
        self._sig_routes = {}           # format signature -> route | None
        self._sessions = OrderedDict()  # path -> _HwSession (a few kept open)
        self._busy = False
        self._alive = True
        self._reset_sessions = False
        # Prepare-ahead: a second thread opens the NEXT clip (cold QSV start
        # ~0.8s) and decodes its first frame before the playhead gets there
        self._prep_request = None       # (path, t)
        self._prep_inflight = None      # path being prepared right now
        self._play_starting = False     # a play request has no frame yet
        self._prepared = {}             # path -> (t, session, generator, first (t, rgb) or None)
        self._thread = threading.Thread(target=self._run, name='hw-player', daemon=True)
        self._thread.start()
        self._prep_thread = threading.Thread(target=self._run_prepare, name='hw-prepare', daemon=True)
        self._prep_thread.start()

    # ---- public (UI thread) ----
    def route_for(self, path: str):
        """Hardware route for `path`, None if no GPU can decode it, or False
        if not tested yet (a background test is scheduled)."""
        with self._cond:
            if path in self._routes:
                return self._routes[path]
            if path not in self._probe_queue:
                self._probe_queue.append(path)
                self._cond.notify_all()
            return False

    def probe(self, paths):
        with self._cond:
            for path in paths:
                if path not in self._routes and path not in self._probe_queue:
                    self._probe_queue.append(path)
            self._cond.notify_all()

    def show(self, path: str, t: float) -> int:
        return self._submit(path, t, 'still')

    def play(self, path: str, t: float) -> int:
        return self._submit(path, t, 'play')

    def prepare(self, path: str, t: float):
        """Warm up `path` at `t` (opened + first frame decoded) for a play()
        that is about to come - e.g. the next clip on the timeline."""
        with self._cond:
            # one prepare per clip: queued, running or done (a duplicate used
            # to open a second Intel session and leak the first one)
            if (path in self._prepared or path == self._prep_inflight
                    or (self._prep_request and self._prep_request[0] == path)):
                return
            self._prep_request = (path, t)
            self._cond.notify_all()

    def is_warm(self, path: str) -> bool:
        """Is `path` already open (no cold Intel start needed)?"""
        with self._cond:
            return path in self._sessions or path in self._prepared

    def is_prepared(self, path: str, t: float) -> bool:
        """A warm session for `path` whose first frame is at/just before t?"""
        with self._cond:
            item = self._prepared.get(path)
            return item is not None and item[0] - 0.05 <= t <= item[0] + 1.0

    def stop(self):
        with self._cond:
            self._request = None
            self._play_frames.clear()
            self._play_id = None
            self._cond.notify_all()

    def take_still(self):
        with self._cond:
            item, self._still = self._still, None
            return item

    def take_play_frame(self, play_id: int, t: float):
        """Newest decoded frame at or before `t` (older ones are dropped).
        Returns (frame_time, rgb) or None, plus the earliest queued time."""
        with self._cond:
            if play_id != self._play_id:
                return None, None
            best = None
            while self._play_frames and self._play_frames[0][0] <= t + 0.005:
                best = self._play_frames.popleft()
            earliest = self._play_frames[0][0] if self._play_frames else None
            self._cond.notify_all()  # room in the queue
            return best, earliest

    def reset_routes(self):
        """Forget tested routes (the GPU choice changed) and close open files."""
        with self._cond:
            self._request = None
            self._play_frames.clear()
            self._play_id = None
            self._still = None
            self._routes.clear()
            self._sig_routes.clear()
            self._probe_queue.clear()
            self._prep_request = None
            self._reset_sessions = True
            self._cond.notify_all()

    def summary(self, paths) -> dict:
        """{gpu label: clip count} for tested files; 'not tested yet' and
        'no hardware decoder' buckets for the rest."""
        out = {}
        with self._cond:
            for path in paths:
                if path not in self._routes:
                    key = 'not tested yet'
                elif self._routes[path] is None:
                    who = 'no GPU' if DIRECT_GPU_MODE in DIRECT_GPU_MODES else DIRECT_GPU_MODE
                    key = f"{who} can't decode in hardware - proxies/VLC used"
                else:
                    r = self._routes[path]
                    api = 'Quick Sync' if r['kind'] == 'qsv' else r['type'].upper()
                    key = f"{r['gpu']} ({api})"
                out[key] = out.get(key, 0) + 1
        return out

    def busy(self) -> bool:
        with self._cond:
            return self._busy or self._request is not None and self._request['mode'] == 'still'

    def shutdown(self):
        with self._cond:
            self._alive = False
            self._cond.notify_all()

    # ---- player thread ----
    def _submit(self, path, t, mode):
        with self._cond:
            self._next_id += 1
            self._request = {'id': self._next_id, 'path': path, 't': t, 'mode': mode}
            if mode == 'play':
                self._play_frames.clear()
                self._play_id = self._next_id
            self._cond.notify_all()
            return self._next_id

    def _trim_sessions(self, keep: int = 3):
        """Keep at most `keep` files open (e.g. two cameras for fast layer
        switches). Each Intel session holds ~240 MB of 4K surfaces; prepared
        sessions taken over by play() used to skip this limit, so every clip
        change during playback kept one more open (the app grew to 8 GB)."""
        while len(self._sessions) > keep:
            self._sessions.popitem(last=False)[1].close()

    def _session(self, path):
        s = self._sessions.get(path)
        if s is None:
            route = self._routes.get(path)
            if not route:
                return None
            s = _HwSession(path, route)
            self._sessions[path] = s
            self._trim_sessions()
        else:
            self._sessions.move_to_end(path)
        return s

    def _run(self):
        while True:
            with self._cond:
                while self._alive and self._request is None and not self._probe_queue and not self._reset_sessions:
                    self._cond.wait()
                if not self._alive:
                    break
                if self._reset_sessions:
                    self._reset_sessions = False
                    for s in self._sessions.values():
                        s.close()
                    self._sessions.clear()
                    for item in self._prepared.values():
                        item[1].close()
                    self._prepared.clear()
                    if self._request is None and not self._probe_queue:
                        continue
                req = self._request
                probe_path = None if req else self._probe_queue.popleft()
                self._busy = True
            try:
                if req is None:
                    self._probe(probe_path)
                elif req['mode'] == 'still':
                    self._do_still(req)
                else:
                    self._do_play(req)
            except Exception:
                with self._cond:
                    if self._request is req:
                        self._request = None
                if req:
                    old = self._sessions.pop(req['path'], None)
                    if old:
                        old.close()
            finally:
                with self._cond:
                    self._busy = False
        for s in self._sessions.values():
            s.close()

    def _do_still(self, req):
        if req['path'] not in self._routes:
            self._probe(req['path'])
        s = self._session(req['path'])
        if s is not None:
            for t, frame in s.frames_from(req['t']):
                rgb = s.rgb(frame)
                with self._cond:
                    self._still = (req['id'], req['path'], t, rgb)
                break
        with self._cond:
            if self._request is req:
                self._request = None

    def _do_play(self, req):
        with self._cond:
            prepared = self._prepared.pop(req['path'], None)
        if prepared is not None:
            prep_t, ps, gen, first = prepared
            old = self._sessions.pop(req['path'], None)
            if old is not None and old is not ps:
                old.close()
            self._sessions[req['path']] = ps  # warm session; stills reuse it later
            self._sessions.move_to_end(req['path'])
            self._trim_sessions()
            if not (prep_t - 0.05 <= req['t'] <= prep_t + 1.0):
                gen, first = None, None       # warm file, but must seek to req t
        else:
            gen, first = None, None
        if req['path'] not in self._routes:
            self._probe(req['path'])
        s = self._session(req['path'])
        if s is None:
            with self._cond:
                if self._request is req:
                    self._request = None
            return

        def frames():
            if gen is not None:
                if first is not None:
                    yield first[0], None, first[1]
                for t, frame in gen:
                    yield t, frame, None
            else:
                for t, frame in s.frames_from(req['t']):
                    yield t, frame, None

        with self._cond:
            self._play_starting = True
        try:
            self._play_loop(req, s, frames)
        finally:
            with self._cond:
                self._play_starting = False
                self._cond.notify_all()

    def _play_loop(self, req, s, frames):
        for t, frame, ready_rgb in frames():
            rgb = ready_rgb if ready_rgb is not None else s.rgb(frame)
            with self._cond:
                while self._request is req and len(self._play_frames) >= 8:
                    self._cond.wait(0.05)
                if self._request is not req:
                    return
                self._play_frames.append((t, rgb))
                if self._play_starting:
                    self._play_starting = False   # first frame out: prepare may run
                    self._cond.notify_all()
        with self._cond:  # end of file
            if self._request is req:
                self._request = None

    def _run_prepare(self):
        while True:
            with self._cond:
                # never cold-start a second Intel session while the player is
                # still cold-starting its own (both slowed down: 0.8 -> 1.9s)
                while self._alive and (self._prep_request is None or self._play_starting):
                    self._cond.wait(0.1)
                if not self._alive:
                    return
                path, t = self._prep_request
                self._prep_request = None
                route = self._routes.get(path)
                self._prep_inflight = path if route else None
            if not route:
                continue
            try:
                s = _HwSession(path, route)
                gen = s.frames_from(t)
                first = next(gen, None)
                item = (t, s, gen, (first[0], s.rgb(first[1])) if first else None)
            except Exception:
                with self._cond:
                    self._prep_inflight = None
                continue
            with self._cond:
                self._prep_inflight = None
                for k in list(self._prepared):  # keep only the newest
                    self._prepared.pop(k)[1].close()
                self._prepared[path] = item
            # drop this thread's own references: they kept the last prepared
            # session, its decoder and a 4K frame alive after it was closed
            s = gen = first = item = None

    @staticmethod
    def _signature(path):
        c = av.open(path)
        try:
            v = c.streams.video[0]
            cc = v.codec_context
            return (cc.name, cc.format.name if cc.format else '', str(v.profile), v.width > 1920)
        finally:
            c.close()

    def _candidate_routes(self, codec: str):
        gpus = [g for g in list_gpus() if g.get('ok')]
        if DIRECT_GPU_MODE not in DIRECT_GPU_MODES:
            # One specific GPU chosen in Settings: only that one
            gpus = [g for g in gpus if g['name'] == DIRECT_GPU_MODE]
        else:
            chosen = selected_gpu(ignore_cpu_mode=True)
            gpus.sort(key=lambda g: 0 if chosen and g['name'] == chosen['name'] else 1)
        for g in gpus:
            if os.name == 'nt' and g['vendor'] == 'Intel':
                yield {'kind': 'hw', 'type': 'd3d11va', 'device': str(g['index']), 'gpu': g['name']}
                if codec in _QSV_DECODERS:
                    yield {'kind': 'qsv', 'decoder': _QSV_DECODERS[codec], 'gpu': g['name']}
            elif g['method'] == 'cuda':
                yield {'kind': 'hw', 'type': 'cuda', 'device': str(g['device']), 'gpu': g['name']}
            elif os.name == 'nt':
                yield {'kind': 'hw', 'type': 'd3d11va', 'device': str(g['index']), 'gpu': g['name']}
            else:
                yield {'kind': 'hw', 'type': g['method'], 'device': g.get('device'), 'gpu': g['name']}

    def _probe(self, path):
        """Find a hardware route for `path` (tested once per format)."""
        route = None
        try:
            sig = self._signature(path)
            if sig in self._sig_routes:
                route = self._sig_routes[sig]
            else:
                for candidate in self._candidate_routes(sig[0]):
                    s = None
                    try:
                        s = _HwSession(path, candidate)
                        if next(s.frames_from(0.0), None) is not None:
                            route = candidate
                            break
                    except Exception:
                        pass
                    finally:
                        if s:
                            s.close()
                self._sig_routes[sig] = route
        except Exception:
            route = None
        with self._cond:
            self._routes[path] = route


# =============================================================================
# LTC Decoder - Robust Multi-FPS Detection
# =============================================================================

# Standard frame rates to try
STANDARD_FPS = [23.976, 24.0, 25.0, 29.97, 30.0]


class LTCDecoder:
    """Decodes LTC (Linear Timecode) from audio samples with robust FPS detection."""

    # LTC sync word: bits 64-79 of an 80-bit frame
    SYNC_WORD = [0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,1]
    SYNC_WORD_REV = [1,0,1,1,1,1,1,1,1,1,1,1,1,1,0,0]

    def __init__(self, sample_rate=48000, expected_fps=None):
        self.sample_rate = sample_rate
        self.expected_fps = expected_fps  # If set, use this FPS directly
        self.reset()

    def reset(self):
        self.last_sample = 0
        self.samples_since_edge = 0
        self.edge_intervals = deque(maxlen=2000)
        self.bit_buffer = deque(maxlen=400)
        self.synced = False
        self.samples_per_bit = 0
        self.half_bit_samples = 0
        self.last_edge_was_half = False
        self.frames_decoded = 0
        self.all_frames = []
        self.timing_locked = False
        self.consecutive_valid = 0
        self.detected_fps = None
        # Absolute sample index of the edge that completed each decoded frame.
        # Successive entries are one real frame period apart, which is how
        # measure_frame_rate() tells 25 from 24/23.976 (and 30 from 29.97)
        # without waiting for a frame number only the faster rate can show.
        self.samples_processed = 0
        self.frame_end_samples = []

    def set_timing_for_fps(self, fps):
        """Set timing parameters for a specific frame rate."""
        # LTC has 80 bits per frame
        samples_per_frame = self.sample_rate / fps
        self.samples_per_bit = samples_per_frame / 80
        self.half_bit_samples = self.samples_per_bit / 2
        self.detected_fps = fps

    def bandpass_filter(self, samples, low_freq=600, high_freq=3500):
        """Apply FFT-based bandpass filter to isolate LTC frequencies.

        LTC signal characteristics:
        - Bit rate: ~2400 baud at 30fps (80 bits × 30 frames)
        - Manchester encoding: transitions at 1200Hz (bit boundaries) and 2400Hz (mid-bit for 1s)
        - Sweet spot: 600-3500 Hz captures LTC while rejecting:
          - 50/60 Hz mains hum
          - High-frequency noise
          - Audio bleed from other channels

        Args:
            samples: Audio samples (numpy array)
            low_freq: Low cutoff frequency in Hz (default 600)
            high_freq: High cutoff frequency in Hz (default 3500)

        Returns:
            Filtered samples
        """
        n = len(samples)
        if n < 64:  # Too short for meaningful FFT
            return samples

        # Compute FFT
        fft = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / self.sample_rate)

        # Create bandpass mask with smooth rolloff (vectorized)
        rolloff = 100.0  # Hz for smooth transition

        # Start with passband (all ones in range)
        mask = np.ones_like(freqs)

        # Low frequency rolloff: cosine taper from (low-rolloff) to low
        low_transition = (freqs >= low_freq - rolloff) & (freqs < low_freq)
        mask[low_transition] = 0.5 * (1 + np.cos(np.pi * (low_freq - freqs[low_transition]) / rolloff))

        # High frequency rolloff: cosine taper from high to (high+rolloff)
        high_transition = (freqs > high_freq) & (freqs <= high_freq + rolloff)
        mask[high_transition] = 0.5 * (1 + np.cos(np.pi * (freqs[high_transition] - high_freq) / rolloff))

        # Zero out frequencies outside the transition bands
        mask[freqs < low_freq - rolloff] = 0
        mask[freqs > high_freq + rolloff] = 0

        # Apply filter and inverse FFT
        filtered = np.fft.irfft(fft * mask, n)

        return filtered.astype(np.float32)

    def auto_detect_timing(self):
        """Detect timing using histogram analysis."""
        if len(self.edge_intervals) < 100:
            return False

        intervals = np.array(list(self.edge_intervals))

        # Remove extreme outliers
        q1, q3 = np.percentile(intervals, [10, 90])
        iqr = q3 - q1
        mask = (intervals >= q1 - 1.5*iqr) & (intervals <= q3 + 1.5*iqr)
        filtered = intervals[mask]

        if len(filtered) < 50:
            return False

        # Find the minimum cluster (half-bit periods)
        # Use KDE-like approach with histogram
        hist, bin_edges = np.histogram(filtered, bins=100)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

        # Smooth histogram
        smoothed = np.convolve(hist, np.ones(3)/3, mode='same')

        # Find first significant peak (half-bit period)
        threshold = np.max(smoothed) * 0.15
        for i in range(1, len(smoothed)-1):
            if smoothed[i] > threshold and smoothed[i] >= smoothed[i-1] and smoothed[i] >= smoothed[i+1]:
                half_bit = bin_centers[i]
                # Calculate FPS from this
                samples_per_frame = half_bit * 2 * 80  # half_bit * 2 = full_bit, * 80 = frame
                raw_fps = self.sample_rate / samples_per_frame

                # Snap to nearest standard FPS
                best_fps = min(STANDARD_FPS, key=lambda x: abs(x - raw_fps))

                # Only accept if reasonably close (within 15%)
                if abs(raw_fps - best_fps) / best_fps < 0.15:
                    self.set_timing_for_fps(best_fps)
                    return True
                break

        return False

    def process_samples(self, samples):
        """Process audio samples and extract LTC frames."""
        if not NUMPY_AVAILABLE or len(samples) == 0:
            return []

        samples = np.array(samples, dtype=np.float32)

        # Normalize audio
        max_val = np.max(np.abs(samples))
        if max_val > 0:
            samples = samples / max_val

        # Apply bandpass filter to isolate LTC frequencies (600-3500 Hz)
        # This rejects 50/60 Hz mains hum, audio bleed, and high-frequency noise
        samples = self.bandpass_filter(samples)

        # Re-normalize after filtering (filter can change amplitude)
        max_val = np.max(np.abs(samples))
        if max_val > 0:
            samples = samples / max_val

        frames = []

        # Find zero crossings
        extended = np.concatenate([[self.last_sample], samples])
        signs = np.sign(extended)
        signs[signs == 0] = 1
        crossings = np.where(np.diff(signs) != 0)[0]

        chunk_base = self.samples_processed
        self.samples_processed += len(samples)

        if len(crossings) == 0:
            self.samples_since_edge += len(samples)
            self.last_sample = samples[-1]
            return frames

        # SANITY GATE: real LTC has at most bitrate*2 crossings/sec
        # (80 bits * 30 fps * 2 = 4800/s). The 600-3500Hz bandpass caps ANY
        # signal at ~7200 crossings/s, so the threshold must sit between LTC's
        # 4800 max and that ceiling or it can never fire (music sits at
        # 5000-7000/s and would grind through the per-crossing Python loop).
        chunk_seconds = len(samples) / self.sample_rate if self.sample_rate else 1.0
        if chunk_seconds > 0 and (len(crossings) / chunk_seconds) > 6000:
            self.samples_since_edge = len(samples) - crossings[-1]
            self.last_sample = samples[-1]
            return frames

        prev_pos = 0
        for crossing_pos in crossings:
            interval = self.samples_since_edge + (crossing_pos - prev_pos)
            self.edge_intervals.append(interval)

            # Try to detect timing if not yet locked
            if not self.timing_locked and self.half_bit_samples == 0:
                if self.expected_fps:
                    self.set_timing_for_fps(self.expected_fps)
                    self.timing_locked = True
                elif self.auto_detect_timing():
                    self.timing_locked = True

            if self.half_bit_samples > 0:
                bit = self._decode_edge(interval)
                if bit is not None:
                    self.bit_buffer.append(bit)
                    frame = self._try_find_frame()
                    if frame:
                        self.frame_end_samples.append(chunk_base + int(crossing_pos))
                        frames.append(frame)
                        self.all_frames.append(frame)

            self.samples_since_edge = 0
            prev_pos = crossing_pos

        self.samples_since_edge = len(samples) - crossings[-1]
        self.last_sample = samples[-1]
        return frames

    def _decode_edge(self, interval):
        """Decode edge interval to bit value."""
        # Tolerance thresholds (30% tolerance)
        half_min = self.half_bit_samples * 0.5
        half_max = self.half_bit_samples * 1.5
        full_max = self.samples_per_bit * 1.5

        if interval < half_min:
            return None
        elif interval <= half_max:
            # Half-bit interval
            if self.last_edge_was_half:
                self.last_edge_was_half = False
                return 1
            else:
                self.last_edge_was_half = True
                return None
        elif interval <= full_max:
            # Full-bit interval
            self.last_edge_was_half = False
            return 0
        else:
            self.last_edge_was_half = False
            return None

    def _try_find_frame(self):
        """Try to find and decode an LTC frame from the bit buffer.

        PERFORMANCE: this runs once per decoded bit. Only the NEWEST 80-bit
        window can contain a new sync-word alignment - every older alignment
        was already checked when its own last bit arrived. Checking only the
        tail makes this O(1) per bit; the previous full-buffer rescan was
        O(n^2) overall and froze the UI for tens of seconds on non-LTC audio
        (band-passed music constantly yields valid-looking bit intervals).
        """
        if len(self.bit_buffer) < 80:
            return None

        candidate = list(self.bit_buffer)[-80:]

        # Check for sync word at position 64-79 (the 16 newest bits)
        if candidate[64:80] == self.SYNC_WORD:
            frame = self._decode_frame(candidate, forward=True)
            if frame:
                self.bit_buffer.clear()
                return frame

        # Check for reversed sync word (tape played backwards)
        if candidate[64:80] == self.SYNC_WORD_REV:
            frame = self._decode_frame(candidate[::-1], forward=False)
            if frame:
                self.bit_buffer.clear()
                return frame

        while len(self.bit_buffer) > 200:
            self.bit_buffer.popleft()

        return None

    def _decode_frame(self, bits, forward=True):
        """Decode an 80-bit LTC frame with validation."""
        if len(bits) != 80:
            return None

        if bits[64:80] != self.SYNC_WORD:
            return None

        try:
            frame_units = bits[0] + bits[1]*2 + bits[2]*4 + bits[3]*8
            frame_tens = bits[8] + bits[9]*2
            drop_frame = bits[10] == 1
            color_frame = bits[11] == 1

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

            # Extract user bits (8 nibbles, 4 bits each)
            # LTC user bits positions: 4-7, 12-15, 20-23, 28-31, 36-39, 44-47, 52-55, 60-63
            ub1 = bits[4] + bits[5]*2 + bits[6]*4 + bits[7]*8
            ub2 = bits[12] + bits[13]*2 + bits[14]*4 + bits[15]*8
            ub3 = bits[20] + bits[21]*2 + bits[22]*4 + bits[23]*8
            ub4 = bits[28] + bits[29]*2 + bits[30]*4 + bits[31]*8
            ub5 = bits[36] + bits[37]*2 + bits[38]*4 + bits[39]*8
            ub6 = bits[44] + bits[45]*2 + bits[46]*4 + bits[47]*8
            ub7 = bits[52] + bits[53]*2 + bits[54]*4 + bits[55]*8
            ub8 = bits[60] + bits[61]*2 + bits[62]*4 + bits[63]*8

            # User bits as raw hex string (all 8 nibbles)
            user_bits_hex = f"{ub1:X}{ub2:X}{ub3:X}{ub4:X}{ub5:X}{ub6:X}{ub7:X}{ub8:X}"

            # Parse MMDDYYCC format (user-configurable camera ID in last 2 digits)
            # ESP32 stores nibbles as: UNITS first, TENS second (reversed order)
            # UB1=month_units, UB2=month_tens, UB3=day_units, UB4=day_tens, etc.
            # Camera ID: UB7=second_char, UB8=first_char (also reversed)
            # So: value = tens * 10 + units = ub2 * 10 + ub1
            ub_month = ub2 * 10 + ub1 if ub2 <= 1 and ub1 <= 9 else None
            ub_day = ub4 * 10 + ub3 if ub4 <= 3 and ub3 <= 9 else None
            ub_year = ub6 * 10 + ub5 if ub6 <= 9 and ub5 <= 9 else None
            # Camera ID: ub8=first_digit (tens), ub7=second_digit (units)
            # System only allows numeric camera IDs 00-99
            ub_camera_id = ub8 * 10 + ub7 if ub8 <= 9 and ub7 <= 9 else None

            # Validate BCD digits
            if frame_units > 9 or frame_tens > 2:
                return None
            if sec_units > 9 or sec_tens > 5:
                return None
            if min_units > 9 or min_tens > 5:
                return None
            if hour_units > 9 or hour_tens > 2:
                return None

            if frames >= 30 or seconds >= 60 or minutes >= 60 or hours >= 24:
                return None

            self.frames_decoded += 1
            self.synced = True
            self.consecutive_valid += 1

            return {
                'hours': hours,
                'minutes': minutes,
                'seconds': seconds,
                'frames': frames,
                'drop_frame': drop_frame,
                'color_frame': color_frame,
                'user_bits': user_bits_hex,
                'ub_month': ub_month,
                'ub_day': ub_day,
                'ub_year': ub_year,
                'ub_camera_id': ub_camera_id
            }
        except:
            return None

    def measure_frame_rate(self):
        """Measure the real LTC frame rate from the spacing of decoded frames.

        Every decoded frame ends on the same edge of its sync word, so the
        distance between consecutive frame ends is exactly one frame period
        (+-1 sample of zero-crossing quantisation). Averaging a handful of
        periods resolves 23.976 vs 24 (0.1% apart) at 48kHz. Gaps where a
        frame failed to decode are skipped by keeping only deltas close to
        the median. Returns None with fewer than 2 consecutive frames.
        """
        ends = self.frame_end_samples
        if len(ends) < 2 or not self.sample_rate:
            return None
        deltas = np.diff(np.asarray(ends, dtype=np.float64))
        median = float(np.median(deltas))
        if median <= 0:
            return None
        consecutive = deltas[np.abs(deltas - median) < median * 0.1]
        if len(consecutive) == 0:
            return None
        return self.sample_rate / float(np.mean(consecutive))

    @staticmethod
    def _frame_index(frame, nominal: int) -> int:
        """Frame count of a decoded TC from midnight (drop-frame aware)."""
        idx = ((frame['hours'] * 60 + frame['minutes']) * 60 + frame['seconds']) * nominal + frame['frames']
        if frame.get('drop_frame') and nominal == 30:
            total_minutes = frame['hours'] * 60 + frame['minutes']
            idx -= 2 * (total_minutes - total_minutes // 10)
        return idx

    def consistent_frames(self, fps: float, min_run: int = 3) -> list:
        """The decoded frames that are REAL LTC, or [] if there is no such run.

        Real LTC counts up one frame per frame period, so between any two of
        its frames the timecode difference equals the number of periods
        elapsed in the audio - even if frames in between failed to decode.
        Bit patterns decoded out of speech/music/noise pass the sync-word and
        BCD checks now and then, but land at random timecodes; two of them
        were enough to accept a microphone channel as LTC (e.g. TC
        00:18:01:02 -> 18:00:00:04 in 10s, "29.97fps", on a 25fps camera).
        Returns the longest chain of mutually consistent frames, if it has
        at least `min_run` frames.
        """
        frames, ends = self.all_frames, self.frame_end_samples
        if len(frames) < min_run or len(ends) != len(frames) or not fps or not self.sample_rate:
            return []
        nominal = int(round(fps))
        day = 24 * 3600 * nominal
        period = self.sample_rate / fps
        idx = [self._frame_index(f, nominal) for f in frames]
        best = []
        for i in range(len(frames)):
            chain = [i]
            for j in range(i + 1, len(frames)):
                k = chain[-1]
                elapsed = round((ends[j] - ends[k]) / period)
                if elapsed > 0 and (idx[j] - idx[k]) % day == elapsed:
                    chain.append(j)
            if len(chain) > len(best):
                best = chain
        return [frames[i] for i in best] if len(best) >= min_run else []

    def get_detected_fps(self):
        """Get the detected or set FPS, snapped to standard values."""
        if self.detected_fps:
            return self.detected_fps
        if self.samples_per_bit > 0:
            raw_fps = self.sample_rate / (self.samples_per_bit * 80)
            # Snap to nearest standard FPS
            return min(STANDARD_FPS, key=lambda x: abs(x - raw_fps))
        return None


def try_decode_with_fps(samples, sample_rate, fps, quick_check=False):
    """Try to decode LTC with a specific FPS setting. Returns (frames_decoded, frames_list).

    Args:
        samples: Audio samples
        sample_rate: Sample rate
        fps: Frame rate to try
        quick_check: If True, only process 2 seconds for quick detection
    """
    decoder = LTCDecoder(sample_rate, expected_fps=fps)
    chunk_size = sample_rate  # 1 second chunks

    # For quick check, only process 2 seconds
    max_samples = sample_rate * 2 if quick_check else sample_rate * 10

    for i in range(0, min(len(samples), max_samples), chunk_size):
        decoder.process_samples(samples[i:i + chunk_size])
        # Yield the GIL briefly so the Tk main loop stays responsive while the
        # analysis thread grinds through pure-Python decode loops
        time.sleep(0.001)
        # Early exit if we found enough frames
        if decoder.frames_decoded >= 5:
            break
        # OPTIMIZATION: If no frames after 2 seconds, likely no LTC on this channel
        # Non-LTC audio won't suddenly have LTC - exit early to save time
        if i >= sample_rate * 2 and decoder.frames_decoded == 0:
            break

    return decoder.frames_decoded, decoder.all_frames, decoder


def detect_ltc_multi_fps(samples, sample_rate):
    """
    Try multiple standard FPS values and return the best result.
    Returns (best_decoder, detected_fps).

    CRITICAL FPS detection rules based on frame numbers:
    - Frames 0-23:  Valid for ALL frame rates (23.976, 24, 25, 29.97, 30)
    - Frame 24:     Valid ONLY for 25, 29.97, 30 fps (NOT 23.976 or 24)
    - Frames 25-29: Valid ONLY for 29.97 or 30 fps (NOT 23.976, 24, or 25!)
    - Drop frame:   Only used with 29.97 fps (NTSC)

    OPTIMIZATION: Quick pre-check with 30fps - if no LTC found in 2 seconds,
    skip full multi-FPS scan. This dramatically speeds up non-LTC audio analysis.
    """
    # QUICK PRE-CHECK: Try 30fps on first 2 seconds to detect if ANY LTC signal exists
    # 30fps is a good initial guess because LTC bit timing is similar across frame rates
    # If there's no LTC-like signal at all, we can skip the expensive multi-FPS scan
    quick_count, _, quick_decoder = try_decode_with_fps(samples, sample_rate, 30.0, quick_check=True)
    if quick_count == 0:
        # No LTC signal detected in quick check - try one more with 25fps (PAL)
        quick_count, _, quick_decoder = try_decode_with_fps(samples, sample_rate, 25.0, quick_check=True)
        if quick_count == 0:
            # Definitely no LTC on this channel - return early
            return None, None

    # FAST PATH: the quick pass already measured the frame period, so decode
    # ONCE at that rate instead of once per STANDARD_FPS entry (5 full passes
    # were ~75% of analysis time and serialised parallel analysis on the GIL).
    # The frame-number / drop-frame rules below still have to agree; if they
    # don't, or the period can't be measured, the full scan runs as before.
    measured_fps = quick_decoder.measure_frame_rate()
    if measured_fps:
        guess = min(STANDARD_FPS, key=lambda f: abs(f - measured_fps))
        if abs(guess - measured_fps) / guess < 0.02:
            count, frames, decoder = try_decode_with_fps(samples, sample_rate, guess)
            if count > 0:
                # Judge on the frames that count up consistently, and by majority: one corrupt
                # decode can carry a flipped drop-frame bit (a 25 fps camera clip was taken as
                # 29.97 DF that way, 2026-10-25) or an impossible frame number.
                run = decoder.consistent_frames(guess) or frames
                max_frame = max(f['frames'] for f in run)
                if sum(1 for f in run if f['drop_frame']) * 2 > len(run):
                    allowed = [29.97]
                elif max_frame >= 25:
                    allowed = [29.97, 30.0]
                elif max_frame >= 24:
                    allowed = [25.0, 29.97, 30.0]
                else:
                    allowed = STANDARD_FPS
                rate = decoder.measure_frame_rate() or measured_fps
                fps = min(allowed, key=lambda f: abs(f - rate))
                if abs(fps - rate) / fps < 0.02:
                    decoder.detected_fps = fps
                    return decoder, fps

    # Collect all decoded frames from all FPS attempts to find max frame number
    all_decoded_frames = []
    results = {}

    # Try all FPS values and collect results
    for fps in STANDARD_FPS:
        count, frames, decoder = try_decode_with_fps(samples, sample_rate, fps)
        results[fps] = {
            'count': count,
            'frames': frames,
            'decoder': decoder
        }
        # Collect all frame numbers from decoded frames
        for f in frames:
            all_decoded_frames.append(f)

    # Find the highest frame number across ALL decoded data
    # One corrupt decode can carry a flipped drop-frame bit or an impossible frame number:
    # take the frame number at least two frames reach, and drop frame only by majority
    numbers = sorted((f['frames'] for f in all_decoded_frames), reverse=True)
    max_frame_number = numbers[1] if len(numbers) >= 2 else (numbers[0] if numbers else 0)
    has_drop_frame = len(all_decoded_frames) > 0 and sum(1 for f in all_decoded_frames if f['drop_frame']) * 2 > len(all_decoded_frames)

    # Determine valid FPS candidates based on max frame number
    # This is the CRITICAL logic:
    if max_frame_number >= 25:
        # Frame 25-29 can ONLY exist in 29.97 or 30 fps
        # 25fps only has frames 0-24!
        valid_fps = [29.97, 30.0]
    elif max_frame_number >= 24:
        # Frame 24 can ONLY exist in 25, 29.97, or 30 fps
        # 23.976 and 24fps only have frames 0-23!
        valid_fps = [25.0, 29.97, 30.0]
    else:
        # Frames 0-23 are valid for any FPS
        valid_fps = STANDARD_FPS.copy()

    # If drop frame flag is set, it MUST be 29.97 (NTSC drop frame)
    if has_drop_frame:
        valid_fps = [29.97]

    # MEASURED RATE: the frame-number rule above only narrows the field once
    # a high frame number shows up, but decoding stops after ~5 frames - so a
    # 25fps clip that starts at frame 0-19 never shows frame 24, all five
    # candidates decode the same 5 frames, and the old tie-break picked the
    # first list entry (23.976). The spacing between decoded frames is the
    # actual frame period, so pick the valid candidate closest to it.
    measured_fps = None
    for fps in sorted(results, key=lambda f: -results[f]['count']):
        measured_fps = results[fps]['decoder'].measure_frame_rate()
        if measured_fps:
            break
    if measured_fps:
        nearest = min(valid_fps, key=lambda f: abs(f - measured_fps))
        if abs(nearest - measured_fps) / nearest < 0.02 and results.get(nearest, {}).get('count', 0) > 0:
            best_decoder = results[nearest]['decoder']
            best_decoder.detected_fps = nearest
            return best_decoder, nearest

    # Find the best decoder among valid FPS options
    best_decoder = None
    best_count = 0
    best_fps = None

    for fps in valid_fps:
        if fps in results and results[fps]['count'] > 0:
            r = results[fps]
            if r['count'] > best_count:
                best_count = r['count']
                best_fps = fps
                best_decoder = r['decoder']

    # Fallback: if no valid FPS found, try all successful ones
    if best_decoder is None:
        for fps in STANDARD_FPS:
            if fps in results and results[fps]['count'] > 0:
                r = results[fps]
                if r['count'] > best_count:
                    best_count = r['count']
                    best_fps = fps
                    best_decoder = r['decoder']

    if best_decoder:
        best_decoder.detected_fps = best_fps

    return best_decoder, best_fps


# =============================================================================
# Media Clip Data Class
# =============================================================================

@dataclass
class MediaClip:
    """Represents a media clip with timecode info."""
    path: str
    filename: str
    duration: float = 0.0
    original_duration: float = 0.0  # File's actual duration - clips can't exceed this
    start_tc: Optional[str] = None  # LTC timecode from audio
    end_tc: Optional[str] = None
    start_frames: int = 0
    fps: float = 30.0
    drop_frame: bool = False
    ltc_channel: int = -1
    ltc_offset: int = 0  # Offset in samples from start of file to first detected LTC frame
    has_linked_audio: bool = False
    linked_audio_path: Optional[str] = None
    width: int = 1920
    height: int = 1080
    video_fps: str = "30/1"
    audio_channels: int = 2
    sample_rate: int = 48000
    sample_count: int = 0  # Exact sample count for sample-accurate duration
    bwf_time_reference: int = 0  # BWF time_reference in samples (for sample-accurate start position)
    status: str = "pending"
    error: str = ""
    ltc_warning: str = ""   # analysis found LTC but something is off (e.g. a timecode jump later in the file)
    file_date: Optional[datetime] = None
    camera_id: Optional[str] = None  # Detected camera identifier
    is_audio_only: bool = False  # True for audio-only files (wav, mp3, etc.)
    track_enabled: bool = True  # Whether this track is enabled in timeline
    embedded_tc: Optional[str] = None  # Embedded camera timecode from file metadata
    embedded_tc_frames: int = 0  # Embedded timecode in frames
    tc_source: str = "auto"  # "auto", "ltc", "embedded" - which TC to use for sync

    # LTC User bits (MMDDYYCC format)
    user_bits: Optional[str] = None  # Raw user bits as hex string (8 chars)
    ub_camera_id: Optional[int] = None  # Camera ID from user bits (00-99)
    ub_date: Optional[str] = None  # Date from user bits (MMDDYY format)

    # Audio linking (for multi-track recorders like Zoom, Sound Devices)
    recording_id: Optional[str] = None  # Base recording ID (e.g., "ZOOM0004")
    track_number: Optional[int] = None  # Track number (e.g., 3 for Tr3)
    split_part: Optional[int] = None  # Split file part (1, 2, etc. from -0001, -0002)
    linked_ltc_path: Optional[str] = None  # Path to the LTC track this audio follows
    is_ltc_track: bool = False  # True if this is the LTC reference track
    linked_audio_tracks: List[str] = None  # Paths of audio tracks linked to this LTC track

    # For timeline display
    timeline_start: float = 0.0  # Position in timeline (seconds)
    color: str = "#4a9eff"  # Display color
    track_index: int = 0  # Which track this clip belongs to

    @property
    def fps_display(self) -> str:
        """Get human-readable FPS string.

        Computed from fps/drop_frame by default. An explicit string may be
        assigned (split/duplicate copy it, some callers set it directly); the
        setter stores it as an override so those assignments don't raise
        AttributeError on this @property.
        """
        override = getattr(self, '_fps_display_override', None)
        if override is not None:
            return override
        if self.fps <= 0:
            return "?fps"
        # Common frame rates - use tight tolerance (0.02) to avoid overlap
        # Check integer rates first to avoid misidentifying 30.0 as 29.97
        if abs(self.fps - 24.0) < 0.02:
            return "24fps"
        elif abs(self.fps - 25.0) < 0.02:
            return "25fps"
        elif abs(self.fps - 30.0) < 0.02:
            return "30fps"
        elif abs(self.fps - 50.0) < 0.02:
            return "50fps"
        elif abs(self.fps - 60.0) < 0.02:
            return "60fps"
        # Then check non-integer rates
        elif abs(self.fps - 23.976) < 0.02:
            return "23.976fps" + (" DF" if self.drop_frame else "")
        elif abs(self.fps - 29.97) < 0.02:
            return "29.97fps" + (" DF" if self.drop_frame else " NDF")
        elif abs(self.fps - 59.94) < 0.02:
            return "59.94fps" + (" DF" if self.drop_frame else "")
        else:
            return f"{self.fps:.2f}fps"

    @fps_display.setter
    def fps_display(self, value):
        # Allow assignment (split/duplicate copy this); None clears the override
        self._fps_display_override = value

    def tc_to_frames(self, tc: str) -> int:
        """Convert timecode string to frame count.

        For drop-frame timecode (29.97 DF), this subtracts the dropped frames
        to get the actual frame count. Drop frame skips frames 0 and 1 at the
        start of each minute, except for minutes 0, 10, 20, 30, 40, 50.
        """
        if not tc:
            return 0
        parts = tc.replace(';', ':').split(':')
        if len(parts) != 4:
            return 0
        h, m, s, f = map(int, parts)
        fps_int = int(round(self.fps))

        # Calculate display frame number (what the timecode shows)
        display_frames = f + s * fps_int + m * 60 * fps_int + h * 3600 * fps_int

        if self.drop_frame and fps_int == 30:
            # For DF, subtract the dropped frames to get actual frame count
            # 2 frames dropped per minute, except every 10th minute (0, 10, 20, 30, 40, 50)
            total_minutes = h * 60 + m
            drop_frames = 2 * (total_minutes - total_minutes // 10)
            return display_frames - drop_frames

        return display_frames

    def tc_to_seconds(self, tc: str) -> float:
        """Convert timecode to seconds."""
        frames = self.tc_to_frames(tc)
        return frames / self.fps if self.fps > 0 else 0


def detect_camera_id(filename: str) -> Optional[str]:
    """
    Detect camera identifier from filename patterns.

    Logic:
    1. Panasonic/ARRI pattern (X###C###): Camera ID = X### (e.g., A025)
       - A025C071.MP4, A025C072.MP4 → CAM A025 (same camera)
       - A026C001.MP4 → CAM A026 (different camera)

    2. All other patterns: Extract letter groups, preserve underscores
       - PENT0985.MP4, PENT0986.MP4 → CAM PENT (same camera)
       - CANON001.MOV, CANON002.MOV → CAM CANON (same camera)
       - MVI_0001.MP4, MVI_0002.MP4 → CAM MVI (same camera)
       - CAM_A_001.mp4, CAM_A_002.mp4 → CAM CAM_A (same camera)
       - 0001Canon.mp4, 0002Canon.mp4 → CAM CANON (same camera)
       - GH010001.MP4, GH020001.MP4 → CAM GH (same camera, GoPro)
    """
    import re
    name = os.path.splitext(filename)[0].upper()

    # 1. Panasonic/ARRI pattern: X###C### (letter + 3 digits + C + 3 digits)
    # The X### part is the camera/reel ID, C### is clip number
    match = re.match(r'^([A-Z]\d{3})C\d{3}', name)
    if match:
        result = f"CAM {match.group(1)}"
        return result

    # 2. RED pattern: X###_C### (with underscore)
    match = re.match(r'^([A-Z]\d{3})_C\d{3}', name)
    if match:
        result = f"CAM {match.group(1)}"
        return result

    # 3. For all other patterns: extract letter groups, preserve underscores
    # This handles: PENT0985, CANON001, MVI_0001, CAM_A_001, 0001Canon, GH010001, etc.

    # Remove all digits from filename
    letters_only = re.sub(r'\d+', '', name)

    # Clean up: collapse multiple underscores/dashes, remove leading/trailing
    letters_only = re.sub(r'[-_]+', '_', letters_only)  # normalize separators to underscore
    letters_only = letters_only.strip('_')  # remove leading/trailing underscores

    if letters_only:
        result = f"CAM {letters_only}"
        return result

    return None


# =============================================================================
# Media Analyzer
# =============================================================================

class MediaAnalyzer:
    """Analyzes media files for LTC timecode."""

    SUPPORTED_VIDEO = {'.mp4', '.mov', '.mxf', '.avi', '.mkv', '.m4v', '.webm'}
    SUPPORTED_AUDIO = {'.wav', '.mp3', '.aac', '.m4a', '.flac', '.aiff'}

    def __init__(self):
        # pid in the name: a later startup can tell a live folder from a
        # leftover of a crashed / killed run (cleanup_stale_temp_dirs)
        self.temp_dir = tempfile.mkdtemp(prefix=f'{TEMP_PREFIX}{os.getpid()}_')

    def set_temp_root(self, root: str):
        """Move this session's temp folder under `root` (the scratch disk)."""
        try:
            os.makedirs(root, exist_ok=True)
            if os.path.normcase(os.path.dirname(self.temp_dir)) == os.path.normcase(os.path.abspath(root)):
                return
            new_dir = tempfile.mkdtemp(prefix=f'{TEMP_PREFIX}{os.getpid()}_', dir=root)
        except OSError:
            return  # keep the current one (e.g. scratch drive not writable)
        old, self.temp_dir = self.temp_dir, new_dir
        shutil.rmtree(old, ignore_errors=True)

    def cleanup(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def is_supported(self, path: str) -> bool:
        ext = Path(path).suffix.lower()
        return ext in self.SUPPORTED_VIDEO or ext in self.SUPPORTED_AUDIO

    def is_audio_file(self, path: str) -> bool:
        """Check if file is audio-only based on extension."""
        ext = Path(path).suffix.lower()
        return ext in self.SUPPORTED_AUDIO

    def _run_ffprobe(self, path: str) -> Optional[Dict]:
        if not FFMPEG_AVAILABLE:
            return None
        try:
            cmd = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                   '-show_format', '-show_streams', path]
            kwargs = {'capture_output': True, 'text': True, 'timeout': 30}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, **kwargs)
            return json.loads(result.stdout) if result.returncode == 0 else None
        except:
            return None

    def _get_wav_sample_count(self, path: str) -> int:
        """Get exact sample count from WAV file by reading the data chunk.

        Returns:
            Sample count (0 if unable to read)
        """
        try:
            with open(path, 'rb') as f:
                # Read RIFF header
                riff = f.read(4)
                if riff != b'RIFF':
                    return 0

                f.read(4)  # File size
                wave = f.read(4)
                if wave != b'WAVE':
                    return 0

                # Find fmt and data chunks
                channels = 0
                bits_per_sample = 0
                data_size = 0

                while True:
                    chunk_id = f.read(4)
                    if len(chunk_id) < 4:
                        break

                    chunk_size = int.from_bytes(f.read(4), 'little')

                    if chunk_id == b'fmt ':
                        fmt_data = f.read(chunk_size)
                        if len(fmt_data) >= 16:
                            channels = int.from_bytes(fmt_data[2:4], 'little')
                            bits_per_sample = int.from_bytes(fmt_data[14:16], 'little')
                    elif chunk_id == b'data':
                        data_size = chunk_size
                        break  # Found data chunk, we're done
                    else:
                        # Skip unknown chunk
                        f.seek(chunk_size, 1)

                if channels > 0 and bits_per_sample > 0 and data_size > 0:
                    bytes_per_sample = bits_per_sample // 8
                    sample_count = data_size // (channels * bytes_per_sample)
                    return sample_count

        except Exception:
            pass  # Silent failure

        return 0

    def _get_embedded_timecode(self, path: str, fps: float = 30.0) -> Tuple[Optional[str], int, int]:
        """Read embedded timecode from video file metadata.

        Returns:
            Tuple of (timecode_string, timecode_frames, bwf_time_reference)
            e.g., ("07:06:23:11", 7685411, 1230336000) for 29.97fps BWF audio
            bwf_time_reference is the raw sample offset from midnight (0 if not BWF)
        """
        if not FFMPEG_AVAILABLE:
            return None, 0, 0

        kwargs = {'capture_output': True, 'text': True, 'timeout': 30}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        video_rate = [None]   # the video stream's rate: a camera TC's frame field counts ITS frames (50p: 0-49)

        def parse_tc(tc_str: str) -> Tuple[Optional[str], int]:
            """Parse timecode string and return (tc_str, total_frames at fps)."""
            if not tc_str:
                return None, 0
            tc_str = tc_str.strip()
            # Handle HH:MM:SS:FF or HH:MM:SS;FF format
            parts = tc_str.replace(';', ':').split(':')
            if len(parts) == 4:
                try:
                    list(map(int, parts))
                    return tc_str, XMLExporter.tc_frames_at(tc_str, fps, video_rate[0])
                except ValueError:
                    pass
            return None, 0

        try:
            # Comprehensive approach: Get ALL format and stream info
            cmd = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                   '-show_format', '-show_streams', path]

            result = subprocess.run(cmd, **kwargs)
            if result.returncode != 0:
                return None, 0, 0

            data = json.loads(result.stdout)
            tc_str = None
            for st in data.get('streams', []):
                if st.get('codec_type') == 'video' and not (st.get('disposition') or {}).get('attached_pic'):
                    video_rate[0] = XMLExporter.video_tc_rate(st.get('r_frame_rate'))
                    break

            # 1. Check format-level tags (various possible keys)
            fmt_tags = data.get('format', {}).get('tags', {})
            # Extended list of TC keys used by various cameras/formats
            tc_keys = ['timecode', 'TIMECODE', 'com.apple.quicktime.timecode',
                       'tc', 'TC', 'time_code', 'TIME_CODE',
                       'com.android.capture.fps', 'encoder',  # Some cameras
                       'creation_time', 'CREATION_TIME']  # May contain TC info
            for key in tc_keys:
                if key in fmt_tags:
                    tc_str, frames = parse_tc(fmt_tags[key])
                    if tc_str:
                        return tc_str, frames, 0  # No BWF time_reference for format tags

            # 1b. Check for BWF time_reference (used by ZOOM, Sound Devices, etc.)
            # time_reference is sample offset from midnight
            time_ref = fmt_tags.get('time_reference') or fmt_tags.get('TIME_REFERENCE')
            if time_ref:
                try:
                    sample_offset = int(time_ref)
                    # Get sample rate from format or default to 48000
                    sample_rate = 48000
                    for stream in data.get('streams', []):
                        if stream.get('codec_type') == 'audio':
                            sr = stream.get('sample_rate')
                            if sr:
                                sample_rate = int(sr)
                            break

                    # Convert sample offset to timecode
                    seconds_from_midnight = sample_offset / sample_rate

                    # Use ACTUAL fps for frame counting (fixes 23.976/29.97 drift)
                    # At 23.976fps over 1 hour: using 24 would drift 3.6 seconds!
                    total_frames = round(seconds_from_midnight * fps)

                    # Use nominal fps for timecode display (24 for 23.976, 30 for 29.97)
                    # Timecode uses integer frame numbers (0-23 for 23.976, 0-29 for 29.97)
                    fps_nominal = int(round(fps))
                    h = total_frames // (3600 * fps_nominal)
                    m = (total_frames % (3600 * fps_nominal)) // (60 * fps_nominal)
                    s = (total_frames % (60 * fps_nominal)) // fps_nominal
                    f = total_frames % fps_nominal
                    tc_str = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
                    return tc_str, total_frames, sample_offset  # Return raw sample offset for FCPXML export
                except (ValueError, TypeError):
                    pass  # Silent failure

            # 2. Check ALL streams for timecode
            for stream in data.get('streams', []):
                codec_type = stream.get('codec_type', '')
                codec_tag_string = stream.get('codec_tag_string', '')

                # Check stream tags
                tags = stream.get('tags', {})
                for key in tc_keys:
                    if key in tags:
                        tc_str, frames = parse_tc(tags[key])
                        if tc_str:
                            return tc_str, frames, 0  # No BWF time_reference

                # Check for tmcd (timecode) track - common in professional formats
                if codec_tag_string == 'tmcd' or codec_type == 'data':
                    # Timecode track found - try to read its start time
                    start_time = stream.get('start_time')
                    if start_time and start_time != 'N/A':
                        try:
                            start_sec = float(start_time)
                            fps_int = int(round(fps))
                            total_frames = int(start_sec * fps_int)
                            h = total_frames // (3600 * fps_int)
                            m = (total_frames % (3600 * fps_int)) // (60 * fps_int)
                            s = (total_frames % (60 * fps_int)) // fps_int
                            f = total_frames % fps_int
                            tc_str = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
                            return tc_str, total_frames, 0  # No BWF time_reference
                        except (ValueError, TypeError):
                            pass

            # 3. Try reading timecode from data streams specifically
            cmd2 = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                    '-show_streams', '-select_streams', 'd', path]
            result2 = subprocess.run(cmd2, **kwargs)
            if result2.returncode == 0:
                data2 = json.loads(result2.stdout)
                for stream in data2.get('streams', []):
                    tags = stream.get('tags', {})
                    for key in tc_keys:
                        tc_val = tags.get(key)
                        if tc_val:
                            tc_str, frames = parse_tc(tc_val)
                            if tc_str:
                                return tc_str, frames, 0  # No BWF time_reference

            # 4. Try reading the first frame's timecode using ffprobe packets
            cmd3 = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                    '-select_streams', 'd:0', '-show_packets', '-read_intervals', '%+#1',
                    path]
            result3 = subprocess.run(cmd3, **kwargs)
            if result3.returncode == 0:
                try:
                    data3 = json.loads(result3.stdout)
                    packets = data3.get('packets', [])
                    if packets:
                        # Some cameras store TC in packet side_data
                        pkt = packets[0]
                        if 'side_data_list' in pkt:
                            for side_data in pkt['side_data_list']:
                                if 'timecode' in side_data:
                                    tc_str, frames = parse_tc(side_data['timecode'])
                                    if tc_str:
                                        return tc_str, frames, 0  # No BWF time_reference
                except json.JSONDecodeError:
                    pass

            # 5. Try reading tmcd track frame data directly (for cameras like Pentax, etc.)
            # The timecode is stored as a 4-byte big-endian frame count in the tmcd track
            for stream in data.get('streams', []):
                codec_tag_string = stream.get('codec_tag_string', '')
                if codec_tag_string == 'tmcd':
                    try:
                        # Extract raw tmcd data using ffmpeg
                        cmd_tmcd = [FFMPEG_PATH, '-v', 'quiet', '-i', path,
                                   '-map', f"0:{stream.get('index', 0)}",
                                   '-c', 'copy', '-f', 'rawvideo', '-']
                        result_tmcd = subprocess.run(cmd_tmcd, capture_output=True, timeout=5,
                                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                        if result_tmcd.returncode == 0 and len(result_tmcd.stdout) >= 4:
                            # First 4 bytes are the frame number (big-endian)
                            frame_bytes = result_tmcd.stdout[:4]
                            total_frames = int.from_bytes(frame_bytes, byteorder='big')

                            # tmcd counter is spec'd SIGNED int32; negative or
                            # absurd values are garbage
                            if total_frames >= 0x80000000:
                                total_frames -= 0x100000000

                            # The counter is in the VIDEO stream's frame base,
                            # not the fps the caller passes (a 50fps camera
                            # carrying 25fps LTC otherwise yields a >24h
                            # nonsense TC and a double-base frame count).
                            tmcd_fps = 0.0
                            for rate_src in (stream, *data.get('streams', [])):
                                rate_str = rate_src.get('avg_frame_rate') or rate_src.get('r_frame_rate') or ''
                                if '/' in rate_str:
                                    try:
                                        r_num, r_den = map(int, rate_str.split('/'))
                                        if r_den > 0 and 5.0 <= r_num / r_den <= 240.0:
                                            tmcd_fps = r_num / r_den
                                            break
                                    except ValueError:
                                        pass
                            base_fps = tmcd_fps if tmcd_fps > 0 else (fps if fps > 0 else 24.0)
                            fps_int = int(round(base_fps))

                            if total_frames <= 0 or total_frames >= 24 * 3600 * fps_int:
                                continue  # invalid counter - try other methods

                            # TC string rendered in the counter's own base
                            h = total_frames // (3600 * fps_int)
                            m = (total_frames % (3600 * fps_int)) // (60 * fps_int)
                            s = (total_frames % (60 * fps_int)) // fps_int
                            f = total_frames % fps_int
                            tc_str = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

                            # Frame count returned in the CALLER's fps base so
                            # it is comparable with every other clip's frames
                            caller_fps = fps if fps > 0 else base_fps
                            ret_frames = round(total_frames * caller_fps / base_fps)
                            return tc_str, ret_frames, 0
                    except Exception:
                        pass

            # 6. Try using ffprobe -show_entries for specific frame info
            try:
                cmd_entries = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                              '-show_entries', 'format_tags:stream_tags:frame_tags',
                              '-read_intervals', '%+#1', path]
                result_entries = subprocess.run(cmd_entries, **kwargs)
                if result_entries.returncode == 0:
                    data_entries = json.loads(result_entries.stdout)
                    # Check frames for timecode
                    for frame in data_entries.get('frames', []):
                        tags = frame.get('tags', {})
                        for key in tc_keys:
                            if key in tags:
                                tc_str, frames = parse_tc(tags[key])
                                if tc_str:
                                    return tc_str, frames, 0
            except Exception:
                pass

            return None, 0, 0

        except Exception:
            return None, 0, 0

    def _extract_channel_to_memory(self, path: str, channel: int, duration: float = 10.0) -> Tuple[np.ndarray, int]:
        """Extract audio channel directly to memory via FFmpeg stdout piping.

        This avoids disk I/O by streaming raw PCM data through stdout instead of
        writing/reading temporary files. Significantly faster for batch processing.

        Args:
            path: Path to media file
            channel: Audio channel index (0-based)
            duration: Seconds of audio to extract (default 10s for fast LTC detection)

        Returns:
            Tuple of (audio samples as float32 numpy array, sample rate)
        """
        if not FFMPEG_AVAILABLE:
            return np.array([], dtype=np.float32), 48000

        sample_rate = 48000
        try:
            # Extract audio directly to stdout as raw PCM (no file I/O)
            # -f s16le = signed 16-bit little-endian raw PCM
            # pipe:1 = output to stdout
            cmd = [
                FFMPEG_PATH, '-y',
                '-i', path,
                '-vn',  # No video
                '-t', str(duration),  # Limit duration
                '-af', f'pan=mono|c0=c{channel}',  # Extract specific channel
                '-acodec', 'pcm_s16le',
                '-ar', str(sample_rate),
                '-f', 's16le',  # Raw PCM format (no WAV header)
                'pipe:1'  # Output to stdout
            ]

            kwargs = {'capture_output': True, 'timeout': 60}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            if result.returncode == 0 and len(result.stdout) >= 2:
                # Convert raw bytes directly to numpy array (zero-copy interpretation)
                audio_data = np.frombuffer(result.stdout, dtype=np.int16)
                return audio_data.astype(np.float32) / 32768.0, sample_rate
            else:
                return np.array([], dtype=np.float32), sample_rate

        except Exception:
            return np.array([], dtype=np.float32), sample_rate

    def _extract_stream_to_memory(self, path: str, stream_index: int, num_channels: int,
                                  duration: float = 10.0, start: float = 0.0) -> Tuple[np.ndarray, int]:
        """Extract EVERY channel of one audio stream in a single FFmpeg run.

        Returns (samples shaped (n, num_channels) float32, sample rate), or an
        empty array on failure. One process instead of one per channel plus a
        second pass for the LTC channel - FFmpeg start-up dominated analysis.
        """
        sample_rate = 48000
        empty = np.zeros((0, max(1, num_channels)), dtype=np.float32)
        if not FFMPEG_AVAILABLE or num_channels <= 0:
            return empty, sample_rate
        cmd = [FFMPEG_PATH, '-y'] + (['-ss', f"{start:.3f}"] if start > 0 else []) + ['-i', path,
               '-map', f'0:{stream_index}', '-vn', '-t', str(duration),
               '-acodec', 'pcm_s16le', '-ar', str(sample_rate), '-f', 's16le', 'pipe:1']
        kwargs = {'capture_output': True, 'timeout': 60}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, **kwargs)
        except Exception:
            return empty, sample_rate
        frame_bytes = 2 * num_channels
        usable = len(result.stdout) // frame_bytes * frame_bytes
        if result.returncode != 0 or usable == 0:
            return empty, sample_rate
        audio = np.frombuffer(result.stdout[:usable], dtype=np.int16).reshape(-1, num_channels)
        return audio.astype(np.float32) / 32768.0, sample_rate

    def _check_ltc_continuity(self, path: str, clip, stream, channel: int, result: Dict, expected_fps) -> str:
        """Warning text when the timecode jumps somewhere in the file, else ''. The analysis reads the first 10 s
        only, so a re-jam / timecode reset later on went unnoticed and the rest of the take was placed by the
        START timecode (10 minutes early after a +10 min re-jam). One look at the file's tail: its timecode must
        be where the start timecode plus the elapsed time says. A dropout with continuous timecode passes."""
        try:
            first = result.get('first')
            fps = result.get('fps') or clip.fps
            if clip.duration <= 12.0 or not first or not fps or not stream or 'index' not in stream:
                return ""
            tail_start = max(10.0, clip.duration - 6.0)
            block, sr = self._extract_stream_to_memory(path, stream['index'], stream['channels'],
                                                       duration=5.0, start=tail_start)
            if not len(block) or channel >= block.shape[1]:
                return ""
            tail = self._detect_ltc(np.ascontiguousarray(block[:, channel]), sr, expected_fps or fps)
            end = tail.get('first') if tail.get('found') else None
            if not end or 'sample_pos' not in end:
                return ""
            rate = XMLExporter.exact_fps(fps)
            nominal = int(round(fps))
            t0 = first.get('sample_pos', 0) / sr
            t1 = tail_start + end['sample_pos'] / sr
            expected = LTCDecoder._frame_index(first, nominal) + (t1 - t0) * rate
            off = (LTCDecoder._frame_index(end, nominal) - expected) / rate   # seconds
            if abs(off) <= max(1.0, 0.001 * (t1 - t0)):
                return ""
            span = f"{abs(off) / 60:.1f} min" if abs(off) >= 120 else f"{abs(off):.1f} s"
            m, sec = divmod(int(tail_start), 60)
            return (f"Timecode jumps {'+' if off > 0 else '-'}{span} between 0:10 and {m}:{sec:02d} "
                    f"(re-jam or reset?) - the rest of this clip is placed by its start timecode")
        except Exception:
            return ""

    def _extract_channel(self, path: str, channel: int, duration: float = 10.0) -> Optional[str]:
        """Extract a single audio channel from media file (legacy file-based method).

        Note: Prefer _extract_channel_to_memory() for better performance.
        This method is kept for compatibility with code that expects file paths.

        Args:
            path: Path to media file
            channel: Audio channel index (0-based)
            duration: Seconds of audio to extract (default 10s for fast LTC detection)
        """
        if not FFMPEG_AVAILABLE:
            return None
        out_path = os.path.join(self.temp_dir, f"ch_{id(path)}_{channel}.wav")
        try:
            # Only extract first N seconds for fast LTC detection
            # -t after -i limits output duration
            cmd = [FFMPEG_PATH, '-y', '-i', path, '-vn',
                   '-t', str(duration),
                   '-af', f'pan=mono|c0=c{channel}',
                   '-acodec', 'pcm_s16le', '-ar', '48000', out_path]
            kwargs = {'capture_output': True, 'text': True, 'timeout': 60}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, **kwargs)
            return out_path if result.returncode == 0 and os.path.exists(out_path) else None
        except Exception:
            return None

    def _read_wav(self, path: str) -> Tuple[np.ndarray, int]:
        """Read WAV file to numpy array (legacy method for file-based extraction)."""
        try:
            with wave.open(path, 'rb') as wf:
                sr = wf.getframerate()
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                if wf.getnchannels() > 1:
                    data = data.reshape(-1, wf.getnchannels())[:, 0]
                return data.astype(np.float32) / 32768.0, sr
        except:
            return np.array([]), 48000

    def _detect_ltc(self, samples: np.ndarray, sr: int, expected_fps: float = None) -> Dict:
        """Detect LTC timecode that starts at the beginning of `samples` (within about 2 s)."""
        return self._detect_ltc_at(samples, sr, expected_fps)

    def _detect_ltc_late(self, samples: np.ndarray, sr: int, expected_fps: float = None,
                         scan_from: float = 2.0) -> Dict:
        """LTC that starts LATER in the audio (a camera rolling before the LTC cable is connected, a recorder
        started before the unit was on): it used to come back as 'no LTC' once it began after about 2 s.
        Searches 2 s windows from scan_from on; a window counts only when it holds several frames that count
        up consistently (speech never does), and then the audio from there is decoded as usual. sample_pos /
        ltc_offset are relative to the start of `samples`."""
        none = {'found': False, 'frames': 0, 'fps': None, 'first': None, 'last': None, 'drop_frame': False}
        win = 2 * sr
        rates = list(dict.fromkeys(r for r in (expected_fps, 30.0, 25.0) if r))
        for start in range(int(scan_from * sr), len(samples) - sr, win):
            seg = samples[start:start + win]
            if len(seg) < sr or float(np.max(np.abs(seg))) < 0.002:
                continue   # silence
            hit = False
            for r in rates:
                _, _, dec = try_decode_with_fps(seg, sr, r, quick_check=True)
                if dec.consistent_frames(r):
                    hit = True
                    break
            if not hit:
                continue
            late = self._detect_ltc_at(samples[start:], sr, expected_fps, base=start)
            if late['found']:
                return late
        return none

    def _detect_ltc_at(self, samples: np.ndarray, sr: int, expected_fps: float = None, base: int = 0) -> Dict:
        """Detect LTC timecode - optimized for fast detection from short samples. `base` = where `samples`
        starts in the file (added to sample_pos)."""
        result_template = {
            'found': False,
            'frames': 0,
            'fps': None,
            'first': None,
            'last': None,
            'drop_frame': False
        }

        def first_frame_start(decoder, run, fps):
            """Sample where the run's first LTC frame STARTS in this audio.

            Each decoded frame's last edge is known to the sample
            (frame_end_samples); frame k of the run ends (k+1) frame periods
            after the first one starts, so every frame gives one estimate and
            their median is exact to a fraction of a millisecond. Without it
            a file was placed as if its first decoded frame began at sample 0
            - 1-2 frames late, by a different amount per file (measured 31,
            45 and 65 ms on one shoot)."""
            ends = {id(f): e for f, e in zip(decoder.all_frames, decoder.frame_end_samples)}
            nom = int(round(fps))
            def idx(f):
                i = ((f['hours'] * 60 + f['minutes']) * 60 + f['seconds']) * nom + f['frames']
                if f.get('drop_frame') and nom in (30, 60):
                    m = f['hours'] * 60 + f['minutes']
                    i -= (2 if nom == 30 else 4) * (m - m // 10)
                return i
            i0 = idx(run[0])
            est = [ends[id(f)] / sr - (idx(f) - i0 + 1) / fps for f in run if id(f) in ends]
            if len(est) < 3:
                return None
            est = np.array(est)
            med = float(np.median(est))
            good = est[np.abs(est - med) < 0.002]
            if len(good) < 3:
                return None
            start = float(good.mean())
            return int(round(start * sr)) if -0.5 / fps < start < 30.0 else None

        def accept(decoder, fps):
            # Only a run of frames that count up consistently is LTC; a
            # couple of stray decodes from a mic channel is not (see
            # LTCDecoder.consistent_frames). first/last come from the run.
            run = decoder.consistent_frames(fps) if decoder else []
            if not run:
                return None
            first = dict(run[0])
            # drop frame by majority of the run (one frame with a flipped DF bit must not decide)
            df = sum(1 for f in run if f['drop_frame']) * 2 > len(run)
            first['drop_frame'] = df
            # user bits (date + camera ID) by majority of the run too: one bad bit in the FIRST frame used to
            # give the whole clip the wrong date (TC + Date: the wrong day) or camera
            for key in ('ub_month', 'ub_day', 'ub_year', 'ub_camera_id', 'user_bits'):
                votes = [f[key] for f in run if f.get(key) is not None]
                if votes:
                    first[key] = Counter(votes).most_common(1)[0][0]   # a tie keeps the earliest value
            pos = first_frame_start(decoder, run, fps)
            if pos is not None:
                first['sample_pos'] = max(0, pos) + base
            elif base:
                first['sample_pos'] = base
            return {
                'found': True,
                'frames': len(run),
                'fps': fps,
                'first': first,
                'last': run[-1],
                'drop_frame': df
            }

        # If expected FPS is set, try that first (fastest path)
        if expected_fps:
            count, frames, decoder = try_decode_with_fps(samples, sr, expected_fps)
            result = accept(decoder, expected_fps)
            if result:
                return result

        # Use multi-FPS detection - tries common frame rates
        decoder, detected_fps = detect_ltc_multi_fps(samples, sr)
        result = accept(decoder, detected_fps)
        if result:
            return result

        return result_template

    def analyze(self, path: str, expected_fps: float = None, camera_id_source: str = "LTC User Bits") -> MediaClip:
        clip = MediaClip(path=path, filename=os.path.basename(path))
        # Set initial audio-only flag based on file extension
        clip.is_audio_only = self.is_audio_file(path)

        try:
            clip.status = "analyzing"

            # Detect camera ID from filename (may be overridden by LTC user bits later)
            filename_camera_id = detect_camera_id(clip.filename)

            # Get file date (use creation time, not modified time - Apply LTC changes modified date)
            clip.file_date = datetime.fromtimestamp(os.path.getctime(path))

            info = self._run_ffprobe(path)
            if not info:
                clip.status = "error"
                clip.error = "Cannot read file (FFmpeg required)"
                return clip

            fmt = info.get('format', {})
            clip.duration = float(fmt.get('duration', 0))
            clip.original_duration = clip.duration  # Store original file duration (may be updated below)

            total_channels = 0
            has_video = False
            streams = info.get('streams', [])

            for idx, stream in enumerate(streams):
                codec_type = stream.get('codec_type', 'unknown')
                channels = stream.get('channels', 0)

                if codec_type == 'audio':
                    total_channels += channels
                    if channels > 0:
                        clip.sample_rate = int(stream.get('sample_rate', 48000))
                        clip.audio_channels = channels
                        # Get sample-accurate duration from nb_samples or duration_ts
                        # nb_samples is for some formats, duration_ts is for WAV/PCM
                        nb_samples = stream.get('nb_samples')
                        duration_ts = stream.get('duration_ts')
                        if nb_samples and clip.sample_rate > 0:
                            clip.sample_count = int(nb_samples)
                            sample_duration = clip.sample_count / clip.sample_rate
                            clip.original_duration = sample_duration
                        elif duration_ts and clip.sample_rate > 0:
                            # duration_ts is the sample count for WAV/PCM files
                            clip.sample_count = int(duration_ts)
                            sample_duration = clip.sample_count / clip.sample_rate
                            clip.original_duration = sample_duration
                        elif stream.get('duration'):
                            # Fall back to stream duration if available
                            stream_duration = float(stream.get('duration'))
                            clip.original_duration = stream_duration
                elif codec_type == 'video':
                    # Skip thumbnail/cover art streams (usually small or attached_pic)
                    disposition = stream.get('disposition', {})
                    if disposition.get('attached_pic', 0) == 1:
                        continue
                    has_video = True
                    clip.width = stream.get('width', 1920)
                    clip.height = stream.get('height', 1080)
                    clip.video_fps = stream.get('r_frame_rate', '30/1')

            # Mark audio-only files
            clip.is_audio_only = not has_video

            # Fallback: Get sample count from WAV file directly if ffprobe didn't provide nb_samples
            if clip.sample_count == 0:
                ext = Path(path).suffix.lower()
                if ext in {'.wav', '.wave', '.bwf'}:
                    clip.sample_count = self._get_wav_sample_count(path)
                    if clip.sample_count > 0:
                        # Calculate sample-accurate duration
                        sample_duration = clip.sample_count / clip.sample_rate
                        clip.original_duration = sample_duration

            # Detect LTC in each channel
            best_channel, best_result = -1, {}
            channels_scanned = 0

            if total_channels == 0:
                clip.status = "no_audio"
                clip.error = "No audio tracks found"
            else:
                # ONE FFmpeg run pulls 10s of every channel of the audio stream
                # FFmpeg picks by default (most channels, first on a tie - the
                # stream the per-channel `pan` extraction below reads). Each
                # channel is then scanned from memory; detect_ltc_multi_fps
                # rejects non-LTC channels within its first 2 seconds.
                audio_streams = [s for s in streams
                                 if s.get('codec_type') == 'audio' and s.get('channels', 0) > 0]
                stream = max(audio_streams, key=lambda s: s.get('channels', 0)) if audio_streams else None
                block, sr = (self._extract_stream_to_memory(path, stream.get('index', 0), stream['channels'])
                             if stream and 'index' in stream else (np.zeros((0, 1), np.float32), 48000))
                if len(block):
                    for ch in range(min(block.shape[1], 8)):
                        channels_scanned += 1
                        result = self._detect_ltc(np.ascontiguousarray(block[:, ch]), sr, expected_fps)
                        if result['found'] and result['frames'] > best_result.get('frames', 0):
                            best_channel, best_result = ch, result
                            # Stop scanning once we find good LTC (2+ frames)
                            if result['frames'] >= 2:
                                break

                # Fallback: per-channel extraction (2-tier: 3s quick, 10s if LTC)
                for ch in ([] if len(block) else range(min(total_channels, 8))):
                    # Quick 3-second extraction first
                    samples, sr = self._extract_channel_to_memory(path, ch, duration=3.0)
                    if len(samples) > 0:
                        channels_scanned += 1
                        result = self._detect_ltc(samples, sr, expected_fps)
                        if result['found']:
                            # LTC found in quick sample - extract more for better accuracy
                            samples, sr = self._extract_channel_to_memory(path, ch, duration=10.0)
                            if len(samples) > 0:
                                result = self._detect_ltc(samples, sr, expected_fps)
                            if result['frames'] > best_result.get('frames', 0):
                                best_channel, best_result = ch, result
                                # Stop scanning once we find good LTC (2+ frames)
                                if result['frames'] >= 2:
                                    break

                # No LTC on any channel at the start: it may begin later (cable connected after the camera started
                # rolling). One more look through the first 30 s - only for files where nothing was found.
                if best_channel < 0 and stream and 'index' in stream and clip.duration > 3.0:
                    block2, sr2 = self._extract_stream_to_memory(path, stream['index'], stream['channels'], duration=30.0)
                    for ch in range(min(block2.shape[1], 8) if len(block2) else 0):
                        result = self._detect_ltc_late(np.ascontiguousarray(block2[:, ch]), sr2, expected_fps)
                        if result['found'] and result['frames'] > best_result.get('frames', 0):
                            best_channel, best_result = ch, result
                            if result['frames'] >= 2:
                                break

                if best_channel >= 0 and best_result.get('found'):
                    clip.ltc_channel = best_channel
                    clip.fps = best_result.get('fps', 30.0) or 30.0
                    clip.drop_frame = best_result.get('drop_frame', False)

                    first, last = best_result.get('first'), best_result.get('last')
                    if first:
                        clip.start_tc = f"{first['hours']:02d}:{first['minutes']:02d}:{first['seconds']:02d}:{first['frames']:02d}"
                        clip.start_frames = clip.tc_to_frames(clip.start_tc)
                        
                        # Store LTC offset (samples from start of file to first detected LTC frame)
                        # This is critical for accurate sync - the file actually starts BEFORE the detected timecode
                        if 'sample_pos' in first:
                            clip.ltc_offset = int(first['sample_pos'])

                        # Extract user bits (MMDDYYCC format) - camera ID from last 2 digits
                        if 'user_bits' in first:
                            clip.user_bits = first['user_bits']
                            if first.get('ub_camera_id') is not None:
                                clip.ub_camera_id = first['ub_camera_id']
                            # Store date from user bits
                            if first.get('ub_month') and first.get('ub_day') and first.get('ub_year') is not None:
                                clip.ub_date = f"{first['ub_month']:02d}{first['ub_day']:02d}{first['ub_year']:02d}"

                        # Apply camera ID based on priority setting
                        # Skip user bits camera ID if 0 (treat as "no camera assigned")
                        ub_cam = f"Cam {clip.ub_camera_id:02d}" if clip.ub_camera_id and clip.ub_camera_id > 0 else None

                        if camera_id_source == "LTC User Bits":
                            # LTC User Bits priority: use user bits if available, else filename
                            clip.camera_id = ub_cam if ub_cam else filename_camera_id
                        else:
                            # Filename priority: use filename if available, else user bits
                            clip.camera_id = filename_camera_id if filename_camera_id else ub_cam

                    if last:
                        clip.end_tc = f"{last['hours']:02d}:{last['minutes']:02d}:{last['seconds']:02d}:{last['frames']:02d}"
                    clip.status = "done"
                    clip.ltc_warning = self._check_ltc_continuity(path, clip, stream, best_channel, best_result, expected_fps)

                    # Read embedded camera timecode from file metadata
                    # This is needed for FCPXML export - DaVinci Resolve matches files by embedded TC
                    clip.embedded_tc, clip.embedded_tc_frames, clip.bwf_time_reference = self._get_embedded_timecode(path, clip.fps)
                    if not clip.embedded_tc:
                        # Fall back to LTC timecode if no embedded TC found
                        clip.embedded_tc = clip.start_tc
                        clip.embedded_tc_frames = clip.start_frames
                else:
                    # No LTC found
                    clip.status = "no_ltc"
                    clip.error = f"No LTC found in {channels_scanned} audio channel(s)"

                    # No LTC means no user bits - use filename camera ID
                    clip.camera_id = filename_camera_id

                    # Without LTC, take the frame rate from the video metadata
                    # instead of leaving the 30fps dataclass default - the
                    # timeline chip, embedded-TC frame math and exports all
                    # read clip.fps (fixes e.g. a 50fps clip showing 30fps).
                    if not clip.is_audio_only and clip.video_fps and '/' in clip.video_fps:
                        try:
                            num, denom = map(int, clip.video_fps.split('/'))
                            if denom > 0:
                                metadata_fps = num / denom
                                if 10.0 <= metadata_fps <= 240.0:
                                    clip.fps = metadata_fps
                        except (ValueError, ZeroDivisionError):
                            pass

                    # Audio-only files have no video metadata rate: use the
                    # project's expected fps so BWF seconds->frames conversion
                    # is on the right base (the 30.0 dataclass default broke
                    # 25/23.976 projects - FIXES_LOG #22 regression path)
                    if clip.is_audio_only and expected_fps and expected_fps > 0:
                        clip.fps = expected_fps

                    # Still try to read embedded TC even without LTC
                    # This allows syncing via embedded TC when LTC is not available
                    clip.embedded_tc, clip.embedded_tc_frames, clip.bwf_time_reference = self._get_embedded_timecode(path, clip.fps if clip.fps > 0 else 24.0)

            # Check for linked audio
            base = os.path.splitext(path)[0]
            for ext in self.SUPPORTED_AUDIO:
                audio_path = base + ext
                if os.path.exists(audio_path):
                    clip.has_linked_audio = True
                    clip.linked_audio_path = audio_path
                    break

        except Exception as e:
            clip.status = "error"
            clip.error = str(e)

        return clip


# =============================================================================
# NLE XML Exporters
# =============================================================================

def _read_mono(path: str, sample: int, count: int, channel: int):
    """`count` samples of one channel from `sample` on, as float32 (+ sample
    rate). PCM WAV is read directly (sample-exact); anything else via FFmpeg."""
    try:
        with wave.open(path, 'rb') as w:
            sr, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            w.setpos(max(0, min(sample, w.getnframes())))
            raw = w.readframes(count)
        if sw == 3:
            b = np.frombuffer(raw, np.uint8)[: len(raw) // 3 * 3].reshape(-1, 3)
            x = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
            x = np.where(x >= 1 << 23, x - (1 << 24), x).astype(np.float32) / float(1 << 23)
        elif sw == 2:
            x = np.frombuffer(raw, '<i2').astype(np.float32) / 32768.0
        elif sw == 4:
            x = np.frombuffer(raw, '<i4').astype(np.float32) / 2147483648.0
        else:
            raise ValueError
        x = x[: len(x) // nch * nch].reshape(-1, nch)
        return x[:, min(channel, nch - 1)], sr
    except Exception:
        pass
    sr = 48000
    kw = {'capture_output': True}
    if os.name == 'nt':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run([FFMPEG_PATH, '-v', 'error', '-ss', f'{sample / sr:.6f}', '-i', path, '-map', '0:a:0',
                        '-t', f'{count / sr:.6f}', '-af', f'pan=mono|c0=c{channel}', '-ar', str(sr),
                        '-f', 'f32le', '-'], **kw)
    return np.frombuffer(r.stdout, np.float32), sr


def ltc_seconds_at(path: str, sample: int, channel: int, fps: float, drop_frame: bool = False,
                   total: int = 0, window: float = 4.0) -> Optional[float]:
    """Timecode at sample `sample` of an audio file, in seconds (frame count /
    fps), from the LTC on `channel` - to a fraction of a millisecond: every LTC
    frame decoded in a short window gives (frame index + 1)/fps minus the time
    its last edge arrives; their median is the timecode at the window start.
    None when there is no readable LTC there."""
    if not NUMPY_AVAILABLE:
        return None
    sr_guess = 48000
    win = int(window * sr_guess)
    start = max(0, min(sample, (total - win) if total > win else 0))
    x, sr = _read_mono(path, start, win, channel)
    if len(x) < sr:
        return None
    dec = LTCDecoder(sr, expected_fps=fps)
    dec.process_samples(x)
    frames, ends = dec.all_frames, dec.frame_end_samples
    n = min(len(frames), len(ends))
    if n < 10:
        return None
    nom = int(round(fps))
    vals = []
    for f, e in zip(frames[:n], ends[:n]):
        idx = ((f['hours'] * 60 + f['minutes']) * 60 + f['seconds']) * nom + f['frames']
        if drop_frame and nom in (30, 60):
            mins = f['hours'] * 60 + f['minutes']
            idx -= (2 if nom == 30 else 4) * (mins - mins // 10)
        vals.append((idx + 1) / fps - e / sr)
    vals = np.array(vals)
    med = float(np.median(vals))
    good = vals[np.abs(vals - med) < 0.002]
    if len(good) < 10:
        return None
    return float(good.mean()) + (sample - start) / sr


class XMLExporter:
    """Exports synchronized clips to NLE XML formats."""

    @staticmethod
    def audio_recorder_track(clip, key: str) -> str:
        """Which recorder track a recording belongs to: same folder (= same recorder's card) + same file
        name pattern (recorder model, e.g. ZOOM) + same track (LR, Tr1, ...). Takes only share a timeline
        track when this matches, so a second recorder always gets tracks of its own."""
        folder = os.path.dirname(os.path.abspath(clip.path))
        take = os.path.basename(folder).lower()
        stem = os.path.splitext(os.path.basename(clip.path))[0].lower()
        if take and (take == (clip.recording_id or '').lower() or stem == take or
                     stem.startswith(take + '_') or stem.startswith(take + '-')):
            # one folder per take (Zoom: .../ZOOM0038/ZOOM0038_LR.WAV): the recorder is the folder above
            folder = os.path.dirname(folder)
        folder = os.path.normcase(folder)
        if clip.recording_id:
            device = re.sub(r'\d+$', '', clip.recording_id) or clip.recording_id
            track = 'LR' if not clip.track_number else f"Tr{clip.track_number}"
            return f"{folder}|{device}|{track}"
        name = key[-1] if isinstance(key, tuple) else key
        return f"{folder}|{re.sub(r'[0-9]+', '#', name)}"

    @staticmethod
    def assign_audio_lanes(clips, key_of, pack: bool = False, margin: float = 0.0) -> dict:
        """{recording key: audio track index (0 = first)} for the audio-only clips.

        A recording key (one recorder track of one take, all its split parts) is the unit.
        pack=False: every recording gets its own track (order of first appearance) - the old behaviour.
        pack=True:  recordings of the SAME recorder track share a track when their time ranges don't
                    overlap; a new track opens only when they would overlap. Different recorders never
                    share a track. Recordings without a sync timecode keep a track of their own."""
        spans, order = {}, []
        for c in clips:
            if not c.is_audio_only:
                continue
            k = key_of(c)
            s0 = c.timeline_start
            e0 = s0 + (c.duration or 0.0)
            if k not in spans:
                spans[k] = [s0, e0, c, XMLExporter._has_sync_tc(c)]
                order.append(k)
            else:
                sp = spans[k]
                sp[0], sp[1] = min(sp[0], s0), max(sp[1], e0)
                sp[3] = sp[3] and XMLExporter._has_sync_tc(c)
        lanes = {}
        if not pack:
            for i, k in enumerate(order):
                lanes[k] = i
            return lanes
        groups, group_order = {}, []
        for k in order:
            sp = spans[k]
            role = XMLExporter.audio_recorder_track(sp[2], k) if sp[3] else f"__own__{k}"
            if role not in groups:
                groups[role] = []
                group_order.append(role)
            groups[role].append(k)
        nxt = 0
        for role in group_order:
            tracks = []   # [[track index, end time of the last take on it]]
            for k in sorted(groups[role], key=lambda kk: spans[kk][0]):
                start, end = spans[k][0], spans[k][1]
                for t in tracks:
                    if start >= t[1] - 0.001 + margin:   # no overlap (touching is fine; margin = required gap)
                        lanes[k] = t[0]
                        t[1] = end
                        break
                else:
                    lanes[k] = nxt
                    tracks.append([nxt, end])
                    nxt += 1
        return lanes

    @staticmethod
    def _frac(v: str):
        """'1234/25s' -> Fraction"""
        from fractions import Fraction
        n, d = v[:-1].split('/')
        return Fraction(int(n), int(d))

    @staticmethod
    def _frac_str(f, denom: int = 0) -> str:
        """Fraction -> 'n/ds', in the timeline's units when it fits them exactly (as Resolve writes it)"""
        if denom and (f * denom).denominator == 1:
            return f"{int(f * denom)}/{denom}s"
        return f"{f.numerator}/{f.denominator}s"

    @staticmethod
    def _export_audio_key(clip) -> tuple:
        """Recording key the exporters group audio by: (folder, recording track). Split parts (-0001..)
        stay together; the folder keeps two recorders with the same file names (ZOOM0001 on both cards)
        apart."""
        folder = os.path.normcase(os.path.dirname(os.path.abspath(clip.path)))
        if clip.recording_id:
            if clip.track_number == 0:
                return (folder, f"{clip.recording_id}_LR")
            return (folder, f"{clip.recording_id}_Tr{clip.track_number or 0}")
        return (folder, re.sub(r'-\d{4}$', '', os.path.splitext(clip.filename)[0]))

    @staticmethod
    def _has_sync_tc(clip) -> bool:
        """True when the clip carries a usable sync TC: LTC OR embedded.

        Export gates must not require LTC only - clips synced by embedded
        camera TC (no LTC) were previously dropped from the export or exiled
        to the end of the timeline."""
        if clip.start_tc:
            return True
        return bool(clip.embedded_tc and clip.embedded_tc != "00:00:00:00")

    @staticmethod
    def _tc_units(frames: int, clip_fps: float, frame_num: int, frame_denom: int) -> int:
        """Convert a frame count in the CLIP's own fps base to FCPXML units
        of the SEQUENCE rate. Identical to frames*frame_num when the bases
        match; prevents a 50fps clip's embedded TC from landing at 2x the
        real time in a 25fps sequence."""
        fps = clip_fps if clip_fps and clip_fps > 0 else (frame_denom / frame_num)
        nominal = round(fps)
        if nominal in (24, 30, 48, 60) and abs(fps - nominal * 1000 / 1001) < 0.005:
            fps = nominal * 1000 / 1001   # 29.97 is really 30000/1001: rounded it put an 11:00 TC 1.2 frames late
        return int(round(frames / fps * frame_denom))

    @staticmethod
    def exact_fps(fps: float) -> float:
        """29.97 -> 30000/1001, 23.976 -> 24000/1001, 59.94 -> 60000/1001 (the rounded value is 1 ppm off:
        36 ms at 10:00:00:00); any other rate unchanged."""
        nominal = round(fps) if fps else 0
        if nominal in (24, 30, 48, 60) and abs(fps - nominal * 1000 / 1001) < 0.005:
            return nominal * 1000 / 1001
        return fps

    @staticmethod
    def tc_frames_at(tc_str, base_fps, tc_rate=None, exact: bool = False):
        """Frame count (base_fps base) of a camera timecode whose frame field counts frames at tc_rate.
        A 50p camera writes '11:26:10:37' (frame 37 of 50) - in a 25 fps project that is 10:18.5, not
        10:37 counted at 25 (0.74 s later). Drop-frame (';', 29.97 / 59.94 DF) skips its dropped labels -
        2 / 4 per minute except every 10th - like the LTC path (counted as NDF, 11:00 DF was 40 s off).
        tc_rate None or equal to the base and no DF: plain HH:MM:SS:FF."""
        tc_str = (tc_str or '').strip()
        drop = ';' in tc_str
        parts = tc_str.replace(';', ':').split(':')
        if len(parts) != 4:
            return 0
        h, m, sec, f = map(int, parts)
        base = int(round(base_fps)) if base_fps and base_fps > 0 else 30
        rate = int(round(tc_rate)) if tc_rate and tc_rate > 0 else base
        idx = (h * 3600 + m * 60 + sec) * rate + f
        if drop and rate in (30, 60):
            minutes = h * 60 + m
            idx -= (rate // 15) * (minutes - minutes // 10)
        if exact:
            return idx * base / rate   # float: where between two base frames it really starts
        return idx if rate == base else int(round(idx * base / rate))

    @staticmethod
    def fit_to_grid(clips) -> list:
        """For an export: every video clip whose camera TC starts BETWEEN two frames of its base rate (a 50p
        clip on an odd frame in a 25 fps project) is moved to the first whole frame inside the file, its end
        to the last whole frame inside it, and placed that bit later on the timeline (sync unchanged).
        Rounding to the nearest frame put the source range up to half a frame outside the file - Resolve
        (iPad) then can't relink it by timecode. Returns [(clip, timeline_start, duration, embedded_tc_frames)]
        to put back afterwards."""
        saved = []
        for c in clips:
            if (c.is_audio_only or not c.embedded_tc or c.embedded_tc == c.start_tc
                    or getattr(c, 'bwf_time_reference', 0) or not c.fps or c.duration <= 0):
                continue
            rate = XMLExporter.video_tc_rate(c.video_fps)
            if not rate or round(rate) == round(c.fps):
                continue
            exact = XMLExporter.tc_frames_at(c.embedded_tc, c.fps, rate, exact=True)
            first = math.ceil(exact - 1e-6)
            last = math.floor(exact + c.duration * c.fps + 1e-6)
            if abs(first - exact) < 1e-6 or last <= first:
                continue
            saved.append((c, c.timeline_start, c.duration, c.embedded_tc_frames, c.original_duration))
            c.timeline_start += (first - exact) / c.fps
            c.embedded_tc_frames = first
            c.duration = (last - first) / c.fps
            if c.original_duration > 0:   # the asset (whole file) length
                c.original_duration = (math.floor(exact + c.original_duration * c.fps + 1e-6) - first) / c.fps
        return saved

    @staticmethod
    def video_tc_rate(video_fps):
        """The video stream's frame rate from an ffprobe rate string ('50/1', '60000/1001'), else None."""
        try:
            num, den = map(int, str(video_fps).split('/'))
            r = num / den if den else 0
            return r if 10.0 <= r <= 240.0 else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def gap_shifts(clips, fps: float, keep_s: float = 0.0) -> dict:
        """'Remove Gaps': {id(clip): seconds to move it earlier}. Clips that overlap (or touch) form one take
        and move together by the same WHOLE number of frames, so sync inside a take never changes; the empty
        time between takes shrinks to keep_s. Clips without a sync position are left alone."""
        rate = fps if fps and fps > 0 else 25.0
        nominal = round(rate)
        if nominal in (24, 30, 48, 60) and abs(rate - nominal * 1000 / 1001) < 0.005:
            rate = nominal * 1000 / 1001
        frame = 1.0 / rate
        placed = sorted((c for c in clips if XMLExporter._has_sync_tc(c) and c.timeline_start >= -0.9),
                        key=lambda c: c.timeline_start)
        shifts = {}
        removed = 0.0
        take_end = None
        for c in placed:
            start, end = c.timeline_start, c.timeline_start + max(c.duration, 0.0)
            if take_end is not None and start > take_end + frame:   # a new take after a gap
                gap = start - take_end - max(keep_s, 0.1)   # >= 0.1 s: touching takes of one recorder would need extra multicam audio angles
                if gap > 0:
                    removed += int(gap / frame + 1e-6) * frame
            shifts[id(c)] = removed
            take_end = end if take_end is None else max(take_end, end)
        return shifts

    @staticmethod
    def camera_audio_key(clip) -> str:
        """Camera a video clip's audio belongs to - the same name the
        timeline shows on that camera's track ("Cam 01", or "Video N")."""
        return clip.camera_id or f"Video {getattr(clip, 'track_index', 0) + 1}"

    @staticmethod
    def _contiguous_split_groups(clips, in_offsets) -> list:
        """Split recordings (-0001, -0002, ...) whose parts lie end to end as
        recorded: 2+ audio parts of one recording+track, only the first part
        IN-trimmed and only the last part OUT-trimmed. These are exported
        side by side at exact SAMPLE positions (see export_resolve)."""
        def off(c):
            return (in_offsets or {}).get(c.path + f"_split_{id(c)}", (in_offsets or {}).get(c.path, 0)) or 0
        groups = {}
        for c in clips:
            if c.is_audio_only and c.recording_id and c.track_number is not None:
                groups.setdefault((c.recording_id, c.track_number), []).append(c)
        result = []
        for parts in groups.values():
            if len(parts) < 2 or not any(c.split_part for c in parts):
                continue
            parts = sorted(parts, key=lambda c: c.split_part or 0)
            ok = len({c.path for c in parts}) == len(parts)
            for k, c in enumerate(parts):
                if not ok:
                    break
                file_s = (c.sample_count / c.sample_rate) if c.sample_count and c.sample_rate else c.original_duration
                ok = ((k == 0 or abs(off(c)) < 1e-6)
                      and (k == len(parts) - 1 or abs((file_s - off(c)) - c.duration) < 0.002)
                      and (k == 0 or abs(c.timeline_start - (parts[k - 1].timeline_start
                                                             + parts[k - 1].duration)) < 0.002))
            if ok:
                result.append(parts)
        return result

    @staticmethod
    def _camera_audio_channels(clip, mute_ltc: bool, include_camera_audio: bool,
                               camera_audio_selection: dict = None) -> List[int]:
        """1-based channels of a VIDEO clip's own audio to put on the timeline.

        Every returned channel is exported as its own MONO item, so a voice
        recorded on one side of a stereo camera track sits centred instead of
        hard-panned next to the LTC. camera_audio_selection maps
        camera_audio_key -> list of channels; cameras missing from it keep
        every channel. The LTC channel is never exported while mute_ltc is on,
        even if selected, and a camera whose only channel is LTC exports no
        audio (it used to fall back to channel 1 = the LTC itself).
        """
        if not include_camera_audio or clip.is_audio_only or clip.status == "no_audio":
            return []
        num_channels = clip.audio_channels if clip.audio_channels and clip.audio_channels > 0 else 2
        chosen = (camera_audio_selection or {}).get(XMLExporter.camera_audio_key(clip))
        channels = range(1, num_channels + 1) if chosen is None else sorted(set(chosen))
        return [ch for ch in channels
                if 1 <= ch <= num_channels
                and not (mute_ltc and clip.ltc_channel >= 0 and ch == clip.ltc_channel + 1)]

    @staticmethod
    def export_premiere(clips: List[MediaClip], output_path: str, fps: float = 30.0,
                        mute_ltc: bool = True, include_camera_audio: bool = False,
                        split_stereo: bool = False, in_offsets: dict = None,
                        camera_audio_selection: dict = None, pack_audio: bool = False):
        """Export Adobe Premiere Pro XML (XMEML 4 format).

        This format matches Sidus Link export format for Premiere Pro compatibility.

        Args:
            mute_ltc: If True, mute the LTC audio channel in video clips.
            include_camera_audio: If True, put camera audio from video clips on
                the sequence (one mono track per kept channel).
            split_stereo: If True, split stereo audio into 2 mono channels. If False, insert raw audio directly.
            camera_audio_selection: camera_audio_key -> channels to keep
                (see XMLExporter._camera_audio_channels).
        """
        import urllib.parse
        import uuid

        # Get output filename for naming
        output_basename = os.path.splitext(os.path.basename(output_path))[0]

        # Priority 1: Check for manual FPS override on any clip
        for clip in clips:
            if getattr(clip, 'fps_override', False) and clip.fps > 0:
                fps = clip.fps
                break
        else:
            # Priority 2: the MOST COMMON video rate (it was the first video clip's: one 50p clip at the
            # top of a 25p project made the whole sequence 50p)
            rates = {}
            for clip in clips:
                if not clip.is_audio_only and clip.video_fps:
                    r = XMLExporter.video_tc_rate(clip.video_fps)
                    if r and r < 120:
                        rates[round(r, 3)] = rates.get(round(r, 3), 0) + 1
            if rates:
                fps = max(rates, key=lambda r: (rates[r], -r))   # a tie goes to the lower rate

        # Determine if NTSC (29.97, 23.976, 59.94)
        is_ntsc = abs(fps - 29.97) < 0.1 or abs(fps - 23.976) < 0.1 or abs(fps - 59.94) < 0.1
        timebase = int(round(fps))

        # Determine drop frame from clips
        is_drop_frame = any(clip.drop_frame for clip in clips if hasattr(clip, 'drop_frame'))
        display_format = "DF" if is_drop_frame else "NDF"

        # Calculate earliest LTC timecode (timeline base)
        earliest_ltc_frames = None
        for clip in clips:
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                ltc_frames = round(clip.timeline_start * fps)
                if earliest_ltc_frames is None or ltc_frames < earliest_ltc_frames:
                    earliest_ltc_frames = ltc_frames
        if earliest_ltc_frames is None:
            earliest_ltc_frames = 0

        # Calculate total duration
        total_duration = 0
        for clip in clips:
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                clip_end = round((clip.timeline_start + clip.duration) * fps)
                if clip_end > total_duration:
                    total_duration = clip_end
        total_duration_frames = total_duration - earliest_ltc_frames if total_duration > earliest_ltc_frames else round(max(c.duration for c in clips) * fps)

        # Get resolution from first video clip
        width, height = 1920, 1080
        for clip in clips:
            if not clip.is_audio_only and hasattr(clip, 'width') and clip.width:
                width = clip.width
                height = clip.height if hasattr(clip, 'height') and clip.height else 1080
                break

        # Create root element
        root = ET.Element('xmeml', version="4")

        # Create main bin
        main_bin = ET.SubElement(root, 'bin')
        ET.SubElement(main_bin, 'name').text = output_basename
        ET.SubElement(main_bin, 'updatebehavior').text = "add"
        ET.SubElement(main_bin, 'uuid').text = "{" + str(uuid.uuid4()).upper() + "}"
        main_children = ET.SubElement(main_bin, 'children')

        # Create "Original Media" bin for master clips
        media_bin = ET.SubElement(main_children, 'bin')
        ET.SubElement(media_bin, 'name').text = "Original Media"
        ET.SubElement(media_bin, 'updatebehavior').text = "add"
        ET.SubElement(media_bin, 'uuid').text = "{" + str(uuid.uuid4()).upper() + "}"
        media_children = ET.SubElement(media_bin, 'children')

        # Helper to add rate element
        def add_rate(parent):
            rate = ET.SubElement(parent, 'rate')
            ET.SubElement(rate, 'ntsc').text = "TRUE" if is_ntsc else "FALSE"
            ET.SubElement(rate, 'timebase').text = str(timebase)
            return rate

        # Track file IDs for referencing
        file_ids = {}

        # Create master clips in Original Media bin
        for i, clip in enumerate(clips):
            clip_name = os.path.splitext(clip.filename)[0]
            clip_id = f"{clip_name}_clip_{i+1}"
            file_id = f"{clip_name}_{abs(hash(clip.path))}_file"
            file_ids[i] = file_id

            # Calculate duration in frames
            dur_frames = round(clip.duration * fps) if clip.duration > 0 else timebase

            # Get embedded TC frame
            if clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                embedded_frame = clip.embedded_tc_frames
            elif XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                embedded_frame = round(clip.timeline_start * fps)
            else:
                embedded_frame = 0

            # Convert frame to TC string (uses nominal integer rate for display)
            def frames_to_tc(frames, fps_val):
                fps_int = int(round(fps_val))  # 29.97->30, 23.976->24, etc.
                total_frames = int(frames)
                f = total_frames % fps_int
                total_secs = total_frames // fps_int
                s = total_secs % 60
                m = (total_secs // 60) % 60
                h = total_secs // 3600
                return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

            tc_string = frames_to_tc(embedded_frame, fps)

            # Determine if video or audio only
            ext = Path(clip.path).suffix.lower()
            video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
            has_video = ext in video_exts and not clip.is_audio_only

            # Get audio channels
            num_channels = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2

            # Create master clip
            master_clip = ET.SubElement(media_children, 'clip', id=clip_id)
            ET.SubElement(master_clip, 'name').text = clip_name
            add_rate(master_clip)
            ET.SubElement(master_clip, 'duration').text = str(dur_frames)
            ET.SubElement(master_clip, 'in').text = "0"
            ET.SubElement(master_clip, 'out').text = str(dur_frames)
            ET.SubElement(master_clip, 'masterclipid').text = clip_id
            ET.SubElement(master_clip, 'ismasterclip').text = "TRUE"

            # Logging info - include camera ID if available
            logginginfo = ET.SubElement(master_clip, 'logginginfo')
            ET.SubElement(logginginfo, 'scene').text = clip.camera_id if clip.camera_id else ""
            ET.SubElement(logginginfo, 'shottake')
            ET.SubElement(logginginfo, 'lognote')
            ET.SubElement(logginginfo, 'good').text = "FALSE"

            # Labels element for camera metadata (Premiere uses this for column display)
            labels = ET.SubElement(master_clip, 'labels')
            ET.SubElement(labels, 'label').text = clip.camera_id if clip.camera_id else ""
            ET.SubElement(labels, 'label2').text = clip.user_bits if hasattr(clip, 'user_bits') and clip.user_bits else ""

            # Media element
            clip_media = ET.SubElement(master_clip, 'media')

            # Video track (if has video)
            if has_video:
                video_elem = ET.SubElement(clip_media, 'video')
                video_track = ET.SubElement(video_elem, 'track')
                video_clipitem = ET.SubElement(video_track, 'clipitem', id=f"{clip_name}_V1_1")
                ET.SubElement(video_clipitem, 'masterclipid').text = clip_id
                ET.SubElement(video_clipitem, 'name').text = clip_name
                add_rate(video_clipitem)
                ET.SubElement(video_clipitem, 'duration').text = str(dur_frames)

                # File definition (first occurrence)
                file_elem = ET.SubElement(video_clipitem, 'file', id=file_id)
                ET.SubElement(file_elem, 'name').text = clip.filename
                # URL encode path
                path_url = "file:" + clip.path.replace('\\', '/').replace(' ', '%20')
                ET.SubElement(file_elem, 'pathurl').text = path_url
                ET.SubElement(file_elem, 'duration').text = str(dur_frames)
                add_rate(file_elem)

                # Timecode
                tc_elem = ET.SubElement(file_elem, 'timecode')
                add_rate(tc_elem)
                ET.SubElement(tc_elem, 'string').text = tc_string
                ET.SubElement(tc_elem, 'frame').text = str(embedded_frame)
                ET.SubElement(tc_elem, 'source').text = "source"
                ET.SubElement(tc_elem, 'displayformat').text = display_format
                reel = ET.SubElement(tc_elem, 'reel')
                # Use camera ID from user bits or filename for reel name
                reel_name = clip.camera_id if clip.camera_id else clip_name[:8]
                ET.SubElement(reel, 'name').text = reel_name

                # Media characteristics
                file_media = ET.SubElement(file_elem, 'media')
                file_video = ET.SubElement(file_media, 'video')
                ET.SubElement(file_video, 'duration').text = str(dur_frames)
                video_chars = ET.SubElement(file_video, 'samplecharacteristics')
                ET.SubElement(video_chars, 'width').text = str(width)
                ET.SubElement(video_chars, 'height').text = str(height)

                # Audio characteristics in file
                for ch in range(1, num_channels + 1):
                    file_audio = ET.SubElement(file_media, 'audio')
                    audio_chars = ET.SubElement(file_audio, 'samplecharacteristics')
                    ET.SubElement(audio_chars, 'samplerate').text = "48000"
                    ET.SubElement(audio_chars, 'depth').text = "16"
                    ET.SubElement(file_audio, 'channelcount').text = "1"
                    ET.SubElement(file_audio, 'layout').text = "mono"
                    audiochannel = ET.SubElement(file_audio, 'audiochannel')
                    ET.SubElement(audiochannel, 'sourcechannel').text = str(ch)
                    ET.SubElement(audiochannel, 'channellabel').text = "discrete"

            # Audio tracks in master clip
            audio_elem = ET.SubElement(clip_media, 'audio')
            for ch in range(1, num_channels + 1):
                audio_track = ET.SubElement(audio_elem, 'track')
                audio_clipitem = ET.SubElement(audio_track, 'clipitem', id=f"{clip_name}_A{ch}_1")
                ET.SubElement(audio_clipitem, 'masterclipid').text = clip_id
                ET.SubElement(audio_clipitem, 'name').text = clip_name
                add_rate(audio_clipitem)
                ET.SubElement(audio_clipitem, 'duration').text = str(dur_frames)

                # File reference (empty after first definition)
                if has_video:
                    ET.SubElement(audio_clipitem, 'file', id=file_id)
                else:
                    # For audio-only, define file in first audio track
                    if ch == 1:
                        file_elem = ET.SubElement(audio_clipitem, 'file', id=file_id)
                        ET.SubElement(file_elem, 'name').text = clip.filename
                        path_url = "file:" + clip.path.replace('\\', '/').replace(' ', '%20')
                        ET.SubElement(file_elem, 'pathurl').text = path_url
                        ET.SubElement(file_elem, 'duration').text = str(dur_frames)
                        add_rate(file_elem)

                        # Timecode for audio
                        tc_elem = ET.SubElement(file_elem, 'timecode')
                        tc_rate = ET.SubElement(tc_elem, 'rate')
                        ET.SubElement(tc_rate, 'ntsc').text = "FALSE"
                        ET.SubElement(tc_rate, 'timebase').text = "0"
                        ET.SubElement(tc_elem, 'string').text = tc_string
                        ET.SubElement(tc_elem, 'frame').text = "0"
                        ET.SubElement(tc_elem, 'source').text = "source"
                        ET.SubElement(tc_elem, 'displayformat').text = display_format

                        # Audio media
                        file_media = ET.SubElement(file_elem, 'media')
                        for ach in range(1, num_channels + 1):
                            file_audio = ET.SubElement(file_media, 'audio')
                            audio_chars = ET.SubElement(file_audio, 'samplecharacteristics')
                            ET.SubElement(audio_chars, 'samplerate').text = "48000"
                            ET.SubElement(audio_chars, 'depth').text = "24"
                            ET.SubElement(file_audio, 'channelcount').text = "1"
                            ET.SubElement(file_audio, 'layout').text = "mono"
                            audiochannel = ET.SubElement(file_audio, 'audiochannel')
                            ET.SubElement(audiochannel, 'sourcechannel').text = str(ach)
                            ET.SubElement(audiochannel, 'channellabel').text = "discrete"
                    else:
                        ET.SubElement(audio_clipitem, 'file', id=file_id)

                # Source track
                sourcetrack = ET.SubElement(audio_clipitem, 'sourcetrack')
                ET.SubElement(sourcetrack, 'mediatype').text = "audio"
                ET.SubElement(sourcetrack, 'trackindex').text = str(ch)

                # Mute the LTC channel, and any camera channel not chosen for export
                is_ltc_ch = mute_ltc and clip.ltc_channel >= 0 and (ch == clip.ltc_channel + 1)
                if is_ltc_ch or (has_video and ch not in XMLExporter._camera_audio_channels(
                        clip, mute_ltc, include_camera_audio, camera_audio_selection)):
                    # Add audio level filter to mute
                    filter_elem = ET.SubElement(audio_clipitem, 'filter')
                    effect = ET.SubElement(filter_elem, 'effect')
                    ET.SubElement(effect, 'name').text = "Audio Levels"
                    ET.SubElement(effect, 'effectid').text = "audiolevels"
                    ET.SubElement(effect, 'effectcategory').text = "audiolevels"
                    ET.SubElement(effect, 'effecttype').text = "audiolevels"
                    ET.SubElement(effect, 'mediatype').text = "audio"
                    param = ET.SubElement(effect, 'parameter')
                    ET.SubElement(param, 'name').text = "Level"
                    ET.SubElement(param, 'parameterid').text = "level"
                    ET.SubElement(param, 'valuemin').text = "0"
                    ET.SubElement(param, 'valuemax').text = "3.98109"
                    ET.SubElement(param, 'value').text = "0.00"

        # Create sequence
        seq_id = f"{output_basename} SyncMap_seq_1"
        sequence = ET.SubElement(main_children, 'sequence', id=seq_id)
        ET.SubElement(sequence, 'updatebehavior').text = "add"

        # Sequence timecode (starts at 00:00:00:00)
        seq_tc = ET.SubElement(sequence, 'timecode')
        add_rate(seq_tc)
        ET.SubElement(seq_tc, 'string').text = "00:00:00:00"
        ET.SubElement(seq_tc, 'frame').text = "0"
        ET.SubElement(seq_tc, 'source').text = "source"
        ET.SubElement(seq_tc, 'displayformat').text = display_format

        ET.SubElement(sequence, 'in').text = "-1"
        ET.SubElement(sequence, 'out').text = "-1"
        ET.SubElement(sequence, 'name').text = f"{output_basename} SyncMap"
        ET.SubElement(sequence, 'duration').text = str(total_duration_frames)
        add_rate(sequence)

        # Sequence media
        seq_media = ET.SubElement(sequence, 'media')

        # Video section
        seq_video = ET.SubElement(seq_media, 'video')

        # Video format
        video_format = ET.SubElement(seq_video, 'format')
        video_chars = ET.SubElement(video_format, 'samplecharacteristics')
        ET.SubElement(video_chars, 'width').text = str(width)
        ET.SubElement(video_chars, 'height').text = str(height)
        ET.SubElement(video_chars, 'anamorphic').text = "FALSE"
        ET.SubElement(video_chars, 'pixelaspectratio').text = "Square"
        ET.SubElement(video_chars, 'fielddominance').text = "none"
        add_rate(video_chars)
        ET.SubElement(video_chars, 'colordepth').text = "24"

        # Codec info
        codec = ET.SubElement(video_chars, 'codec')
        ET.SubElement(codec, 'name').text = "Apple ProRes 422"
        appdata = ET.SubElement(codec, 'appspecificdata')
        ET.SubElement(appdata, 'appname').text = "Final Cut Pro"
        ET.SubElement(appdata, 'appmanufacturer').text = "Apple Inc."
        ET.SubElement(appdata, 'appversion').text = "7.0"
        data = ET.SubElement(appdata, 'data')
        qtcodec = ET.SubElement(data, 'qtcodec')
        ET.SubElement(qtcodec, 'codecname').text = "Apple ProRes 422"
        ET.SubElement(qtcodec, 'codectypename').text = "Apple ProRes 422"
        ET.SubElement(qtcodec, 'codectypecode').text = "apcn"
        ET.SubElement(qtcodec, 'codecvendorcode').text = "appl"
        ET.SubElement(qtcodec, 'spatialquality').text = "1024"
        ET.SubElement(qtcodec, 'temporalquality').text = "0"
        ET.SubElement(qtcodec, 'keyframerate').text = "0"
        ET.SubElement(qtcodec, 'datarate').text = "0"

        # Video tracks: one per camera (they were all on V1, so overlapping cameras collided); a camera
        # gets another track only where two of its own clips overlap.
        video_items = []   # (start_frame, clip index, clip)
        for i, clip in enumerate(clips):
            if (not clip.is_audio_only and XMLExporter._has_sync_tc(clip)
                    and Path(clip.path).suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}):
                video_items.append((max(0, round(clip.timeline_start * fps) - earliest_ltc_frames), i, clip))
        video_items.sort(key=lambda x: (x[0], x[1]))
        cam_tracks, cam_order, item_track = {}, [], {}   # camera -> [[track no. within camera, end frame]]
        for start_frame, i, clip in video_items:
            cam = XMLExporter.camera_audio_key(clip)
            if cam not in cam_tracks:
                cam_tracks[cam] = []
                cam_order.append(cam)
            end_frame = start_frame + round(clip.duration * fps)
            for t in cam_tracks[cam]:
                if start_frame >= t[1]:
                    t[1] = end_frame
                    item_track[i] = (cam, t[0])
                    break
            else:
                item_track[i] = (cam, len(cam_tracks[cam]))
                cam_tracks[cam].append([len(cam_tracks[cam]), end_frame])
        track_no, n = {}, 0
        for cam in cam_order:
            for k in range(len(cam_tracks[cam])):
                track_no[(cam, k)] = n
                n += 1
        video_tracks = [ET.SubElement(seq_video, 'track') for _ in range(max(n, 1))]

        clip_counter = 0
        for start_frame, i, clip in video_items:
            video_track = video_tracks[track_no[item_track[i]]]
            clip_counter += 1
            clip_name = os.path.splitext(clip.filename)[0]
            dur_frames = round(clip.duration * fps)

            # Timeline position
            end_frame = start_frame + dur_frames

            # Honor IN-trims/splits: source in-point comes from the
            # app's clip_in_offsets (same lookup as timeline playback)
            _split_key = clip.path + f"_split_{id(clip)}"
            _in_off = (in_offsets or {}).get(_split_key, (in_offsets or {}).get(clip.path, 0))
            in_frames = round(_in_off * fps)

            clipitem = ET.SubElement(video_track, 'clipitem', id=f"{clip_name}_V1_{clip_counter}")
            ET.SubElement(clipitem, 'name').text = clip_name
            add_rate(clipitem)
            ET.SubElement(clipitem, 'duration').text = str(dur_frames)
            ET.SubElement(clipitem, 'file', id=file_ids[i])
            ET.SubElement(clipitem, 'in').text = str(in_frames)
            ET.SubElement(clipitem, 'out').text = str(in_frames + dur_frames)   # was +1: a frame longer than on the timeline
            ET.SubElement(clipitem, 'start').text = str(start_frame)
            ET.SubElement(clipitem, 'end').text = str(end_frame)
            ET.SubElement(clipitem, 'pixelaspectratio').text = "Square"
            ET.SubElement(clipitem, 'anamorphic').text = "FALSE"
            ET.SubElement(clipitem, 'alphatype').text = "none"
            ET.SubElement(clipitem, 'fielddominance').text = "none"
            sourcetrack = ET.SubElement(clipitem, 'sourcetrack')
            ET.SubElement(sourcetrack, 'mediatype').text = "video"

        # Audio section
        seq_audio = ET.SubElement(seq_media, 'audio')

        # Audio format
        audio_format = ET.SubElement(seq_audio, 'format')
        audio_chars = ET.SubElement(audio_format, 'samplecharacteristics')
        ET.SubElement(audio_chars, 'depth').text = "16"
        ET.SubElement(audio_chars, 'samplerate').text = "48000"

        ET.SubElement(seq_audio, 'in').text = "-1"
        ET.SubElement(seq_audio, 'out').text = "-1"

        # Collect audio clips and organize by track (split recordings stay together on one track)
        premiere_lanes = XMLExporter.assign_audio_lanes(clips, XMLExporter._export_audio_key, pack_audio)
        audio_clips_by_track = {}  # {track_key: [(clip_index, clip, start_frame, source_ch)]}

        for i, clip in enumerate(clips):
            if clip.is_audio_only and XMLExporter._has_sync_tc(clip):
                # Skip LTC-only tracks if mute_ltc is enabled
                if mute_ltc and clip.is_ltc_track:
                    continue
                # Skip single-channel LTC tracks
                num_channels = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2
                if mute_ltc and num_channels == 1 and clip.ltc_channel >= 0:
                    continue

                start_frame = max(0, round(clip.timeline_start * fps) - earliest_ltc_frames)

                # Recorder track (one per recording, or shared by non-overlapping takes of the
                # same recorder track when pack_audio is on)
                if pack_audio:
                    base_track = premiere_lanes.get(XMLExporter._export_audio_key(clip), getattr(clip, 'track_index', 0))
                else:
                    base_track = getattr(clip, 'track_index', 0)   # the app's track (one per recording)

                for ch in range(num_channels):
                    # Skip LTC channel within multi-channel files
                    if mute_ltc and clip.ltc_channel >= 0 and ch == clip.ltc_channel:
                        continue

                    track_key = (1, base_track, ch)  # (recorder, track_index, channel)
                    if track_key not in audio_clips_by_track:
                        audio_clips_by_track[track_key] = []
                    audio_clips_by_track[track_key].append((i, clip, start_frame, ch + 1))

        # Camera audio: each kept channel of each camera on its own mono track,
        # aligned with the camera's video clipitem (same start/in as the video).
        # Camera tracks sort before recorder tracks.
        for i, clip in enumerate(clips):
            if clip.is_audio_only or not XMLExporter._has_sync_tc(clip):
                continue
            if Path(clip.path).suffix.lower() not in {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}:
                continue
            start_frame = max(0, round(clip.timeline_start * fps) - earliest_ltc_frames)
            cam_track = getattr(clip, 'track_index', 0)
            for src_ch in XMLExporter._camera_audio_channels(
                    clip, mute_ltc, include_camera_audio, camera_audio_selection):
                audio_clips_by_track.setdefault((0, cam_track, src_ch - 1), []).append(
                    (i, clip, start_frame, src_ch))

        # Create audio tracks - sort by (kind, track_index, channel) tuple
        track_number = 0
        for track_key in sorted(audio_clips_by_track.keys()):
            track_number += 1
            audio_track = ET.SubElement(seq_audio, 'track')

            # Sort clips within each track by timeline position (start_frame)
            # This ensures split recordings appear in chronological order
            sorted_track_clips = sorted(audio_clips_by_track[track_key], key=lambda x: x[2])

            for clip_info in sorted_track_clips:
                clip_idx, clip, start_frame, source_ch = clip_info
                clip_name = os.path.splitext(clip.filename)[0]
                dur_frames = round(clip.duration * fps)

                # Camera audio gets its own id prefix: "{name}_A{ch}_1" is already
                # taken by the master clip's audio item for that channel
                item_tag = "A" if clip.is_audio_only else "CA"
                clipitem = ET.SubElement(audio_track, 'clipitem', id=f"{clip_name}_{item_tag}{source_ch}_{track_number}")
                ET.SubElement(clipitem, 'name').text = clip_name
                add_rate(clipitem)
                ET.SubElement(clipitem, 'duration').text = str(dur_frames)
                ET.SubElement(clipitem, 'file', id=file_ids[clip_idx])

                sourcetrack = ET.SubElement(clipitem, 'sourcetrack')
                ET.SubElement(sourcetrack, 'mediatype').text = "audio"
                ET.SubElement(sourcetrack, 'trackindex').text = str(source_ch)

                # Honor IN-trims/splits (same offset lookup as timeline playback)
                _split_key = clip.path + f"_split_{id(clip)}"
                _in_off = (in_offsets or {}).get(_split_key, (in_offsets or {}).get(clip.path, 0))
                _in_frames = round(_in_off * fps)
                ET.SubElement(clipitem, 'in').text = str(_in_frames)
                ET.SubElement(clipitem, 'out').text = str(_in_frames + dur_frames)
                ET.SubElement(clipitem, 'start').text = str(start_frame)
                ET.SubElement(clipitem, 'end').text = str(start_frame + dur_frames)

                # Camera audio is mono per track and not linked
                if not clip.is_audio_only:
                    continue

                # Add links for stereo pairing within the same base track
                num_channels = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2
                for link_ch in range(1, num_channels + 1):
                    # Skip LTC channel in links
                    if mute_ltc and clip.ltc_channel >= 0 and link_ch - 1 == clip.ltc_channel:
                        continue
                    link = ET.SubElement(clipitem, 'link')
                    ET.SubElement(link, 'linkclipref').text = f"{clip_name}_A{link_ch}_{track_number}"
                    ET.SubElement(link, 'mediatype').text = "audio"
                    ET.SubElement(link, 'trackindex').text = str(link_ch)
                    ET.SubElement(link, 'clipindex').text = "1"

        # Generate XML with proper indentation
        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="\t")
        # Remove extra blank lines
        xml_lines = [line for line in xml_str.split('\n') if line.strip()]
        xml_str = '\n'.join(xml_lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)

    @staticmethod
    def export_resolve(clips: List[MediaClip], output_path: str, fps: float = 30.0,
                       mute_ltc: bool = True, include_camera_audio: bool = False,
                       split_stereo: bool = False, multicam_export: bool = False,
                       in_offsets: dict = None, camera_audio_selection: dict = None,
                       pack_audio: bool = False):
        """Export DaVinci Resolve compatible XML (FCPXML 1.7 format).

        This format matches DaVinci Resolve's own export format.
        Key: Asset 'start' attribute contains the EMBEDDED CAMERA timecode (for file matching).
             Clip 'offset' attribute contains the LTC timecode (for timeline positioning).
             Audio files use sample-based fractions (e.g., "56996/1s") not frame-based units.

        Args:
            mute_ltc: If True, mute the LTC audio channel in video clips that have LTC recorded.
            include_camera_audio: If True, include camera audio in video clips (when False, only video is exported).
            split_stereo: If True, split stereo audio into 2 mono channels. If False, insert raw audio directly.
            multicam_export: If True, create a multicam clip structure for simultaneous cameras.
            camera_audio_selection: camera_audio_key -> channels to keep; each
                kept channel becomes its own mono audio item under the video.
        """
        import urllib.parse
        from math import gcd

        def simplify_fraction(numerator: int, denominator: int) -> tuple:
            """Simplify a fraction to its lowest terms."""
            if numerator == 0:
                return (0, 1)
            divisor = gcd(abs(numerator), abs(denominator))
            return (numerator // divisor, denominator // divisor)

        def samples_to_fraction(samples: int, sample_rate: int) -> str:
            """Convert samples to a simplified fraction of seconds (DaVinci format).

            Examples:
                2735808000 samples @ 48000 Hz -> "56996/1s" (whole seconds)
                357826560 samples @ 48000 Hz -> "186368/25s"
            """
            num, denom = simplify_fraction(samples, sample_rate)
            return f"{num}/{denom}s"

        def read_bwf_time_reference(filepath: str) -> int:
            """Read BWF time_reference directly from file using ffprobe.

            Returns the actual time_reference in samples, or 0 if not found.
            """
            if not FFPROBE_PATH or not os.path.exists(filepath):
                return 0
            try:
                import subprocess
                import json
                kwargs = {'capture_output': True, 'text': True, 'timeout': 10}
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                cmd = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                       '-show_format', filepath]
                result = subprocess.run(cmd, **kwargs)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    tags = data.get('format', {}).get('tags', {})
                    # Try different tag names
                    ref = tags.get('time_reference') or tags.get('TIME_REFERENCE')
                    if ref:
                        return int(ref)
            except Exception:
                pass
            return 0

        def read_wav_sample_count(filepath: str) -> int:
            """Read exact sample count from WAV file header."""
            try:
                with open(filepath, 'rb') as f:
                    if f.read(4) != b'RIFF':
                        return 0
                    f.read(4)  # file size
                    if f.read(4) != b'WAVE':
                        return 0
                    channels = bits = data_size = 0
                    while True:
                        chunk_id = f.read(4)
                        if len(chunk_id) < 4:
                            break
                        chunk_size = int.from_bytes(f.read(4), 'little')
                        if chunk_id == b'fmt ':
                            fmt_data = f.read(chunk_size)
                            if len(fmt_data) >= 16:
                                channels = int.from_bytes(fmt_data[2:4], 'little')
                                bits = int.from_bytes(fmt_data[14:16], 'little')
                        elif chunk_id == b'data':
                            data_size = chunk_size
                            break
                        else:
                            f.seek(chunk_size, 1)
                    if channels > 0 and bits > 0 and data_size > 0:
                        return data_size // (channels * (bits // 8))
            except Exception:
                pass
            return 0

        # Priority 1: Check for manual FPS override on any clip
        fps_source = "default"
        for clip in clips:
            if getattr(clip, 'fps_override', False) and clip.fps > 0:
                fps = clip.fps
                fps_source = "manual override"
                print(f"[EXPORT] Using manual FPS override: {fps}")
                break
        else:
            # Priority 2: Get actual frame rate from video file metadata
            # video_fps is a string like "30000/1001" (29.97fps) or "25/1" (25fps)
            for clip in clips:
                print(f"[EXPORT DEBUG] Clip: {clip.filename}, is_audio_only={clip.is_audio_only}, video_fps={getattr(clip, 'video_fps', 'N/A')}, ltc_fps={clip.fps}")
                if not clip.is_audio_only and clip.video_fps:
                    try:
                        if '/' in clip.video_fps:
                            num, denom = map(int, clip.video_fps.split('/'))
                            actual_fps = num / denom
                            # Use video metadata fps
                            if actual_fps > 10 and actual_fps < 120:  # Sanity check
                                fps = actual_fps
                                fps_source = f"video metadata ({clip.video_fps})"
                                print(f"[EXPORT] Using video metadata fps: {fps} from {clip.video_fps}")
                                break  # Only break after successfully setting fps
                    except (ValueError, ZeroDivisionError) as e:
                        print(f"[EXPORT] Error parsing video_fps: {e}")

        print(f"[EXPORT] Final FPS: {fps} (source: {fps_source})")

        # Determine frame rate fraction (handles drop-frame rates properly)
        # Map common frame rates to their exact fractions
        frame_rate_map = [
            (23.976, 1001, 24000),
            (24.0, 1, 24),
            (25.0, 1, 25),
            (29.97, 1001, 30000),
            (30.0, 1, 30),
            (50.0, 1, 50),
            (59.94, 1001, 60000),
            (60.0, 1, 60),
        ]

        # Find closest matching frame rate with tolerance (fixes 29.97 vs 30 issue)
        frame_num, frame_denom = 1, int(round(fps))  # Default fallback
        for target_fps, num, denom in frame_rate_map:
            if abs(fps - target_fps) < 0.01:  # Tolerance of 0.01 fps
                frame_num, frame_denom = num, denom
                break

        # Get video dimensions from first video clip
        width, height = 1920, 1080
        for clip in clips:
            if not clip.is_audio_only and clip.width and clip.height:
                width = clip.width
                height = clip.height
                break

        # Determine format name based on resolution and fps
        fps_str = str(int(round(fps))) if fps == int(fps) else f"{fps:.2f}".replace('.', '')
        format_name = f"FFVideoFormat{height}p{fps_str}"

        # Determine timecode format from clips' actual drop_frame setting
        # Only use DF if clips actually have drop frame flag set (detected from LTC)
        is_drop_frame = any(clip.drop_frame for clip in clips if hasattr(clip, 'drop_frame'))
        tc_format = "DF" if is_drop_frame else "NDF"

        # Calculate the earliest LTC timecode to use as timeline base
        # timeline_start is in seconds, convert to FCPXML units: seconds * frame_denom
        earliest_ltc_frames = None
        for clip in clips:
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                ltc_frames = round(clip.timeline_start * frame_denom)
                if earliest_ltc_frames is None or ltc_frames < earliest_ltc_frames:
                    earliest_ltc_frames = ltc_frames

        if earliest_ltc_frames is None:
            earliest_ltc_frames = 0

        def clip_in_offset(clip) -> float:
            """IN-trim/split source offset (same lookup as timeline playback)."""
            key = clip.path + f"_split_{id(clip)}"
            return (in_offsets or {}).get(key, (in_offsets or {}).get(clip.path, 0)) or 0

        def audio_dur_units(clip, skip: float = 0.0) -> int:
            """Timeline length of an audio-only clip in FCPXML units: the
            clip's (possibly trimmed) duration - never past the end of the file
            after its IN-trim - floored to whole frames.

            Trims used to be ignored here: the full file length was exported,
            so an OUT-trim came back and an IN-trim asked for media past the
            end of the file. Split parts (-0001, -0002) need no special case:
            _merge_split_clips already chains them by their exact lengths, so
            each part goes where the app timeline shows it (re-chaining them
            here by FLOORED lengths slid every later part up to a frame early).
            """
            sr = clip.sample_rate if clip.sample_rate and clip.sample_rate > 0 else 48000
            samples = read_wav_sample_count(clip.path) or clip.sample_count
            file_seconds = samples / sr if samples > 0 else (clip.original_duration or clip.duration)
            available = file_seconds - clip_in_offset(clip)
            # an untrimmed clip's duration can differ from the sample count by
            # rounding - treat anything within 1 ms of the rest of the file as "all of it"
            seconds = (clip.duration if 0 < clip.duration < available - 0.001 else available) - skip
            return max(1, int(seconds * frame_denom / frame_num + 1e-9)) * frame_num

        # Split recordings: the parts go side by side at exact SAMPLE positions
        # (offset/duration in 1/sample-rate units, as Resolve writes audio),
        # each part starting on the very sample the previous one ends - no
        # gap, no overlap, no extra length. Frame-grid placement can't do that:
        # recorders split at an arbitrary sample, a frame grid always left a
        # gap or shifted the following parts. {id(clip): (offset, length, rate)}
        exact_split_parts = {}
        for c in clips:
            if getattr(c, '_exact_audio', False) and c.is_audio_only:
                # drift-corrected segment: exact sample position and length
                sr = c.sample_rate if c.sample_rate and c.sample_rate > 0 else 48000
                pos = max(0, int(round((c.timeline_start - earliest_ltc_frames / frame_denom) * sr)))
                end = int(round((c.timeline_start + c.duration - earliest_ltc_frames / frame_denom) * sr))
                total = read_wav_sample_count(c.path) or c.sample_count
                avail = total - int(round(clip_in_offset(c) * sr))
                exact_split_parts[id(c)] = (pos, max(1, min(end - pos, avail)), sr)
        for parts in XMLExporter._contiguous_split_groups(
                [c for c in clips if not getattr(c, '_exact_audio', False)], in_offsets):
            first = parts[0]
            if not (XMLExporter._has_sync_tc(first) and first.timeline_start >= -0.9):
                continue
            sr = first.sample_rate if first.sample_rate and first.sample_rate > 0 else 48000
            pos = max(0, int(round((first.timeline_start - earliest_ltc_frames / frame_denom) * sr)))
            for k, c in enumerate(parts):
                total = read_wav_sample_count(c.path) or c.sample_count
                avail = total - (int(round(clip_in_offset(c) * sr)) if k == 0 else 0)
                want = int(round(c.duration * sr))
                length = avail if k < len(parts) - 1 or want >= avail - 48 else min(want, avail)
                exact_split_parts[id(c)] = (pos, length, sr)
                pos += length

        root = ET.Element('fcpxml', version="1.8")
        resources = ET.SubElement(root, 'resources')

        # Add format with proper frame duration fraction (for video)
        ET.SubElement(resources, 'format',
                                     id="format_0",
                                     name=format_name,
                                     frameDuration=f"{frame_num}/{frame_denom}s",
                                     width=str(width),
                                     height=str(height))

        # Note: Audio-only files don't need a format element (DaVinci style)
        # They use sample-based fractions instead of frame-based units

        # Add assets for ALL clips
        for i, clip in enumerate(clips):
            # Format file path for FCPXML (Sidus format: just path with URL encoding, no file:// prefix)
            # Example: C:/test/new/New%20folder/file.mp4
            file_path = clip.path.replace('\\', '/')
            if len(file_path) > 1 and file_path[1] == ':':
                # Windows path: encode spaces and special chars, keep drive letter
                drive = file_path[:2]
                rest = file_path[2:]
                encoded_rest = urllib.parse.quote(rest, safe='/')
                file_url = f"{drive}{encoded_rest}"
            else:
                # Unix path
                file_url = urllib.parse.quote(file_path, safe='/')

            # Get asset duration in FCPXML units (whole file)
            sample_rate = clip.sample_rate if hasattr(clip, 'sample_rate') and clip.sample_rate else 48000

            if clip.is_audio_only and clip.sample_count > 0 and clip.sample_rate > 0:
                # Frame-aligned duration (floor to whole frames)
                frame_count = (clip.sample_count * frame_denom) / (frame_num * clip.sample_rate)
                frame_count_int = int(frame_count)
                dur_frames = frame_count_int * frame_num
            elif clip.is_audio_only and clip.original_duration > 0:
                frame_count = clip.original_duration * frame_denom / frame_num
                frame_count_int = int(frame_count)
                dur_frames = frame_count_int * frame_num
            else:
                # ASSET duration must describe the WHOLE FILE, not the (possibly
                # trimmed/split) clip: the spine references source ranges up to
                # trim+duration, and Resolve flags clips whose range exceeds
                # the asset's media extent ("longer than actual" class of bug).
                asset_seconds = clip.original_duration if clip.original_duration > 0 else clip.duration
                if asset_seconds > 0:
                    frame_count = asset_seconds * frame_denom / frame_num
                    frame_count_int = int(frame_count)
                    dur_frames = frame_count_int * frame_num
                else:
                    dur_frames = frame_num

            # Get EMBEDDED camera timecode for asset 'start' (used by DaVinci to match files)
            # This must match the timecode embedded in the original file metadata

            if clip.is_audio_only and hasattr(clip, 'bwf_time_reference') and clip.bwf_time_reference > 0:
                # Use BWF time_reference for audio files
                embedded_start_frames = clip.bwf_time_reference * frame_denom // sample_rate
                print(f"[EXPORT] Asset {i}: BWF start = {embedded_start_frames}/{frame_denom}s "
                      f"(bwf_ref={clip.bwf_time_reference})")
            elif clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                # Use embedded camera timecode from file metadata (for video files)
                # Convert frame count to FCPXML time units: frames * frame_num
                # (N frames at 29.97fps = N * 1001/30000 seconds = N * 1001 in FCPXML units)
                embedded_start_frames = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
                print(f"[EXPORT] Asset {i}: Using embedded TC ({clip.embedded_tc}) = {embedded_start_frames}/{frame_denom}s")
            elif XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                # Fall back to LTC if no embedded TC (audio files, etc.)
                # timeline_start is in seconds, so multiply by frame_denom
                embedded_start_frames = round(clip.timeline_start * frame_denom)
                print(f"[EXPORT] Asset {i}: No embedded TC, using LTC fallback = {embedded_start_frames}/{frame_denom}s")
            else:
                embedded_start_frames = 0

            # Determine if clip has video/audio
            ext = Path(clip.path).suffix.lower()
            video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
            is_video_clip = ext in video_exts or not clip.is_audio_only
            has_audio = "1"

            # Asset attributes - 'start' is EMBEDDED camera timecode (for file matching)
            if is_video_clip:
                # Video asset - uses file://localhost/ prefix like DaVinci
                asset_attribs = {
                    'format': "format_0",
                    'hasAudio': has_audio,
                    'id': f"asset_{i}",
                    'name': clip.filename,
                    'duration': f"{dur_frames}/{frame_denom}s",
                    'src': f"file://localhost/{file_url}",
                    'hasVideo': "1",
                    'audioSources': "1",
                    'start': f"{embedded_start_frames}/{frame_denom}s",
                    'audioChannels': str(clip.audio_channels) if hasattr(clip, 'audio_channels') and clip.audio_channels else "2"
                }
            else:
                # Audio-only asset - use sample-based fractions (DaVinci format)
                # DaVinci uses simplified fractions of seconds, not frame-based units
                # Example: start="56996/1s" duration="186368/25s"
                audio_sample_rate = clip.sample_rate if hasattr(clip, 'sample_rate') and clip.sample_rate else 48000

                # CRITICAL: Read ACTUAL BWF time_reference from the file
                # This ensures FCPXML matches what DaVinci will read from the file
                actual_bwf_ref = read_bwf_time_reference(clip.path)
                if actual_bwf_ref > 0:
                    audio_start_str = samples_to_fraction(actual_bwf_ref, audio_sample_rate)
                    print(f"[EXPORT] Audio asset {i}: Read actual BWF time_reference={actual_bwf_ref} from file -> start={audio_start_str}")
                elif hasattr(clip, 'bwf_time_reference') and clip.bwf_time_reference > 0:
                    # Fallback to clip attribute (may not match file)
                    audio_start_str = samples_to_fraction(clip.bwf_time_reference, audio_sample_rate)
                    print(f"[EXPORT] Audio asset {i}: WARNING - Using clip.bwf_time_reference={clip.bwf_time_reference} (could not read from file)")
                else:
                    # No BWF - use 0
                    audio_start_str = "0/1s"
                    print(f"[EXPORT] Audio asset {i}: No BWF time_reference found, using 0")

                # CRITICAL: Read ACTUAL sample count from the file
                actual_sample_count = read_wav_sample_count(clip.path)
                if actual_sample_count > 0:
                    audio_duration_str = samples_to_fraction(actual_sample_count, audio_sample_rate)
                    print(f"[EXPORT] Audio asset {i}: Read actual sample_count={actual_sample_count} from file -> duration={audio_duration_str}")
                elif clip.sample_count > 0:
                    audio_duration_str = samples_to_fraction(clip.sample_count, audio_sample_rate)
                    print(f"[EXPORT] Audio asset {i}: Using clip.sample_count={clip.sample_count} -> duration={audio_duration_str}")
                else:
                    # Fallback: convert frame-based duration to samples, then to fraction
                    dur_samples = int(dur_frames * audio_sample_rate / frame_denom)
                    audio_duration_str = samples_to_fraction(dur_samples, audio_sample_rate)
                    print(f"[EXPORT] Audio asset {i}: WARNING - no sample_count, using fallback -> duration={audio_duration_str}")

                print(f"[EXPORT] Audio asset {i}: FINAL start={audio_start_str}, duration={audio_duration_str}")

                # DaVinci format: no 'format' attribute, no 'audioRate', uses file://localhost/ prefix
                asset_attribs = {
                    'hasAudio': "1",
                    'id': f"asset_{i}",
                    'name': clip.filename,
                    'duration': audio_duration_str,
                    'src': f"file://localhost/{file_url}",
                    'audioSources': "1",
                    'start': audio_start_str,
                    'audioChannels': str(clip.audio_channels) if hasattr(clip, 'audio_channels') and clip.audio_channels else "2"
                }

            asset = ET.SubElement(resources, 'asset', **asset_attribs)

            # Add camera metadata only for video assets (DaVinci doesn't add metadata for audio)
            if is_video_clip:
                asset_meta = ET.SubElement(asset, 'metadata')
                camera_name = clip.camera_id if clip.camera_id else ""
                ET.SubElement(asset_meta, 'md',
                              key="com.apple.proapps.mio.cameraName",
                              value=camera_name)
                if camera_name:   # FCP "Camera Angle" - used to build multicam angles
                    ET.SubElement(asset_meta, 'md',
                                  key="com.apple.proapps.mio.cameraAngle",
                                  value=camera_name)

        # Calculate total sequence duration based on clips
        # All values in FCPXML units
        # Audio clips use their trim-aware, frame-floored length (audio_dur_units)
        total_duration_frames = 0
        for i, clip in enumerate(clips):
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                ltc_frames = round(clip.timeline_start * frame_denom)
                if clip.is_audio_only:
                    dur_frames = audio_dur_units(clip)
                else:
                    actual_dur = clip.original_duration if clip.original_duration > 0 else clip.duration
                    dur_frames = round(actual_dur * frame_denom)
                clip_end = (ltc_frames - earliest_ltc_frames) + dur_frames
            else:
                if clip.is_audio_only:
                    dur_frames = audio_dur_units(clip)
                else:
                    actual_dur = clip.original_duration if clip.original_duration > 0 else clip.duration
                    dur_frames = round(actual_dur * frame_denom)
                total_duration_frames += dur_frames
                continue
            if clip_end > total_duration_frames:
                total_duration_frames = clip_end

        library = ET.SubElement(root, 'library')
        event = ET.SubElement(library, 'event', name="LTC Sync Export")
        project = ET.SubElement(event, 'project', name="Synced Timeline")

        # Sequence - tcStart is 0 (timeline starts at 0, clips are offset from there)
        # Sidus format: duration, tcStart in fractional form
        sequence = ET.SubElement(project, 'sequence',
                                  duration=f"{total_duration_frames}/{frame_denom}s",
                                  format="format_0",
                                  tcStart=f"0/{frame_denom}s",
                                  tcFormat=tc_format,
                                  audioLayout="stereo",
                                  audioRate="48k")
        spine = ET.SubElement(sequence, 'spine')

        # Track current timeline position
        current_timeline_pos = 0

        # Detect video clips and assign lanes for multi-camera support
        # Group by camera_id so clips from the same camera stay on the same lane
        video_lane_map = {}  # {clip_index: lane_number}
        video_clips_info = []  # [(index, start_time, end_time, clip)]

        for i, clip in enumerate(clips):
            if not clip.is_audio_only:
                ext = Path(clip.path).suffix.lower()
                video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
                if ext in video_exts:
                    if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                        start_time = clip.timeline_start
                        end_time = start_time + clip.duration
                        video_clips_info.append((i, start_time, end_time, clip))

        # Sort by start time
        video_clips_info.sort(key=lambda x: x[1])

        # Assign lanes to video clips grouped by camera_id
        # Lane 0 = primary camera (first camera encountered), Lane 1+ = additional cameras
        # Clips from the same camera stay on the same lane regardless of time overlap
        camera_lane_map = {}  # {camera_id: lane_number}
        next_video_lane = 0

        for idx, start_time, end_time, clip in video_clips_info:
            # Get camera identifier - use camera_id if available, otherwise use filename pattern
            camera_id = clip.camera_id
            if not camera_id:
                # Extract camera identifier from filename (e.g., A025C071 -> A025)
                # Use first 4 chars as camera identifier for ARRI/RED style naming
                name = os.path.splitext(clip.filename)[0].upper()
                match = re.match(r'^([A-Z]\d{3})', name)
                if match:
                    camera_id = match.group(1)
                else:
                    camera_id = f"_unknown_{idx}"  # Fallback for unidentified cameras

            # Assign lane based on camera
            if camera_id not in camera_lane_map:
                camera_lane_map[camera_id] = next_video_lane
                next_video_lane += 1
                print(f"[EXPORT] Camera '{camera_id}' assigned to video lane {camera_lane_map[camera_id]}")

            video_lane_map[idx] = camera_lane_map[camera_id]
            print(f"[EXPORT] Video clip '{clip.filename}' (camera: {camera_id}) on lane {camera_lane_map[camera_id]}")

        # Check if we have multiple cameras
        has_multicam = len(camera_lane_map) > 1
        if has_multicam:
            print(f"[EXPORT] Detected {len(camera_lane_map)} camera(s): {list(camera_lane_map.keys())}")

        # Build lane mapping for audio files - group split files (0001, 0002) on same lane
        # This ensures DaVinci Resolve puts them on the same audio track
        # (pack_audio: non-overlapping takes of the same recorder track share one lane)
        audio_lane_map = {k: v + 1 for k, v in   # {recording key: lane}, lane 1+ (lane 0 is video)
                          XMLExporter.assign_audio_lanes(clips, XMLExporter._export_audio_key, pack_audio).items()}
        for key, lane in audio_lane_map.items():
            print(f"[EXPORT] Audio track '{key}' assigned to lane {lane}")

        mc_audio_info = {}   # clip index -> (offset, start, duration) of its audio item, for the multicam angles

        # Sort clips by timeline position for proper FCPXML ordering
        # DaVinci Resolve expects clips in the spine to be in chronological order
        clips_with_indices = []
        for i, clip in enumerate(clips):
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                sort_key = clip.timeline_start
            else:
                sort_key = float('inf')  # Put clips without TC at the end
            clips_with_indices.append((i, clip, sort_key))

        clips_with_indices.sort(key=lambda x: x[2])
        print("[EXPORT] Clips sorted by timeline position for spine ordering")

        for i, clip, _ in clips_with_indices:
            # Calculate LTC timecode for timeline offset
            # timeline_start is in seconds, so convert to FCPXML units: seconds * frame_denom
            if XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                ltc_start_frames = round(clip.timeline_start * frame_denom)
                # Timeline offset relative to earliest clip; clamp: the
                # earliest clip can sit a few hundredths NEGATIVE after the
                # sub-frame LTC-offset correction in _sync_clips
                timeline_offset_frames = max(0, ltc_start_frames - earliest_ltc_frames)
            else:
                ltc_start_frames = 0
                timeline_offset_frames = current_timeline_pos

            # Audio-only clips: a timeline item can only start on a frame, but
            # the audio's true start (LTC with sub-frame offset, BWF sample TC)
            # is usually between two frames. Start the item on the NEXT frame
            # and skip that sub-frame bit of the media, so the sound sits at
            # its exact time instead of up to half a frame early or late.
            audio_skip = 0.0
            if (clip.is_audio_only and id(clip) not in exact_split_parts
                    and XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9):
                exact_units = clip.timeline_start * frame_denom - earliest_ltc_frames
                if exact_units > 0:
                    snapped = -(-int(round(exact_units / frame_num * 1e6)) // 1000000) * frame_num
                    audio_skip = max(0.0, (snapped - exact_units) / frame_denom)
                    timeline_offset_frames = snapped

            # Get embedded camera timecode for clip/video/audio 'start' attributes
            # This must match the asset 'start' attribute
            # For BWF audio files, use sample-accurate calculation to match asset
            sample_rate = clip.sample_rate if hasattr(clip, 'sample_rate') and clip.sample_rate else 48000

            if clip.is_audio_only and hasattr(clip, 'bwf_time_reference') and clip.bwf_time_reference > 0:
                # Sample-accurate calculation for BWF audio files (must match asset)
                embedded_start_frames = clip.bwf_time_reference * frame_denom // sample_rate
            elif clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                # embedded_tc_frames is at nominal fps, convert: frames * frame_num
                # (N frames at 29.97fps = N * 1001/30000 seconds = N * 1001 in FCPXML units)
                embedded_start_frames = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
            elif XMLExporter._has_sync_tc(clip) and clip.timeline_start >= -0.9:
                embedded_start_frames = ltc_start_frames  # Fallback to LTC
            else:
                embedded_start_frames = 0

            # IN-trim/split source offset (same lookup as timeline playback).
            # Shifts the SPINE-side source start only - the asset definition
            # keeps the whole file's TC so Resolve can still relink by
            # embedded TC. Wrapper start and inner video offset/start all move
            # together, so no duration extension is needed.
            _in_off = clip_in_offset(clip)
            if _in_off:
                embedded_start_frames += int(round(_in_off * frame_denom / frame_num)) * frame_num

            # Duration in FCPXML units
            # Use pre-calculated duration for split files, or sample-accurate duration for WAV files
            audio_duration_str = None  # For audio clips, store sample-based duration string
            sample_rate = clip.sample_rate if hasattr(clip, 'sample_rate') and clip.sample_rate > 0 else 48000

            # First, determine dur_frames (frame-based duration for timeline calculations)
            # For proper frame alignment, dur_frames must be a multiple of frame_num
            # This ensures clips end on frame boundaries, preventing "longer than actual" issues
            if clip.is_audio_only:
                dur_frames = audio_dur_units(clip, audio_skip)  # trim-aware, never past the file end
            else:
                # Frame-align the duration
                if clip.duration > 0:
                    frame_count = clip.duration * frame_denom / frame_num
                    frame_count_int = int(frame_count)
                    dur_frames = frame_count_int * frame_num
                else:
                    dur_frames = frame_num  # 1 frame default

            # For audio clips, always use sample-based duration (DaVinci format)
            # This ensures clip duration matches asset duration exactly
            # CRITICAL: Read actual sample count from file to match asset definition
            if clip.is_audio_only:
                actual_sample_count = read_wav_sample_count(clip.path)
                if actual_sample_count > 0:
                    # Use actual sample count from file
                    audio_duration_str = samples_to_fraction(actual_sample_count, sample_rate)
                    print(f"[EXPORT] Audio clip {i} '{clip.filename}': Read actual sample_count={actual_sample_count} from file -> duration={audio_duration_str}")
                elif clip.sample_count > 0:
                    # Fallback to clip attribute
                    audio_duration_str = samples_to_fraction(clip.sample_count, sample_rate)
                    print(f"[EXPORT] Audio clip {i} '{clip.filename}': WARNING - Using clip.sample_count={clip.sample_count} (fallback)")
                else:
                    # Fallback: convert dur_frames to sample-based fraction
                    dur_samples = int(dur_frames * sample_rate / frame_denom)
                    audio_duration_str = samples_to_fraction(dur_samples, sample_rate)
                    print(f"[EXPORT] Audio clip {i} '{clip.filename}': WARNING - no sample_count, using dur_frames fallback -> duration={audio_duration_str}")

            # Determine if clip has video
            ext = Path(clip.path).suffix.lower()
            video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
            has_video = ext in video_exts or not clip.is_audio_only

            # Camera channels to put on the timeline (LTC + unchosen removed)
            audio_channels_to_use = XMLExporter._camera_audio_channels(
                clip, mute_ltc, include_camera_audio, camera_audio_selection)

            # Log the timecode values for debugging
            print(f"[EXPORT] Clip {i}: embedded_tc={clip.embedded_tc}, ltc={clip.start_tc}")
            print(f"[EXPORT]   offset={timeline_offset_frames}/{frame_denom}s (LTC-based timeline pos)")
            print(f"[EXPORT]   start={embedded_start_frames}/{frame_denom}s (embedded camera TC)")
            if mute_ltc and clip.ltc_channel >= 0:
                print(f"[EXPORT]   LTC on channel {clip.ltc_channel + 1}, using channels: {audio_channels_to_use}")

            # Create clip element
            # - offset = timeline position based on LTC (relative to earliest clip)
            # - duration = clip duration
            # - start = embedded camera timecode (must match asset start)
            # For audio files, use LTC-derived start (embedded_start_frames already set from LTC above)
            clip_start = embedded_start_frames  # Use whatever was calculated (embedded TC or LTC fallback)

            # For audio clips, also compute sample-based start string to match asset exactly
            # CRITICAL: Read actual BWF time_reference from file to match asset definition
            if clip.is_audio_only:
                actual_bwf_ref = read_bwf_time_reference(clip.path)
                audio_sr = clip.sample_rate if clip.sample_rate else 48000
                _trim_samples = int(round((_in_off + audio_skip) * audio_sr))
                if actual_bwf_ref > 0:
                    audio_start_str = samples_to_fraction(actual_bwf_ref + _trim_samples, audio_sr)
                    print(f"[EXPORT] Clip {i}: Read actual BWF time_reference={actual_bwf_ref} from file -> start={audio_start_str}")
                elif hasattr(clip, 'bwf_time_reference') and clip.bwf_time_reference > 0:
                    audio_start_str = samples_to_fraction(clip.bwf_time_reference + _trim_samples, audio_sr)
                    print(f"[EXPORT] Clip {i}: WARNING - Using clip.bwf_time_reference={clip.bwf_time_reference} (fallback)")
                elif _trim_samples:
                    audio_start_str = samples_to_fraction(_trim_samples, audio_sr)
                else:
                    audio_start_str = "0/1s"
            else:
                audio_start_str = None

            # Build clip attributes
            # For audio clips, use sample-based fractions to match asset exactly (prevents duration mismatch)
            if clip.is_audio_only and audio_duration_str:
                clip_attribs = {
                    'name': clip.filename,
                    'offset': f"{timeline_offset_frames}/{frame_denom}s",
                    'duration': audio_duration_str,
                    'start': audio_start_str if audio_start_str else f"{clip_start}/{frame_denom}s",
                    'tcFormat': tc_format
                }
            else:
                clip_attribs = {
                    'name': clip.filename,
                    'offset': f"{timeline_offset_frames}/{frame_denom}s",
                    'duration': f"{dur_frames}/{frame_denom}s",
                    'start': f"{clip_start}/{frame_denom}s",
                    'tcFormat': tc_format
                }

            # Add lane attribute for overlapping video clips (multi-camera)
            # Lane 0 clips don't need lane attribute (primary track)
            # Lane 1+ clips get lane attribute to put them on separate video tracks
            if has_video and i in video_lane_map and video_lane_map[i] > 0:
                clip_attribs['lane'] = str(video_lane_map[i])

            clip_elem = ET.SubElement(spine, 'clip', **clip_attribs)

            # Add video element (Sidus format)
            # Camera audio is optional - controlled by include_camera_audio parameter
            if has_video:
                video_elem = ET.SubElement(clip_elem, 'video',
                                           offset=f"{embedded_start_frames}/{frame_denom}s",
                                           duration=f"{dur_frames}/{frame_denom}s",
                                           start=f"{embedded_start_frames}/{frame_denom}s",
                                           ref=f"asset_{i}")

                # Add conform-rate element
                ET.SubElement(video_elem, 'conform-rate',
                                             srcFrameRate=f"{fps:.2f}".replace('.', ','),
                                             scaleEnabled="0")

                # One mono audio item per kept camera channel, stacked BELOW
                # the video (negative lanes -1, -2, ...; the old str(idx) put
                # the 2nd+ channel on positive lanes, i.e. above the video)
                for idx, src_ch in enumerate(audio_channels_to_use):
                    ET.SubElement(video_elem, 'audio',
                                  offset=f"{embedded_start_frames}/{frame_denom}s",
                                  duration=f"{dur_frames}/{frame_denom}s",
                                  start=f"{embedded_start_frames}/{frame_denom}s",
                                  ref=f"asset_{i}",
                                  srcCh=str(src_ch),
                                  lane=str(-(idx + 1)))
            else:
                # Audio-only file - use asset-clip format (DaVinci Resolve compatible)
                # Skip LTC-only tracks entirely (single-channel files with LTC)
                num_audio_ch = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2
                if mute_ltc and clip.is_ltc_track:
                    print(f"[EXPORT] Skipping LTC-only track: {clip.filename}")
                    # Remove the clip_elem we already created since we're skipping
                    spine.remove(clip_elem)
                    continue
                if mute_ltc and num_audio_ch == 1 and clip.ltc_channel >= 0:
                    print(f"[EXPORT] Skipping single-channel LTC track: {clip.filename}")
                    # Remove the clip_elem we already created since we're skipping
                    spine.remove(clip_elem)
                    continue

                # Get lane for this audio file (groups split files on same track)
                lane_key = XMLExporter._export_audio_key(clip)
                audio_lane = audio_lane_map.get(lane_key, 1)

                # Remove the clip element we created earlier - we'll use asset-clip instead
                spine.remove(clip_elem)

                # For asset-clip, use frame-based duration (DaVinci Resolve format)
                # DaVinci uses frame-aligned duration for clips, even for audio
                # This prevents the "audio longer than actual" issue in Resolve
                audio_offset_str = f"{timeline_offset_frames}/{frame_denom}s"
                audio_start_str_final = audio_start_str if audio_start_str else f"{embedded_start_frames}/{frame_denom}s"

                # Use frame-based duration for asset-clip (not sample-based)
                # This is what DaVinci does - the clip duration is frame-aligned
                audio_dur_str = f"{dur_frames}/{frame_denom}s"
                if id(clip) in exact_split_parts:
                    _pos, _len, _sr = exact_split_parts[id(clip)]
                    audio_offset_str = samples_to_fraction(_pos, _sr)
                    audio_dur_str = samples_to_fraction(_len, _sr)

                # Create asset-clip element directly (DaVinci Resolve format)
                # This is the proper format for audio-only clips in FCPXML
                asset_clip_attribs = {
                    'name': clip.filename,
                    'duration': audio_dur_str,
                    'lane': str(-audio_lane),
                    'offset': audio_offset_str,
                    'start': audio_start_str_final,
                    'ref': f"asset_{i}",
                    'enabled': "1"
                }

                # Add tcFormat for consistency
                asset_clip_attribs['tcFormat'] = tc_format

                asset_clip_elem = ET.SubElement(spine, 'asset-clip', **asset_clip_attribs)
                mc_audio_info[i] = (audio_offset_str, audio_start_str_final, audio_dur_str)   # for the multicam angles

                # Add metadata to asset-clip
                metadata_elem = ET.SubElement(asset_clip_elem, 'metadata')
                ET.SubElement(metadata_elem, 'md',
                                        key="com.apple.proapps.studio.reel",
                                        value="00000000")

                print(f"[EXPORT] Audio-only clip {i}: {clip.filename} using asset-clip format (lane={-audio_lane}, group: {lane_key})")

                # Skip the metadata section below since we already added it
                # Update position for next clip if no timecode
                if not XMLExporter._has_sync_tc(clip):
                    current_timeline_pos += dur_frames
                continue

            # Add metadata
            metadata_elem = ET.SubElement(clip_elem, 'metadata')
            ET.SubElement(metadata_elem, 'md',
                                    key="com.apple.proapps.studio.reel",
                                    value="00000000")

            # Update position for next clip if no timecode
            if not clip.start_tc:
                current_timeline_pos += dur_frames

        # Create multicam structure if multicam_export is enabled
        if multicam_export and has_multicam:
            print("[EXPORT] Creating multicam structure...")

            # Get the output filename for the multicam name
            output_basename = os.path.splitext(os.path.basename(output_path))[0]

            # Create media element for multicam in resources
            media_elem = ET.SubElement(resources, 'media',
                                       id="media_multicam",
                                       name=f"{output_basename} [Multicam]")

            multicam_elem = ET.SubElement(media_elem, 'multicam',
                                          format="format_0",
                                          tcStart=f"0/{frame_denom}s",
                                          tcFormat=tc_format)

            # Group clips by video lane (camera angle)
            num_video_angles = max(video_lane_map.values()) + 1 if video_lane_map else 1

            # Create mc-angle for each video angle
            for angle_num in range(num_video_angles):
                angle_id = f"angle_v{angle_num + 1}"
                mc_angle = ET.SubElement(multicam_elem, 'mc-angle',
                                         name=f"Video Angle {angle_num + 1}",
                                         angleID=angle_id)

                # Get clips for this angle - store original clip index
                angle_clips = [(info[0], info[1], info[2], info[3])
                               for info in video_clips_info
                               if video_lane_map.get(info[0], 0) == angle_num]

                # Sort by start time
                angle_clips.sort(key=lambda x: x[1])

                # Add clips and gaps for this angle
                current_pos = 0
                for clip_i, start_time, end_time, clip in angle_clips:

                    # Calculate positions in FCPXML units
                    clip_offset = max(0, round(start_time * frame_denom) - earliest_ltc_frames)   # rounded like the normal timeline (int() put clips a frame early)
                    clip_duration = round(clip.duration * frame_denom)

                    # Get embedded TC
                    if clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                        clip_embedded = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
                    else:
                        clip_embedded = round(start_time * frame_denom)

                    # Add gap if needed
                    if clip_offset > current_pos:
                        gap_duration = clip_offset - current_pos
                        ET.SubElement(mc_angle, 'gap',
                                      name="Pause",
                                      offset=f"{current_pos}/{frame_denom}s",
                                      duration=f"{gap_duration}/{frame_denom}s",
                                      start=f"0/{frame_denom}s")

                    # Add clip
                    mc_clip = ET.SubElement(mc_angle, 'clip',
                                            name=clip.filename,
                                            offset=f"{clip_offset}/{frame_denom}s",
                                            duration=f"{clip_duration}/{frame_denom}s",
                                            start=f"{clip_embedded}/{frame_denom}s",
                                            tcFormat=tc_format)

                    # Add video element
                    mc_video = ET.SubElement(mc_clip, 'video',
                                             offset=f"{clip_embedded}/{frame_denom}s",
                                             duration=f"{clip_duration}/{frame_denom}s",
                                             start=f"{clip_embedded}/{frame_denom}s",
                                             ref=f"asset_{clip_i}")
                    ET.SubElement(mc_video, 'conform-rate',
                                  srcFrameRate=f"{fps:.2f}".replace('.', ','),
                                  scaleEnabled="0")

                    # Add metadata
                    mc_meta = ET.SubElement(mc_clip, 'metadata')
                    ET.SubElement(mc_meta, 'md',
                                  key="com.apple.proapps.studio.reel",
                                  value="00000000")

                    current_pos = clip_offset + clip_duration

            # Audio angles: one per RECORDER TRACK - takes of the same recorder track side by side (as with
            # "Share Tracks"; a second recorder never shares), each item referencing its file and placed
            # exactly as on the normal timeline (same offset / media start / duration).
            from fractions import Fraction
            exported = [c for k, c in enumerate(clips) if k in mc_audio_info]
            mc_lanes = XMLExporter.assign_audio_lanes(exported, XMLExporter._export_audio_key, True, margin=0.05)
            by_lane = {}
            for k, c in enumerate(clips):
                if k in mc_audio_info:
                    by_lane.setdefault(mc_lanes[XMLExporter._export_audio_key(c)], []).append(k)
            audio_angle_num = 0
            for lane in sorted(by_lane):
                audio_angle_num += 1
                first = clips[by_lane[lane][0]]
                angle_name = (f"{re.sub(r'[0-9]+$', '', first.recording_id) or first.recording_id}_"
                              f"{'LR' if not first.track_number else f'Tr{first.track_number}'}"
                              if first.recording_id else f"Audio Angle {audio_angle_num}")
                mc_angle = ET.SubElement(multicam_elem, 'mc-angle',
                                         name=angle_name,
                                         angleID=f"angle_a{audio_angle_num}")
                cur = Fraction(0)
                for k in sorted(by_lane[lane], key=lambda k: XMLExporter._frac(mc_audio_info[k][0])):
                    off_s, start_s, dur_s = mc_audio_info[k]
                    off = max(XMLExporter._frac(off_s), cur)   # sequential container: never step back
                    if off > cur:
                        ET.SubElement(mc_angle, 'gap', name="Pause",
                                      offset=XMLExporter._frac_str(cur, frame_denom),
                                      duration=XMLExporter._frac_str(off - cur, frame_denom),
                                      start=f"0/{frame_denom}s")
                    mc_item = ET.SubElement(mc_angle, 'asset-clip',
                                            name=clips[k].filename,
                                            ref=f"asset_{k}",
                                            offset=off_s if off == XMLExporter._frac(off_s) else XMLExporter._frac_str(off, frame_denom),
                                            duration=dur_s,
                                            start=start_s,
                                            tcFormat=tc_format,
                                            enabled="1")
                    mc_meta = ET.SubElement(mc_item, 'metadata')
                    ET.SubElement(mc_meta, 'md', key="com.apple.proapps.studio.reel", value="00000000")
                    cur = off + XMLExporter._frac(dur_s)

            # Add mc-clip to library event
            mc_clip_elem = ET.SubElement(event, 'mc-clip',
                                         name=f"{output_basename} [Multicam]",
                                         duration=f"{total_duration_frames}/{frame_denom}s",
                                         ref="media_multicam")

            # Add mc-source for each video angle
            for angle_num in range(num_video_angles):
                ET.SubElement(mc_clip_elem, 'mc-source',
                              angleID=f"angle_v{angle_num + 1}",
                              srcEnable="video" if angle_num == 0 else "none")

            # Add mc-source for each audio angle
            for i in range(audio_angle_num):
                ET.SubElement(mc_clip_elem, 'mc-source',
                              angleID=f"angle_a{i + 1}",
                              srcEnable="audio")

            # Add keyword
            ET.SubElement(mc_clip_elem, 'keyword',
                          duration=f"0/{frame_denom}s",
                          start=f"0/{frame_denom}s",
                          value="synced")

            # Add keyword-collection to event
            ET.SubElement(event, 'keyword-collection', name="synced")

            # The imported TIMELINE is the multicam: its spine holds ONE mc-clip (it used to keep the normal clips,
            # so Resolve built an ordinary timeline and the multicam only existed as a library clip)
            for child in list(spine):
                spine.remove(child)
            tl_mc = ET.SubElement(spine, 'mc-clip',
                                  name=f"{output_basename} [Multicam]",
                                  ref="media_multicam",
                                  offset=f"0/{frame_denom}s",
                                  duration=f"{total_duration_frames}/{frame_denom}s")
            for angle_num in range(num_video_angles):
                ET.SubElement(tl_mc, 'mc-source', angleID=f"angle_v{angle_num + 1}",
                              srcEnable="video" if angle_num == 0 else "none")
            for i in range(audio_angle_num):
                ET.SubElement(tl_mc, 'mc-source', angleID=f"angle_a{i + 1}", srcEnable="audio")

            print(f"[EXPORT] Created multicam with {num_video_angles} video angle(s) and {audio_angle_num} audio angle(s)")

        # Camera number / angle for Resolve's Media Pool. Resolve imports little metadata from FCPXML, but it
        # imports a CSV: File > Import Metadata To > Media Pool, matched by file name -> "Camera #" and "Angle".
        XMLExporter.last_csv = None   # reported by the export dialog
        try:
            import csv
            rows = []
            no_cam = 0
            for clip in clips:
                if clip.is_audio_only:
                    continue
                if not clip.camera_id:
                    no_cam += 1
                    continue
                digits = re.findall(r'\d+', str(clip.camera_id))
                try:   # the TC box's camera number (user bits), else the number in the camera ID
                    cam_no = (f"{int(clip.ub_camera_id):02d}" if clip.ub_camera_id is not None
                              else (digits[-1] if digits else str(clip.camera_id)))
                except (TypeError, ValueError):
                    cam_no = digits[-1] if digits else str(clip.camera_id)
                rows.append([os.path.basename(clip.path), os.path.dirname(os.path.abspath(clip.path)), cam_no,
                             str(clip.camera_id)])
            csv_path = os.path.splitext(output_path)[0] + "_camera_metadata.csv"
            if rows:
                with open(csv_path, 'w', newline='', encoding='utf-8') as fcsv:   # no BOM: Resolve reads a BOM as part of "File Name"
                    w = csv.writer(fcsv)
                    w.writerow(["File Name", "Clip Directory", "Camera #", "Angle"])
                    w.writerows(rows)
                XMLExporter.last_csv = (f"Camera numbers for Resolve ({len(rows)} clips):\n{csv_path}"
                                        + (f"\n({no_cam} video clips without a camera ID left out)" if no_cam else ""))
            else:
                XMLExporter.last_csv = "No camera-numbers file: none of the video clips has a camera ID."
        except Exception as e:
            XMLExporter.last_csv = f"Camera-numbers file NOT written: {e}"
        print(f"[EXPORT] {XMLExporter.last_csv}")

        # Generate pretty XML with proper indentation
        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="\t")
        # Remove XML declaration line and extra blank lines
        xml_lines = xml_str.split('\n')
        # Keep XML declaration, remove empty lines
        xml_lines = [line for line in xml_lines if line.strip()]
        xml_str = '\n'.join(xml_lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)

    @staticmethod
    def export_fcpx(clips: List[MediaClip], output_path: str, fps: float = 30.0,
                    in_offsets: dict = None,
                    mute_ltc: bool = True, include_camera_audio: bool = False,
                    split_stereo: bool = False, multicam_export: bool = False,
                    camera_audio_selection: dict = None, pack_audio: bool = False):
        """Export Final Cut Pro X XML (FCPXML 1.7).

        This format is compatible with Final Cut Pro X 10.4+ and DaVinci Resolve.

        Args:
            mute_ltc: If True, exclude LTC audio channel from video clips.
            include_camera_audio: If True, include camera audio in video clips.
            split_stereo: If True, split stereo audio into 2 mono channels. If False, insert raw audio directly.
            multicam_export: If True, create a multicam clip structure.
        """
        import urllib.parse

        # Get output filename for naming
        output_basename = os.path.splitext(os.path.basename(output_path))[0]

        # Priority 1: Check for manual FPS override on any clip
        for clip in clips:
            if getattr(clip, 'fps_override', False) and clip.fps > 0:
                fps = clip.fps
                break
        else:
            # Priority 2: the MOST COMMON video rate (it was the first video clip's: one 50p clip at the
            # top of a 25p project made the whole sequence 50p)
            rates = {}
            for clip in clips:
                if not clip.is_audio_only and clip.video_fps:
                    r = XMLExporter.video_tc_rate(clip.video_fps)
                    if r and r < 120:
                        rates[round(r, 3)] = rates.get(round(r, 3), 0) + 1
            if rates:
                fps = max(rates, key=lambda r: (rates[r], -r))   # a tie goes to the lower rate

        # Determine frame rate fraction (handles NTSC rates properly)
        frame_rate_map = [
            (23.976, 1001, 24000),
            (24.0, 1, 24),
            (25.0, 1, 25),
            (29.97, 1001, 30000),
            (30.0, 1, 30),
            (59.94, 1001, 60000),
            (60.0, 1, 60),
        ]

        frame_num, frame_denom = 1001, 30000  # Default to 29.97
        for rate, num, denom in frame_rate_map:
            if abs(fps - rate) < 0.1:
                frame_num, frame_denom = num, denom
                break

        # Determine drop frame from clips
        is_drop_frame = any(clip.drop_frame for clip in clips if hasattr(clip, 'drop_frame'))
        tc_format = "DF" if is_drop_frame else "NDF"

        # Helper functions for sample-based calculations
        def simplify_fraction(numerator: int, denominator: int):
            """Simplify a fraction using GCD."""
            from math import gcd
            if numerator == 0:
                return (0, 1)
            divisor = gcd(abs(numerator), abs(denominator))
            return (numerator // divisor, denominator // divisor)

        def samples_to_fraction(samples: int, sample_rate: int) -> str:
            """Convert samples to a simplified fraction of seconds."""
            num, denom = simplify_fraction(samples, sample_rate)
            return f"{num}/{denom}s"

        def read_bwf_time_reference(filepath: str) -> int:
            """Read BWF time_reference directly from file using ffprobe."""
            if not FFPROBE_PATH or not os.path.exists(filepath):
                return 0
            try:
                import subprocess
                import json
                kwargs = {'capture_output': True, 'text': True, 'timeout': 10}
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                cmd = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
                       '-show_format', filepath]
                result = subprocess.run(cmd, **kwargs)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    tags = data.get('format', {}).get('tags', {})
                    ref = tags.get('time_reference') or tags.get('TIME_REFERENCE')
                    if ref:
                        return int(ref)
            except Exception:
                pass
            return 0

        def read_wav_sample_count(filepath: str) -> int:
            """Read exact sample count from WAV file header."""
            try:
                with open(filepath, 'rb') as f:
                    if f.read(4) != b'RIFF':
                        return 0
                    f.read(4)  # file size
                    if f.read(4) != b'WAVE':
                        return 0
                    channels = bits = data_size = 0
                    while True:
                        chunk_id = f.read(4)
                        if len(chunk_id) < 4:
                            break
                        chunk_size = int.from_bytes(f.read(4), 'little')
                        if chunk_id == b'fmt ':
                            fmt_data = f.read(chunk_size)
                            if len(fmt_data) >= 16:
                                channels = int.from_bytes(fmt_data[2:4], 'little')
                                bits = int.from_bytes(fmt_data[14:16], 'little')
                        elif chunk_id == b'data':
                            data_size = chunk_size
                            break
                        else:
                            f.seek(chunk_size, 1)
                    if channels > 0 and bits > 0 and data_size > 0:
                        return data_size // (channels * (bits // 8))
            except Exception:
                pass
            return 0

        # Get resolution from first video clip
        width, height = 1920, 1080
        for clip in clips:
            if not clip.is_audio_only and hasattr(clip, 'width') and clip.width:
                width = clip.width
                height = clip.height if hasattr(clip, 'height') and clip.height else 1080
                break

        # Calculate earliest LTC timecode - supports both LTC and embedded TC sources
        earliest_ltc_frames = None
        for clip in clips:
            has_valid_tc = (clip.start_tc or (clip.embedded_tc and clip.embedded_tc != "00:00:00:00"))
            if has_valid_tc and clip.timeline_start >= 0:
                ltc_frames = round(clip.timeline_start * frame_denom)
                if earliest_ltc_frames is None or ltc_frames < earliest_ltc_frames:
                    earliest_ltc_frames = ltc_frames
        if earliest_ltc_frames is None:
            earliest_ltc_frames = 0

        # Calculate total duration - supports both LTC and embedded TC sources
        total_duration_frames = 0
        for clip in clips:
            has_valid_tc = (clip.start_tc or (clip.embedded_tc and clip.embedded_tc != "00:00:00:00"))
            if has_valid_tc and clip.timeline_start >= 0:
                clip_end = round((clip.timeline_start + clip.duration) * frame_denom)
                if clip_end > total_duration_frames:
                    total_duration_frames = clip_end
        total_duration_frames = total_duration_frames - earliest_ltc_frames if total_duration_frames > earliest_ltc_frames else round(max(c.duration for c in clips) * frame_denom)

        # Build format name
        fps_int = int(round(fps))
        if abs(fps - 29.97) < 0.1:
            fps_str = "2997"
        elif abs(fps - 23.976) < 0.1:
            fps_str = "2398"
        elif abs(fps - 59.94) < 0.1:
            fps_str = "5994"
        else:
            fps_str = str(fps_int)
        format_name = f"FFVideoFormat{width}x{height}p{fps_str}"

        # Create root element
        root = ET.Element('fcpxml', version="1.8")
        resources = ET.SubElement(root, 'resources')

        # Create video format
        ET.SubElement(resources, 'format',
                      id="format_0",
                      name=format_name,
                      frameDuration=f"{frame_num}/{frame_denom}s",
                      width=str(width),
                      height=str(height),
                      colorSpace="1-1-1 (Rec. 709)")

        # Note: Audio-only files don't need a format element (DaVinci style)
        # They use sample-based fractions instead of frame-based units

        # Create assets
        for i, clip in enumerate(clips):
            # URL encode the path
            path_url = clip.path.replace('\\', '/').replace(' ', '%20')

            # Determine if video or audio only
            ext = Path(clip.path).suffix.lower()
            video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
            has_video = ext in video_exts and not clip.is_audio_only

            # Get audio channels
            num_channels = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2

            if has_video:
                # Video asset - use frame-based units
                dur_frames = round(clip.duration * frame_denom) if clip.duration > 0 else frame_denom

                # Get embedded TC for start - supports both LTC and embedded TC sources
                if clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                    start_frames = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
                elif clip.timeline_start >= 0:
                    # Fallback to timeline position (works for both LTC and embedded TC sync)
                    start_frames = round(clip.timeline_start * frame_denom)
                else:
                    start_frames = 0

                asset_attribs = {
                    'format': "format_0",
                    'hasAudio': "1",
                    'id': f"asset_{i}",
                    'name': os.path.splitext(clip.filename)[0],
                    'duration': f"{dur_frames}/{frame_denom}s",
                    'src': f"file://localhost/{path_url}",
                    'hasVideo': "1",
                    'audioSources': "1",
                    'start': f"{start_frames}/{frame_denom}s",
                    'audioChannels': str(num_channels)
                }
            else:
                # Audio-only asset - use sample-based fractions (DaVinci format)
                audio_sample_rate = clip.sample_rate if hasattr(clip, 'sample_rate') and clip.sample_rate else 48000

                # CRITICAL: Read ACTUAL BWF time_reference from the file
                # This ensures FCPXML matches what DaVinci/FCP will read from the file
                actual_bwf_ref = read_bwf_time_reference(clip.path)
                if actual_bwf_ref > 0:
                    audio_start_str = samples_to_fraction(actual_bwf_ref, audio_sample_rate)
                elif hasattr(clip, 'bwf_time_reference') and clip.bwf_time_reference > 0:
                    # Fallback to clip attribute (may not match file)
                    audio_start_str = samples_to_fraction(clip.bwf_time_reference, audio_sample_rate)
                else:
                    audio_start_str = "0/1s"

                # CRITICAL: Read ACTUAL sample count from the file
                actual_sample_count = read_wav_sample_count(clip.path)
                if actual_sample_count > 0:
                    audio_duration_str = samples_to_fraction(actual_sample_count, audio_sample_rate)
                elif hasattr(clip, 'sample_count') and clip.sample_count > 0:
                    # Fallback to clip attribute
                    audio_duration_str = samples_to_fraction(clip.sample_count, audio_sample_rate)
                else:
                    # Fallback: convert duration seconds to samples
                    dur_samples = round(clip.duration * audio_sample_rate) if clip.duration > 0 else audio_sample_rate
                    audio_duration_str = samples_to_fraction(dur_samples, audio_sample_rate)

                # DaVinci format: no 'format' attribute, uses file://localhost/ prefix
                asset_attribs = {
                    'hasAudio': "1",
                    'id': f"asset_{i}",
                    'name': os.path.splitext(clip.filename)[0],
                    'duration': audio_duration_str,
                    'src': f"file://localhost/{path_url}",
                    'audioSources': "1",
                    'start': audio_start_str,
                    'audioChannels': str(num_channels)
                }

            asset_elem = ET.SubElement(resources, 'asset', **asset_attribs)

            # Add camera metadata only for video assets (DaVinci doesn't add metadata for audio)
            if has_video:
                metadata = ET.SubElement(asset_elem, 'metadata')
                camera_name = clip.camera_id if clip.camera_id else ""
                ET.SubElement(metadata, 'md',
                              key="com.apple.proapps.mio.cameraName",
                              value=camera_name)

        # Detect overlapping video clips for multi-camera
        video_lane_map = {}
        video_clips_info = []

        for i, clip in enumerate(clips):
            if not clip.is_audio_only:
                ext = Path(clip.path).suffix.lower()
                video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
                # Check for EITHER LTC (start_tc) OR embedded TC - supports mixed TC sources
                has_valid_tc = (clip.start_tc or (clip.embedded_tc and clip.embedded_tc != "00:00:00:00"))
                if ext in video_exts and has_valid_tc and clip.timeline_start >= 0:
                    start_time = clip.timeline_start
                    end_time = start_time + clip.duration
                    video_clips_info.append((i, start_time, end_time, clip))

        video_clips_info.sort(key=lambda x: x[1])

        active_lanes = []
        for idx, start_time, end_time, clip in video_clips_info:
            assigned_lane = None
            for j, (lane_end, lane_num) in enumerate(active_lanes):
                if lane_end <= start_time:
                    assigned_lane = lane_num
                    active_lanes[j] = (end_time, lane_num)
                    break
            if assigned_lane is None:
                assigned_lane = len(active_lanes)
                active_lanes.append((end_time, assigned_lane))
            video_lane_map[idx] = assigned_lane

        has_multicam = any(lane > 0 for lane in video_lane_map.values())

        # Create library structure
        library = ET.SubElement(root, 'library')
        event = ET.SubElement(library, 'event', name=f"{output_basename} [Synced]")

        # Create project with sequence
        project = ET.SubElement(event, 'project', name=f"{output_basename} [Timeline]")
        sequence = ET.SubElement(project, 'sequence',
                                 duration=f"{total_duration_frames}/{frame_denom}s",
                                 format="format_0",
                                 tcStart="0/1s",
                                 tcFormat=tc_format,
                                 audioLayout="stereo",
                                 audioRate="48k",
                                 keywords="synced")
        spine = ET.SubElement(sequence, 'spine')

        # Build audio lane map for grouping
        # Audio lanes are negative in FCP (pack_audio: non-overlapping takes of the same recorder
        # track share one lane)
        audio_lane_map = {k: -(v + 1) for k, v in
                          XMLExporter.assign_audio_lanes(clips, XMLExporter._export_audio_key, pack_audio).items()}

        # Add clips to spine
        current_pos = 0
        for i, clip in enumerate(clips):
            # Check for EITHER LTC (start_tc) OR embedded TC - supports mixed TC sources
            has_valid_tc = (clip.start_tc or (clip.embedded_tc and clip.embedded_tc != "00:00:00:00"))
            if not has_valid_tc or clip.timeline_start < 0:
                continue

            ext = Path(clip.path).suffix.lower()
            video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
            has_video = ext in video_exts and not clip.is_audio_only

            # Calculate positions
            timeline_offset = max(0, round(clip.timeline_start * frame_denom) - earliest_ltc_frames)
            dur_frames = round(clip.duration * frame_denom)

            # Get embedded TC - supports both LTC and embedded TC sources
            if clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                start_frames = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
            elif clip.timeline_start >= 0:
                # Fallback to timeline position (works for both LTC and embedded TC sync)
                start_frames = round(clip.timeline_start * frame_denom)
            else:
                start_frames = 0

            # IN-trim/split source offset (same lookup as timeline playback):
            # wrapper start and inner video offset/start share start_frames,
            # so one shift trims the visible source range cleanly
            _split_key = clip.path + f"_split_{id(clip)}"
            _in_off = (in_offsets or {}).get(_split_key, (in_offsets or {}).get(clip.path, 0)) or 0
            if _in_off:
                start_frames += int(round(_in_off * frame_denom / frame_num)) * frame_num

            if has_video:
                # Add gap if needed
                if timeline_offset > current_pos:
                    gap_duration = timeline_offset - current_pos
                    gap = ET.SubElement(spine, 'gap',
                                        name="Pause",
                                        offset=f"{current_pos}/{frame_denom}s",
                                        duration=f"{gap_duration}/{frame_denom}s",
                                        start=f"0/{frame_denom}s")

                    # Add audio clips that start during this gap
                    for j, audio_clip in enumerate(clips):
                        # Check for EITHER LTC (start_tc) OR embedded TC - supports mixed TC sources
                        has_audio_tc = (audio_clip.start_tc or (audio_clip.embedded_tc and audio_clip.embedded_tc != "00:00:00:00"))
                        if audio_clip.is_audio_only and has_audio_tc:
                            audio_offset = round(audio_clip.timeline_start * frame_denom) - earliest_ltc_frames
                            if current_pos <= audio_offset < timeline_offset:
                                audio_dur = round(audio_clip.duration * frame_denom)
                                clip_name = os.path.splitext(audio_clip.filename)[0]

                                # Get lane
                                lane = audio_lane_map.get(XMLExporter._export_audio_key(audio_clip), -1)

                                audio_clip_elem = ET.SubElement(gap, 'clip',
                                                                name=clip_name,
                                                                offset=f"{audio_offset}/{frame_denom}s",
                                                                duration=f"{audio_dur}/{frame_denom}s",
                                                                start=f"0/{frame_denom}s",
                                                                tcFormat=tc_format,
                                                                lane=str(lane))
                                ET.SubElement(audio_clip_elem, 'note')

                                # Add audio channels
                                num_ch = audio_clip.audio_channels if hasattr(audio_clip, 'audio_channels') and audio_clip.audio_channels > 0 else 2
                                if num_ch >= 2:
                                    audio_elem = ET.SubElement(audio_clip_elem, 'audio',
                                                               offset=f"0/{frame_denom}s",
                                                               duration=f"{audio_dur}/{frame_denom}s",
                                                               start=f"0/{frame_denom}s",
                                                               ref=f"asset_{j}",
                                                               srcCh="1")
                                    ET.SubElement(audio_elem, 'audio',
                                                  offset=f"0/{frame_denom}s",
                                                  duration=f"{audio_dur}/{frame_denom}s",
                                                  start=f"0/{frame_denom}s",
                                                  ref=f"asset_{j}",
                                                  srcCh="2",
                                                  lane="-2")

                                for ch in range(1, num_ch + 1):
                                    ET.SubElement(audio_clip_elem, 'audio-channel-source',
                                                  role=f"dialogue.Trk{ch}",
                                                  srcCh=str(ch))

                                metadata = ET.SubElement(audio_clip_elem, 'metadata')
                                ET.SubElement(metadata, 'md',
                                              key="com.apple.proapps.studio.reel",
                                              value="00000000")

                # Add video clip
                clip_name = os.path.splitext(clip.filename)[0]
                clip_attribs = {
                    'name': clip_name,
                    'offset': f"{timeline_offset}/{frame_denom}s",
                    'duration': f"{dur_frames}/{frame_denom}s",
                    'start': f"{start_frames}/{frame_denom}s",
                    'tcFormat': tc_format
                }

                # Add lane for overlapping clips
                if i in video_lane_map and video_lane_map[i] > 0:
                    clip_attribs['lane'] = str(video_lane_map[i])

                clip_elem = ET.SubElement(spine, 'clip', **clip_attribs)

                # Add video element
                video_elem = ET.SubElement(clip_elem, 'video',
                                           offset=f"{start_frames}/{frame_denom}s",
                                           duration=f"{dur_frames}/{frame_denom}s",
                                           start=f"{start_frames}/{frame_denom}s",
                                           ref=f"asset_{i}")

                # Add conform-rate
                fps_display = f"{fps:.2f}".replace('.', ',')
                ET.SubElement(video_elem, 'conform-rate',
                              srcFrameRate=fps_display,
                              scaleEnabled="0")

                # Camera audio: one mono item per kept channel, below the video
                for idx, src_ch in enumerate(XMLExporter._camera_audio_channels(
                        clip, mute_ltc, include_camera_audio, camera_audio_selection)):
                    ET.SubElement(video_elem, 'audio',
                                  offset=f"{start_frames}/{frame_denom}s",
                                  duration=f"{dur_frames}/{frame_denom}s",
                                  start=f"{start_frames}/{frame_denom}s",
                                  ref=f"asset_{i}",
                                  srcCh=str(src_ch),
                                  lane=str(-(idx + 1)))

                # Add metadata
                metadata = ET.SubElement(clip_elem, 'metadata')
                ET.SubElement(metadata, 'md',
                              key="com.apple.proapps.studio.reel",
                              value="00000000")

                current_pos = timeline_offset + dur_frames

        # Add final gap if there are remaining audio clips
        if current_pos < total_duration_frames:
            gap = ET.SubElement(spine, 'gap',
                                name="Pause",
                                offset=f"{current_pos}/{frame_denom}s",
                                duration=f"{total_duration_frames - current_pos}/{frame_denom}s",
                                start=f"0/{frame_denom}s")

        # Create multicam if enabled and has overlapping cameras
        if multicam_export and has_multicam:
            num_video_angles = max(video_lane_map.values()) + 1 if video_lane_map else 1

            # Create media element for multicam
            media_elem = ET.SubElement(resources, 'media',
                                       id="media_multicam",
                                       name=f"{output_basename} [Multicam]")
            multicam_elem = ET.SubElement(media_elem, 'multicam',
                                          format="format_0",
                                          tcStart="0/1s",
                                          tcFormat=tc_format)

            # Video angles
            for angle_num in range(num_video_angles):
                mc_angle = ET.SubElement(multicam_elem, 'mc-angle',
                                         name=f"Video Angle {angle_num + 1}",
                                         angleID=f"angle_v{angle_num + 1}")

                angle_clips = [(info[0], info[1], info[2], info[3])
                               for info in video_clips_info
                               if video_lane_map.get(info[0], 0) == angle_num]
                angle_clips.sort(key=lambda x: x[1])

                current_pos = 0
                for clip_i, start_time, end_time, clip in angle_clips:
                    clip_offset = round(start_time * frame_denom) - earliest_ltc_frames
                    clip_duration = round(clip.duration * frame_denom)

                    if clip.embedded_tc_frames and clip.embedded_tc_frames > 0:
                        clip_start = XMLExporter._tc_units(clip.embedded_tc_frames, clip.fps, frame_num, frame_denom)
                    else:
                        clip_start = round(start_time * frame_denom)

                    if clip_offset > current_pos:
                        ET.SubElement(mc_angle, 'gap',
                                      name="Pause",
                                      offset=f"{current_pos}/{frame_denom}s",
                                      duration=f"{clip_offset - current_pos}/{frame_denom}s",
                                      start=f"0/{frame_denom}s")

                    mc_clip = ET.SubElement(mc_angle, 'clip',
                                            name=os.path.splitext(clip.filename)[0],
                                            offset=f"{clip_offset}/{frame_denom}s",
                                            duration=f"{clip_duration}/{frame_denom}s",
                                            start=f"{clip_start}/{frame_denom}s",
                                            tcFormat=tc_format)

                    video_elem = ET.SubElement(mc_clip, 'video',
                                               offset=f"{clip_start}/{frame_denom}s",
                                               duration=f"{clip_duration}/{frame_denom}s",
                                               start=f"{clip_start}/{frame_denom}s",
                                               ref=f"asset_{clip_i}")
                    fps_display = f"{fps:.2f}".replace('.', ',')
                    ET.SubElement(video_elem, 'conform-rate',
                                  srcFrameRate=fps_display,
                                  scaleEnabled="0")

                    metadata = ET.SubElement(mc_clip, 'metadata')
                    ET.SubElement(metadata, 'md',
                                  key="com.apple.proapps.studio.reel",
                                  value="00000000")

                    current_pos = clip_offset + clip_duration

            # Audio angles: one per RECORDER TRACK (takes side by side, a second recorder never shares), each
            # take at its real position (the old code put every take at the start of the angle)
            def mc_exportable(c):
                if not c.is_audio_only or not XMLExporter._has_sync_tc(c) or c.timeline_start < -0.9:
                    return False
                n_ch = c.audio_channels if hasattr(c, 'audio_channels') and c.audio_channels > 0 else 2
                return not (mute_ltc and (c.is_ltc_track or (n_ch == 1 and c.ltc_channel >= 0)))
            mc_idx = [k for k, c in enumerate(clips) if mc_exportable(c)]
            mc_lanes = XMLExporter.assign_audio_lanes([clips[k] for k in mc_idx], XMLExporter._export_audio_key,
                                                      True, margin=0.05)
            by_lane = {}
            for k in mc_idx:
                by_lane.setdefault(mc_lanes[XMLExporter._export_audio_key(clips[k])], []).append(k)
            audio_angle_num = 0
            for lane in sorted(by_lane):
                audio_angle_num += 1
                mc_angle = ET.SubElement(multicam_elem, 'mc-angle',
                                         name=f"Audio Angle {audio_angle_num}",
                                         angleID=f"angle_a{audio_angle_num}")
                cur = 0
                for i in sorted(by_lane[lane], key=lambda k: clips[k].timeline_start):
                    clip = clips[i]
                    clip_duration = round(clip.duration * frame_denom)
                    clip_offset = max(round(clip.timeline_start * frame_denom) - earliest_ltc_frames, cur)
                    if clip_offset > cur:
                        ET.SubElement(mc_angle, 'gap', name="Pause",
                                      offset=f"{cur}/{frame_denom}s",
                                      duration=f"{clip_offset - cur}/{frame_denom}s",
                                      start=f"0/{frame_denom}s")
                    mc_clip = ET.SubElement(mc_angle, 'clip',
                                            name=os.path.splitext(clip.filename)[0],
                                            offset=f"{clip_offset}/{frame_denom}s",
                                            duration=f"{clip_duration}/{frame_denom}s",
                                            start=f"0/{frame_denom}s",
                                            tcFormat=tc_format)
                    ET.SubElement(mc_clip, 'note')

                    num_ch = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels > 0 else 2
                    if num_ch >= 2:
                        audio_elem = ET.SubElement(mc_clip, 'audio',
                                                   offset=f"0/{frame_denom}s",
                                                   duration=f"{clip_duration}/{frame_denom}s",
                                                   start=f"0/{frame_denom}s",
                                                   ref=f"asset_{i}",
                                                   srcCh="1")
                        ET.SubElement(audio_elem, 'audio',
                                      offset=f"0/{frame_denom}s",
                                      duration=f"{clip_duration}/{frame_denom}s",
                                      start=f"0/{frame_denom}s",
                                      ref=f"asset_{i}",
                                      srcCh="2",
                                      lane="-2")
                    else:
                        ET.SubElement(mc_clip, 'audio',
                                      offset=f"0/{frame_denom}s",
                                      duration=f"{clip_duration}/{frame_denom}s",
                                      start=f"0/{frame_denom}s",
                                      ref=f"asset_{i}",
                                      srcCh="1")

                    for ch in range(1, num_ch + 1):
                        ET.SubElement(mc_clip, 'audio-channel-source',
                                      role=f"dialogue.Trk{ch}",
                                      srcCh=str(ch))

                    metadata = ET.SubElement(mc_clip, 'metadata')
                    ET.SubElement(metadata, 'md',
                                  key="com.apple.proapps.studio.reel",
                                  value="00000000")
                    cur = clip_offset + clip_duration

            # Add mc-clip to event
            mc_clip_elem = ET.SubElement(event, 'mc-clip',
                                         name=f"{output_basename} [Multicam]",
                                         duration=f"{total_duration_frames}/{frame_denom}s",
                                         ref="media_multicam")

            for angle_num in range(num_video_angles):
                ET.SubElement(mc_clip_elem, 'mc-source',
                              angleID=f"angle_v{angle_num + 1}",
                              srcEnable="video" if angle_num == 0 else "none")

            for i in range(audio_angle_num):
                ET.SubElement(mc_clip_elem, 'mc-source',
                              angleID=f"angle_a{i + 1}",
                              srcEnable="audio")

            ET.SubElement(mc_clip_elem, 'keyword',
                          duration=f"0/{frame_denom}s",
                          start=f"0/{frame_denom}s",
                          value="synced")

            ET.SubElement(event, 'keyword-collection', name="synced")

        # Generate XML with proper indentation
        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="\t")
        xml_lines = [line for line in xml_str.split('\n') if line.strip()]
        xml_str = '\n'.join(xml_lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)


# =============================================================================
# Tooltip Helper Class
# =============================================================================

class ToolTip:
    """Tooltip popup for clip information on hover."""

    def __init__(self, widget, text_callback, delay=500):
        self.widget = widget
        self.text_callback = text_callback
        self.delay = delay
        self.tip_window = None
        self.schedule_id = None

        widget.bind('<Enter>', self._schedule)
        widget.bind('<Leave>', self._hide)
        widget.bind('<Button>', self._hide)

    def _schedule(self, event=None):
        self._hide()
        self.schedule_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self.tip_window:
            return

        text = self.text_callback()
        if not text:
            return

        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        # Tooltip styling
        frame = tk.Frame(tw, bg='#2d2d4a', bd=1, relief=tk.SOLID)
        frame.pack()

        label = tk.Label(frame, text=text, justify=tk.LEFT, font=('Segoe UI', 9),
                        bg='#2d2d4a', fg='#ffffff', padx=8, pady=6)
        label.pack()

    def _hide(self, event=None):
        if self.schedule_id:
            self.widget.after_cancel(self.schedule_id)
            self.schedule_id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

# =============================================================================
# Frame Buffer Playback System - Audio Master Clock Architecture
# =============================================================================

class AudioMasterClock:
    """
    Master clock driven by sounddevice audio callback.
    Provides ground-truth timing for the entire playback system.
    Audio position in samples is the single source of truth.
    """

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._sample_position = 0  # Absolute sample position
        self._lock = threading.Lock()
        self._playing = False

    @property
    def time_seconds(self) -> float:
        """Current playback time in seconds."""
        with self._lock:
            return self._sample_position / self.sample_rate

    @property
    def sample_position(self) -> int:
        """Current position in samples."""
        with self._lock:
            return self._sample_position

    def advance(self, samples: int):
        """Called by audio callback to advance the clock."""
        with self._lock:
            self._sample_position += samples

    def seek(self, time_seconds: float):
        """Seek to a specific time position."""
        with self._lock:
            self._sample_position = int(time_seconds * self.sample_rate)

    def reset(self):
        """Reset clock to zero."""
        with self._lock:
            self._sample_position = 0

    def set_playing(self, playing: bool):
        """Set playing state."""
        self._playing = playing

    @property
    def is_playing(self) -> bool:
        return self._playing


class ClipFrameBuffer:
    """
    Frame buffer for a single video clip.
    Manages pre-decoded frames in memory with automatic cleanup.
    Uses OrderedDict for LRU eviction.
    """

    def __init__(self, clip, buffer_size: int = 60, frame_size: tuple = (480, 270)):
        self.clip = clip
        self.buffer_size = buffer_size
        self._frame_size = frame_size
        self.frames: OrderedDict = OrderedDict()  # {frame_num: PhotoImage}
        self.raw_frames: OrderedDict = OrderedDict()  # {frame_num: BGR numpy array}
        self._lock = threading.RLock()
        self._memory_bytes = 0
        self._max_memory_bytes = 100 * 1024 * 1024  # 100MB per clip

        # Clip properties
        self.fps = clip.fps if hasattr(clip, 'fps') and clip.fps > 0 else 30.0
        self.total_frames = int(clip.duration * self.fps) if hasattr(clip, 'duration') else 0
        self.in_offset_frames = 0  # For trimmed clips

    def get_frame(self, frame_num: int):
        """Get a frame from the buffer (thread-safe). Returns PhotoImage or None."""
        with self._lock:
            if frame_num in self.frames:
                # Move to end (LRU)
                self.frames.move_to_end(frame_num)
                return self.frames[frame_num]
            # Try raw frame and convert
            if frame_num in self.raw_frames:
                photo = self._convert_frame(self.raw_frames[frame_num])
                if photo:
                    self.frames[frame_num] = photo
                    return photo
        return None

    def put_raw_frame(self, frame_num: int, frame):
        """Store a raw BGR frame (called by decoder thread)."""
        with self._lock:
            if frame_num not in self.raw_frames:
                self.raw_frames[frame_num] = frame
                self._memory_bytes += frame.nbytes if hasattr(frame, 'nbytes') else 0
                self._cleanup_if_needed()

    def has_frame(self, frame_num: int) -> bool:
        """Check if frame is buffered."""
        with self._lock:
            return frame_num in self.frames or frame_num in self.raw_frames

    def get_buffered_range(self) -> tuple:
        """Get the range of buffered frames (min, max)."""
        with self._lock:
            all_frames = set(self.frames.keys()) | set(self.raw_frames.keys())
            if not all_frames:
                return (-1, -1)
            return (min(all_frames), max(all_frames))

    def clear_before(self, frame_num: int, keep_count: int = 5):
        """Clear frames before a certain point (keeping some for backward scrub)."""
        with self._lock:
            frames_to_remove = [f for f in list(self.raw_frames.keys())
                               if f < frame_num - keep_count]
            for f in frames_to_remove:
                if f in self.raw_frames:
                    frame_data = self.raw_frames[f]
                    self._memory_bytes -= frame_data.nbytes if hasattr(frame_data, 'nbytes') else 0
                    del self.raw_frames[f]
                if f in self.frames:
                    del self.frames[f]

    def clear(self):
        """Clear all buffered frames."""
        with self._lock:
            self.frames.clear()
            self.raw_frames.clear()
            self._memory_bytes = 0

    def _convert_frame(self, bgr_frame):
        """Convert BGR numpy array to PhotoImage."""
        try:
            if not OPENCV_AVAILABLE:
                return None
            rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            scale = min(self._frame_size[0] / w, self._frame_size[1] / h)
            if scale < 1.0:
                new_size = (int(w * scale), int(h * scale))
                rgb = cv2.resize(rgb, new_size, interpolation=cv2.INTER_LINEAR)
            img = Image.fromarray(rgb)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None  # Silent failure

    def _cleanup_if_needed(self):
        """Clean up old frames if memory limit exceeded."""
        while self._memory_bytes > self._max_memory_bytes and self.raw_frames:
            oldest = next(iter(self.raw_frames))
            frame_data = self.raw_frames[oldest]
            self._memory_bytes -= frame_data.nbytes if hasattr(frame_data, 'nbytes') else 0
            del self.raw_frames[oldest]
            if oldest in self.frames:
                del self.frames[oldest]


class BackgroundDecoder(threading.Thread):
    """
    Background thread that continuously decodes frames ahead of playback.
    Uses a priority queue to handle seek requests immediately.
    """

    def __init__(self, clip_path: str, frame_buffer: ClipFrameBuffer, start_frame: int = 0):
        super().__init__(daemon=True)
        self.clip_path = clip_path
        self.buffer = frame_buffer
        self._stop_event = threading.Event()
        self._target_frame = start_frame
        self._priority_frame = -1  # For immediate seek
        self._lock = threading.Lock()

    def run(self):
        """Main decode loop."""
        cap = None
        try:
            if not OPENCV_AVAILABLE:
                return

            cap = open_video_capture(self.clip_path)

            if not cap.isOpened():
                return

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            last_sequential_frame = -1

            while not self._stop_event.is_set():
                # Check for priority seek
                priority = self._get_priority_frame()
                if priority >= 0:
                    current_decode_frame = priority
                    self._clear_priority()
                    last_sequential_frame = -1  # Force seek
                else:
                    current_decode_frame = None

                # Get current target
                with self._lock:
                    target = self._target_frame

                # Determine what to decode
                buffer_start = max(0, target - 5)  # Keep some behind
                buffer_end = min(total_frames, target + self.buffer.buffer_size)

                # Find next frame to decode
                decode_frame = current_decode_frame
                if decode_frame is None:
                    for f in range(buffer_start, buffer_end):
                        if not self.buffer.has_frame(f):
                            decode_frame = f
                            break

                if decode_frame is None:
                    # Buffer is full ahead, wait a bit
                    time.sleep(0.005)
                    continue

                # Seek if not sequential
                if abs(decode_frame - last_sequential_frame) > 1:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, decode_frame)

                ret, frame = cap.read()
                if ret and frame is not None:
                    self.buffer.put_raw_frame(decode_frame, frame)
                    last_sequential_frame = decode_frame
                else:
                    # Read failed
                    last_sequential_frame = -1
                    time.sleep(0.001)

                # Clean up old frames periodically
                self.buffer.clear_before(target)

        except Exception:
            pass  # Silent failure
        finally:
            if cap:
                cap.release()

    def set_target(self, frame_num: int):
        """Set the target frame (called from main thread)."""
        with self._lock:
            self._target_frame = frame_num

    def seek_immediate(self, frame_num: int):
        """Request immediate seek to frame (priority decode)."""
        with self._lock:
            self._priority_frame = frame_num
            self._target_frame = frame_num

    def _get_priority_frame(self) -> int:
        with self._lock:
            return self._priority_frame

    def _clear_priority(self):
        with self._lock:
            self._priority_frame = -1

    def stop(self):
        """Stop the decoder thread."""
        self._stop_event.set()


class FrameBufferManager:
    """
    Central manager for all clip frame buffers.
    Handles multi-clip scenarios and buffer preloading.
    """

    def __init__(self, max_total_memory_mb: int = 500):
        self._buffers: Dict[str, ClipFrameBuffer] = {}  # clip_id -> buffer
        self._decoders: Dict[str, BackgroundDecoder] = {}  # clip_id -> decoder
        self._current_clip_id: Optional[str] = None
        self._lock = threading.Lock()
        self._max_memory = max_total_memory_mb * 1024 * 1024
        self._clips: List = []
        self._clip_in_offsets: Dict[str, float] = {}

    def set_clips(self, clips: list, in_offsets: dict = None):
        """Set the clip list for timeline playback."""
        self._clips = [c for c in clips if not getattr(c, 'is_audio_only', False)
                       and getattr(c, 'duration', 0) > 0]
        self._clip_in_offsets = in_offsets or {}

    def get_clip_id(self, clip) -> str:
        """Generate unique ID for a clip."""
        return f"{clip.path}_{id(clip)}"

    def prepare_clip(self, clip, start_frame: int = 0) -> ClipFrameBuffer:
        """Prepare a clip's buffer and start decoding."""
        clip_id = self.get_clip_id(clip)

        with self._lock:
            if clip_id in self._buffers:
                # Already prepared, update target
                if clip_id in self._decoders:
                    self._decoders[clip_id].set_target(start_frame)
                return self._buffers[clip_id]

            # Create new buffer
            buffer = ClipFrameBuffer(clip)
            buffer.in_offset_frames = self._get_in_offset_frames(clip)
            self._buffers[clip_id] = buffer

            # Start decoder
            decoder = BackgroundDecoder(clip.path, buffer, start_frame)
            self._decoders[clip_id] = decoder
            decoder.start()

            return buffer

    def get_frame(self, clip, frame_num: int):
        """Get a frame for display. Returns PhotoImage or None."""
        clip_id = self.get_clip_id(clip)

        with self._lock:
            if clip_id not in self._buffers:
                self.prepare_clip(clip, frame_num)
                return None  # Frame not ready yet

            buffer = self._buffers[clip_id]

            # Update decoder target
            if clip_id in self._decoders:
                self._decoders[clip_id].set_target(frame_num)

            return buffer.get_frame(frame_num)

    def seek_clip(self, clip, frame_num: int):
        """Seek within a clip (priority decode)."""
        clip_id = self.get_clip_id(clip)

        with self._lock:
            if clip_id in self._decoders:
                self._decoders[clip_id].seek_immediate(frame_num)

    def preload_next_clip(self, current_clip, current_time: float):
        """Preload the next clip in timeline order."""
        current_end = getattr(current_clip, 'timeline_start', 0) + getattr(current_clip, 'duration', 0)

        next_clip = None
        min_gap = float('inf')

        for clip in self._clips:
            clip_start = getattr(clip, 'timeline_start', 0)
            if clip_start >= current_end:
                gap = clip_start - current_end
                if gap < min_gap:
                    min_gap = gap
                    next_clip = clip

        if next_clip and min_gap < 2.0:  # Preload if within 2 seconds
            self.prepare_clip(next_clip, 0)

    def switch_clip(self, new_clip, frame_num: int):
        """Switch to a new clip (handles transition)."""
        new_clip_id = self.get_clip_id(new_clip)

        with self._lock:
            self._current_clip_id = new_clip_id

            if new_clip_id not in self._buffers:
                # Release lock before prepare_clip to avoid deadlock
                pass

        # Prepare outside lock
        if new_clip_id not in self._buffers:
            self.prepare_clip(new_clip, frame_num)
        else:
            with self._lock:
                if new_clip_id in self._decoders:
                    self._decoders[new_clip_id].set_target(frame_num)

        # Cleanup buffers for clips that are far from current
        self._cleanup_distant_buffers(new_clip)

    def get_buffer_status(self, clip) -> str:
        """Get debug status of buffer for a clip."""
        clip_id = self.get_clip_id(clip)
        with self._lock:
            if clip_id not in self._buffers:
                return "no_buffer"
            buffer = self._buffers[clip_id]
            min_f, max_f = buffer.get_buffered_range()
            raw_count = len(buffer.raw_frames)
            photo_count = len(buffer.frames)
            return f"raw={raw_count} photo={photo_count} range={min_f}-{max_f}"

    def _cleanup_distant_buffers(self, current_clip):
        """Remove buffers for clips far from current position."""
        current_time = getattr(current_clip, 'timeline_start', 0)

        clips_to_remove = []
        with self._lock:
            for clip_id, buffer in self._buffers.items():
                clip = buffer.clip
                if abs(getattr(clip, 'timeline_start', 0) - current_time) > 60:  # More than 60s away
                    clips_to_remove.append(clip_id)

        for clip_id in clips_to_remove:
            with self._lock:
                if clip_id in self._decoders:
                    self._decoders[clip_id].stop()
                    del self._decoders[clip_id]
                if clip_id in self._buffers:
                    self._buffers[clip_id].clear()
                    del self._buffers[clip_id]

    def _get_in_offset_frames(self, clip) -> int:
        """Get the IN offset in frames for a trimmed clip."""
        split_key = clip.path + f"_split_{id(clip)}"
        in_offset = self._clip_in_offsets.get(split_key,
                   self._clip_in_offsets.get(clip.path, 0))
        fps = getattr(clip, 'fps', 30.0)
        return int(in_offset * fps)

    def stop_all(self):
        """Stop all decoders and clear buffers."""
        with self._lock:
            for decoder in self._decoders.values():
                decoder.stop()
            self._decoders.clear()
            for buffer in self._buffers.values():
                buffer.clear()
            self._buffers.clear()


class FrameDisplayScheduler:
    """
    Schedules frame display based on audio master clock.
    Handles frame dropping and timing precision.
    """

    def __init__(self, audio_clock: AudioMasterClock,
                 buffer_manager: FrameBufferManager,
                 display_callback):
        self.audio_clock = audio_clock
        self.buffer_manager = buffer_manager
        self.display_callback = display_callback

        self._running = False
        self._current_clip = None
        self._clips: List = []
        self._last_displayed_frame = -1
        self._clip_in_offsets: Dict[str, float] = {}
        self._stats = {
            'frames_displayed': 0,
            'frames_dropped': 0,
        }

    def set_clips(self, clips: list, in_offsets: dict):
        """Set clips for timeline playback."""
        self._clips = [c for c in clips if not getattr(c, 'is_audio_only', False)
                       and getattr(c, 'duration', 0) > 0]
        self._clip_in_offsets = in_offsets or {}
        self.buffer_manager.set_clips(clips, in_offsets)

    def start(self):
        """Start the scheduler."""
        self._running = True
        self._last_displayed_frame = -1
        self._current_clip = None

    def stop(self):
        """Stop the scheduler."""
        self._running = False

    def tick(self):
        """Called from main loop - displays appropriate frame for current time."""
        if not self._running:
            return

        # Get current time from audio clock
        current_time = self.audio_clock.time_seconds

        # Find clip at current time
        clip = self._find_clip_at_time(current_time)

        if clip is None:
            # No video at this time
            self._current_clip = None
            return

        # Handle clip change
        if clip != self._current_clip:
            self._on_clip_change(clip, current_time)
            self._current_clip = clip

        # Calculate target frame
        position_in_clip = current_time - getattr(clip, 'timeline_start', 0)
        in_offset = self._get_in_offset(clip)
        position_in_file = position_in_clip + in_offset

        fps = getattr(clip, 'fps', 30.0)
        if fps <= 0:
            fps = 30.0
        target_frame = int(position_in_file * fps)

        # Skip if same frame
        if target_frame == self._last_displayed_frame:
            return

        # Check for frame dropping
        if target_frame > self._last_displayed_frame + 1 and self._last_displayed_frame >= 0:
            frames_skipped = target_frame - self._last_displayed_frame - 1
            self._stats['frames_dropped'] += frames_skipped

        # Get frame from buffer
        photo = self.buffer_manager.get_frame(clip, target_frame)

        if photo:
            try:
                self.display_callback(photo)
                self._last_displayed_frame = target_frame
                self._stats['frames_displayed'] += 1
            except Exception:
                pass  # Silently handle display errors for smooth playback
        else:
            # Frame not ready - try previous frame
            for offset in range(1, 5):
                photo = self.buffer_manager.get_frame(clip, target_frame - offset)
                if photo:
                    try:
                        self.display_callback(photo)
                    except:
                        pass
                    break

        # Preload next clip if approaching end
        time_remaining = (getattr(clip, 'timeline_start', 0) + getattr(clip, 'duration', 0)) - current_time
        if time_remaining < 2.0:
            self.buffer_manager.preload_next_clip(clip, current_time)

    def seek(self, time_seconds: float):
        """Handle seek operation."""
        self._last_displayed_frame = -1

        clip = self._find_clip_at_time(time_seconds)
        if clip:
            position_in_clip = time_seconds - getattr(clip, 'timeline_start', 0)
            in_offset = self._get_in_offset(clip)
            position_in_file = position_in_clip + in_offset
            fps = getattr(clip, 'fps', 30.0)
            if fps <= 0:
                fps = 30.0
            target_frame = int(position_in_file * fps)

            self.buffer_manager.seek_clip(clip, target_frame)

    def _find_clip_at_time(self, time_seconds: float):
        """Find video clip at the given time."""
        for clip in self._clips:
            clip_start = getattr(clip, 'timeline_start', 0)
            clip_duration = getattr(clip, 'duration', 0)
            if clip_start <= time_seconds < clip_start + clip_duration:
                return clip
        return None

    def _on_clip_change(self, new_clip, current_time: float):
        """Handle transition to a new clip."""
        position_in_clip = current_time - getattr(new_clip, 'timeline_start', 0)
        in_offset = self._get_in_offset(new_clip)
        fps = getattr(new_clip, 'fps', 30.0)
        if fps <= 0:
            fps = 30.0
        start_frame = int((position_in_clip + in_offset) * fps)

        self.buffer_manager.switch_clip(new_clip, start_frame)
        self._last_displayed_frame = -1

    def _get_in_offset(self, clip) -> float:
        """Get the IN offset for a clip."""
        split_key = clip.path + f"_split_{id(clip)}"
        return self._clip_in_offsets.get(split_key,
               self._clip_in_offsets.get(clip.path, 0))

    def get_stats(self) -> dict:
        """Get playback statistics."""
        return self._stats.copy()


# =============================================================================
# Main Application - Sidus TC Sync Style
# =============================================================================

class SegmentAudioCache:
    """Audio for timeline playback, decoded by FFmpeg in SEG-second pieces on ONE worker thread and kept in an LRU
    cache with a memory limit. Works for every file type (camera MP4/MOV as well as WAV) and never holds a whole
    long recording in memory. The audio callback only reads (read() never waits and never starts FFmpeg - a
    missing piece is queued and that clip is silent for the moment); prefetch() asks ahead of the playhead."""
    SEG = 30.0               # seconds per piece (48 kHz stereo float32 = 11.5 MB)
    RATE = 48000

    def __init__(self, max_bytes: int = 512 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._segs = OrderedDict()   # (path, index) -> np.ndarray (n, 2) float32
        self._bytes = 0
        self._cv = threading.Condition()
        self._queue = deque()        # (path, index); urgent requests go to the front
        self._pending = set()
        self._failed = set()
        self._thread = None

    def clear(self):
        with self._cv:
            self._segs.clear()
            self._bytes = 0
            self._queue.clear()
            self._pending.clear()
            self._failed.clear()

    def request(self, path: str, index: int, urgent: bool = False):
        key = (path, index)
        with self._cv:
            if key in self._segs or key in self._pending or key in self._failed:
                return
            self._pending.add(key)
            (self._queue.appendleft if urgent else self._queue.append)(key)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True, name="segment-audio")
                self._thread.start()
            self._cv.notify()

    def prefetch(self, path: str, start_s: float, end_s: float):
        if end_s <= start_s:
            return
        first = max(0, int(start_s // self.SEG))
        last = max(first, int(end_s // self.SEG))
        for i in range(first, last + 1):
            self.request(path, i)

    def read(self, path: str, start_sample: int, frames: int):
        """(frames, 2) float32 from the cache, or None when the piece isn't decoded yet (it gets queued)."""
        if start_sample < 0:
            return None
        seg_len = int(self.SEG * self.RATE)
        out = None
        filled = 0
        pos = start_sample
        while filled < frames:
            idx, off = divmod(pos, seg_len)
            with self._cv:
                data = self._segs.get((path, idx))
                if data is not None:
                    self._segs.move_to_end((path, idx))
            if data is None:
                self.request(path, idx, urgent=True)
                break
            take = min(frames - filled, len(data) - off)
            if take <= 0:
                break   # past the end of the file
            if out is None:
                out = np.zeros((frames, 2), dtype=np.float32)
            out[filled:filled + take] = data[off:off + take]
            filled += take
            pos += take
            if len(data) < seg_len:
                break   # that was the last piece of the file
        return out

    def _worker(self):
        while True:
            with self._cv:
                while not self._queue:
                    if not self._cv.wait(timeout=30):
                        self._thread = None   # idle: the next request starts a new worker
                        return
                key = self._queue.popleft()
            path, idx = key
            data = None
            try:
                cmd = [FFMPEG_PATH or 'ffmpeg', '-v', 'error', '-ss', f"{idx * self.SEG:.3f}", '-t', f"{self.SEG:.3f}",
                       '-i', path, '-vn', '-ac', '2', '-ar', str(self.RATE), '-f', 's16le', '-acodec', 'pcm_s16le', '-']
                kwargs = {'capture_output': True, 'timeout': 60}
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                r = subprocess.run(cmd, **kwargs)
                if r.returncode == 0 and len(r.stdout) >= 4:
                    raw = r.stdout[:len(r.stdout) // 4 * 4]
                    data = (np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float32) / 32768.0)
            except Exception:
                data = None
            with self._cv:
                self._pending.discard(key)
                if data is None or len(data) == 0:
                    self._failed.add(key)   # no audio there (or past the end): don't retry every tick
                    continue
                self._segs[key] = data
                self._bytes += data.nbytes
                while self._bytes > self.max_bytes and len(self._segs) > 1:
                    _, old = self._segs.popitem(last=False)
                    self._bytes -= old.nbytes


class LTCSyncApp:
    """LTC Timecode Sync - Sidus TC Sync Style Interface."""

    COLORS = {
        # Main UI colors (DaVinci Resolve inspired)
        'bg': '#1a1a1f',
        'bg_light': '#252530',
        'bg_card': '#2a2a35',
        'bg_header': '#1e1e26',
        'accent': '#e04050',
        'accent_hover': '#ff5566',
        'text': '#e8e8e8',
        'text_dim': '#707080',
        'text_bright': '#ffffff',
        'success': '#4ade80',
        'warning': '#fbbf24',
        'error': '#ef4444',

        # Timeline specific colors
        'timeline_bg': '#141418',
        'timeline_ruler': '#252530',
        'timeline_grid': '#2a2a35',
        'track_header': '#1e1e26',
        'track_divider': '#3a3a48',
        'playhead': '#ff4455',
        'playhead_line': '#ff6666',

        # Video track colors (warm tones)
        'video_track_colors': ['#5b8def', '#ef6b9b', '#6bdfaa', '#efaa4b', '#9b7fef', '#5bdfef'],

        # Audio track colors (cool tones - darker, more subdued)
        'audio_track_colors': ['#3a6bbf', '#bf4a7b', '#4abf8a', '#bf8a3a', '#7b5fbf', '#3abfbf'],

        # Track enabled/disabled
        'track_enabled': '#4ade80',
        'track_disabled': '#555566',
        'track_muted': '#ef4444',

        # Legacy compatibility
        'clip_colors': ['#5b8def', '#ef6b9b', '#6bdfaa', '#efaa4b', '#9b7fef', '#5bdfef']
    }

    # Version info
    VERSION = "1.0.0"
    APP_NAME = "LTC Sync App"
    BUILD_DATE = "2025-01"

    # Settings file location
    SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ltc_sync_settings.json')

    def __init__(self, root):
        self.root = root
        self.root.title("LTC Timecode Sync")
        self.root.geometry("1200x800")
        self.root.configure(bg=self.COLORS['bg'])
        self.root.minsize(900, 600)

        self.clips: List[MediaClip] = []
        self.analyzer = MediaAnalyzer()
        self.queue = queue.Queue()
        self.analyzing = False
        self.synced = False
        self.timeline_locked = True  # Timeline locked by default (playback only)
        self.timeline_zoom = 1.0
        self.timeline_offset = 0.0
        self.zoom_focus_mode = "Cursor"  # "Playhead" or "Cursor" - controls zoom center point
        self.camera_id_source = "LTC User Bits"  # "LTC User Bits" or "Filename" - priority for camera ID detection
        self.analysis_threads = "Auto"  # files analysed at once: "Auto" or a number
        self.decode_gpu = "Auto"  # GPU used for video decoding: "Auto" or a GPU name

        # Camera color mapping (camera_id -> color)
        self.camera_color_map = {}
        self.next_color_index = 0

        # Track management for NLE-style timeline
        self.video_tracks_enabled = True  # Toggle all video tracks
        self.audio_tracks_enabled = True  # Toggle all audio tracks
        self.track_states = {}  # {track_name: {'enabled': True, 'muted': False}}
        self.track_height = 45  # Height of each track in pixels
        self.track_header_width = 140  # Width of track header panel

        # Video preview state
        self.selected_clip: Optional[MediaClip] = None
        self.selected_clips: List[MediaClip] = []  # For multi-select
        self.last_clicked_index: int = -1  # For shift+click range selection
        self.preview_playing = False
        self.preview_position = 0.0  # Current position in seconds
        self.preview_frame_cache = {}  # Cache thumbnail frames
        self.preview_update_id = None
        self.playback_speed = 1.0  # Playback speed multiplier (negative = reverse)
        self.playback_speed_levels = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]  # Available speed levels

        # Frame buffer for smooth playback (FFmpeg fallback)
        self.frame_buffer = {}  # {frame_number: PhotoImage}
        self.frame_buffer_clip = None  # Path of clip in buffer
        self.frame_buffer_fps = 30  # Frame rate for buffer
        self.frame_buffer_lock = threading.Lock()
        self.frame_buffer_extracting = False
        self.frame_buffer_target_start = 0  # Target start frame to extract
        self.frame_buffer_size = 60  # Number of frames to keep in buffer

        # OpenCV video playback (faster than FFmpeg subprocess)
        self.cv_capture = None  # cv2.VideoCapture object
        self.cv_capture_path = None  # Path of currently opened video
        self.cv_frame_cache = {}  # {frame_number: PhotoImage} - recent frames
        self.cv_cache_size = 60  # Keep more frames cached for smooth playback
        self.cv_playback_thread = None
        self.cv_playback_running = False
        self.cv_frame_queue = queue.Queue(maxsize=10)  # Raw frames from thread
        self.cv_last_requested_frame = -1  # Last frame number requested
        self.cv_capture_lock = threading.Lock()  # Lock for VideoCapture access

        # VLC video playback (hardware-accelerated, smooth playback)
        self.vlc_instance = None  # VLC instance
        self.vlc_player = None  # VLC media player
        self.vlc_media = None  # Current VLC media
        self.vlc_media_path = None  # Path of current media
        # Direct GPU playback engine (see HardwarePlayer)
        self.hw_player = HardwarePlayer() if AV_AVAILABLE else None
        self._direct_display = False     # preview canvas currently shows engine frames
        self._direct_still_id = None     # latest still request we want to show
        self._direct_play_id = None      # current play request (timeline playback)
        self._direct_play_path = None
        self._direct_last_frame_t = None
        self._direct_poll_id = None
        self._direct_play_started = 0.0
        self._direct_warm_id = None
        self._direct_start_latency = 0.3   # learned: request -> first frame (s)
        self._direct_first_pending = False
        self._direct_next_check = 0.0
        # Scrub clip switches run on a worker thread (see _vlc_switch_media_for_scrub)
        self._vlc_switch_lock = threading.Lock()
        self._vlc_switch_target = (None, 0.0)  # (path, seconds in file) - latest wins
        self._vlc_switch_busy = False
        self._vlc_switch_thread = None
        self.vlc_frame = None  # Frame widget for VLC to render into
        self.vlc_position_update_id = None  # ID for position update timer
        self.use_vlc = VLC_AVAILABLE  # Use VLC if available

        # Frame buffer playback system (audio master clock architecture)
        self.use_frame_buffer_system = True  # Re-enabled - scrubbing works great, fixing playback
        self.audio_master_clock = None  # AudioMasterClock instance
        self.frame_buffer_manager = None  # FrameBufferManager instance
        self.frame_display_scheduler = None  # FrameDisplayScheduler instance

        # Audio playback state
        self.audio_stream = None  # sounddevice stream
        self.audio_data = None  # Raw audio data for current clip
        self.audio_sample_rate = 48000
        self.audio_position = 0  # Current sample position
        self.audio_cache = {}  # {clip_path: (audio_data, sample_rate)}
        self.seg_audio = SegmentAudioCache()  # timeline playback audio in 30 s pieces, memory-capped

        # Multi-track timeline audio mixing
        self.timeline_audio_mode = True  # Use multi-track mixing for timeline
        self.audio_extraction_queue = []  # Queue of clips to extract audio from
        self.audio_extraction_running = False
        self.timeline_audio_ready = False  # True when all audio is extracted
        self.timeline_audio_position = 0.0  # Audio thread's own position tracker (seconds)

        # Timeline thumbnail cache
        self.timeline_thumbnails = {}  # {clip_path: PhotoImage}
        self.thumbnail_extraction_queue = []
        self.thumbnail_extraction_running = False

        # Audio waveform cache for timeline
        self.audio_waveform_cache = {}  # {clip_path: list of peak values}
        self.waveform_extraction_queue = []
        self.waveform_extraction_running = False

        # Persistent waveform disk cache (survives app restarts)
        self.waveform_cache_custom_dir = None  # Custom cache directory (None = use default)
        self.waveform_cache_dir = self._init_waveform_cache_dir()
        self._sync_proxy_dir()
        self.waveform_cache_max_size_gb = 7  # Max cache size in GB (between 5-10GB)

        # Timeline playback state
        self.timeline_playing = False
        self.timeline_update_id = None
        self.timeline_start_time = 0.0  # Start of timeline (earliest clip)
        self.timeline_end_time = 0.0
        self.playhead_item_line = None  # Canvas item ID for playhead line
        self.playhead_item_handle = None  # Canvas item ID for playhead handle
        self.timeline_total_height = 500  # Track total height for playhead
        self.timeline_last_clip = None  # Last clip during timeline playback (avoid redundant updates)
        self.timeline_playback_start_time = None  # Wall clock time when playback started
        self.timeline_playback_start_pos = 0.0  # Playhead position when playback started

        # Playback optimization state
        self._is_scrubbing = False  # True when user is dragging playhead
        self._last_scrub_update = 0  # Last time scrub preview was updated
        self._ui_update_counter = 0  # Counter for throttled UI updates during playback
        self._deferred_redraw_id = None  # Timer ID for deferred timeline redraw
        self._last_auto_scroll_time = 0  # Throttle auto-scroll rate
        self._current_cursor = ''  # Track current cursor to avoid redundant configure() calls
        self._clip_draw_positions = {}  # Cache: {clip_id: (x, y, width)} for selection updates
        self._clip_hit_rects = []       # [(x, y, width, clip)] in draw order - clicks are tested against these
        # Paths whose embedded-TC probe already ran this session (analysis or
        # refresh) - _link_audio_tracks must not re-run a known-failed ffprobe
        # on the main thread during every first sync
        self._embedded_probe_attempted = set()

        # Timecode display cache (avoid iterating clips on every update)
        self._tc_ref_clip = None
        self._tc_ref_fps = 30.0
        self._tc_min_frames = 0

        # Last used directory for file dialogs
        self.last_directory = os.path.expanduser("~")

        # Current project path
        self.current_project_path = None

        # Recent files list (max 10)
        self.recent_files = []
        self.MAX_RECENT_FILES = 10


        # Timeline markers
        self.timeline_markers = []  # List of {time: float, color: str, label: str}
        self.marker_colors = ['#ff4444', '#44ff44', '#4444ff', '#ffff44', '#ff44ff', '#44ffff', '#ff8800', '#00ff88']
        self.next_marker_color = 0

        # Clip trimming state
        self.trim_mode = None  # 'in', 'out', or None
        self.trim_clip = None
        self.trim_start_x = 0
        self.trim_original_start = 0
        self.trim_original_duration = 0
        self.clip_in_offsets = {}  # {clip_path: in_offset} - trimmed in points
        self.clip_out_offsets = {}  # {clip_path: out_offset} - trimmed out points

        # Split clips tracking
        self.split_clips = {}  # {original_path: [(split_clip_1, split_clip_2), ...]}

        # Undo/Redo system
        self.undo_stack = []  # List of actions that can be undone
        self.redo_stack = []  # List of actions that can be redone
        self.max_undo_levels = 50

        # Clip snapping
        self.snap_enabled = True
        self.snap_threshold = 10  # pixels
        self.snap_to_playhead = True
        self.snap_to_markers = True
        self.snap_to_clips = True
        self.snap_indicator_pos = None  # Position of snap indicator line (time)
        self.snap_indicator_type = None  # Type of snap target

        # In/Out points for range selection
        self.in_point = None  # Time in seconds
        self.out_point = None  # Time in seconds

        # Loop playback between IN/OUT points (P key)
        self.loop_playback = False

        # Seek/playback coordination
        self._resume_after_scrub = False   # Auto-resume playback after ruler scrub
        self._audio_seek_pending = False   # Audio mixer must snap to playhead on next callback
        self._audio_output_latency = 0.0   # Measured output latency for A/V alignment
        self._audio_smooth_pos = 0.0       # Mixer position (audio-callback thread)
        self._last_vlc_drift_check = 0.0   # Rate limiter for VLC drift correction

        # Clip locking
        self.locked_clips = set()  # Set of locked clip paths

        # Clip grouping
        self.clip_groups = {}  # {group_id: [clip_paths]}
        self.clip_to_group = {}  # {clip_path: group_id}
        self.next_group_id = 1

        # Track mute/solo state (for audio)
        self.muted_tracks = set()  # Set of muted track IDs
        # Export: camera name -> camera audio channels (1-based) to keep.
        # Cameras not listed keep every non-LTC channel.
        self.camera_audio_selection = {}
        # Per-camera offset in frames (+ = that camera's clips sit later on the timeline). A camera's
        # LTC is recorded through its audio path, whose delay differs per model / mode (R6 vs R8).
        self.camera_offsets = {}
        self.soloed_tracks = set()  # Set of soloed track IDs

        # Video track hide/solo state (separate from audio mute/solo)
        self.hidden_video_tracks = set()  # Set of hidden video track names
        self.soloed_video_tracks = set()  # Set of soloed video track names

        # Auto-save
        self.auto_save_enabled = True
        self.auto_save_interval = 60000  # 60 seconds
        self.auto_save_id = None
        self.last_auto_save_time = 0

        self._setup_styles()
        self._build_ui()
        self._setup_keybindings()
        self._load_settings()  # Load saved settings
        self._update_loop()

    def _setup_keybindings(self):
        """Setup keyboard shortcuts."""
        # File operations
        self.root.bind('<Control-o>', lambda e: self._add_files())
        self.root.bind('<Control-O>', lambda e: self._add_files())
        self.root.bind('<Control-Shift-o>', lambda e: self._add_folder())
        self.root.bind('<Control-Shift-O>', lambda e: self._add_folder())

        # Analysis (use Shift+Ctrl+A for analyze, Ctrl+A for select all)
        self.root.bind('<Control-Shift-a>', lambda e: self._analyze_clips())
        self.root.bind('<Control-Shift-A>', lambda e: self._analyze_clips())
        self.root.bind('<Control-a>', lambda e: self._select_all_clips())
        self.root.bind('<Control-A>', lambda e: self._select_all_clips())
        self.root.bind('<Control-r>', lambda e: self._reanalyze_selected())
        self.root.bind('<Control-R>', lambda e: self._reanalyze_selected())
        self.root.bind('<Control-Shift-r>', lambda e: self._reanalyze_all())
        self.root.bind('<Control-Shift-R>', lambda e: self._reanalyze_all())

        # Refresh embedded timecode (Shift+T to avoid conflict with copy TC)
        self.root.bind('<Control-Shift-t>', lambda e: self._refresh_embedded_timecode())
        self.root.bind('<Control-Shift-T>', lambda e: self._refresh_embedded_timecode())

        # Sync and export
        self.root.bind('<Control-s>', lambda e: self._save_project())
        self.root.bind('<Control-S>', lambda e: self._save_project())
        self.root.bind('<Control-Shift-s>', lambda e: self._sync_clips())
        self.root.bind('<Control-Shift-S>', lambda e: self._sync_clips())
        self.root.bind('<Control-Alt-s>', lambda e: self._toggle_auto_save())
        self.root.bind('<Control-Alt-S>', lambda e: self._toggle_auto_save())
        self.root.bind('<Control-e>', lambda e: self._export_xml())
        self.root.bind('<Control-E>', lambda e: self._export_xml())
        self.root.bind('<Control-p>', lambda e: self._load_project())
        self.root.bind('<Control-P>', lambda e: self._load_project())

        # Markers
        self.root.bind('<m>', lambda e: self._add_marker_at_playhead())
        self.root.bind('<M>', lambda e: self._add_marker_at_playhead())
        self.root.bind('<Shift-m>', lambda e: self._delete_nearest_marker())
        self.root.bind('<Shift-M>', lambda e: self._delete_nearest_marker())
        self.root.bind('<Control-Right>', lambda e: self._go_to_next_marker())
        self.root.bind('<Control-Left>', lambda e: self._go_to_prev_marker())

        # Clip operations
        self.root.bind('<u>', lambda e: self._reset_clip_trim())
        self.root.bind('<U>', lambda e: self._reset_clip_trim())
        self.root.bind('<s>', lambda e: self._split_clip_at_playhead())
        self.root.bind('<S>', lambda e: self._split_clip_at_playhead())
        self.root.bind('<Control-b>', lambda e: self._split_clip_at_playhead())
        self.root.bind('<Control-B>', lambda e: self._split_clip_at_playhead())
        # NOTE: lowercase <l> belongs to J/K/L transport (bound below).
        # Clip lock uses Ctrl+L and Shift+L to avoid the conflict.
        self.root.bind('<Control-l>', lambda e: self._toggle_clip_lock())
        self.root.bind('<L>', lambda e: self._toggle_clip_lock())

        # In/Out points
        self.root.bind('<i>', lambda e: self._set_in_point())
        self.root.bind('<I>', lambda e: self._set_in_point())
        self.root.bind('<o>', lambda e: self._set_out_point())
        self.root.bind('<O>', lambda e: self._set_out_point())
        self.root.bind('<Control-i>', lambda e: self._go_to_in_point())
        self.root.bind('<Control-I>', lambda e: self._go_to_in_point())
        self.root.bind('<Alt-o>', lambda e: self._go_to_out_point())
        self.root.bind('<x>', lambda e: self._clear_in_out_points())
        self.root.bind('<X>', lambda e: self._clear_in_out_points())

        # Undo/Redo
        self.root.bind('<Control-z>', lambda e: self._undo())
        self.root.bind('<Control-Z>', lambda e: self._undo())
        self.root.bind('<Control-y>', lambda e: self._redo())
        self.root.bind('<Control-Y>', lambda e: self._redo())
        self.root.bind('<Control-Shift-z>', lambda e: self._redo())
        self.root.bind('<Control-Shift-Z>', lambda e: self._redo())

        # Snapping toggle
        self.root.bind('<n>', lambda e: self._toggle_snapping())
        self.root.bind('<N>', lambda e: self._toggle_snapping())

        # Ripple delete
        self.root.bind('<Shift-Delete>', lambda e: self._ripple_delete())

        # Clip grouping
        self.root.bind('<Control-g>', lambda e: self._group_selected_clips())
        self.root.bind('<Control-G>', lambda e: self._group_selected_clips())
        self.root.bind('<Control-Shift-g>', lambda e: self._ungroup_selected_clips())
        self.root.bind('<Control-Shift-G>', lambda e: self._ungroup_selected_clips())

        # Duplicate clip
        self.root.bind('<Control-d>', lambda e: self._duplicate_selected_clips())
        self.root.bind('<Control-D>', lambda e: self._duplicate_selected_clips())

        # Clip nudging (move by small amount)
        self.root.bind('<Alt-Left>', lambda e: self._nudge_clips(-1))
        self.root.bind('<Alt-Right>', lambda e: self._nudge_clips(1))
        self.root.bind('<Alt-Shift-Left>', lambda e: self._nudge_clips(-10))
        self.root.bind('<Alt-Shift-Right>', lambda e: self._nudge_clips(10))

        # Go to clip boundaries
        self.root.bind('<bracketleft>', lambda e: self._go_to_clip_start())
        self.root.bind('<bracketright>', lambda e: self._go_to_clip_end())

        # Search
        self.root.bind('<Control-f>', lambda e: self._focus_search())
        self.root.bind('<Control-F>', lambda e: self._focus_search())

        # Copy timecode
        self.root.bind('<Control-t>', lambda e: self._copy_current_tc())
        self.root.bind('<Control-T>', lambda e: self._copy_current_tc())

        # Clip management
        self.root.bind('<Delete>', lambda e: self._remove_selected())
        self.root.bind('<Control-Delete>', lambda e: self._clear_all_clips())
        self.root.bind('<Escape>', lambda e: self._clear_selection())

        # Preview controls - use bind_all with 'break' to prevent button activation
        self.root.bind_all('<space>', self._handle_spacebar)

        # J/K/L playback controls (standard NLE shortcuts)
        self.root.bind('<j>', lambda e: self._jkl_reverse())
        self.root.bind('<J>', lambda e: self._jkl_reverse())
        self.root.bind('<k>', lambda e: self._jkl_pause())
        self.root.bind('<K>', lambda e: self._jkl_pause())
        self.root.bind('<l>', lambda e: self._jkl_forward())
        # Note: Shift+L = clip lock toggle, so it won't trigger forward

        # Loop playback between IN/OUT points
        self.root.bind('<p>', lambda e: self._toggle_loop_playback())
        self.root.bind('<P>', lambda e: self._toggle_loop_playback())

        # Timeline navigation
        self.root.bind('<Left>', lambda e: self._timeline_step(-1))
        self.root.bind('<Right>', lambda e: self._timeline_step(1))
        self.root.bind('<Control-Left>', lambda e: self._timeline_step(-10))
        self.root.bind('<Control-Right>', lambda e: self._timeline_step(10))

        # Timeline zoom
        self.root.bind('<plus>', lambda e: self._zoom_timeline(1.2))
        self.root.bind('<minus>', lambda e: self._zoom_timeline(0.8))
        self.root.bind('<equal>', lambda e: self._zoom_timeline(1.2))  # + without shift
        self.root.bind('<Control-plus>', lambda e: self._zoom_timeline(2.0))
        self.root.bind('<Control-minus>', lambda e: self._zoom_timeline(0.5))
        self.root.bind('<Control-0>', lambda e: self._reset_timeline_zoom())

        # Clip navigation
        self.root.bind('<Up>', lambda e: self._select_prev_clip())
        self.root.bind('<Down>', lambda e: self._select_next_clip())
        self.root.bind('<Home>', lambda e: self._select_first_clip())
        self.root.bind('<End>', lambda e: self._select_last_clip())

        # Help
        self.root.bind('<F1>', lambda e: self._show_keyboard_shortcuts())

    def _reanalyze_selected(self):
        """Re-analyze the selected clip(s); with nothing selected, offer all."""
        if self.analyzing:
            self.status_label.configure(text="Analysis already in progress - wait for it to finish")
            return
        clips_to_analyze = self.selected_clips if self.selected_clips else ([self.selected_clip] if self.selected_clip else [])
        if not clips_to_analyze:
            # Used to do nothing at all with no selection
            self._reanalyze_all()
            return
        if clips_to_analyze:
            # Pause playback if running (re-analysis modifies clip states)
            if self.timeline_playing:
                self._toggle_timeline_playback()
            for clip in clips_to_analyze:
                clip.status = 'pending'
            self._refresh_clips_list()
            self._analyze_clips()
            if len(clips_to_analyze) > 1:
                self.status_label.configure(text=f"Re-analyzing {len(clips_to_analyze)} clips...")

    def _reanalyze_all(self):
        """Re-analyze all clips in the project."""
        if not self.clips:
            self.status_label.configure(text="No clips to re-analyze")
            return

        # Stop playback if running (re-analysis modifies clip states)
        if self.timeline_playing:
            self._stop_timeline()
        if self.preview_playing:
            self._reset_preview()

        if self.analyzing:
            self.status_label.configure(text="Analysis already in progress...")
            return

        # Confirm if there are many clips
        if len(self.clips) > 5:
            if not messagebox.askyesno("Re-analyze All",
                                       f"Re-analyze all {len(self.clips)} clips?\n\n"
                                       "This will reset and re-scan all clip timecodes."):
                return

        # Reset all clips to pending
        for clip in self.clips:
            clip.status = 'pending'

        self._refresh_clips_list()
        self._analyze_clips()
        self.status_label.configure(text=f"Re-analyzing all {len(self.clips)} clips...")

    def _refresh_embedded_timecode(self):
        """Refresh embedded camera timecode for all clips (fast, no LTC re-analysis).

        Runs in a worker thread: each clip is a synchronous ffprobe (up to 30s
        timeout on slow storage) - doing that on the Tk main thread froze the
        window for the whole batch."""
        if not self.clips:
            self.status_label.configure(text="No clips to refresh")
            return
        if getattr(self, '_embedded_refresh_running', False):
            self.status_label.configure(text="Embedded TC refresh already running...")
            return
        self._embedded_refresh_running = True
        self.status_label.configure(text="Refreshing embedded TC...")
        clips_snapshot = list(self.clips)

        def worker():
            updated = 0
            failed = 0
            total = len(clips_snapshot)
            for idx, clip in enumerate(clips_snapshot):
                if not os.path.exists(clip.path):
                    failed += 1
                    continue
                try:
                    fps = clip.fps if clip.fps else 30.0
                    tc, frames, bwf_ref = self.analyzer._get_embedded_timecode(clip.path, fps)
                    self._embedded_probe_attempted.add(clip.path)
                    if tc:
                        clip.embedded_tc = tc
                        clip.embedded_tc_frames = frames
                        clip.bwf_time_reference = bwf_ref
                        updated += 1
                    else:
                        # Fall back to LTC if no embedded TC found
                        if clip.start_tc:
                            clip.embedded_tc = clip.start_tc
                            clip.embedded_tc_frames = clip.start_frames
                        failed += 1
                except Exception:
                    failed += 1
                if (idx + 1) % 10 == 0:
                    done = idx + 1
                    try:
                        self.root.after(0, lambda d=done, t=total: self.status_label.configure(
                            text=f"Refreshing embedded TC... ({d}/{t})"))
                    except Exception:
                        pass

            def finish():
                self._embedded_refresh_running = False
                self._refresh_clips_list()
                self.status_label.configure(
                    text=f"Refreshed embedded TC: {updated} updated, {failed} failed/no TC")
            try:
                self.root.after(0, finish)
            except Exception:
                self._embedded_refresh_running = False

        threading.Thread(target=worker, daemon=True, name="EmbeddedTCRefresh").start()

    def _remove_selected(self):
        """Remove the selected clip(s)."""
        clips_to_remove = self.selected_clips if self.selected_clips else ([self.selected_clip] if self.selected_clip else [])
        if clips_to_remove:
            # Pause playback if running (removing clips can cause sync issues)
            if self.timeline_playing:
                self._toggle_timeline_playback()  # Pause playback
            # Stop an active clip-preview session too - otherwise removing the
            # previewing clip leaves an orphaned session that keeps playing and
            # cannot be stopped (Pause bails on selected_clip=None)
            if self.preview_playing:
                self._force_stop_preview()
            count = len(clips_to_remove)
            for clip in clips_to_remove:
                if clip in self.clips:
                    self.clips.remove(clip)
                    # Clean up audio cache to free memory
                    if clip.path in self.audio_cache:
                        del self.audio_cache[clip.path]
            self.selected_clip = None
            self.selected_clips = []
            self.last_clicked_index = -1
            self._refresh_clips_list()
            self._draw_timeline()
            self.status_label.configure(text=f"Removed {count} clip(s)")

    def _clear_all_clips(self):
        """Remove all clips and reset project state."""
        if self.clips:
            if messagebox.askyesno("Clear All", "Remove all clips and reset the project?"):
                # Stop playback if running to prevent errors
                if self.timeline_playing:
                    self._stop_timeline()
                if self.preview_playing:
                    self._reset_preview()

                # Clear clips list
                self.clips.clear()
                self.selected_clip = None
                self.selected_clips = []
                self.last_clicked_index = -1

                # Reset camera colors
                self.camera_color_map.clear()
                self.next_color_index = 0

                # Clear all caches to free memory
                self.audio_cache.clear()
                self.seg_audio.clear()
                self.timeline_thumbnails.clear()
                self.audio_waveform_cache.clear()
                self.cv_frame_cache.clear()
                self.frame_buffer.clear()
                self.preview_frame_cache.clear()

                # Clear trim/edit state
                self.clip_in_offsets.clear()
                self.clip_out_offsets.clear()
                self.split_clips.clear()

                # Clear playback-range state
                self.in_point = None
                self.out_point = None
                self.loop_playback = False

                # Clear grouping
                self.clip_groups.clear()
                self.clip_to_group.clear()

                # Clear track states
                self.track_states.clear()

                # Clear timeline markers
                self.timeline_markers.clear()

                # Clear undo/redo history
                self.undo_stack.clear()
                self.redo_stack.clear()

                # Reset sync state
                self.synced = False

                # Reset project path (new unsaved project)
                self.current_project_path = None

                # Invalidate timecode cache
                self._invalidate_tc_cache()

                # Clear clip draw positions cache
                self._clip_draw_positions.clear()
                self._clip_hit_rects.clear()

                # Refresh UI
                self._refresh_clips_list()
                self._draw_timeline()
                self._reset_preview()

                # Update window title to show no project
                self.root.title("LTC Sync App")

                # Update status
                self.status_label.configure(text="Project cleared - Ready to add files")

    def _select_all_clips(self):
        """Select all clips."""
        if self.clips:
            self.selected_clips = list(self.clips)
            self.selected_clip = self.selected_clips[-1] if self.selected_clips else None
            self._update_clip_selection_display()
            self.status_label.configure(text=f"Selected all {len(self.clips)} clips")

    def _clear_selection(self):
        """Clear all selections."""
        self.selected_clips = []
        self.selected_clip = None
        self.last_clicked_index = -1
        self._update_clip_selection_display()
        self.status_label.configure(text="Selection cleared")

    def _select_prev_clip(self):
        """Select the previous clip in the list."""
        if not self.clips:
            return
        filtered = self._get_filtered_clips()
        if not filtered:
            return

        if self.selected_clip and self.selected_clip in filtered:
            idx = filtered.index(self.selected_clip)
            if idx > 0:
                self._select_clip_by_index(idx - 1, filtered)
        else:
            self._select_clip_by_index(len(filtered) - 1, filtered)

    def _select_next_clip(self):
        """Select the next clip in the list."""
        if not self.clips:
            return
        filtered = self._get_filtered_clips()
        if not filtered:
            return

        if self.selected_clip and self.selected_clip in filtered:
            idx = filtered.index(self.selected_clip)
            if idx < len(filtered) - 1:
                self._select_clip_by_index(idx + 1, filtered)
        else:
            self._select_clip_by_index(0, filtered)

    def _select_first_clip(self):
        """Select the first clip in the list."""
        filtered = self._get_filtered_clips()
        if filtered:
            self._select_clip_by_index(0, filtered)

    def _select_last_clip(self):
        """Select the last clip in the list."""
        filtered = self._get_filtered_clips()
        if filtered:
            self._select_clip_by_index(len(filtered) - 1, filtered)

    def _select_clip_by_index(self, index: int, clip_list: list = None):
        """Select a clip by its index in the given list."""
        clips = clip_list if clip_list else self._get_filtered_clips()
        if 0 <= index < len(clips):
            clip = clips[index]
            self.selected_clip = clip
            self.selected_clips = [clip]
            self.last_clicked_index = self.clips.index(clip) if clip in self.clips else index
            self._update_clip_selection_display()
            self._scroll_to_clip(clip)
            self._draw_timeline()

    def _show_clip_context_menu(self, event, clip: MediaClip, index: int):
        """Show right-click context menu for a clip."""
        # Select the clip if not already selected
        if clip not in self.selected_clips:
            self.selected_clips = [clip]
            self.selected_clip = clip
            self.last_clicked_index = index
            self._refresh_clips_list()

        # Create context menu
        menu = tk.Menu(self.root, tearoff=0, bg=self.COLORS['bg_card'],
                       fg=self.COLORS['text'], activebackground=self.COLORS['accent'],
                       activeforeground='white')

        # Selection info
        count = len(self.selected_clips)
        if count > 1:
            menu.add_command(label=f"{count} clips selected", state='disabled')
            menu.add_separator()

        # Actions
        menu.add_command(label="Re-analyze Selected  (Ctrl+R)", command=self._reanalyze_selected)
        menu.add_command(label="Re-analyze All  (Ctrl+Shift+R)", command=self._reanalyze_all)
        menu.add_command(label="Remove", command=self._remove_selected)
        menu.add_separator()

        # Selection options
        menu.add_command(label="Select All  (Ctrl+A)", command=self._select_all_clips)
        menu.add_command(label="Clear Selection  (Esc)", command=self._clear_selection)
        menu.add_separator()

        # Clip info and copy options
        if clip.start_tc:
            menu.add_command(label=f"Copy TC: {clip.start_tc}", command=lambda: self._copy_to_clipboard(clip.start_tc))
            menu.add_command(label=f"FPS: {clip.fps_display}", state='disabled')
        if clip.camera_id:
            menu.add_command(label=f"Camera: {clip.camera_id}", state='disabled')

        menu.add_separator()
        menu.add_command(label="Copy Filename", command=lambda: self._copy_to_clipboard(clip.filename))
        menu.add_command(label="Copy Full Path", command=lambda: self._copy_to_clipboard(clip.path))
        menu.add_separator()

        # Clip color submenu
        color_menu = tk.Menu(menu, tearoff=0, bg=self.COLORS['bg_card'],
                            fg=self.COLORS['text'], activebackground=self.COLORS['accent'])
        clip_colors = [
            ('#5b8def', 'Blue'),
            ('#4ecdc4', 'Teal'),
            ('#2ecc71', 'Green'),
            ('#f1c40f', 'Yellow'),
            ('#e67e22', 'Orange'),
            ('#e74c3c', 'Red'),
            ('#9b59b6', 'Purple'),
            ('#ff6b9d', 'Pink'),
            ('#1abc9c', 'Turquoise'),
            ('#95a5a6', 'Gray'),
        ]
        for color_hex, color_name in clip_colors:
            color_menu.add_command(
                label=f"  {color_name}",
                command=lambda c=color_hex: self._set_clip_color(c)
            )
        color_menu.add_separator()
        color_menu.add_command(label="Reset to Default", command=self._reset_clip_color)
        menu.add_cascade(label="Set Clip Color", menu=color_menu)

        # FPS override submenu (for when LTC detection gets FPS wrong)
        fps_menu = tk.Menu(menu, tearoff=0, bg=self.COLORS['bg_card'],
                          fg=self.COLORS['text'], activebackground=self.COLORS['accent'])
        fps_options = [
            (23.976, "23.976 (Film)"),
            (24.0, "24 (Cinema)"),
            (25.0, "25 (PAL)"),
            (29.97, "29.97 DF (NTSC Drop)"),
            (29.97, "29.97 NDF (NTSC Non-Drop)"),
            (30.0, "30"),
            (50.0, "50 (PAL High)"),
            (59.94, "59.94"),
            (60.0, "60"),
        ]
        for fps_val, fps_name in fps_options:
            is_drop = "DF" in fps_name and "NDF" not in fps_name
            fps_menu.add_command(
                label=fps_name,
                command=lambda f=fps_val, d=is_drop: self._override_clip_fps(f, d)
            )
        fps_menu.add_separator()
        fps_menu.add_command(label="Reset to Detected", command=self._reset_clip_fps)
        menu.add_cascade(label="Override FPS", menu=fps_menu)

        # TC Source override submenu (for selecting LTC vs Embedded timecode for sync)
        tc_source_menu = tk.Menu(menu, tearoff=0, bg=self.COLORS['bg_card'],
                                 fg=self.COLORS['text'], activebackground=self.COLORS['accent'])

        # Show current TC source and available options
        current_source = clip.tc_source if hasattr(clip, 'tc_source') else "auto"
        has_ltc = bool(clip.start_tc)
        has_embedded = bool(clip.embedded_tc and clip.embedded_tc != "00:00:00:00")

        # Auto option (use global setting)
        auto_label = "Auto (Use Global)"
        if current_source == "auto":
            auto_label = "* " + auto_label
        tc_source_menu.add_command(label=auto_label,
                                   command=lambda: self._set_clip_tc_source("auto"))

        # LTC option
        ltc_label = "LTC (Audio)"
        if has_ltc:
            ltc_label += f" - {clip.start_tc}"
        else:
            ltc_label += " - Not detected"
        if current_source == "ltc":
            ltc_label = "* " + ltc_label
        tc_source_menu.add_command(label=ltc_label,
                                   command=lambda: self._set_clip_tc_source("ltc"),
                                   state='normal' if has_ltc else 'disabled')

        # Embedded option
        emb_label = "Embedded (File)"
        if has_embedded:
            emb_label += f" - {clip.embedded_tc}"
        else:
            emb_label += " - Not found"
        if current_source == "embedded":
            emb_label = "* " + emb_label
        tc_source_menu.add_command(label=emb_label,
                                   command=lambda: self._set_clip_tc_source("embedded"),
                                   state='normal' if has_embedded else 'disabled')

        menu.add_cascade(label="TC Source for Sync", menu=tc_source_menu)

        menu.add_separator()
        menu.add_command(label="Open File Location", command=lambda: self._open_file_location(clip))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_file_location(self, clip: MediaClip):
        """Open the folder containing the clip file."""
        import subprocess
        folder = os.path.dirname(clip.path)
        if os.name == 'nt':  # Windows
            # (breaks away from the app's job: the window stays when the app closes)
            subprocess.run(['explorer', '/select,', clip.path], creationflags=0x01000000)
        elif os.name == 'posix':  # macOS/Linux
            if os.uname().sysname == 'Darwin':
                subprocess.run(['open', '-R', clip.path])
            else:
                subprocess.run(['xdg-open', folder])

    def _copy_to_clipboard(self, text: str):
        """Copy text to system clipboard."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()  # Required for clipboard to persist
            self.status_label.configure(text=f"Copied: {text}")
        except Exception as e:
            self.status_label.configure(text=f"Copy failed: {e}")

    def _set_clip_color(self, color: str):
        """Set custom color for selected clips."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected")
            return

        for clip in self.selected_clips:
            clip.color = color
            clip.custom_color = True   # _assign_camera_colors leaves it alone

        count = len(self.selected_clips)
        self.status_label.configure(text=f"Set color for {count} clip(s)")
        self._draw_timeline()
        self._refresh_clips_list()

    def _reset_clip_color(self):
        """Reset selected clips to their default camera-based color."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected")
            return

        for clip in self.selected_clips:
            clip.color = None  # Will use camera color
            clip.custom_color = False

        # Re-assign camera colors
        self._assign_camera_colors()

        count = len(self.selected_clips)
        self.status_label.configure(text=f"Reset color for {count} clip(s)")
        self._draw_timeline()
        self._refresh_clips_list()

    def _override_clip_fps(self, fps: float, is_drop_frame: bool = False):
        """Override FPS for selected clips (when LTC detection got it wrong)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected")
            return

        for clip in self.selected_clips:
            # Store original values for reset
            if not hasattr(clip, '_original_fps'):
                clip._original_fps = clip.fps
                clip._original_drop_frame = getattr(clip, 'drop_frame', False)

            # Apply override. fps_display is a computed @property (from fps +
            # drop_frame) - it must NOT be assigned (that raised AttributeError
            # and crashed the override menu for 24/25/30/50/60 rates); set the
            # inputs and let the property render.
            clip.fps = fps
            clip.fps_override = True
            clip.drop_frame = is_drop_frame if fps == 29.97 else False
            clip._fps_display_override = None  # show the computed rate

            # Frame counts are cached at the OLD fps - recompute so sync/export
            # (which divide these by the new fps) don't drift
            if clip.start_tc:
                clip.start_frames = clip.tc_to_frames(clip.start_tc)
            if clip.embedded_tc:
                clip.embedded_tc_frames = clip.tc_to_frames(clip.embedded_tc)

        self._invalidate_tc_cache()   # cached frame counts were at the old fps
        count = len(self.selected_clips)
        self.status_label.configure(text=f"FPS overridden to {fps} for {count} clip(s)")
        self._refresh_clips_list()

    def _reset_clip_fps(self):
        """Reset FPS to original detected value for selected clips."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected")
            return

        reset_count = 0
        for clip in self.selected_clips:
            if hasattr(clip, '_original_fps'):
                clip.fps = clip._original_fps
                clip.drop_frame = clip._original_drop_frame
                clip.fps_override = False
                delattr(clip, '_original_fps')
                delattr(clip, '_original_drop_frame')
                # Recompute cached frame counts at the restored fps
                if clip.start_tc:
                    clip.start_frames = clip.tc_to_frames(clip.start_tc)
                if clip.embedded_tc:
                    clip.embedded_tc_frames = clip.tc_to_frames(clip.embedded_tc)
                reset_count += 1

        if reset_count > 0:
            self._invalidate_tc_cache()
            self.status_label.configure(text=f"Reset FPS for {reset_count} clip(s)")
            self._refresh_clips_list()
        else:
            self.status_label.configure(text="No clips with overridden FPS")

    def _set_clip_tc_source(self, source: str):
        """Set TC source for selected clips (for sync).

        Args:
            source: "auto", "ltc", or "embedded"
        """
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected")
            return

        changed_count = 0
        for clip in self.selected_clips:
            clip.tc_source = source
            changed_count += 1

        source_name = {"auto": "Auto (Global)", "ltc": "LTC (Audio)", "embedded": "Embedded (File)"}.get(source, source)
        self.status_label.configure(text=f"Set TC source to '{source_name}' for {changed_count} clip(s) - Resync to apply")
        self._refresh_clips_list()

    def _set_all_no_ltc_to_embedded(self):
        """Set all clips without LTC to use embedded timecode for sync."""
        changed_count = 0
        for clip in self.clips:
            # Check if clip has no LTC but has embedded TC
            if not clip.start_tc and clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                clip.tc_source = "embedded"
                changed_count += 1

        if changed_count > 0:
            self.status_label.configure(text=f"Set {changed_count} clip(s) without LTC to use Embedded TC - Resync to apply")
            self._refresh_clips_list()
        else:
            self.status_label.configure(text="No clips found without LTC that have Embedded TC")

    def _copy_current_tc(self):
        """Copy the current timecode (from preview position or selected clip's start TC)."""
        if self.selected_clip:
            if hasattr(self, 'preview_tc_label'):
                # Get the current preview timecode if available
                tc_text = self.preview_tc_label.cget('text')
                if tc_text and tc_text != "--:--:--:--":
                    self._copy_to_clipboard(tc_text)
                    return
            # Fallback to clip's start TC
            if self.selected_clip.start_tc:
                self._copy_to_clipboard(self.selected_clip.start_tc)
                return
        self.status_label.configure(text="No timecode to copy - select a clip first")

    def _add_to_recent_files(self, file_path: str):
        """Add a file path to the recent files list."""
        # Normalize the path
        file_path = os.path.normpath(file_path)

        # Remove if already exists (will be re-added at the top)
        if file_path in self.recent_files:
            self.recent_files.remove(file_path)

        # Add at the beginning
        self.recent_files.insert(0, file_path)

        # Trim to max size
        self.recent_files = self.recent_files[:self.MAX_RECENT_FILES]

        # Update the recent files menu if it exists
        self._update_recent_files_menu()

    def _update_recent_files_menu(self):
        """Update the recent files dropdown menu."""
        if hasattr(self, 'recent_files_btn'):
            # Enable/disable the button based on whether there are recent files
            state = 'normal' if self.recent_files else 'disabled'
            self.recent_files_btn.configure(state=state)

    def _show_recent_files_menu(self, event=None):
        """Show the recent files dropdown menu."""
        if not self.recent_files:
            self.status_label.configure(text="No recent files")
            return

        menu = tk.Menu(self.root, tearoff=0, bg=self.COLORS['bg_card'],
                       fg=self.COLORS['text'], activebackground=self.COLORS['accent'],
                       activeforeground='white')

        for i, file_path in enumerate(self.recent_files[:self.MAX_RECENT_FILES]):
            if os.path.exists(file_path):
                filename = os.path.basename(file_path)
                # Truncate long paths for display
                display_path = file_path
                if len(display_path) > 60:
                    display_path = "..." + display_path[-57:]
                menu.add_command(
                    label=f"{i+1}. {filename}",
                    command=lambda p=file_path: self._load_recent_file(p)
                )
            else:
                # File no longer exists - show in gray
                filename = os.path.basename(file_path)
                menu.add_command(label=f"{i+1}. {filename} (missing)", state='disabled')

        if self.recent_files:
            menu.add_separator()
            menu.add_command(label="Clear Recent Files", command=self._clear_recent_files)

        try:
            if event:
                menu.tk_popup(event.x_root, event.y_root)
            else:
                # Position near the button
                btn = self.recent_files_btn
                x = btn.winfo_rootx()
                y = btn.winfo_rooty() + btn.winfo_height()
                menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _load_recent_file(self, file_path: str):
        """Load a file from the recent files list."""
        if os.path.exists(file_path):
            # Check if file is already in clips
            existing_paths = [c.path for c in self.clips]
            if file_path in existing_paths:
                self.status_label.configure(text=f"Already loaded: {os.path.basename(file_path)}")
                return

            # Add the file as a new clip
            if self.analyzer.is_supported(file_path):
                clip = MediaClip(path=file_path, filename=os.path.basename(file_path))
                clip.is_audio_only = self.analyzer.is_audio_file(file_path)
                clip.color = self.COLORS['clip_colors'][len(self.clips) % len(self.COLORS['clip_colors'])]
                self.clips.append(clip)
                self._refresh_clips_list()
                self.synced = False
                self.status_label.configure(text=f"Loaded: {os.path.basename(file_path)}")
            else:
                self.status_label.configure(text=f"Unsupported file type: {os.path.basename(file_path)}")
        else:
            # Remove from recent files if it doesn't exist
            if file_path in self.recent_files:
                self.recent_files.remove(file_path)
            self.status_label.configure(text="File not found - removed from recent files")

    def _clear_recent_files(self):
        """Clear the recent files list."""
        self.recent_files = []
        self._update_recent_files_menu()
        self.status_label.configure(text="Recent files cleared")

    def _show_keyboard_shortcuts(self):
        """Show a popup with keyboard shortcuts."""
        shortcuts = """
KEYBOARD SHORTCUTS

File Operations:
  Ctrl+O           Add Files
  Ctrl+Shift+O     Add Folder

Analysis:
  Ctrl+Shift+A     Analyze Clips
  Ctrl+R           Re-analyze Selected
  Ctrl+Shift+R     Re-analyze All
  Ctrl+Shift+T     Refresh Embedded TC

Project:
  Ctrl+S           Save Project
  Ctrl+P           Load Project
  Ctrl+Alt+S       Toggle Auto-Save

Sync & Export:
  Ctrl+Shift+S     Sync Clips
  Ctrl+E           Export XML

Search:
  Ctrl+F           Focus Search Box

Copy:
  Ctrl+T           Copy Current Timecode
  Double-Click     Copy Clip Start TC

Selection:
  Click            Select Clip
  Ctrl+Click       Toggle Selection
  Shift+Click      Range Selection
  Ctrl+A           Select All
  Escape           Clear Selection
  Delete           Remove Selected
  Ctrl+Delete      Clear All Clips
  Shift+Delete     Ripple Delete

Navigation:
  Up               Previous Clip
  Down             Next Clip
  Home             First Clip
  End              Last Clip
  [                Go to Clip Start
  ]                Go to Clip End

Preview:
  Space            Play/Pause
  Left/Right       Step 1 Frame
  J                Reverse (press multiple = faster)
  K                Pause
  L                Forward (press multiple = faster)
  P                Loop Playback (IN/OUT range or full timeline)

Timeline:
  +/-              Zoom In/Out
  Ctrl+0           Reset Zoom
  N                Toggle Snapping

Markers:
  M                Add Marker at Playhead
  Shift+M          Delete Nearest Marker
  Ctrl+Left/Right  Go to Next/Prev Marker

In/Out Points:
  I                Set IN Point
  O                Set OUT Point
  Ctrl+I           Go to IN Point
  Alt+O            Go to OUT Point
  X                Clear IN/OUT Points

Clip Editing:
  Drag Handles     Trim In/Out Points
  S / Ctrl+B       Split Clip at Playhead
  U                Reset Clip Trim
  Ctrl+L / Shift+L Lock/Unlock Clip
  Ctrl+D           Duplicate Selected Clips
  Alt+Left/Right   Nudge Clips (1 frame)
  Alt+Shift+Left/Right   Nudge (10 frames)

Clip Grouping:
  Ctrl+G           Group Selected Clips
  Ctrl+Shift+G     Ungroup Selected Clips

Track Controls:
  M/S Buttons      Mute/Solo Track (click header)

Undo/Redo:
  Ctrl+Z           Undo
  Ctrl+Y           Redo
  Ctrl+Shift+Z     Redo

Help:
  F1               Show This Help


EXPORT OPTIONS (checkboxes next to Multicam)

Multicam
  Resolve / FCPX: the timeline is ONE multicam clip,
  an angle per camera and per recorder track.

Share Tracks
  Takes of the same recorder track share one audio
  track when they don't overlap. Another recorder
  always gets its own tracks.

Remove Gaps  [0 / 1 / 2 / 5 / 10]
  Exports leave out the empty time between takes.
  The number = seconds to keep between takes
  (0 = about 0.1 s). Gaps already shorter stay.
  Clips that overlap move together: sync is kept.
  This app's timeline keeps the real timecode.

Cam Offset...
  Shift all clips of a camera by frames.
  Picture LATE (clap after the sound): enter MINUS.
  Picture EARLY: enter plus.
  Saved, and applied on every sync and export.

Camera numbers in Resolve
  A Resolve export also writes
  <name>_camera_metadata.csv. In Resolve:
  File > Import Metadata To > Media Pool,
  tick "Ignore file extension",
  untick "Match using clip start and end timecode".
"""

        # Create a popup window
        popup = tk.Toplevel(self.root)
        popup.title("Help")
        popup.geometry("420x650")
        popup.configure(bg=self.COLORS['bg_card'])
        popup.transient(self.root)
        popup.grab_set()

        # Center on parent
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 420) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 650) // 2
        popup.geometry(f"+{x}+{y}")

        # Text widget
        # Close button (packed first so it stays visible; the text scrolls)
        close_btn = tk.Button(popup, text="Close", command=popup.destroy,
                              bg=self.COLORS['accent'], fg='white',
                              font=('Segoe UI', 10), padx=20, pady=5,
                              relief=tk.FLAT)
        close_btn.pack(side=tk.BOTTOM, pady=10)
        scroll = ttk.Scrollbar(popup, orient=tk.VERTICAL)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text = tk.Text(popup, bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                       font=('Consolas', 10), padx=20, pady=20, relief=tk.FLAT,
                       yscrollcommand=scroll.set)
        text.pack(fill=tk.BOTH, expand=True)
        scroll.configure(command=text.yview)
        text.insert('1.0', shortcuts)
        text.configure(state='disabled')

        # Close on Escape
        popup.bind('<Escape>', lambda e: popup.destroy())
        popup.bind('<F1>', lambda e: popup.destroy())

    def _show_about(self):
        """Show the About dialog with version information."""
        # Create popup window
        popup = tk.Toplevel(self.root)
        popup.title(f"About {self.APP_NAME}")
        popup.geometry("450x400")
        popup.configure(bg=self.COLORS['bg_card'])
        popup.transient(self.root)
        popup.grab_set()
        popup.resizable(False, False)

        # Center on parent
        x = self.root.winfo_x() + (self.root.winfo_width() - 450) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 400) // 2
        popup.geometry(f"+{x}+{y}")

        # Main content frame
        content = tk.Frame(popup, bg=self.COLORS['bg_card'])
        content.pack(fill=tk.BOTH, expand=True, padx=30, pady=20)

        # App name
        tk.Label(content, text=self.APP_NAME, font=('Segoe UI', 24, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(pady=(10, 5))

        # Version
        tk.Label(content, text=f"Version {self.VERSION}", font=('Segoe UI', 12),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['accent']).pack(pady=(0, 5))

        # Build date
        tk.Label(content, text=f"Build: {self.BUILD_DATE}", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(pady=(0, 15))

        # Description
        desc = """A professional LTC timecode sync application
for multi-camera video production.

Decodes LTC audio timecode from media files
and generates edit-ready metadata for
DaVinci Resolve, Premiere Pro, and Final Cut Pro."""

        tk.Label(content, text=desc, font=('Segoe UI', 10), justify='center',
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(pady=(0, 15))

        # Feature list
        features = "✓ Auto FPS detection  ✓ Multi-camera support\n✓ Timeline visualization  ✓ Batch processing"
        tk.Label(content, text=features, font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['success']).pack(pady=(0, 15))

        # Separator
        tk.Frame(content, height=1, bg=self.COLORS['text_dim']).pack(fill=tk.X, pady=10)

        # Dependencies status
        deps_frame = tk.Frame(content, bg=self.COLORS['bg_card'])
        deps_frame.pack(pady=(0, 10))

        # FFmpeg status
        ffmpeg_status = "✓ FFmpeg" if FFMPEG_AVAILABLE else "✗ FFmpeg (not found)"
        ffmpeg_color = self.COLORS['success'] if FFMPEG_AVAILABLE else self.COLORS['error']
        tk.Label(deps_frame, text=ffmpeg_status, font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=ffmpeg_color).pack(side=tk.LEFT, padx=10)

        # VLC status
        vlc_status = "✓ VLC" if VLC_AVAILABLE else "✗ VLC (optional)"
        vlc_color = self.COLORS['success'] if VLC_AVAILABLE else self.COLORS['text_dim']
        tk.Label(deps_frame, text=vlc_status, font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=vlc_color).pack(side=tk.LEFT, padx=10)

        # HW accel status
        gpu_name = self._gpu_decoder_text()
        hw_status = f"GPU: {gpu_name}  |  Decode: {DECODE_DEVICE}"
        hw_ok = _GPU_LIST is not None and selected_gpu(ignore_cpu_mode=True) and DECODE_DEVICE != "CPU"
        hw_color = self.COLORS['success'] if hw_ok else self.COLORS['text_dim']
        tk.Label(deps_frame, text=hw_status, font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=hw_color).pack(side=tk.LEFT, padx=10)

        # Copyright
        tk.Label(content, text="© 2025-2026 idnawsi - source-available, free for noncommercial use and small independent users", font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack()

        # Close button
        close_btn = tk.Button(popup, text="Close", command=popup.destroy,
                              bg=self.COLORS['accent'], fg='white',
                              font=('Segoe UI', 10), padx=20, pady=5,
                              relief=tk.FLAT)
        close_btn.pack(pady=10)

        # Close on Escape
        popup.bind('<Escape>', lambda e: popup.destroy())

    def _show_settings_dialog(self):
        """Show the Settings dialog for configuring app preferences."""
        # Create popup window
        popup = tk.Toplevel(self.root)
        popup.title("LTC Sync - Settings")
        popup.geometry("640x1050")
        popup.configure(bg=self.COLORS['bg_card'])

        # Center on parent
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 500) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 450) // 2
        popup.geometry(f"+{x}+{y}")

        # Bring to front and focus
        popup.lift()
        popup.focus_force()

        # Main content frame
        content = tk.Frame(popup, bg=self.COLORS['bg_card'])
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # Title
        tk.Label(content, text="Settings", font=('Segoe UI', 16, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(pady=(0, 15))

        # === Scratch Disk Section (waveforms + proxies + temp files) ===
        cache_frame = tk.LabelFrame(content, text="Scratch Disk",
                                    font=('Segoe UI', 10, 'bold'),
                                    bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                                    padx=10, pady=8)
        cache_frame.pack(fill=tk.X, pady=(0, 10))

        # Cache size setting
        size_row = tk.Frame(cache_frame, bg=self.COLORS['bg_card'])
        size_row.pack(fill=tk.X, pady=3)

        tk.Label(size_row, text="Maximum size (waveforms + proxies):", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        cache_size_var = tk.StringVar(value=str(self.waveform_cache_max_size_gb))
        cache_spinbox = tk.Spinbox(size_row, from_=1, to=50, width=5,
                                    textvariable=cache_size_var,
                                    font=('Segoe UI', 10))
        cache_spinbox.pack(side=tk.LEFT, padx=10)

        tk.Label(size_row, text="GB", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        # Current usage, per kind
        def get_cache_size(target_dir):
            u = self._scratch_usage(target_dir)
            return sum(v[0] for v in u.values()), u

        def usage_text(target_dir):
            total, u = get_cache_size(target_dir)
            mb = lambda b: f"{b / 2**20:.0f} MB"
            return (f"Using {mb(total)}: waveforms {mb(u['waveforms'][0])} ({u['waveforms'][1]}), "
                    f"proxies {mb(u['proxies'][0])} ({u['proxies'][1]}), temp {mb(u['temp'][0])}")

        cache_path = getattr(self, 'waveform_cache_dir', 'Not initialized')
        new_cache_dir = [cache_path]

        # Current usage
        usage_label = tk.Label(cache_frame, text=usage_text(cache_path),
                               font=('Segoe UI', 9), bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
        usage_label.pack(anchor='w', pady=3)
        tk.Label(cache_frame, text="Waveforms, preview proxies (proxies\\) and temporary files (temp\\) all go here.\n"
                                   "Pick a fast drive with free space. Leftover temp folders are cleaned at startup.",
                 font=('Segoe UI', 9), justify=tk.LEFT, anchor='w',
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(anchor='w')

        # Location label
        tk.Label(cache_frame, text="Location:", font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(anchor='w', pady=(5, 0))

        path_label = tk.Label(cache_frame, text=cache_path, font=('Segoe UI', 8),
                              bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'],
                              wraplength=420, anchor='w', justify='left')
        path_label.pack(anchor='w', pady=(2, 5))

        # Helper to update usage display
        def update_usage(target_dir):
            usage_label.configure(text=usage_text(target_dir))

        # Button functions
        def browse_cache_dir():
            from tkinter import filedialog
            new_dir = filedialog.askdirectory(title="Select Scratch Disk Folder", initialdir=new_cache_dir[0])
            if new_dir:
                new_cache_dir[0] = new_dir
                path_label.configure(text=new_dir)
                update_usage(new_dir)

        def reset_cache_dir():
            if os.name == 'nt':
                base_dir = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
                default_dir = os.path.join(base_dir, 'LTC_Sync', 'waveform_cache')
            else:
                default_dir = os.path.expanduser('~/.cache/ltc_sync/waveforms')
            new_cache_dir[0] = default_dir
            path_label.configure(text=default_dir)
            update_usage(default_dir)

        def clear_cache():
            target_dir = new_cache_dir[0]
            if target_dir and os.path.exists(target_dir):
                cleared = 0
                for f in os.listdir(target_dir):
                    if f.endswith('.waveform'):
                        try:
                            os.remove(os.path.join(target_dir, f))
                            cleared += 1
                        except:
                            pass
                # Clear in-memory waveform cache
                self.audio_waveform_cache.clear()
                update_usage(target_dir)
                self.status_label.configure(text=f"Cleared {cleared} cached waveforms")

        # === BUTTONS - Create each on its own line ===
        btn_container = tk.Frame(cache_frame, bg=self.COLORS['bg_card'])
        btn_container.pack(fill=tk.X, pady=(5, 0))

        # Change Location button
        browse_btn = tk.Button(btn_container, text="Change Location...", command=browse_cache_dir,
                               bg='#E74C3C', fg='white', font=('Segoe UI', 9, 'bold'),
                               padx=15, pady=5, relief=tk.FLAT, cursor='hand2')
        browse_btn.pack(anchor='w', pady=2)

        # Reset to Default button
        reset_btn = tk.Button(btn_container, text="Reset to Default", command=reset_cache_dir,
                              bg='#555555', fg='white', font=('Segoe UI', 9),
                              padx=15, pady=5, relief=tk.FLAT, cursor='hand2')
        reset_btn.pack(anchor='w', pady=2)

        # Clear Cache button
        clear_btn = tk.Button(btn_container, text="Clear All Cache", command=clear_cache,
                              bg='#C0392B', fg='white', font=('Segoe UI', 9),
                              padx=15, pady=5, relief=tk.FLAT, cursor='hand2')
        clear_btn.pack(anchor='w', pady=2)

        # === Timeline Section ===
        timeline_frame = tk.LabelFrame(content, text="Timeline",
                                       font=('Segoe UI', 10, 'bold'),
                                       bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                                       padx=10, pady=8)
        timeline_frame.pack(fill=tk.X, pady=(10, 10))

        # Zoom Focus Mode setting
        zoom_row = tk.Frame(timeline_frame, bg=self.COLORS['bg_card'])
        zoom_row.pack(fill=tk.X, pady=3)

        tk.Label(zoom_row, text="Zoom focus:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        zoom_focus_var = tk.StringVar(value=self.zoom_focus_mode)
        zoom_dropdown = ttk.Combobox(zoom_row, textvariable=zoom_focus_var,
                                     values=["Cursor", "Playhead"],
                                     state="readonly", width=12,
                                     font=('Segoe UI', 10))
        zoom_dropdown.pack(side=tk.LEFT, padx=10)

        tk.Label(zoom_row, text="(where zoom centers when scrolling)", font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(side=tk.LEFT)

        # === Analysis Section ===
        analysis_frame = tk.LabelFrame(content, text="Analysis",
                                       font=('Segoe UI', 10, 'bold'),
                                       bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                                       padx=10, pady=8)
        analysis_frame.pack(fill=tk.X, pady=(10, 10))

        # Camera ID Source setting
        cam_row = tk.Frame(analysis_frame, bg=self.COLORS['bg_card'])
        cam_row.pack(fill=tk.X, pady=3)

        tk.Label(cam_row, text="Camera ID source:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        camera_id_source_var = tk.StringVar(value=self.camera_id_source)
        cam_dropdown = ttk.Combobox(cam_row, textvariable=camera_id_source_var,
                                    values=["LTC User Bits", "Filename"],
                                    state="readonly", width=14,
                                    font=('Segoe UI', 10))
        cam_dropdown.pack(side=tk.LEFT, padx=10)

        tk.Label(cam_row, text="(priority for camera detection)", font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(side=tk.LEFT)

        # Parallel analysis setting
        threads_row = tk.Frame(analysis_frame, bg=self.COLORS['bg_card'])
        threads_row.pack(fill=tk.X, pady=3)

        tk.Label(threads_row, text="Files at once:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        analysis_threads_var = tk.StringVar(value=str(self.analysis_threads))
        threads_dropdown = ttk.Combobox(threads_row, textvariable=analysis_threads_var,
                                        values=["Auto", "1", "2", "4", "6", "8", "12", "16"],
                                        state="readonly", width=14,
                                        font=('Segoe UI', 10))
        threads_dropdown.pack(side=tk.LEFT, padx=10)

        tk.Label(threads_row, text=f"(CPU threads; Auto = {min(8, os.cpu_count() or 2)} on this PC)",
                 font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(side=tk.LEFT)

        # === Video Decoding (GPU / CPU) Section ===
        # Same setting as the toolbar's "Decode (GPU/CPU)" combo
        video_frame = tk.LabelFrame(content, text="Video Decoding (GPU / CPU)",
                                    font=('Segoe UI', 10, 'bold'),
                                    bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                                    padx=10, pady=8)
        video_frame.pack(fill=tk.X, pady=(10, 10))

        decode_row = tk.Frame(video_frame, bg=self.COLORS['bg_card'])
        decode_row.pack(fill=tk.X, pady=3)

        tk.Label(decode_row, text="Decode with:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)

        decode_device_var = tk.StringVar(value=self.decode_device.get())
        decode_dropdown = ttk.Combobox(decode_row, textvariable=decode_device_var,
                                       values=list(DECODE_DEVICES),
                                       state="readonly", width=14,
                                       font=('Segoe UI', 10))
        decode_dropdown.pack(side=tk.LEFT, padx=10)

        tk.Label(decode_row, text="(Auto = GPU where faster)", font=('Segoe UI', 9),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(side=tk.LEFT)

        # Which GPU - filled in by a background test (~0.2s per GPU first time)
        gpu_row = tk.Frame(video_frame, bg=self.COLORS['bg_card'])
        gpu_row.pack(fill=tk.X, pady=3)
        tk.Label(gpu_row, text="GPU:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)
        gpu_choice_var = tk.StringVar(value="Detecting GPUs...")
        gpu_dropdown = ttk.Combobox(gpu_row, textvariable=gpu_choice_var, values=[],
                                    state="disabled", width=40, font=('Segoe UI', 10))
        gpu_dropdown.pack(side=tk.LEFT, padx=(48, 6))
        gpu_label_to_name = {}

        gpu_status = tk.Label(video_frame, text="", font=('Segoe UI', 9), justify=tk.LEFT, anchor='w',
                              bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
        gpu_status.pack(fill=tk.X, pady=(2, 0))

        def fill_gpu_list(gpus):
            if not popup.winfo_exists():
                return
            # the Direct GPU playback menu lists the GPUs too
            for g in gpus:
                if g.get('ok'):
                    direct_label_to_mode[g['name']] = g['name']
            direct_dropdown.configure(values=list(direct_label_to_mode))
            if self.hw_player is not None and self.clips:
                self.hw_player.probe([c.path for c in self.clips if not c.is_audio_only])
            refresh_direct_status()
            gpu_label_to_name.clear()
            best = selected_gpu(ignore_cpu_mode=True)
            auto_label = f"Auto (best: {best['name']})" if best else "Auto (no working GPU - CPU is used)"
            gpu_label_to_name[auto_label] = "Auto"
            for g in gpus:
                gpu_label_to_name[gpu_label(g)] = g['name']
            gpu_dropdown.configure(values=list(gpu_label_to_name), state="readonly" if gpus else "disabled")
            current = next((lbl for lbl, name in gpu_label_to_name.items() if name == DECODE_GPU), auto_label)
            gpu_choice_var.set(current)
            working = [g for g in gpus if g.get('ok')]
            if not gpus:
                gpu_status.configure(text="No GPU found - video is decoded on the CPU.")
            else:
                gpu_status.configure(text=(
                    f"{len(gpus)} GPU(s) installed, {len(working)} can decode video (tested). "
                    "No extra software needed - just the GPU driver.\n"
                    "Thumbnails and frame grabs use the GPU chosen here. VLC playback uses the GPU\n"
                    "Windows assigns to this app (Windows Settings > System > Display > Graphics)."))

        def detect_gpus(refresh=False):
            gpu_dropdown.configure(state="disabled")
            gpu_choice_var.set("Testing GPUs...")
            result = []  # the worker only stores; Tk is touched on the UI thread

            def work():
                result.append(list_gpus(refresh=refresh))

            def poll():
                if not popup.winfo_exists():
                    return
                if result:
                    fill_gpu_list(result[0])
                else:
                    popup.after(100, poll)
            threading.Thread(target=work, daemon=True, name='gpu-detect').start()
            poll()

        # Direct GPU playback (hardware decoding of the original files)
        direct_row = tk.Frame(video_frame, bg=self.COLORS['bg_card'])
        direct_row.pack(fill=tk.X, pady=(8, 3))
        tk.Label(direct_row, text="Direct GPU playback:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)
        # Menu: Auto, Off, then each GPU (filled once detection finishes)
        direct_label_to_mode = {"Auto (best GPU per file)": "Auto", "Off": "Off"}
        if DIRECT_GPU_MODE not in DIRECT_GPU_MODES:
            direct_label_to_mode[DIRECT_GPU_MODE] = DIRECT_GPU_MODE
        direct_mode_var = tk.StringVar(value=next(
            lbl for lbl, mode in direct_label_to_mode.items() if mode == DIRECT_GPU_MODE))
        direct_dropdown = ttk.Combobox(direct_row, textvariable=direct_mode_var,
                                       values=list(direct_label_to_mode),
                                       state="readonly" if AV_AVAILABLE else "disabled", width=30,
                                       font=('Segoe UI', 10))
        direct_dropdown.pack(side=tk.LEFT, padx=6)
        if not AV_AVAILABLE:
            tk.Label(direct_row, text="(needs PyAV: pip install av)", font=('Segoe UI', 9),
                     bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(side=tk.LEFT)
        direct_status = tk.Label(video_frame, text="", font=('Segoe UI', 9), justify=tk.LEFT, anchor='w',
                                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
        direct_status.pack(fill=tk.X)

        def refresh_direct_status():
            """How this project's video clips play right now, per GPU."""
            if not popup.winfo_exists():
                return
            if not AV_AVAILABLE or self.hw_player is None:
                direct_status.configure(text="Plays original files on a GPU that decodes them in hardware.")
                return
            paths = [c.path for c in self.clips if not c.is_audio_only]
            if not paths:
                direct_status.configure(text="Plays original files on a GPU that decodes them in hardware "
                                             "(tested per file format; no CPU fallback).")
                return
            counts = self.hw_player.summary(paths)
            parts = [f"{n} clip(s) -> {key}" for key, n in sorted(counts.items(), key=lambda kv: -kv[1])]
            direct_status.configure(text="This project: " + ";  ".join(parts))
            if 'not tested yet' in counts:
                popup.after(500, refresh_direct_status)  # tests run in the background

        # Preview proxies
        proxy_row = tk.Frame(video_frame, bg=self.COLORS['bg_card'])
        proxy_row.pack(fill=tk.X, pady=(8, 3))
        tk.Label(proxy_row, text="Preview proxies:", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(side=tk.LEFT)
        proxy_mode_var = tk.StringVar(value=PREVIEW_PROXY_MODE)
        ttk.Combobox(proxy_row, textvariable=proxy_mode_var, values=list(PREVIEW_PROXY_MODES),
                     state="readonly", width=8, font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=6)
        proxy_size_label = tk.Label(proxy_row, text=f"({PROXIES.size_bytes() / 2**20:.0f} MB)", font=('Segoe UI', 9),
                                    bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
        proxy_size_label.pack(side=tk.LEFT)

        def clear_proxies():
            removed = PROXIES.clear()
            proxy_size_label.configure(text=f"({PROXIES.size_bytes() / 2**20:.0f} MB)")
            self.vlc_media_path = None
            self.status_label.configure(text=f"Deleted {removed} preview proxies")
        tk.Button(proxy_row, text="Delete proxies", command=clear_proxies,
                  bg='#555555', fg='white', font=('Segoe UI', 9),
                  padx=8, pady=1, relief=tk.FLAT, cursor='hand2').pack(side=tk.LEFT, padx=6)
        tk.Label(video_frame,
                 text="Small 960px copies of 4K / 4:2:2 / 10-bit footage for smooth preview, made in the\n"
                      "background. Auto = only footage that needs it. Exports always use the original files.",
                 font=('Segoe UI', 9), justify=tk.LEFT, anchor='w',
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(fill=tk.X)

        retest_btn = tk.Button(gpu_row, text="Re-test", command=lambda: detect_gpus(refresh=True),
                               bg='#555555', fg='white', font=('Segoe UI', 9),
                               padx=8, pady=1, relief=tk.FLAT, cursor='hand2')
        retest_btn.pack(side=tk.LEFT)
        detect_gpus()

        # Separator
        tk.Frame(content, height=1, bg=self.COLORS['text_dim']).pack(fill=tk.X, pady=10)

        # Save/Cancel buttons
        btn_frame = tk.Frame(content, bg=self.COLORS['bg_card'])
        btn_frame.pack(fill=tk.X)

        def save_settings():
            try:
                new_size = int(cache_size_var.get())
                if 1 <= new_size <= 50:
                    self.waveform_cache_max_size_gb = new_size
                    selected_dir = new_cache_dir[0]

                    # Get default directory for comparison
                    if os.name == 'nt':
                        base_dir = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
                        default_dir = os.path.join(base_dir, 'LTC_Sync', 'waveform_cache')
                    else:
                        default_dir = os.path.expanduser('~/.cache/ltc_sync/waveforms')

                    # Normalize paths for comparison
                    selected_normalized = os.path.normpath(selected_dir).lower() if selected_dir else ""
                    default_normalized = os.path.normpath(default_dir).lower()

                    if selected_normalized == default_normalized:
                        self.waveform_cache_custom_dir = None
                    else:
                        self.waveform_cache_custom_dir = os.path.normpath(selected_dir)

                    self.waveform_cache_dir = self._init_waveform_cache_dir(selected_dir)
                    self._sync_proxy_dir()

                    # Save zoom focus mode
                    self.zoom_focus_mode = zoom_focus_var.get()

                    # Save camera ID source
                    self.camera_id_source = camera_id_source_var.get()

                    # Save parallel analysis setting
                    self.analysis_threads = analysis_threads_var.get()

                    # Direct GPU playback
                    global DIRECT_GPU_MODE
                    chosen_direct = direct_label_to_mode.get(direct_mode_var.get(), DIRECT_GPU_MODE)
                    if chosen_direct != DIRECT_GPU_MODE:
                        DIRECT_GPU_MODE = chosen_direct
                        self._direct_stop_playback()
                        self._direct_release_display()
                        self.timeline_last_clip = None
                        if self.hw_player is not None:
                            self.hw_player.reset_routes()  # re-test on the chosen GPU
                        if DIRECT_GPU_MODE != "Off":
                            self._queue_preview_proxies()

                    # Preview proxies
                    global PREVIEW_PROXY_MODE
                    if proxy_mode_var.get() != PREVIEW_PROXY_MODE:
                        PREVIEW_PROXY_MODE = proxy_mode_var.get()
                        self.vlc_media_path = None  # reload current clip from the right source
                        if PREVIEW_PROXY_MODE == "Off":
                            PROXIES.cancel_all()
                        else:
                            self._queue_preview_proxies()

                    # Decode device (trace on decode_device applies it) and GPU
                    global DECODE_GPU
                    chosen_gpu = gpu_label_to_name.get(gpu_choice_var.get())
                    if chosen_gpu:
                        DECODE_GPU = chosen_gpu
                        self.decode_gpu = chosen_gpu
                    self.decode_device.set(decode_device_var.get())

                    self._save_settings()
                    self.status_label.configure(text=f"Settings saved (cache: {new_size} GB)")
                    popup.destroy()
                else:
                    self.status_label.configure(text="Cache size must be between 1-50 GB")
            except ValueError:
                self.status_label.configure(text="Invalid cache size value")

        save_btn = tk.Button(btn_frame, text="Save", command=save_settings,
                             bg=self.COLORS['accent'], fg='white',
                             font=('Segoe UI', 10), padx=20, pady=5,
                             relief=tk.FLAT)
        save_btn.pack(side=tk.RIGHT, padx=5)

        cancel_btn = tk.Button(btn_frame, text="Cancel", command=popup.destroy,
                               bg=self.COLORS['bg_light'], fg=self.COLORS['text'],
                               font=('Segoe UI', 10), padx=20, pady=5,
                               relief=tk.FLAT)
        cancel_btn.pack(side=tk.RIGHT, padx=5)

        # Close on Escape
        popup.bind('<Escape>', lambda e: popup.destroy())

    def _show_camera_audio_dialog(self):
        """Choose which camera audio channels go on the exported timeline.

        Per camera (same names as the timeline tracks), tick the channels to
        keep. Each ticked channel exports as its own mono track. The LTC
        channel is shown but can't be ticked while Mute LTC is on.
        """
        self._finalize_scrub_before_modal()
        cameras = {}  # camera key -> [video clips]
        for clip in self.clips:
            if not clip.is_audio_only and clip.status != "no_audio":
                cameras.setdefault(XMLExporter.camera_audio_key(clip), []).append(clip)
        if not cameras:
            messagebox.showinfo("Camera Audio", "No video clips with audio.\n\nAdd and analyze camera clips first.")
            return

        bg, panel = self.COLORS['bg_card'], self.COLORS['bg_light']
        popup = tk.Toplevel(self.root)
        popup.title("Camera Audio")
        popup.configure(bg=bg)
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.grab_set()

        content = tk.Frame(popup, bg=bg)
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)
        tk.Label(content, text="Camera Audio", font=('Segoe UI', 16, 'bold'),
                 bg=bg, fg=self.COLORS['text']).pack(pady=(0, 5))
        tk.Label(content, text="Tick the channels to keep on the exported timeline.\n"
                               "Each ticked channel is exported as its own mono track.",
                 font=('Segoe UI', 10), bg=bg, fg=self.COLORS['text_dim'],
                 justify=tk.CENTER).pack(pady=(0, 12))

        mute_ltc = self.mute_ltc.get()
        channel_vars = {}  # camera key -> {channel: BooleanVar}
        for key in sorted(cameras, key=str.lower):
            cam_clips = cameras[key]
            num_ch = max((c.audio_channels or 2) for c in cam_clips)
            ltc_counts = {}
            for c in cam_clips:
                if c.ltc_channel >= 0:
                    ltc_counts[c.ltc_channel + 1] = ltc_counts.get(c.ltc_channel + 1, 0) + 1
            saved = self.camera_audio_selection.get(key)

            row = tk.Frame(content, bg=panel, padx=10, pady=8)
            row.pack(fill=tk.X, pady=(0, 6))
            clips_txt = f"{len(cam_clips)} clip" + ("s" if len(cam_clips) != 1 else "")
            tk.Label(row, text=key, font=('Segoe UI', 10, 'bold'), width=14, anchor='w',
                     bg=panel, fg=self.COLORS['text']).pack(side=tk.LEFT)
            tk.Label(row, text=clips_txt, font=('Segoe UI', 9), width=8, anchor='w',
                     bg=panel, fg=self.COLORS['text_dim']).pack(side=tk.LEFT)

            channel_vars[key] = {}
            for ch in range(1, num_ch + 1):
                ltc_everywhere = ltc_counts.get(ch, 0) == len(cam_clips)
                is_ltc = mute_ltc and ltc_everywhere
                if is_ltc:
                    label = f"Ch{ch} (LTC)"
                elif mute_ltc and ltc_counts.get(ch):
                    label = f"Ch{ch} (LTC in {ltc_counts[ch]})"
                else:
                    label = f"Ch{ch}"
                var = tk.BooleanVar(value=(not is_ltc) and (saved is None or ch in saved))
                channel_vars[key][ch] = var
                ttk.Checkbutton(row, text=label, variable=var, takefocus=False,
                                state='disabled' if is_ltc else 'normal').pack(side=tk.LEFT, padx=6)
                if is_ltc:
                    var.set(False)

        def set_all(value):
            for key, chans in channel_vars.items():
                for ch, var in chans.items():
                    ltc_clip_count = sum(1 for c in cameras[key] if c.ltc_channel + 1 == ch)
                    var.set(value and not (mute_ltc and ltc_clip_count == len(cameras[key])))

        def apply():
            self.camera_audio_selection = {
                key: [ch for ch, var in chans.items() if var.get()]
                for key, chans in channel_vars.items()}
            any_audio = any(self.camera_audio_selection.values())
            self.include_camera_audio.set(any_audio)
            kept = sum(len(v) for v in self.camera_audio_selection.values())
            self.status_label.configure(
                text=f"Camera audio: {kept} channel(s) from {sum(1 for v in self.camera_audio_selection.values() if v)} camera(s)"
                if any_audio else "Camera audio: off")
            popup.destroy()

        buttons = tk.Frame(content, bg=bg)
        buttons.pack(fill=tk.X, pady=(10, 0))
        self._create_button(buttons, "All", lambda: set_all(True)).pack(side=tk.LEFT, padx=(0, 4))
        self._create_button(buttons, "None", lambda: set_all(False)).pack(side=tk.LEFT)
        self._create_button(buttons, "OK", apply, 'accent').pack(side=tk.RIGHT)
        self._create_button(buttons, "Cancel", popup.destroy).pack(side=tk.RIGHT, padx=4)

        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - popup.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - popup.winfo_height()) // 2
        popup.geometry(f"+{x}+{y}")

    def _show_camera_offset_dialog(self):
        """Per-camera offset in frames: shifts all clips of a camera on the timeline (and so in every
        export). A camera's LTC goes through its audio path, whose delay differs per model and mode,
        so two cameras fed the same LTC can show pictures a frame or two apart (e.g. R6 vs R8)."""
        self._finalize_scrub_before_modal()
        cameras = {}  # camera key -> [video clips]
        for clip in self.clips:
            if not clip.is_audio_only:
                cameras.setdefault(XMLExporter.camera_audio_key(clip), []).append(clip)
        keys = sorted(set(cameras) | set(self.camera_offsets), key=str.lower)
        if not keys:
            messagebox.showinfo("Camera Offset", "No camera clips.\n\nAdd and analyze camera clips first.")
            return

        bg, panel = self.COLORS['bg_card'], self.COLORS['bg_light']
        popup = tk.Toplevel(self.root)
        popup.title("Camera Offset")
        popup.configure(bg=bg)
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.grab_set()

        content = tk.Frame(popup, bg=bg)
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)
        tk.Label(content, text="Camera Offset", font=('Segoe UI', 16, 'bold'),
                 bg=bg, fg=self.COLORS['text']).pack(pady=(0, 5))
        tk.Label(content, text="Shift every clip of a camera by a number of frames.\n"
                               "+ = later on the timeline, - = earlier (fractions allowed, e.g. 1.5).\n"
                               "Picture LATE (the clap lands after the sound)? Enter a MINUS value, e.g. -2.\n"
                               "Picture EARLY? Enter a plus value.\n"
                               "Find it once with a clap: how many frames the camera's picture is off\n"
                               "against the sound / the other cameras. Kept for future projects too.",
                 font=('Segoe UI', 10), bg=bg, fg=self.COLORS['text_dim'],
                 justify=tk.CENTER).pack(pady=(0, 12))

        entries = {}
        for key in keys:
            cam_clips = cameras.get(key, [])
            row = tk.Frame(content, bg=panel, padx=10, pady=8)
            row.pack(fill=tk.X, pady=(0, 6))
            names = sorted({re.sub(r'[0-9]+', '#', os.path.splitext(c.filename)[0]) for c in cam_clips})
            info = (f"{len(cam_clips)} clip" + ("s" if len(cam_clips) != 1 else "") +
                    (f"  ({', '.join(names[:2])})" if names else "  (not in this project)"))
            tk.Label(row, text=key, font=('Segoe UI', 10, 'bold'), width=12, anchor='w',
                     bg=panel, fg=self.COLORS['text']).pack(side=tk.LEFT)
            tk.Label(row, text=info, font=('Segoe UI', 9), width=28, anchor='w',
                     bg=panel, fg=self.COLORS['text_dim']).pack(side=tk.LEFT)
            var = tk.StringVar(value=f"{self.camera_offsets.get(key, 0.0):g}")
            ttk.Spinbox(row, textvariable=var, from_=-50, to=50, increment=1, width=7).pack(side=tk.LEFT, padx=(6, 4))
            tk.Label(row, text="frames", font=('Segoe UI', 9), bg=panel, fg=self.COLORS['text_dim']).pack(side=tk.LEFT)
            entries[key] = var

        def apply():
            new = {}
            for key, var in entries.items():
                try:
                    v = float(var.get().replace(',', '.'))
                except ValueError:
                    messagebox.showerror("Camera Offset", f"'{var.get()}' for {key} is not a number.", parent=popup)
                    return
                if abs(v) > 1e-6:
                    new[key] = v
            changed = new != self.camera_offsets
            self.camera_offsets = new
            popup.destroy()
            self._save_settings()
            if changed and self.synced and self.clips:
                self._sync_clips()   # re-place the clips with the new offsets
            self.status_label.configure(
                text="Camera offset: " + (", ".join(f"{k} {v:+g} fr" for k, v in new.items()) if new else "none"))

        buttons = tk.Frame(content, bg=bg)
        buttons.pack(fill=tk.X, pady=(10, 0))
        self._create_button(buttons, "OK", apply, 'accent').pack(side=tk.RIGHT)
        self._create_button(buttons, "Cancel", popup.destroy).pack(side=tk.RIGHT, padx=4)

        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - popup.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - popup.winfo_height()) // 2
        popup.geometry(f"+{x}+{y}")

    def _show_apply_ltc_dialog(self):
        """Show the Apply LTC to Files dialog - writes detected LTC timecode to file metadata."""
        # Debug: show all clips and their LTC track status
        # Check if we have any clips with detected LTC (exclude LTC reference tracks)
        clips_with_ltc = [c for c in self.clips if c.start_tc and c.start_frames > 0 and not c.is_ltc_track]

        if not clips_with_ltc:
            messagebox.showwarning("Apply LTC", "No clips with detected LTC timecode.\n\nPlease analyze your clips first.")
            return

        # Create popup window
        popup = tk.Toplevel(self.root)
        popup.title("Apply LTC to Files")
        popup.geometry("520x600")
        popup.configure(bg=self.COLORS['bg_card'])
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.grab_set()

        # Center on parent
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 520) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 600) // 2
        popup.geometry(f"+{x}+{y}")

        # Main content frame
        content = tk.Frame(popup, bg=self.COLORS['bg_card'])
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # Title
        tk.Label(content, text="Apply LTC to Files", font=('Segoe UI', 16, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text']).pack(pady=(0, 5))
        tk.Label(content, text="Write detected LTC timecode to file metadata", font=('Segoe UI', 10),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim']).pack(pady=(0, 15))

        # === File Handling Section ===
        tk.Label(content, text="File Handling", font=('Segoe UI', 10, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text'], anchor='w').pack(fill=tk.X, pady=(0, 5))

        file_frame = tk.Frame(content, bg=self.COLORS['bg_light'], padx=10, pady=8)
        file_frame.pack(fill=tk.X, pady=(0, 10))

        file_mode_var = tk.StringVar(value="copy")

        copy_radio = ttk.Radiobutton(file_frame, text="Create copy with suffix (e.g., file_LTC.mp4)",
                                     variable=file_mode_var, value="copy")
        copy_radio.pack(anchor='w', pady=2)

        overwrite_radio = ttk.Radiobutton(file_frame, text="Overwrite original files",
                                          variable=file_mode_var, value="overwrite")
        overwrite_radio.pack(anchor='w', pady=2)

        # Warning label for overwrite
        warning_frame = tk.Frame(file_frame, bg=self.COLORS['bg_light'])
        warning_label = tk.Label(warning_frame, text="  Warning: Overwriting is irreversible!",
                                 font=('Segoe UI', 9), bg=self.COLORS['bg_light'], fg='#E74C3C')
        warning_label.pack(anchor='w')

        def on_mode_change(*args):
            if file_mode_var.get() == "overwrite":
                warning_frame.pack(anchor='w', pady=(5, 0))
            else:
                warning_frame.pack_forget()

        file_mode_var.trace_add('write', on_mode_change)

        # === Clip Selection Section ===
        tk.Label(content, text="Clips to Process", font=('Segoe UI', 10, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text'], anchor='w').pack(fill=tk.X, pady=(5, 5))

        scope_var = tk.StringVar(value="all")

        # Count clips in each category
        selected_clips = [c for c in clips_with_ltc if c in self.selected_clips]
        audio_clips = [c for c in clips_with_ltc if c.is_audio_only]
        video_clips = [c for c in clips_with_ltc if not c.is_audio_only]

        scope_frame = tk.Frame(content, bg=self.COLORS['bg_light'])
        scope_frame.pack(fill=tk.X, pady=(0, 10), ipadx=10, ipady=8)

        all_radio = ttk.Radiobutton(scope_frame, text=f"All clips with LTC ({len(clips_with_ltc)} clips)",
                                    variable=scope_var, value="all")
        all_radio.pack(anchor='w', padx=10, pady=2)

        selected_radio = ttk.Radiobutton(scope_frame, text=f"Selected clips only ({len(selected_clips)} clips)",
                                         variable=scope_var, value="selected",
                                         state='normal' if selected_clips else 'disabled')
        selected_radio.pack(anchor='w', padx=10, pady=2)

        audio_radio = ttk.Radiobutton(scope_frame, text=f"Audio files only ({len(audio_clips)} clips)",
                                      variable=scope_var, value="audio",
                                      state='normal' if audio_clips else 'disabled')
        audio_radio.pack(anchor='w', padx=10, pady=2)

        video_radio = ttk.Radiobutton(scope_frame, text=f"Video files only ({len(video_clips)} clips)",
                                      variable=scope_var, value="video",
                                      state='normal' if video_clips else 'disabled')
        video_radio.pack(anchor='w', padx=10, pady=2)

        # === Output Directory Section ===
        tk.Label(content, text="Output Directory (for copies)", font=('Segoe UI', 10, 'bold'),
                 bg=self.COLORS['bg_card'], fg=self.COLORS['text'], anchor='w').pack(fill=tk.X, pady=(5, 5))

        output_dir_var = tk.StringVar(value="same")

        output_frame = tk.Frame(content, bg=self.COLORS['bg_light'])
        output_frame.pack(fill=tk.X, pady=(0, 10), ipadx=10, ipady=8)

        same_radio = ttk.Radiobutton(output_frame, text="Same directory as original files",
                                     variable=output_dir_var, value="same")
        same_radio.pack(anchor='w', padx=10, pady=2)

        custom_row = tk.Frame(output_frame, bg=self.COLORS['bg_light'])
        custom_row.pack(fill=tk.X, padx=10, pady=2)

        custom_radio = ttk.Radiobutton(custom_row, text="Custom:",
                                       variable=output_dir_var, value="custom")
        custom_radio.pack(side=tk.LEFT)

        custom_path_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_row, textvariable=custom_path_var, width=25)
        custom_entry.pack(side=tk.LEFT, padx=5)

        def browse_output_dir():
            dir_path = filedialog.askdirectory(title="Select Output Directory")
            if dir_path:
                custom_path_var.set(dir_path)
                output_dir_var.set("custom")

        browse_btn = ttk.Button(custom_row, text="Browse...", command=browse_output_dir)
        browse_btn.pack(side=tk.LEFT)

        # Separator
        tk.Frame(content, height=1, bg=self.COLORS['text_dim']).pack(fill=tk.X, pady=15)

        # Action buttons
        btn_frame = tk.Frame(content, bg=self.COLORS['bg_card'])
        btn_frame.pack(fill=tk.X)

        def apply_ltc():
            # Get selected options
            mode = file_mode_var.get()
            scope = scope_var.get()
            output_dir = output_dir_var.get()
            custom_dir = custom_path_var.get() if output_dir == "custom" else None

            # Validate custom directory
            if mode == "copy" and output_dir == "custom":
                if not custom_dir or not os.path.isdir(custom_dir):
                    messagebox.showerror("Error", "Please select a valid output directory.")
                    return

            # Confirm overwrite
            if mode == "overwrite":
                result = messagebox.askyesno("Confirm Overwrite",
                    "WARNING: This will modify your original files!\n\n"
                    "This operation cannot be undone.\n\n"
                    "Are you sure you want to continue?",
                    icon='warning')
                if not result:
                    return

            # Get clips to process
            if scope == "all":
                clips_to_process = clips_with_ltc
            elif scope == "selected":
                clips_to_process = selected_clips
            elif scope == "audio":
                clips_to_process = audio_clips
            else:  # video
                clips_to_process = video_clips

            # De-duplicate by file path (timeline splits have same path)
            # For duplicate paths, use the clip with earliest timeline position
            seen_paths = {}
            unique_clips = []
            skipped_splits = 0
            for clip in clips_to_process:
                if clip.path not in seen_paths:
                    seen_paths[clip.path] = clip
                    unique_clips.append(clip)
                else:
                    # Duplicate path - timeline split
                    # Keep the one with earlier timeline position (start of file)
                    existing = seen_paths[clip.path]
                    if clip.timeline_start < existing.timeline_start:
                        # This clip starts earlier, use its timecode
                        unique_clips.remove(existing)
                        unique_clips.append(clip)
                        seen_paths[clip.path] = clip
                    skipped_splits += 1

            if skipped_splits > 0:
                # Warn user about timeline splits
                messagebox.showinfo("Timeline Splits Detected",
                    f"{skipped_splits} timeline split(s) found pointing to the same file.\n\n"
                    f"The timecode from the earliest timeline position will be used.",
                    icon='info')

            popup.destroy()

            # Process unique clips only
            self._apply_ltc_to_clips(unique_clips, mode, custom_dir)

        apply_btn = tk.Button(btn_frame, text="Apply LTC", command=apply_ltc,
                              bg=self.COLORS['accent'], fg='white',
                              font=('Segoe UI', 10, 'bold'), padx=20, pady=5,
                              relief=tk.FLAT, cursor='hand2')
        apply_btn.pack(side=tk.RIGHT, padx=5)

        cancel_btn = tk.Button(btn_frame, text="Cancel", command=popup.destroy,
                               bg=self.COLORS['bg_light'], fg=self.COLORS['text'],
                               font=('Segoe UI', 10), padx=20, pady=5,
                               relief=tk.FLAT, cursor='hand2')
        cancel_btn.pack(side=tk.RIGHT, padx=5)

        popup.bind('<Escape>', lambda e: popup.destroy())

    def _apply_ltc_to_clips(self, clips: list, mode: str, custom_output_dir: Optional[str] = None):
        """Apply LTC timecode to file metadata using ffmpeg."""
        if not check_ffmpeg():
            messagebox.showerror("Error", "FFmpeg not found. Please install FFmpeg to use this feature.")
            return

        total = len(clips)
        success_count = 0
        error_count = 0
        errors = []

        # Cancellation state
        cancel_requested = [False]  # Use list to allow modification in nested functions
        self._apply_ltc_current_process = None  # Track current FFmpeg process
        self._apply_ltc_current_output = None  # Track current output file for cleanup

        # Create progress dialog
        progress_popup = tk.Toplevel(self.root)
        progress_popup.title("Applying LTC to Files")
        progress_popup.geometry("400x180")
        progress_popup.configure(bg=self.COLORS['bg_card'])
        progress_popup.transient(self.root)
        progress_popup.grab_set()

        # Center on parent
        progress_popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 180) // 2
        progress_popup.geometry(f"+{x}+{y}")

        progress_frame = tk.Frame(progress_popup, bg=self.COLORS['bg_card'])
        progress_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        progress_label = tk.Label(progress_frame, text="Processing...", font=('Segoe UI', 10),
                                  bg=self.COLORS['bg_card'], fg=self.COLORS['text'])
        progress_label.pack(pady=(0, 10))

        progress_bar = ttk.Progressbar(progress_frame, length=350, mode='determinate', maximum=total)
        progress_bar.pack(pady=5)

        file_label = tk.Label(progress_frame, text="", font=('Segoe UI', 9),
                              bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
        file_label.pack(pady=5)

        # Cancel button
        cancel_btn = tk.Button(progress_frame, text="Cancel", command=lambda: request_cancel(),
                               bg='#ef4444', fg='white', font=('Segoe UI', 9),
                               padx=20, pady=5, cursor='hand2')
        cancel_btn.pack(pady=(10, 0))

        def request_cancel():
            """Request cancellation of the Apply LTC operation."""
            cancel_requested[0] = True
            cancel_btn.configure(state='disabled', text='Cancelling...')
            progress_label.configure(text="Cancelling...")
            # Kill current FFmpeg process if running
            if self._apply_ltc_current_process:
                try:
                    self._apply_ltc_current_process.terminate()
                    self._apply_ltc_current_process.wait(timeout=2)
                except Exception:
                    try:
                        self._apply_ltc_current_process.kill()
                    except Exception:
                        pass
            # Clean up current output file
            if self._apply_ltc_current_output and os.path.exists(self._apply_ltc_current_output):
                try:
                    os.remove(self._apply_ltc_current_output)
                except Exception:
                    pass

        def on_close():
            """Handle window close (X button)."""
            request_cancel()
            # Wait briefly for cleanup, then close
            progress_popup.after(100, progress_popup.destroy)

        # Handle X button close
        progress_popup.protocol("WM_DELETE_WINDOW", on_close)

        # Process in a thread
        def process_clips():
            nonlocal success_count, error_count

            for i, clip in enumerate(clips):
                # Check for cancellation
                if cancel_requested[0]:
                    break

                # Update UI
                progress_popup.after(0, lambda idx=i, c=clip: update_progress(idx, c))

                try:
                    result = self._apply_ltc_to_single_file(clip, mode, custom_output_dir, cancel_requested)
                    if cancel_requested[0]:
                        break
                    if result:
                        success_count += 1
                    else:
                        error_count += 1
                        errors.append(f"{clip.filename}: " + (getattr(self, '_apply_ltc_last_error', '') or "Failed to apply LTC"))
                except Exception as e:
                    if not cancel_requested[0]:
                        error_count += 1
                        errors.append(f"{clip.filename}: {str(e)}")

            # Done - show results (unless cancelled)
            progress_popup.after(0, lambda: show_results(cancel_requested[0]))

        def update_progress(idx, clip):
            if cancel_requested[0]:
                return
            progress_bar['value'] = idx + 1
            progress_label.configure(text=f"Processing {idx + 1}/{total}...")
            file_label.configure(text=clip.filename)
            try:
                progress_popup.update()
            except Exception:
                pass  # Window may be closing

        def show_results(was_cancelled):
            # Clean up state
            self._apply_ltc_current_process = None
            self._apply_ltc_current_output = None

            try:
                progress_popup.destroy()
            except Exception:
                pass

            if was_cancelled:
                self.status_label.configure(text=f"Apply LTC cancelled ({success_count} completed)")
                messagebox.showinfo("Apply LTC Cancelled",
                    f"Operation cancelled.\n\n"
                    f"Successfully processed: {success_count} file(s)")
            elif error_count == 0:
                messagebox.showinfo("Apply LTC Complete",
                    f"Successfully applied LTC to {success_count} file(s).\n\n"
                    f"Files now have their embedded timecode set to the detected LTC timecode.")
            else:
                error_msg = "\n".join(errors[:5])
                if len(errors) > 5:
                    error_msg += f"\n... and {len(errors) - 5} more errors"
                messagebox.showwarning("Apply LTC Complete",
                    f"Processed {total} files:\n"
                    f"  ✓ Success: {success_count}\n"
                    f"  ✗ Failed: {error_count}\n\n"
                    f"Errors:\n{error_msg}")

            if not was_cancelled:
                self.status_label.configure(text=f"Applied LTC to {success_count}/{total} files")

        # Start processing thread
        thread = threading.Thread(target=process_clips, daemon=True)
        thread.start()

    def _apply_ltc_to_single_file(self, clip, mode: str, custom_output_dir: Optional[str] = None,
                                   cancel_requested: list = None) -> bool:
        """Apply LTC timecode to a single file. Returns True on success."""
        if not clip.start_tc or clip.start_frames <= 0:
            return False

        input_path = clip.path
        filename = os.path.basename(input_path)
        name, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        is_bwf = ext_lower in {'.wav', '.wave', '.bwf'}
        is_overwrite = mode == "overwrite"

        # For BWF overwrite mode, we can modify original directly (no temp file needed!)
        # This is INSTANT compared to copying the whole file
        if is_bwf and is_overwrite:
            try:
                success = self._apply_ltc_to_bwf(clip, input_path, None, cancel_requested, is_overwrite_mode=True)
                if success:
                    return True
                # If direct modification failed, fall through to FFmpeg path
            except Exception:
                pass

        self._apply_ltc_last_error = ""
        out_dir_check = os.path.dirname(os.path.abspath(input_path)) if (is_overwrite or not custom_output_dir) else custom_output_dir
        if not is_bwf:
            try:   # a remux writes a whole copy: refuse before touching anything when the disk can't hold it
                if shutil.disk_usage(out_dir_check).free < os.path.getsize(input_path) * 1.05 + 50 * 1024 * 1024:
                    self._apply_ltc_last_error = "not enough free disk space for the new copy (nothing was changed)"
                    return False
            except OSError:
                pass

        # Determine output path
        if is_overwrite:
            # The temp file goes in the SAME folder (same drive) as the original: the final replace is one atomic
            # rename, and a full disk or a power loss while writing leaves the original untouched. (It used to go
            # to the system temp folder, and shutil.move across drives copies over the original in place.)
            temp_fd, temp_path = tempfile.mkstemp(suffix=ext, prefix=".ltc_tmp_",
                                                  dir=os.path.dirname(os.path.abspath(input_path)))
            os.close(temp_fd)
            output_path = temp_path
            final_path = input_path
        else:
            # Create copy with _LTC suffix
            if custom_output_dir:
                output_dir = custom_output_dir
            else:
                output_dir = os.path.dirname(input_path)
            output_path = os.path.join(output_dir, f"{name}_LTC{ext}")
            final_path = output_path

        # Track current output for cleanup on cancel
        self._apply_ltc_current_output = output_path

        try:
            if is_bwf:
                # BWF audio file - update time_reference (copy mode, or overwrite fallback)
                success = self._apply_ltc_to_bwf(clip, input_path, output_path, cancel_requested, is_overwrite_mode=False)
            elif ext_lower in {'.mp4', '.mov', '.mxf', '.avi'}:
                # Video file - add timecode track (always requires full copy)
                success = self._apply_ltc_to_video(clip, input_path, output_path, cancel_requested)
            else:
                return False

            # Check if cancelled
            if cancel_requested and cancel_requested[0]:
                # Clean up output file
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        pass
                return False

            # A remuxed video must have the same streams and length as the original before it may replace it
            if success and not is_bwf:
                problem = self._remux_problem(input_path, output_path)
                if problem:
                    self._apply_ltc_last_error = f"the new file did not check out ({problem}); the original was not changed"
                    success = False
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass

            if success and is_overwrite:
                # Replace original with processed file (same folder: atomic)
                os.replace(output_path, final_path)

            self._apply_ltc_current_output = None
            return success

        except Exception:
            # Clean up temp file if exists
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except:
                    pass
            self._apply_ltc_current_output = None
            return False

    def _remux_problem(self, original: str, new: str) -> str:
        """'' when `new` has the same stream types and (nearly) the same duration as `original`, else why not."""
        a, b = self.analyzer._run_ffprobe(original), self.analyzer._run_ffprobe(new)
        if not a or not b:
            return "could not be read back"
        def kinds(info):
            out = {}
            for st in info.get('streams', []):
                t = st.get('codec_type', '?')
                if t in ('video', 'audio'):
                    out[t] = out.get(t, 0) + 1
            return out
        if kinds(a) != kinds(b):
            return f"streams {kinds(a)} -> {kinds(b)}"
        da, db = float(a.get('format', {}).get('duration', 0) or 0), float(b.get('format', {}).get('duration', 0) or 0)
        if da > 0 and abs(da - db) > max(0.5, da * 0.005):
            return f"duration {da:.1f} s -> {db:.1f} s"
        return ""

    def _apply_ltc_to_video(self, clip, input_path: str, output_path: str,
                             cancel_requested: list = None) -> bool:
        """Apply LTC timecode to video file using ffmpeg timecode stream."""
        # Format timecode for ffmpeg (HH:MM:SS:FF)
        tc = clip.start_tc

        # Build ffmpeg command
        # -timecode adds a timecode track to the output
        cmd = [
            FFMPEG_PATH,
            '-y',  # Overwrite output
            '-nostdin', '-nostats', '-loglevel', 'error',  # stderr is never read below: progress lines could fill the pipe and block ffmpeg
            '-i', input_path,
            '-c', 'copy',  # Copy all streams without re-encoding
            '-timecode', tc,
            output_path
        ]

        kwargs = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        # Use Popen to track process for cancellation
        process = subprocess.Popen(cmd, **kwargs)
        self._apply_ltc_current_process = process

        try:
            # Poll for completion while checking for cancellation
            while process.poll() is None:
                if cancel_requested and cancel_requested[0]:
                    # Terminate the process
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                    return False
                # Brief sleep to avoid busy-waiting
                time.sleep(0.1)

            self._apply_ltc_current_process = None
            if process.returncode == 0:
                # CRITICAL: Update clip.embedded_tc_frames to match what we wrote to the file
                # This ensures export uses the same value that DaVinci will read from the file
                clip.embedded_tc = clip.start_tc
                clip.embedded_tc_frames = clip.start_frames
                return True
            return False

        except Exception:
            self._apply_ltc_current_process = None
            try:
                process.kill()
            except Exception:
                pass
            return False

    def _apply_ltc_to_bwf(self, clip, input_path: str, output_path: str,
                           cancel_requested: list = None, is_overwrite_mode: bool = False) -> bool:
        """Apply LTC timecode to BWF audio file by updating time_reference.

        For overwrite mode: modifies original file directly (instant, no copy!)
        For copy mode: copies file then modifies, or falls back to FFmpeg.
        """
        # Calculate time_reference in samples
        # Priority: fps_override > clip.fps > 25.0 fallback
        if getattr(clip, 'fps_override', False) and clip.fps > 0:
            fps = clip.fps
        elif clip.fps and clip.fps > 0:
            fps = clip.fps
        else:
            fps = 25.0
        fps = XMLExporter.exact_fps(fps)   # 29.97 / 23.976 rounded put the time reference 36 ms off at 10:00:00:00
        sample_rate = clip.sample_rate if clip.sample_rate else 48000
        
        # Calculate time_reference (samples since midnight)
        # We must account for the offset to the first detected LTC frame
        # If LTC was found at sample 5000, the file actually starts 5000 samples BEFORE that LTC time
        base_samples = round(clip.start_frames / fps * sample_rate)
        ltc_offset = getattr(clip, 'ltc_offset', 0)
        
        # TimeReference = Time of LTC frame - Offset to that frame
        time_reference = base_samples - ltc_offset
        
        # Ensure non-negative (BWF TimeReference is unsigned 64-bit)
        if time_reference < 0:
            time_reference = 0

        # CRITICAL: Update clip.bwf_time_reference to match what we're writing to the file
        # This ensures export uses the same value that DaVinci will read from the file
        clip.bwf_time_reference = time_reference

        # OVERWRITE MODE: Modify original file directly - INSTANT!
        if is_overwrite_mode:
            if self._modify_bwf_time_reference_inplace(input_path, time_reference):
                # Success! No need to copy anything
                # Create empty output file as marker (will be deleted, original already modified)
                try:
                    # Signal success by NOT creating output - caller will skip the move
                    return True
                except Exception:
                    pass
            # Fall through to FFmpeg if direct modification failed (no BEXT chunk)

        # COPY MODE: Need to create a copy with modified time_reference
        # Try fast copy + modify first
        try:
            shutil.copy2(input_path, output_path)
            if self._modify_bwf_time_reference_inplace(output_path, time_reference):
                return True
            # If in-place modification failed, delete and fall back to FFmpeg
            os.remove(output_path)
        except Exception:
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass

        # Fall back to Python BWF writer (avoids FFmpeg trimming issues)
        # This inserts a BEXT chunk if missing, preserving original audio data exactly
        return self._apply_ltc_to_bwf_python(clip, input_path, output_path, time_reference, cancel_requested)

    def _apply_ltc_to_bwf_python(self, clip, input_path: str, output_path: str,
                                  time_reference: int, cancel_requested: list = None) -> bool:
        """Apply LTC to BWF by inserting/updating BEXT chunk using pure Python.
        
        This avoids FFmpeg's container rewriting which can cause audio trimming issues.
        """
        try:
            with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
                # Read and check RIFF header
                riff_id = fin.read(4)
                if riff_id != b'RIFF':
                    return False
                
                fin.read(4) # Skip original size
                wave_id = fin.read(4)
                if wave_id != b'WAVE':
                    return False
                
                # Write new RIFF header (size will be patched later)
                fout.write(b'RIFF')
                fout.write(b'\x00\x00\x00\x00') # Placeholder size
                fout.write(b'WAVE')
                
                # Create BEXT chunk data (Version 0, 602 bytes)
                # Description (256) + Originator (32) + Ref (32) + Date (10) + Time (8)
                bext_data = bytearray(602)
                
                # Set TimeReference (offset 338)
                # 256 + 32 + 32 + 10 + 8 = 338
                time_ref_bytes = time_reference.to_bytes(8, 'little')
                bext_data[338:346] = time_ref_bytes
                
                # Set Version to 1 (offset 346)
                bext_data[346] = 1
                
                # Write BEXT chunk
                fout.write(b'bext')
                fout.write(len(bext_data).to_bytes(4, 'little'))
                fout.write(bext_data)
                
                # Copy existing chunks, skipping any existing bext (though logically we shouldn't be here if it existed)
                while True:
                    if cancel_requested and cancel_requested[0]:
                        return False
                        
                    chunk_header = fin.read(8)
                    if len(chunk_header) < 8:
                        break
                        
                    chunk_id = chunk_header[0:4]
                    chunk_size = int.from_bytes(chunk_header[4:8], 'little')
                    
                    if chunk_id == b'bext':
                        # Skip existing bext if we found one (we already wrote ours)
                        fin.seek(chunk_size + (chunk_size % 2), 1)
                        continue
                        
                    # Copy other chunks (fmt, data, etc)
                    fout.write(chunk_header)
                    
                    # Copy chunk data in blocks to be memory efficient
                    bytes_to_copy = chunk_size + (chunk_size % 2)
                    copied = 0
                    block_size = 1024 * 1024 # 1MB blocks
                    
                    while copied < bytes_to_copy:
                        read_size = min(block_size, bytes_to_copy - copied)
                        data = fin.read(read_size)
                        if not data:
                            break
                        fout.write(data)
                        copied += len(data)
                
                # Patch file size in RIFF header
                file_size = fout.tell() - 8
                fout.seek(4)
                fout.write(file_size.to_bytes(4, 'little'))
                
            return True
            
        except Exception as e:
            print(f"Error in Python BWF writer: {e}")
            # Clean up partial file
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            return False

    def _modify_bwf_time_reference_inplace(self, filepath: str, time_reference: int) -> bool:
        """Modify BWF time_reference directly in file (fast, no full copy).

        Returns True if successful, False if file doesn't have BEXT chunk.
        """
        try:
            with open(filepath, 'r+b') as f:
                # Read RIFF header
                riff = f.read(4)
                if riff != b'RIFF':
                    return False

                f.read(4)  # Skip file size
                wave = f.read(4)
                if wave != b'WAVE':
                    return False

                # Search for bext chunk
                while True:
                    chunk_id = f.read(4)
                    if len(chunk_id) < 4:
                        return False  # No BEXT chunk found

                    chunk_size_bytes = f.read(4)
                    if len(chunk_size_bytes) < 4:
                        return False

                    chunk_size = int.from_bytes(chunk_size_bytes, 'little')

                    if chunk_id == b'bext':
                        # Found BEXT chunk!
                        # BEXT structure: description (256) + originator (32) + originator_ref (32)
                        #                + origination_date (10) + origination_time (8) + time_reference (8)
                        # time_reference is at offset 256 + 32 + 32 + 10 + 8 = 338 bytes into bext data
                        bext_start = f.tell()
                        time_ref_offset = bext_start + 256 + 32 + 32 + 10 + 8

                        # Seek to time_reference position and write new value
                        f.seek(time_ref_offset)
                        # time_reference is a 64-bit unsigned little-endian integer
                        f.write(time_reference.to_bytes(8, 'little'))
                        return True

                    # Skip to next chunk (account for padding byte if odd size)
                    skip = chunk_size + (chunk_size % 2)
                    f.seek(skip, 1)

        except Exception:
            return False

    def _apply_ltc_to_bwf_ffmpeg(self, clip, input_path: str, output_path: str,
                                  time_reference: int, cancel_requested: list = None) -> bool:
        """DEPRECATED: Apply LTC to BWF using FFmpeg."""
        # This method is kept as a reference but replaced by _apply_ltc_to_bwf_python
        # to avoid audio trimming issues caused by FFmpeg's container rewriting.
        pass

    def _sort_clips(self):
        """Sort clips based on the selected sort option."""
        sort_by = self.sort_option.get()

        if sort_by == "Name":
            self.clips.sort(key=lambda c: c.filename.lower())
        elif sort_by == "Timecode":
            # Sort by start timecode (clips without TC go to end)
            self.clips.sort(key=lambda c: (c.start_frames == 0, c.start_frames))
        elif sort_by == "Camera":
            # Sort by camera ID (None goes to end)
            self.clips.sort(key=lambda c: (c.camera_id is None, c.camera_id or ""))
        elif sort_by == "Duration":
            # Sort by duration (longest first)
            self.clips.sort(key=lambda c: -c.duration)
        elif sort_by == "Status":
            # Sort by status (done first, then pending, then failed)
            status_order = {'done': 0, 'analyzing': 1, 'pending': 2, 'no_ltc': 3, 'no_audio': 4, 'error': 5}
            self.clips.sort(key=lambda c: status_order.get(c.status, 9))

        # Preserve current selection
        self._refresh_clips_list()
        self.status_label.configure(text=f"Sorted by {sort_by}")

    def _on_search_focus_in(self, event):
        """Clear placeholder text when search entry gains focus."""
        if self.search_entry.get() == "Filter clips (name, TC, camera)...":
            self.search_entry.delete(0, tk.END)

    def _on_search_focus_out(self, event):
        """Restore placeholder text if search entry is empty."""
        if not self.search_entry.get():
            self.search_entry.insert(0, "Filter clips (name, TC, camera)...")

    def _clear_search(self):
        """Clear the search filter."""
        self.search_var.set("")
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, "Filter clips (name, TC, camera)...")
        self.root.focus_set()  # Remove focus from search entry
        self._refresh_clips_list()

    def _focus_search(self):
        """Focus the search entry (Ctrl+F shortcut)."""
        self.search_entry.focus_set()
        self.search_entry.select_range(0, tk.END)
        if self.search_entry.get() == "Filter clips (name, TC, camera)...":
            self.search_entry.delete(0, tk.END)

    def _filter_clips(self):
        """Filter clips based on search text and refresh the list."""
        self._refresh_clips_list()

    def _get_filtered_clips(self) -> list:
        """Get clips filtered by search text."""
        search_text = self.search_var.get().strip().lower()

        # Ignore placeholder text
        if not search_text or search_text == "filter clips (name, tc, camera)...":
            return self.clips

        filtered = []
        for clip in self.clips:
            # Search in filename
            if search_text in clip.filename.lower():
                filtered.append(clip)
                continue
            # Search in timecode
            if clip.start_tc and search_text in clip.start_tc.lower():
                filtered.append(clip)
                continue
            # Search in camera ID
            if clip.camera_id and search_text in clip.camera_id.lower():
                filtered.append(clip)
                continue
            # Search in path
            if search_text in clip.path.lower():
                filtered.append(clip)
                continue

        return filtered

    def _handle_spacebar(self, event):
        """Handle spacebar press - always triggers play/pause regardless of focus.

        Returns 'break' to prevent spacebar from activating focused buttons.
        """
        # Don't handle if we're in a text entry widget
        widget = event.widget
        if isinstance(widget, (tk.Entry, tk.Text)):
            return  # Let text widgets handle space normally

        # Toggle playback
        self._toggle_preview_play()

        # Return focus to the timeline canvas to prevent button focus issues
        try:
            self.timeline_canvas.focus_set()
        except:
            pass

        # Return 'break' to prevent spacebar from activating any focused button
        return 'break'

    def _toggle_preview_play(self):
        """Toggle play/pause for timeline or preview playback."""
        # If synced, toggle timeline playback
        if self.synced and self.clips:
            self._toggle_timeline_playback()
        # Otherwise toggle clip preview playback
        elif self.selected_clip:
            self._toggle_playback()

    def _timeline_step(self, frames):
        """Step the playhead by number of frames."""
        # Pause playback if running (frame stepping is typically done when paused)
        if self.timeline_playing:
            self._toggle_timeline_playback()

        if hasattr(self, 'playhead_position'):
            fps = 30.0
            if self.selected_clip:
                fps = self.selected_clip.fps
            step = frames / fps

            # Unified seek keeps audio mixer, clock and scheduler in step
            self._seek_playhead(self.playhead_position + step)

            # Full preview update for frame stepping (not during scrub)
            self._update_playhead_preview()

    def _zoom_timeline(self, factor, event=None):
        """Zoom the timeline by a factor, centered on cursor or playhead based on zoom_focus_mode."""
        # Store old zoom
        old_zoom = self.timeline_zoom
        old_pps = 50 * old_zoom  # pixels per second before zoom

        # Get visible width
        canvas_width = self.timeline_canvas.winfo_width()

        # Determine the focus point for zoom based on zoom_focus_mode
        if self.zoom_focus_mode == "Cursor" and event is not None:
            # Use cursor position as zoom center
            cursor_x = event.x
            # Calculate the time at cursor position
            focus_time = self.timeline_offset + (cursor_x / old_pps)
            # Cursor's relative position in the canvas
            relative_pos = cursor_x / canvas_width if canvas_width > 0 else 0.5
        else:
            # Use playhead position as zoom center (default behavior)
            focus_time = self.playhead_position
            # Calculate playhead position in widget coordinates before zoom
            playhead_widget_x = (self.playhead_position - self.timeline_offset) * old_pps
            # Calculate playhead's relative position in visible area (0.0 to 1.0)
            if 0 <= playhead_widget_x <= canvas_width:
                relative_pos = playhead_widget_x / canvas_width if canvas_width > 0 else 0.5
            else:
                relative_pos = 0.5  # Center if playhead not visible

        # Apply zoom
        self.timeline_zoom = max(0.01, min(10.0, self.timeline_zoom * factor))
        new_pps = 50 * self.timeline_zoom

        # Calculate new timeline_offset to keep focus point at same relative position
        # We want: focus_time - new_offset = relative_pos * canvas_width / new_pps
        # So: new_offset = focus_time - (relative_pos * canvas_width / new_pps)
        new_offset = focus_time - (relative_pos * canvas_width / new_pps)
        self.timeline_offset = max(0, new_offset)

        self._update_zoom_label()
        self._draw_timeline()
        self._sync_scrollbar_to_offset()

    def _update_zoom_label(self):
        """Update the zoom level indicator."""
        if hasattr(self, 'zoom_label'):
            percent = int(self.timeline_zoom * 100)
            # For very small zoom values, show decimal
            if percent < 1:
                self.zoom_label.configure(text=f"{self.timeline_zoom * 100:.1f}%")
            else:
                self.zoom_label.configure(text=f"{percent}%")

    def _reset_timeline_zoom(self):
        """Reset timeline zoom to fit all clips."""
        self._fit_timeline()

    # =========================================================================
    # Settings Persistence
    # =========================================================================

    def _load_settings(self):
        """Load saved settings from JSON file."""
        try:
            if os.path.exists(self.SETTINGS_FILE):
                with open(self.SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    settings = json.load(f)

                # Restore window geometry
                if 'geometry' in settings:
                    try:
                        self.root.geometry(settings['geometry'])
                    except:
                        pass  # Ignore invalid geometry

                # Restore UI settings
                if 'sync_logic' in settings:
                    self.sync_logic.set(settings['sync_logic'])

                if 'export_format' in settings:
                    self.export_format.set(settings['export_format'])

                if 'expected_fps' in settings:
                    self.expected_fps.set(settings['expected_fps'])
                if settings.get('decode_device') in DECODE_DEVICES:
                    self.decode_device.set(settings['decode_device'])
                if isinstance(settings.get('direct_gpu_playback'), str) and settings['direct_gpu_playback']:
                    global DIRECT_GPU_MODE
                    DIRECT_GPU_MODE = settings['direct_gpu_playback']  # Auto, Off or a GPU name
                if settings.get('preview_proxies') in PREVIEW_PROXY_MODES:
                    global PREVIEW_PROXY_MODE
                    PREVIEW_PROXY_MODE = settings['preview_proxies']
                if isinstance(settings.get('decode_gpu'), str) and settings['decode_gpu']:
                    global DECODE_GPU
                    self.decode_gpu = DECODE_GPU = settings['decode_gpu']

                if 'mute_ltc' in settings:
                    self.mute_ltc.set(settings['mute_ltc'])

                if 'include_camera_audio' in settings:
                    self.include_camera_audio.set(settings['include_camera_audio'])

                if 'split_stereo' in settings:
                    self.split_stereo.set(settings['split_stereo'])

                if 'waveform_cache_max_size_gb' in settings:
                    self.waveform_cache_max_size_gb = settings['waveform_cache_max_size_gb']

                if 'waveform_cache_custom_dir' in settings:
                    custom_dir = settings['waveform_cache_custom_dir']
                    if custom_dir:
                        # Try to create the directory if it doesn't exist
                        try:
                            os.makedirs(custom_dir, exist_ok=True)
                            self.waveform_cache_custom_dir = custom_dir
                            self.waveform_cache_dir = self._init_waveform_cache_dir(custom_dir)
                            self._sync_proxy_dir()
                        except Exception:
                            pass

                if 'use_linked' in settings:
                    self.use_linked.set(settings['use_linked'])

                if 'multicam_export' in settings:
                    self.multicam_export.set(settings['multicam_export'])
                if isinstance(settings.get('camera_offsets'), dict):
                    self.camera_offsets = {str(k): float(v) for k, v in settings['camera_offsets'].items()}
                if 'pack_audio_tracks' in settings:
                    self.pack_audio_tracks.set(settings['pack_audio_tracks'])
                if 'close_gaps' in settings:
                    self.close_gaps.set(bool(settings['close_gaps']))
                if str(settings.get('close_gaps_keep', '')) in ("0", "1", "2", "5", "10"):
                    self.close_gaps_keep.set(str(settings['close_gaps_keep']))
                if 'drift_correct' in settings:
                    self.drift_correct.set(settings['drift_correct'])
                if 'drift_segment_min' in settings:
                    self.drift_segment_min.set(str(settings['drift_segment_min']))

                if 'last_directory' in settings:
                    if os.path.isdir(settings['last_directory']):
                        self.last_directory = settings['last_directory']

                if 'timeline_zoom' in settings:
                    self.timeline_zoom = settings['timeline_zoom']
                    self._update_zoom_label()

                if 'show_tc_overlay' in settings:
                    self.show_tc_overlay.set(settings['show_tc_overlay'])

                if 'sort_option' in settings:
                    self.sort_option.set(settings['sort_option'])

                if 'recent_files' in settings:
                    # Filter out files that no longer exist
                    self.recent_files = [f for f in settings['recent_files'] if os.path.exists(f)]

                if 'zoom_focus_mode' in settings:
                    self.zoom_focus_mode = settings['zoom_focus_mode']

                if 'camera_id_source' in settings:
                    self.camera_id_source = settings['camera_id_source']
                if str(settings.get('analysis_threads', '')) in ("Auto", "1", "2", "4", "6", "8", "12", "16"):
                    self.analysis_threads = str(settings['analysis_threads'])

        except Exception:
            pass

    def _save_settings(self):
        """Save current settings to JSON file."""
        try:
            settings = {
                'geometry': self.root.geometry(),
                'sync_logic': self.sync_logic.get(),
                'export_format': self.export_format.get(),
                'expected_fps': self.expected_fps.get(),
                'decode_device': self.decode_device.get(),
                'decode_gpu': self.decode_gpu,
                'preview_proxies': PREVIEW_PROXY_MODE,
                'direct_gpu_playback': DIRECT_GPU_MODE,
                'mute_ltc': self.mute_ltc.get(),
                'include_camera_audio': self.include_camera_audio.get(),
                'split_stereo': self.split_stereo.get(),
                'use_linked': self.use_linked.get(),
                'multicam_export': self.multicam_export.get(),
                'pack_audio_tracks': self.pack_audio_tracks.get(),
                'close_gaps': self.close_gaps.get(),
                'close_gaps_keep': self.close_gaps_keep.get(),
                'camera_offsets': self.camera_offsets,
                'drift_correct': self.drift_correct.get(),
                'drift_segment_min': self.drift_segment_min.get(),
                'last_directory': self.last_directory,
                'timeline_zoom': self.timeline_zoom,
                'show_tc_overlay': self.show_tc_overlay.get(),
                'sort_option': self.sort_option.get(),
                'recent_files': self.recent_files[:self.MAX_RECENT_FILES],
                'waveform_cache_max_size_gb': self.waveform_cache_max_size_gb,
                'waveform_cache_custom_dir': self.waveform_cache_custom_dir,
                'zoom_focus_mode': self.zoom_focus_mode,
                'camera_id_source': self.camera_id_source,
                'analysis_threads': self.analysis_threads,
                'version': '1.0'  # For future compatibility
            }

            with open(self.SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)

        except Exception:
            pass

    # =========================================================================
    # Project Save/Load
    # =========================================================================

    def _project_data(self) -> dict:
        """Everything a project file holds. Used by Save AND auto-save, so the crash-recovery file
        loads exactly what a saved project does."""
        clips_data = []
        for clip in self.clips:
            clip_dict = {
                'path': clip.path,
                'filename': clip.filename,
                'duration': clip.duration,
                'original_duration': clip.original_duration,
                'start_tc': clip.start_tc,
                'end_tc': clip.end_tc,
                'start_frames': clip.start_frames,
                'fps': clip.fps,
                'drop_frame': clip.drop_frame,
                'ltc_channel': clip.ltc_channel,
                'ltc_offset': clip.ltc_offset,
                'has_linked_audio': clip.has_linked_audio,
                'linked_audio_path': clip.linked_audio_path,
                'width': clip.width,
                'height': clip.height,
                'video_fps': clip.video_fps,
                'audio_channels': clip.audio_channels,
                'sample_rate': clip.sample_rate,
                'status': clip.status,
                'error': clip.error,
                'ltc_warning': clip.ltc_warning,
                'file_date': clip.file_date.isoformat() if clip.file_date else None,
                'camera_id': clip.camera_id,
                'is_audio_only': clip.is_audio_only,
                'timeline_start': clip.timeline_start,
                'color': clip.color,
                'custom_color': bool(getattr(clip, 'custom_color', False)),
                'track_index': clip.track_index,
                # Audio linking fields
                'recording_id': clip.recording_id,
                'track_number': clip.track_number,
                'split_part': clip.split_part,
                'linked_ltc_path': clip.linked_ltc_path,
                'is_ltc_track': clip.is_ltc_track,
                'linked_audio_tracks': clip.linked_audio_tracks,
                # Embedded camera timecode (needed for FCPXML export)
                'embedded_tc': clip.embedded_tc,
                'embedded_tc_frames': clip.embedded_tc_frames,
                'bwf_time_reference': clip.bwf_time_reference,
                # TC source for sync (ltc, embedded, or auto)
                'tc_source': clip.tc_source,
                # LTC User bits (MMDDYYCC format with camera ID)
                'user_bits': clip.user_bits,
                'ub_camera_id': clip.ub_camera_id,
                'ub_date': clip.ub_date,
                # IN trim of a SPLIT part (its own key in clip_in_offsets; whole-file trims are below)
                'in_offset_split': self.clip_in_offsets.get(clip.path + f"_split_{id(clip)}"),
            }
            clips_data.append(clip_dict)

        split_key = re.compile(r'.*_split_\d+$')
        return {
            'version': '1.3',  # Bumped version for user bits support
            'app': 'LTC Sync App',
            'created': datetime.now().isoformat(),
            'synced': self.synced,
            'timeline_zoom': self.timeline_zoom,
            'timeline_offset': self.timeline_offset,
            'playhead_position': self.playhead_position,
            'in_point': self.in_point,
            'out_point': self.out_point,
            'loop_playback': self.loop_playback,
            'sync_logic': self.sync_logic.get(),
            'export_format': self.export_format.get(),
            'expected_fps': self.expected_fps.get(),
            'global_tc_source': self.global_tc_source.get(),  # Global TC source setting
            'clips': clips_data,
            'markers': self.timeline_markers,
            'camera_audio_selection': self.camera_audio_selection,
            'camera_offsets': self.camera_offsets,
            # Editing state - trims, locks, groups, track mute / solo / hide
            'clip_in_offsets': {k: v for k, v in self.clip_in_offsets.items() if not split_key.match(k)},
            'clip_out_offsets': dict(self.clip_out_offsets),
            'locked_clips': sorted(self.locked_clips),
            'clip_groups': {str(k): list(v) for k, v in self.clip_groups.items()},
            'clip_to_group': dict(self.clip_to_group),
            'next_group_id': self.next_group_id,
            'muted_tracks': sorted(self.muted_tracks),
            'soloed_tracks': sorted(self.soloed_tracks),
            'hidden_video_tracks': sorted(self.hidden_video_tracks),
            'soloed_video_tracks': sorted(self.soloed_video_tracks),
        }

    def _save_project(self):
        """Save the current project (clips and state) to a JSON file."""
        self._finalize_scrub_before_modal()
        if not self.clips:
            messagebox.showinfo("Info", "No clips to save")
            return

        # Force window to front
        self.root.lift()
        self.root.focus_force()
        self.root.update_idletasks()
        self.root.update()

        output_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Project",
            defaultextension=".ltcproj",
            initialdir=self.last_directory,
            filetypes=[("LTC Sync Project", "*.ltcproj"), ("JSON files", "*.json"), ("All files", "*.*")]
        )

        if not output_path:
            return

        self.last_directory = os.path.dirname(output_path)

        try:
            project = self._project_data()

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(project, f, indent=2)

            self.current_project_path = output_path
            project_name = os.path.basename(output_path)
            self.root.title(f"LTC Timecode Sync - {project_name}")
            messagebox.showinfo("Success", f"Project saved to:\n{output_path}")
            self.status_label.configure(text=f"Project saved: {project_name}")

            # Start auto-save timer
            self._start_auto_save()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to save project:\n{str(e)}")

    def _load_project(self):
        """Load a project from a JSON file."""
        self._finalize_scrub_before_modal()
        # Force window to front
        self.root.lift()
        self.root.focus_force()
        self.root.update_idletasks()
        self.root.update()

        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="Load Project",
            initialdir=self.last_directory,
            filetypes=[("LTC Sync Project", "*.ltcproj"), ("JSON files", "*.json"), ("All files", "*.*")]
        )

        if not file_path:
            return

        self.last_directory = os.path.dirname(file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                project = json.load(f)

            # Stop playback if running to prevent errors
            if self.timeline_playing:
                self._stop_timeline()
            if self.preview_playing:
                self._reset_preview()

            # Clear existing clips and audio cache
            self.clips.clear()
            self.selected_clips.clear()
            self.selected_clip = None
            self.audio_cache.clear()
            self.seg_audio.clear()
            self.clip_in_offsets.clear()  # Clear trim offsets to prevent stale data affecting playback

            # Playback-range state belongs to the project being loaded - never
            # let the previous project's IN/OUT/loop silently apply here
            self.in_point = project.get('in_point')
            self.out_point = project.get('out_point')
            self.loop_playback = bool(project.get('loop_playback', False))
            self.timeline_last_clip = None
            self._resume_after_scrub = False
            self._is_scrubbing = False

            # Load clips
            for clip_dict in project.get('clips', []):
                # Check if file still exists
                if not os.path.exists(clip_dict['path']):
                    continue

                clip = MediaClip(
                    path=clip_dict['path'],
                    filename=clip_dict['filename']
                )

                # Restore all attributes
                clip.duration = clip_dict.get('duration', 0.0)
                # original_duration defaults to duration if not saved (for old project files)
                clip.original_duration = clip_dict.get('original_duration', clip.duration)
                clip.start_tc = clip_dict.get('start_tc')
                clip.end_tc = clip_dict.get('end_tc')
                clip.start_frames = clip_dict.get('start_frames', 0)
                clip.fps = clip_dict.get('fps', 30.0)
                clip.drop_frame = clip_dict.get('drop_frame', False)
                clip.ltc_channel = clip_dict.get('ltc_channel', -1)
                clip.ltc_offset = clip_dict.get('ltc_offset', 0)
                clip.has_linked_audio = clip_dict.get('has_linked_audio', False)
                clip.linked_audio_path = clip_dict.get('linked_audio_path')
                clip.width = clip_dict.get('width', 1920)
                clip.height = clip_dict.get('height', 1080)
                clip.video_fps = clip_dict.get('video_fps', "30/1")
                clip.audio_channels = clip_dict.get('audio_channels', 2)
                clip.sample_rate = clip_dict.get('sample_rate', 48000)
                clip.status = clip_dict.get('status', 'pending')
                clip.ltc_warning = clip_dict.get('ltc_warning', '') or ''
                clip.error = clip_dict.get('error', '')
                clip.camera_id = clip_dict.get('camera_id')
                saved_audio_only = clip_dict.get('is_audio_only', False)
                # Verify against file extension - video extensions should not be audio-only
                ext = Path(clip.path).suffix.lower()
                video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.m4v', '.webm', '.wmv'}
                if saved_audio_only and ext in video_exts:
                    clip.is_audio_only = False
                else:
                    clip.is_audio_only = saved_audio_only
                clip.timeline_start = clip_dict.get('timeline_start', 0.0)
                clip.color = clip_dict.get('color', '#4a9eff')
                clip.custom_color = bool(clip_dict.get('custom_color', False))
                clip.track_index = clip_dict.get('track_index', 0)
                # Audio linking fields
                clip.recording_id = clip_dict.get('recording_id')
                clip.track_number = clip_dict.get('track_number')
                clip.split_part = clip_dict.get('split_part')
                clip.linked_ltc_path = clip_dict.get('linked_ltc_path')
                clip.is_ltc_track = clip_dict.get('is_ltc_track', False)
                clip.linked_audio_tracks = clip_dict.get('linked_audio_tracks')
                # Embedded camera timecode (for FCPXML export)
                clip.embedded_tc = clip_dict.get('embedded_tc')
                clip.embedded_tc_frames = clip_dict.get('embedded_tc_frames', 0)
                clip.bwf_time_reference = clip_dict.get('bwf_time_reference', 0)
                if (not clip.is_audio_only and clip.embedded_tc and 'video_fps' in clip_dict and clip.fps
                        and not clip.bwf_time_reference
                        and clip.embedded_tc != clip_dict.get('start_tc')):   # equal: no camera TC, LTC stood in
                    # older versions counted a 50p camera TC's frame field at the 25 fps LTC rate
                    clip.embedded_tc_frames = XMLExporter.tc_frames_at(
                        clip.embedded_tc, clip.fps, XMLExporter.video_tc_rate(clip.video_fps))
                # TC source for sync (ltc, embedded, or auto)
                clip.tc_source = clip_dict.get('tc_source', 'auto')
                # LTC User bits (MMDDYYCC format with camera ID)
                clip.user_bits = clip_dict.get('user_bits')
                clip.ub_camera_id = clip_dict.get('ub_camera_id')
                clip.ub_date = clip_dict.get('ub_date')

                # Auto-read embedded TC for old projects that don't have it
                if not clip.embedded_tc and os.path.exists(clip.path) and clip.fps:
                    try:
                        clip.embedded_tc, clip.embedded_tc_frames, clip.bwf_time_reference = self.analyzer._get_embedded_timecode(
                            clip.path, clip.fps
                        )
                        if not clip.embedded_tc:
                            # Fall back to LTC
                            clip.embedded_tc = clip.start_tc
                            clip.embedded_tc_frames = clip.start_frames
                    except Exception:
                        pass

                if clip_dict.get('file_date'):
                    try:
                        clip.file_date = datetime.fromisoformat(clip_dict['file_date'])
                    except:
                        pass

                if clip_dict.get('in_offset_split') is not None:   # IN point of a split part
                    self.clip_in_offsets[clip.path + f"_split_{id(clip)}"] = float(clip_dict['in_offset_split'])
                self.clips.append(clip)

            # Restore state
            self.synced = project.get('synced', False)
            self.timeline_zoom = project.get('timeline_zoom', 1.0)
            self.timeline_offset = project.get('timeline_offset', 0.0)
            self.playhead_position = project.get('playhead_position', 0.0)

            if 'sync_logic' in project:
                self.sync_logic.set(project['sync_logic'])
            if 'export_format' in project:
                self.export_format.set(project['export_format'])
            if 'expected_fps' in project:
                self.expected_fps.set(project['expected_fps'])
            if 'global_tc_source' in project:
                self.global_tc_source.set(project['global_tc_source'])

            # Restore markers
            self.timeline_markers = project.get('markers', [])
            self.camera_audio_selection = {
                str(k): [int(c) for c in v]
                for k, v in (project.get('camera_audio_selection') or {}).items()}
            if isinstance(project.get('camera_offsets'), dict):   # older projects keep the remembered offsets
                self.camera_offsets = {str(k): float(v) for k, v in project['camera_offsets'].items()}

            # Editing state: trims, locks, groups, track mute / solo / hide (older projects have none -> empty)
            self.clip_in_offsets.update({str(k): float(v) for k, v in (project.get('clip_in_offsets') or {}).items()})
            self.clip_out_offsets = {str(k): float(v) for k, v in (project.get('clip_out_offsets') or {}).items()}
            self.locked_clips = set(project.get('locked_clips') or [])
            self.clip_groups = {int(k): list(v) for k, v in (project.get('clip_groups') or {}).items()}
            self.clip_to_group = {str(k): int(v) for k, v in (project.get('clip_to_group') or {}).items()}
            self.next_group_id = int(project.get('next_group_id') or (max(self.clip_groups, default=0) + 1))
            self.muted_tracks = set(project.get('muted_tracks') or [])
            self.soloed_tracks = set(project.get('soloed_tracks') or [])
            self.hidden_video_tracks = set(project.get('hidden_video_tracks') or [])
            self.soloed_video_tracks = set(project.get('soloed_video_tracks') or [])

            # Re-assign camera colors
            self._assign_camera_colors()

            # Update UI
            self._refresh_clips_list()
            self._draw_timeline()
            self._update_zoom_label()

            self.current_project_path = file_path
            project_name = os.path.basename(file_path)
            self.root.title(f"LTC Timecode Sync - {project_name}")
            messagebox.showinfo("Success", f"Project loaded:\n{file_path}\n\n{len(self.clips)} clips restored.")
            self.status_label.configure(text=f"Loaded project: {project_name} ({len(self.clips)} clips)")

            # DISABLED: Old audio extraction - now using lazy loading during playback
            # if self.synced:
            #     self._prepare_timeline_audio()

            # Start auto-save timer
            self._start_auto_save()

            # Proxies for heavy footage (already-built ones are reused)
            self._queue_preview_proxies()
            self._schedule_direct_warm(1500)  # after the format tests

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load project:\n{str(e)}")

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')

        style.configure('TFrame', background=self.COLORS['bg'])
        style.configure('Card.TFrame', background=self.COLORS['bg_card'])
        style.configure('TLabel', background=self.COLORS['bg'], foreground=self.COLORS['text'], font=('Segoe UI', 10))
        style.configure('Header.TLabel', font=('Segoe UI', 24, 'bold'), foreground=self.COLORS['accent'])
        style.configure('SubHeader.TLabel', font=('Segoe UI', 12), foreground=self.COLORS['text_dim'])
        style.configure('TC.TLabel', font=('Consolas', 14, 'bold'), foreground=self.COLORS['success'])

        style.configure('Accent.TButton', font=('Segoe UI', 11, 'bold'))
        style.map('Accent.TButton', background=[('active', self.COLORS['accent_hover'])])

        style.configure('TCombobox', font=('Segoe UI', 10))
        style.configure('TProgressbar', troughcolor=self.COLORS['bg_light'], background=self.COLORS['accent'])

    def _build_ui(self):
        # Main container
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # Header section
        self._build_header(main)

        # Toolbar
        self._build_toolbar(main)

        # Main content area (clips + preview + timeline)
        content = ttk.Frame(main)
        content.pack(fill=tk.BOTH, expand=True, pady=10)

        # Left panel - Clips list
        self._build_clips_panel(content)

        # Right side container (preview + timeline)
        right_container = ttk.Frame(content)
        right_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Video Preview panel (top right)
        self._build_preview_panel(right_container)

        # Timeline panel (bottom right)
        self._build_timeline_panel(right_container)

        # Bottom - Status & Progress
        self._build_status_bar(main)

    def _build_header(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(header, text="LTC Timecode Sync", style='Header.TLabel').pack(side=tk.LEFT)

        # TC Source selector (LTC or Embedded timecode)
        tc_source_frame = ttk.Frame(header)
        tc_source_frame.pack(side=tk.RIGHT, padx=(20, 0))

        ttk.Label(tc_source_frame, text="TC Source:", style='SubHeader.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        self.global_tc_source = tk.StringVar(value="LTC (Audio)")
        tc_source_combo = ttk.Combobox(tc_source_frame, textvariable=self.global_tc_source, width=14, state='readonly',
                                        values=["LTC (Audio)", "Embedded (File)", "Auto (LTC>Emb)"])
        tc_source_combo.pack(side=tk.LEFT)

        # Button to use embedded TC for clips without LTC
        self._create_button(tc_source_frame, "Use Emb for No-LTC",
                           self._set_all_no_ltc_to_embedded, 'normal').pack(side=tk.LEFT, padx=(10, 0))

        # Sync logic selector
        logic_frame = ttk.Frame(header)
        logic_frame.pack(side=tk.RIGHT)

        ttk.Label(logic_frame, text="Sync Logic:", style='SubHeader.TLabel').pack(side=tk.LEFT, padx=(0, 10))
        self.sync_logic = tk.StringVar(value="TC Only")
        logic_combo = ttk.Combobox(logic_frame, textvariable=self.sync_logic, width=15, state='readonly',
                                    values=["TC Only", "TC + Filename", "TC + Date"])
        logic_combo.pack(side=tk.LEFT)

    def _build_toolbar(self, parent):
        # Two rows: one row needed ~2600px, so on a 2560px (or smaller)
        # window the controls packed last - Decode, and the Settings/Help/About
        # buttons - were silently clipped off the right edge.
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, pady=5)
        row1 = ttk.Frame(toolbar)
        row1.pack(fill=tk.X)
        row2 = ttk.Frame(toolbar)
        row2.pack(fill=tk.X, pady=(5, 0))

        # Help, Settings and About buttons (far right) - packed FIRST so their
        # space is reserved even when the window is narrow
        help_frame = ttk.Frame(row1)
        help_frame.pack(side=tk.RIGHT)
        self._create_button(help_frame, "?", self._show_keyboard_shortcuts, 'normal').pack(side=tk.RIGHT, padx=2)
        self._create_button(help_frame, "ⓘ", self._show_about, 'normal').pack(side=tk.RIGHT, padx=2)
        settings_btn = self._create_button(help_frame, "⚙ Settings", self._show_settings_dialog, 'normal')
        settings_btn.pack(side=tk.RIGHT, padx=2)

        # Left buttons
        left = ttk.Frame(row1)
        left.pack(side=tk.LEFT)

        self._create_button(left, "+ Add Files", self._add_files, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(left, "+ Add Folder", self._add_folder, 'normal').pack(side=tk.LEFT, padx=2)
        self.recent_files_btn = self._create_button(left, "Recent", self._show_recent_files_menu, 'normal')
        self.recent_files_btn.pack(side=tk.LEFT, padx=2)
        # Update state based on whether there are recent files
        self.root.after(100, self._update_recent_files_menu)
        self._create_button(left, "Clear All", self._clear_all_clips, 'normal').pack(side=tk.LEFT, padx=2)

        # Project operations separator
        ttk.Separator(left, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self._create_button(left, "Save Project", self._save_project, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(left, "Load Project", self._load_project, 'normal').pack(side=tk.LEFT, padx=2)

        # Separator
        ttk.Separator(row1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=15)

        # Center - Main actions
        center = ttk.Frame(row1)
        center.pack(side=tk.LEFT)

        self.analyze_btn = self._create_button(center, "Analyze", self._analyze_clips, 'accent')
        self.analyze_btn.pack(side=tk.LEFT, padx=2)

        self._create_button(center, "Re-analyze", self._reanalyze_selected, 'normal').pack(side=tk.LEFT, padx=2)

        self.sync_btn = self._create_button(center, "Resync", self._sync_clips, 'accent')
        self.sync_btn.pack(side=tk.LEFT, padx=2)

        # Lock/Unlock button for timeline editing
        self.lock_btn = self._create_button(center, "Unlock", self._toggle_timeline_lock, 'normal')
        self.lock_btn.pack(side=tk.LEFT, padx=2)

        # Separator
        ttk.Separator(row1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=15)

        # Right - Export
        right = ttk.Frame(row1)
        right.pack(side=tk.LEFT)

        ttk.Label(right, text="Export:").pack(side=tk.LEFT, padx=5)
        self.export_format = tk.StringVar(value="Premiere Pro")
        export_combo = ttk.Combobox(right, textvariable=self.export_format, width=15, state='readonly',
                                     values=["Premiere Pro", "DaVinci Resolve", "Final Cut Pro X"])
        export_combo.pack(side=tk.LEFT, padx=2)

        self._create_button(right, "Export XML", self._export_xml, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(right, "EDL", self._export_edl, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(right, "Report", self._export_report, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(right, "Apply LTC", self._show_apply_ltc_dialog, 'normal').pack(side=tk.LEFT, padx=2)

        # Row 2 - export audio options
        audio_frame = ttk.Frame(row2)
        audio_frame.pack(side=tk.LEFT)

        self.mute_ltc = tk.BooleanVar(value=True)
        ttk.Checkbutton(audio_frame, text="Mute LTC", variable=self.mute_ltc,
                       takefocus=False).pack(side=tk.LEFT, padx=5)

        self.include_camera_audio = tk.BooleanVar(value=False)
        ttk.Checkbutton(audio_frame, text="Camera Audio", variable=self.include_camera_audio,
                       takefocus=False).pack(side=tk.LEFT, padx=(5, 0))
        cam_audio_btn = self._create_button(audio_frame, "Choose...", self._show_camera_audio_dialog)
        cam_audio_btn.configure(padx=6, pady=1, font=('Segoe UI', 9))
        cam_audio_btn.pack(side=tk.LEFT, padx=(2, 5))
        ToolTip(cam_audio_btn, lambda: "Pick which camera audio channels to export, per camera (each as a mono track)")
        cam_off_btn = self._create_button(audio_frame, "Cam Offset...", self._show_camera_offset_dialog)
        cam_off_btn.configure(padx=6, pady=1, font=('Segoe UI', 9))
        cam_off_btn.pack(side=tk.LEFT, padx=(0, 5))
        ToolTip(cam_off_btn, lambda: ("Per-camera offset in frames\n"
                                      "Each camera records the LTC through its audio, and the audio delay differs per\n"
                                      "camera model and mode. If one camera's picture is off on a clap, shift that camera here.\n"
                                      "+ = later on the timeline, - = earlier. Applies to the timeline and every export."))

        self.split_stereo = tk.BooleanVar(value=False)
        ttk.Checkbutton(audio_frame, text="Split Stereo", variable=self.split_stereo,
                       takefocus=False).pack(side=tk.LEFT, padx=5)

        self.use_linked = tk.BooleanVar(value=True)
        ttk.Checkbutton(audio_frame, text="Use Linked Audio", variable=self.use_linked,
                       takefocus=False).pack(side=tk.LEFT, padx=5)

        self.multicam_export = tk.BooleanVar(value=False)
        ttk.Checkbutton(audio_frame, text="Multicam", variable=self.multicam_export,
                       takefocus=False).pack(side=tk.LEFT, padx=5)

        self.close_gaps = tk.BooleanVar(value=False)
        gaps_chk = ttk.Checkbutton(audio_frame, text="Remove Gaps", variable=self.close_gaps, takefocus=False)
        gaps_chk.pack(side=tk.LEFT, padx=(5, 2))
        self.close_gaps_keep = tk.StringVar(value="0")
        gaps_combo = ttk.Combobox(audio_frame, textvariable=self.close_gaps_keep, width=3, state='readonly',
                                  values=["0", "1", "2", "5", "10"])
        gaps_combo.pack(side=tk.LEFT, padx=(0, 5))
        for w in (gaps_chk, gaps_combo):
            ToolTip(w, lambda: ("Remove gaps (exports only)\n"
                                "The exported timeline leaves out the empty time between takes; the number\n"
                                "is the gap in seconds to keep between them (0 = about 0.1 s).\n"
                                "Clips that overlap stay together and keep their sync. This app's timeline\n"
                                "keeps the real timecode."))

        self.pack_audio_tracks = tk.BooleanVar(value=False)
        pack_chk = ttk.Checkbutton(audio_frame, text="Share Tracks", variable=self.pack_audio_tracks,
                                   takefocus=False, command=self._on_pack_audio_changed)
        pack_chk.pack(side=tk.LEFT, padx=5)
        ToolTip(pack_chk, lambda: ("Share audio tracks between takes\n"
                                   "Off: every recording gets its own audio track.\n"
                                   "On: takes from the SAME recorder and the same recorder track (LR, Tr1, ...)\n"
                                   "go on one track, side by side, when their timecodes don't overlap.\n"
                                   "A new track is added only for an overlap. A different recorder\n"
                                   "(another folder or file name pattern) always gets its own tracks.\n"
                                   "Applies to the timeline and to every XML export."))

        self.drift_correct = tk.BooleanVar(value=False)
        drift_chk = ttk.Checkbutton(audio_frame, text="Drift fix", variable=self.drift_correct, takefocus=False)
        drift_chk.pack(side=tk.LEFT, padx=(5, 2))
        self.drift_segment_min = tk.StringVar(value="5")
        drift_combo = ttk.Combobox(audio_frame, textvariable=self.drift_segment_min, width=3, state='readonly',
                                   values=["1", "2", "5", "10", "15", "30"])
        drift_combo.pack(side=tk.LEFT)
        ttk.Label(audio_frame, text="min").pack(side=tk.LEFT, padx=(2, 5))
        drift_tip = ("Recorder drift fix (DaVinci Resolve export)\n"
                     "A recorder's clock is never exact (a Zoom H6 measured ~10 ppm = 4 frames in 5 h).\n"
                     "On: long recordings with LTC on a track are cut every N minutes on the timeline,\n"
                     "each piece placed by its own LTC - stays within ~0.1 frame. No new files:\n"
                     "every piece points at the original WAV.")
        ToolTip(drift_chk, lambda: drift_tip)
        ToolTip(drift_combo, lambda: drift_tip)

        # FPS selector for LTC detection
        ttk.Separator(row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=15)

        fps_frame = ttk.Frame(row2)
        fps_frame.pack(side=tk.LEFT)

        ttk.Label(fps_frame, text="Expected FPS:").pack(side=tk.LEFT, padx=5)
        self.expected_fps = tk.StringVar(value="Auto")
        fps_combo = ttk.Combobox(fps_frame, textvariable=self.expected_fps, width=10, state='readonly',
                                  values=["Auto", "23.976", "24", "25", "29.97 DF", "29.97 NDF", "30"])
        fps_combo.pack(side=tk.LEFT, padx=2)

        # Video decode device for preview / playback / thumbnails
        ttk.Label(fps_frame, text="Decode (GPU/CPU):").pack(side=tk.LEFT, padx=(10, 5))
        self.decode_device = tk.StringVar(value=DECODE_DEVICE)
        decode_combo = ttk.Combobox(fps_frame, textvariable=self.decode_device, width=6,
                                    state='readonly', values=list(DECODE_DEVICES))
        decode_combo.pack(side=tk.LEFT, padx=2)
        self.decode_device.trace_add('write', self._on_decode_device_change)
        ToolTip(decode_combo, lambda: (
            "Video decoding for preview, playback and thumbnails\n"
            "Auto - GPU where it is faster (VLC playback, thumbnails)\n"
            "GPU  - GPU everywhere it can be used\n"
            "CPU  - no GPU decoding\n"
            f"GPU decoder: {self._gpu_decoder_text()}\n"
            "LTC sync analysis reads audio only and always runs on the CPU"))

    def _get_camera_color(self, camera_id: Optional[str], clip_index: int = 0) -> str:
        """Get or assign a consistent color for a camera ID."""
        if camera_id is None:
            # No camera detected - give each unknown clip a unique color based on index
            colors = self.COLORS['clip_colors']
            return colors[clip_index % len(colors)]

        if camera_id not in self.camera_color_map:
            # Assign next available color
            colors = self.COLORS['clip_colors']
            self.camera_color_map[camera_id] = colors[self.next_color_index % len(colors)]
            self.next_color_index += 1

        return self.camera_color_map[camera_id]

    def _assign_camera_colors(self):
        """Assign colors to all clips based on their camera ID."""
        # Reset color mapping to reassign based on analysis results
        self.camera_color_map = {}
        self.next_color_index = 0

        # First pass: assign colors to clips WITH camera_id
        for clip in self.clips:
            if clip.camera_id:
                self._get_camera_color(clip.camera_id)

        # Second pass: assign colors to all clips (a colour the user picked is kept)
        for i, clip in enumerate(self.clips):
            if getattr(clip, 'custom_color', False):
                continue
            clip.color = self._get_camera_color(clip.camera_id, i)

        # Update the camera legend in the timeline
        self._update_camera_legend()

    def _create_button(self, parent, text, command, style='normal'):
        def on_click():
            self.root.focus_set()  # Ensure main window has focus
            command()

        btn = tk.Button(parent, text=text, command=on_click, font=('Segoe UI', 10),
                        relief=tk.FLAT, cursor='hand2', padx=15, pady=5, bd=0,
                        takefocus=False)
        if style == 'accent':
            btn.configure(bg=self.COLORS['accent'], fg='white',
                         activebackground=self.COLORS['accent_hover'], activeforeground='white')
        else:
            btn.configure(bg=self.COLORS['bg_light'], fg=self.COLORS['text'],
                         activebackground=self.COLORS['bg_card'], activeforeground=self.COLORS['text'])

        # Hover effects
        def on_enter(e):
            if style == 'accent':
                btn.configure(bg=self.COLORS['accent_hover'])
            else:
                btn.configure(bg=self.COLORS['bg_card'])
        def on_leave(e):
            if style == 'accent':
                btn.configure(bg=self.COLORS['accent'])
            else:
                btn.configure(bg=self.COLORS['bg_light'])

        btn.bind('<Enter>', on_enter)
        btn.bind('<Leave>', on_leave)
        return btn

    def _build_clips_panel(self, parent):
        # Left panel frame
        left_panel = ttk.Frame(parent, width=450)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 10))
        left_panel.pack_propagate(False)

        # Header
        header = ttk.Frame(left_panel)
        header.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(header, text="Media Files", font=('Segoe UI', 12, 'bold')).pack(side=tk.LEFT)

        # Sort dropdown
        sort_frame = ttk.Frame(header)
        sort_frame.pack(side=tk.RIGHT, padx=(0, 10))
        ttk.Label(sort_frame, text="Sort:", style='SubHeader.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        self.sort_option = tk.StringVar(value="Name")
        sort_combo = ttk.Combobox(sort_frame, textvariable=self.sort_option, width=10, state='readonly',
                                   values=["Name", "Timecode", "Camera", "Duration", "Status"])
        sort_combo.pack(side=tk.LEFT)
        sort_combo.bind('<<ComboboxSelected>>', lambda e: self._sort_clips())

        self.clip_count_label = ttk.Label(header, text="0 clips", style='SubHeader.TLabel')
        self.clip_count_label.pack(side=tk.RIGHT)

        # Search/filter bar
        search_frame = ttk.Frame(left_panel)
        search_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(search_frame, text="🔍", font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=(5, 2))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.search_entry.insert(0, "Filter clips (name, TC, camera)...")
        self.search_entry.bind('<FocusIn>', self._on_search_focus_in)
        self.search_entry.bind('<FocusOut>', self._on_search_focus_out)
        self.search_var.trace_add('write', lambda *args: self._filter_clips())

        clear_btn = ttk.Button(search_frame, text="✕", width=3, command=self._clear_search)
        clear_btn.pack(side=tk.LEFT)

        # Scrollable clips container
        canvas_frame = ttk.Frame(left_panel)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        self.clips_canvas = tk.Canvas(canvas_frame, bg=self.COLORS['bg_light'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.clips_canvas.yview)

        self.clips_frame = ttk.Frame(self.clips_canvas)
        self.clips_canvas.create_window((0, 0), window=self.clips_frame, anchor='nw')

        self.clips_canvas.configure(yscrollcommand=scrollbar.set)
        self.clips_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.clips_frame.bind('<Configure>', lambda e: self.clips_canvas.configure(
            scrollregion=self.clips_canvas.bbox('all')))

        # Drop zone overlay
        self._create_drop_zone()

        # Setup drag-and-drop if available
        if DND_AVAILABLE:
            self._setup_dnd()

    def _create_drop_zone(self):
        """Create drag & drop visual zone."""
        dnd_text = "\n\nDrag & Drop Files Here\n\nor click 'Add Files'\n\n"
        if not DND_AVAILABLE:
            dnd_text = "\n\nClick 'Add Files' to add media\n\nor 'Add Folder' to scan a directory\n\n"
        self.drop_label = ttk.Label(self.clips_frame,
                                     text=dnd_text,
                                     font=('Segoe UI', 12), foreground=self.COLORS['text_dim'],
                                     anchor='center', justify='center')
        self.drop_label.pack(pady=50, padx=20)

    def _setup_dnd(self):
        """Setup drag-and-drop handlers."""
        if not DND_AVAILABLE:
            return

        # Register drop target on the clips canvas
        self.clips_canvas.drop_target_register(DND_FILES)
        self.clips_canvas.dnd_bind('<<Drop>>', self._handle_drop)

        # Visual feedback on drag enter/leave
        self.clips_canvas.dnd_bind('<<DragEnter>>', self._on_drag_enter)
        self.clips_canvas.dnd_bind('<<DragLeave>>', self._on_drag_leave)

    def _handle_drop(self, event):
        """Handle dropped files."""
        try:
            # Parse dropped files (format varies by platform)
            data = event.data
            files = []

            # Windows format: files separated by spaces, paths with spaces in curly braces
            if '{' in data:
                # Extract paths from curly braces
                import re
                files = re.findall(r'\{([^}]+)\}', data)
                # Also get non-braced paths
                remaining = re.sub(r'\{[^}]+\}', '', data).strip()
                if remaining:
                    files.extend(remaining.split())
            else:
                files = data.split()

            # Get existing paths for fast duplicate check
            existing_paths = self._get_existing_paths()

            count = 0
            skipped = 0
            for f in files:
                f = f.strip()
                if os.path.isfile(f) and self.analyzer.is_supported(f):
                    # Check for duplicate
                    normalized = os.path.normpath(os.path.abspath(f)).lower()
                    if normalized in existing_paths:
                        skipped += 1
                        continue

                    clip = MediaClip(path=f, filename=os.path.basename(f))
                    clip.is_audio_only = self.analyzer.is_audio_file(f)
                    clip.color = self.COLORS['clip_colors'][len(self.clips) % len(self.COLORS['clip_colors'])]
                    self.clips.append(clip)
                    existing_paths.add(normalized)  # Track newly added
                    count += 1
                elif os.path.isdir(f):
                    # Handle dropped folders
                    for root_dir, _, dir_files in os.walk(f):
                        for df in dir_files:
                            path = os.path.join(root_dir, df)
                            if self.analyzer.is_supported(path):
                                # Check for duplicate
                                normalized = os.path.normpath(os.path.abspath(path)).lower()
                                if normalized in existing_paths:
                                    skipped += 1
                                    continue

                                clip = MediaClip(path=path, filename=df)
                                clip.is_audio_only = self.analyzer.is_audio_file(path)
                                clip.color = self.COLORS['clip_colors'][len(self.clips) % len(self.COLORS['clip_colors'])]
                                self.clips.append(clip)
                                existing_paths.add(normalized)  # Track newly added
                                count += 1

            if count > 0 or skipped > 0:
                self._refresh_clips_list()
                self.synced = False
                if skipped > 0:
                    self.status_label.configure(text=f"Added {count} file(s), skipped {skipped} duplicate(s)")
                else:
                    self.status_label.configure(text=f"Added {count} file(s) via drag & drop")

            # Reset canvas background
            self.clips_canvas.configure(bg=self.COLORS['bg_light'])

        except Exception:
            pass

    def _on_drag_enter(self, event):
        """Visual feedback when dragging over."""
        self.clips_canvas.configure(bg=self.COLORS['bg_card'])

    def _on_drag_leave(self, event):
        """Reset visual feedback."""
        self.clips_canvas.configure(bg=self.COLORS['bg_light'])

    def _build_preview_panel(self, parent):
        """Build video preview panel."""
        preview_frame = ttk.Frame(parent)
        preview_frame.pack(fill=tk.X, pady=(0, 10))

        # Header
        header = ttk.Frame(preview_frame)
        header.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(header, text="Preview", font=('Segoe UI', 12, 'bold')).pack(side=tk.LEFT)

        self.preview_clip_label = ttk.Label(header, text="No clip selected", style='SubHeader.TLabel')
        self.preview_clip_label.pack(side=tk.RIGHT)

        # Preview container
        preview_container = tk.Frame(preview_frame, bg=self.COLORS['timeline_bg'])
        preview_container.pack(fill=tk.X)

        # Video display frame (container for VLC or canvas)
        self.preview_video_frame = tk.Frame(preview_container, bg='black',
                                            width=480, height=270)
        self.preview_video_frame.pack(side=tk.LEFT, padx=5, pady=5)
        self.preview_video_frame.pack_propagate(False)  # Maintain fixed size

        # VLC frame (for hardware-accelerated playback)
        self.vlc_frame = tk.Frame(self.preview_video_frame, bg='black',
                                  width=480, height=270)

        # Video display canvas (fallback for thumbnails/audio-only)
        self.preview_canvas = tk.Canvas(self.preview_video_frame, bg=self.COLORS['timeline_bg'],
                                         width=480, height=270, highlightthickness=0)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)

        # Draw placeholder
        self.preview_canvas.create_text(240, 135, text="Click a clip to preview",
                                        fill=self.COLORS['text_dim'], font=('Segoe UI', 11))

        # Initialize VLC if available
        if VLC_AVAILABLE:
            self._init_vlc_player()

        # Controls panel (right side of preview)
        controls = tk.Frame(preview_container, bg=self.COLORS['bg_light'], padx=10, pady=10)
        controls.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Timecode display
        tc_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        tc_frame.pack(fill=tk.X, pady=5)

        ttk.Label(tc_frame, text="Current TC:", background=self.COLORS['bg_light']).pack(side=tk.LEFT)
        self.preview_tc_label = tk.Label(tc_frame, text="--:--:--:--", font=('Consolas', 16, 'bold'),
                                         bg=self.COLORS['bg_light'], fg=self.COLORS['success'])
        self.preview_tc_label.pack(side=tk.LEFT, padx=10)

        # FPS display
        fps_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        fps_frame.pack(fill=tk.X, pady=2)
        ttk.Label(fps_frame, text="Frame Rate:", background=self.COLORS['bg_light']).pack(side=tk.LEFT)
        self.preview_fps_label = tk.Label(fps_frame, text="--", font=('Segoe UI', 11),
                                          bg=self.COLORS['bg_light'], fg=self.COLORS['warning'])
        self.preview_fps_label.pack(side=tk.LEFT, padx=10)

        # Camera ID display
        cam_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        cam_frame.pack(fill=tk.X, pady=2)
        ttk.Label(cam_frame, text="Camera:", background=self.COLORS['bg_light']).pack(side=tk.LEFT)
        self.preview_cam_label = tk.Label(cam_frame, text="--", font=('Segoe UI', 11),
                                          bg=self.COLORS['bg_light'], fg=self.COLORS['accent'])
        self.preview_cam_label.pack(side=tk.LEFT, padx=10)

        # Playback buttons
        btn_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        btn_frame.pack(fill=tk.X, pady=10)

        self.play_btn = self._create_button(btn_frame, "▶ Play", self._toggle_playback, 'accent')
        self.play_btn.pack(side=tk.LEFT, padx=2)

        self._create_button(btn_frame, "⏮ Start", self._seek_start, 'normal').pack(side=tk.LEFT, padx=2)
        self._create_button(btn_frame, "⏭ End", self._seek_end, 'normal').pack(side=tk.LEFT, padx=2)

        # Position slider
        slider_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        slider_frame.pack(fill=tk.X, pady=5)

        self.position_var = tk.DoubleVar(value=0)
        self.position_slider = ttk.Scale(slider_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                                          variable=self.position_var, command=self._on_slider_change)
        self.position_slider.pack(fill=tk.X)

        # Position label
        self.position_label = tk.Label(slider_frame, text="0:00 / 0:00", font=('Segoe UI', 9),
                                        bg=self.COLORS['bg_light'], fg=self.COLORS['text_dim'])
        self.position_label.pack()

        # Overlay options
        overlay_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
        overlay_frame.pack(fill=tk.X, pady=5)

        self.show_tc_overlay = tk.BooleanVar(value=True)
        ttk.Checkbutton(overlay_frame, text="Show TC Overlay",
                        variable=self.show_tc_overlay,
                        command=self._on_overlay_toggle).pack(side=tk.LEFT, padx=5)

        # Audio output device selection
        if SOUNDDEVICE_AVAILABLE:
            audio_frame = tk.Frame(controls, bg=self.COLORS['bg_light'])
            audio_frame.pack(fill=tk.X, pady=5)

            ttk.Label(audio_frame, text="Audio Output:",
                     background=self.COLORS['bg_light']).pack(side=tk.LEFT, padx=(0, 5))

            self.audio_device_var = tk.StringVar()
            self.audio_device_combo = ttk.Combobox(audio_frame, textvariable=self.audio_device_var,
                                                    state='readonly', width=25)
            self.audio_device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.audio_device_combo.bind('<<ComboboxSelected>>', self._on_audio_device_change)

            # Populate audio devices
            self._refresh_audio_devices()

    def _refresh_audio_devices(self):
        """Refresh the list of available audio output devices."""
        if not SOUNDDEVICE_AVAILABLE:
            return

        try:
            devices = sd.query_devices()
            output_devices = []
            self.audio_device_ids = {}  # Map display name to device index

            for i, dev in enumerate(devices):
                if dev['max_output_channels'] > 0:
                    # Clean up device name
                    name = dev['name']
                    if len(name) > 35:
                        name = name[:32] + "..."
                    output_devices.append(name)
                    self.audio_device_ids[name] = i

            if output_devices:
                self.audio_device_combo['values'] = output_devices
                # Select default device
                default_idx = sd.default.device[1]  # Output device
                if default_idx is not None and default_idx < len(devices):
                    default_name = devices[default_idx]['name']
                    if len(default_name) > 35:
                        default_name = default_name[:32] + "..."
                    if default_name in output_devices:
                        self.audio_device_var.set(default_name)
                    else:
                        self.audio_device_var.set(output_devices[0])
                else:
                    self.audio_device_var.set(output_devices[0])
        except Exception:
            pass

    def _on_audio_device_change(self, event=None):
        """Handle audio device selection change."""
        device_name = self.audio_device_var.get()
        if device_name in self.audio_device_ids:
            device_id = self.audio_device_ids[device_name]
            try:
                sd.default.device = (sd.default.device[0], device_id)
                self.status_label.configure(text=f"Audio output: {device_name}")
            except Exception:
                pass

    def _on_overlay_toggle(self):
        """Handle overlay toggle - redraw the preview frame."""
        if hasattr(self, 'preview_canvas') and hasattr(self.preview_canvas, 'image'):
            # Redraw with or without overlay
            if self.preview_canvas.image:
                self._display_frame(self.preview_canvas.image)
            elif self.selected_clip:
                self._update_preview_frame()

    def _build_timeline_panel(self, parent):
        """Build professional NLE-style timeline with track headers and toggles."""
        # Timeline panel frame
        timeline_panel = ttk.Frame(parent)
        timeline_panel.pack(fill=tk.BOTH, expand=True)

        # === TOP HEADER BAR ===
        header = tk.Frame(timeline_panel, bg=self.COLORS['bg_header'], height=40)
        header.pack(fill=tk.X, pady=(0, 2))
        header.pack_propagate(False)

        # Timeline label
        tk.Label(header, text="TIMELINE", font=('Segoe UI', 10, 'bold'),
                 bg=self.COLORS['bg_header'], fg=self.COLORS['text']).pack(side=tk.LEFT, padx=10, pady=8)

        # Video/Audio track toggles
        toggle_frame = tk.Frame(header, bg=self.COLORS['bg_header'])
        toggle_frame.pack(side=tk.LEFT, padx=20)

        # Video tracks toggle
        self.video_toggle_var = tk.BooleanVar(value=True)
        self.video_toggle = tk.Checkbutton(toggle_frame, text="V", font=('Segoe UI', 9, 'bold'),
                                           variable=self.video_toggle_var, command=self._toggle_video_tracks,
                                           bg=self.COLORS['bg_header'], fg=self.COLORS['text'],
                                           selectcolor=self.COLORS['bg_card'], activebackground=self.COLORS['bg_header'],
                                           indicatoron=False, width=3, height=1, relief=tk.FLAT,
                                           bd=2, padx=8, pady=4)
        self.video_toggle.pack(side=tk.LEFT, padx=2)
        self._style_track_toggle(self.video_toggle, True, 'video')

        # Audio tracks toggle
        self.audio_toggle_var = tk.BooleanVar(value=True)
        self.audio_toggle = tk.Checkbutton(toggle_frame, text="A", font=('Segoe UI', 9, 'bold'),
                                           variable=self.audio_toggle_var, command=self._toggle_audio_tracks,
                                           bg=self.COLORS['bg_header'], fg=self.COLORS['text'],
                                           selectcolor=self.COLORS['bg_card'], activebackground=self.COLORS['bg_header'],
                                           indicatoron=False, width=3, height=1, relief=tk.FLAT,
                                           bd=2, padx=8, pady=4)
        self.audio_toggle.pack(side=tk.LEFT, padx=2)
        self._style_track_toggle(self.audio_toggle, True, 'audio')

        # Timeline playback controls
        play_frame = tk.Frame(header, bg=self.COLORS['bg_header'])
        play_frame.pack(side=tk.LEFT, padx=10)

        # Stop button
        self.tl_stop_btn = tk.Button(play_frame, text="⏹", font=('Segoe UI', 11),
                                     command=self._stop_timeline,
                                     bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                                     activebackground=self.COLORS['bg_light'], activeforeground=self.COLORS['text'],
                                     relief=tk.FLAT, bd=0, width=3, cursor='hand2',
                                     takefocus=False)
        self.tl_stop_btn.pack(side=tk.LEFT, padx=1)

        # Play/Pause button
        self.tl_play_btn = tk.Button(play_frame, text="▶", font=('Segoe UI', 12),
                                     command=self._toggle_timeline_playback,
                                     bg=self.COLORS['accent'], fg='white',
                                     activebackground=self.COLORS['accent_hover'], activeforeground='white',
                                     relief=tk.FLAT, bd=0, width=3, cursor='hand2',
                                     takefocus=False)
        self.tl_play_btn.pack(side=tk.LEFT, padx=1)

        # Playhead timecode display
        tc_frame = tk.Frame(header, bg=self.COLORS['bg_card'], padx=10, pady=4)
        tc_frame.pack(side=tk.LEFT, padx=10)
        self.playhead_tc_label = tk.Label(tc_frame, text="00:00:00:00", font=('Consolas', 12, 'bold'),
                                          bg=self.COLORS['bg_card'], fg=self.COLORS['success'])
        self.playhead_tc_label.pack()

        # Zoom controls
        zoom_frame = tk.Frame(header, bg=self.COLORS['bg_header'])
        zoom_frame.pack(side=tk.RIGHT, padx=10)

        self.zoom_label = tk.Label(zoom_frame, text="100%", font=('Segoe UI', 9),
                                   bg=self.COLORS['bg_header'], fg=self.COLORS['text_dim'], width=5)
        self.zoom_label.pack(side=tk.LEFT, padx=5)

        for text, factor in [("-", 0.8), ("+", 1.25)]:
            btn = tk.Button(zoom_frame, text=text, font=('Segoe UI', 10, 'bold'),
                           command=lambda f=factor: self._zoom_timeline(f),
                           bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                           activebackground=self.COLORS['bg_light'], activeforeground=self.COLORS['text'],
                           relief=tk.FLAT, bd=0, width=3, cursor='hand2',
                           takefocus=False)
            btn.pack(side=tk.LEFT, padx=1)

        fit_btn = tk.Button(zoom_frame, text="FIT", font=('Segoe UI', 8),
                           command=self._fit_timeline,
                           bg=self.COLORS['bg_card'], fg=self.COLORS['text'],
                           activebackground=self.COLORS['bg_light'], activeforeground=self.COLORS['text'],
                           relief=tk.FLAT, bd=0, padx=8, cursor='hand2',
                           takefocus=False)
        fit_btn.pack(side=tk.LEFT, padx=5)

        # Camera color legend frame
        self.camera_legend_frame = tk.Frame(timeline_panel, bg=self.COLORS['bg'])
        self.camera_legend_frame.pack(fill=tk.X, pady=(0, 2))

        # === MAIN TIMELINE AREA ===
        main_timeline = tk.Frame(timeline_panel, bg=self.COLORS['timeline_bg'])
        main_timeline.pack(fill=tk.BOTH, expand=True)

        # Vertical scrollbar
        v_scroll = ttk.Scrollbar(main_timeline, orient=tk.VERTICAL)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Horizontal scrollbar
        h_scroll = ttk.Scrollbar(main_timeline, orient=tk.HORIZONTAL)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        # Track headers panel (left side)
        self.track_headers_frame = tk.Frame(main_timeline, bg=self.COLORS['track_header'],
                                            width=self.track_header_width)
        self.track_headers_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.track_headers_frame.pack_propagate(False)

        # Track headers canvas (scrollable)
        self.track_headers_canvas = tk.Canvas(self.track_headers_frame, bg=self.COLORS['track_header'],
                                              width=self.track_header_width, highlightthickness=0)
        self.track_headers_canvas.pack(fill=tk.BOTH, expand=True)

        # Timeline canvas (right side - clips area)
        # Note: We don't use xscrollcommand because we manage horizontal scroll
        # manually via timeline_offset and _sync_scrollbar_to_offset()
        self.timeline_canvas = tk.Canvas(main_timeline, bg=self.COLORS['timeline_bg'],
                                          highlightthickness=0,
                                          yscrollcommand=self._sync_scroll_y)
        self.timeline_canvas.pack(fill=tk.BOTH, expand=True)

        # Configure scrollbars
        v_scroll.config(command=self._scroll_timeline_y)
        h_scroll.config(command=self._on_h_scroll)
        self.timeline_h_scroll = h_scroll  # Save reference for manual sync

        # Playhead position (in seconds from timeline start)
        self.playhead_position = 0.0

        # Bind events
        self.timeline_canvas.bind('<Configure>', self._draw_timeline)
        self.timeline_canvas.bind('<MouseWheel>', self._on_timeline_scroll)
        self.timeline_canvas.bind('<Button-1>', self._on_timeline_click)
        self.timeline_canvas.bind('<B1-Motion>', self._on_timeline_drag)
        self.timeline_canvas.bind('<ButtonRelease-1>', self._on_timeline_release)
        self.timeline_canvas.bind('<Motion>', self._on_timeline_motion)
        self.timeline_canvas.bind('<Shift-MouseWheel>', self._on_timeline_h_scroll)
        self.track_headers_canvas.bind('<MouseWheel>', self._on_timeline_scroll)
        self.track_headers_canvas.bind('<Button-1>', self._on_track_header_click)

    def _on_track_header_click(self, event):
        """Handle clicks on track header canvas (hide/mute/solo buttons)."""
        # Convert to canvas coordinates (important for scrolled canvas)
        canvas_x = self.track_headers_canvas.canvasx(event.x)
        canvas_y = self.track_headers_canvas.canvasy(event.y)

        # Find item at click position
        items = self.track_headers_canvas.find_overlapping(canvas_x - 2, canvas_y - 2,
                                                            canvas_x + 2, canvas_y + 2)
        for item in items:
            tags = self.track_headers_canvas.gettags(item)
            for tag in tags:
                if tag.startswith('hide_'):
                    track_id = tag[5:]  # Remove 'hide_' prefix
                    self._toggle_video_hide(track_id)
                    return
                elif tag.startswith('video_solo_'):
                    track_id = tag[11:]  # Remove 'video_solo_' prefix
                    self._toggle_video_solo(track_id)
                    return
                elif tag.startswith('mute_'):
                    track_id = tag[5:]  # Remove 'mute_' prefix
                    self._toggle_track_mute(track_id)
                    return
                elif tag.startswith('solo_'):
                    track_id = tag[5:]  # Remove 'solo_' prefix
                    self._toggle_track_solo(track_id)
                    return

    def _style_track_toggle(self, widget, enabled, track_type):
        """Style a track toggle button based on its state."""
        if enabled:
            if track_type == 'video':
                widget.configure(bg='#5b8def', fg='white')
            else:
                widget.configure(bg='#3a6bbf', fg='white')
        else:
            widget.configure(bg=self.COLORS['track_disabled'], fg=self.COLORS['text_dim'])

    def _toggle_video_tracks(self):
        """Toggle all video tracks on/off."""
        self.video_tracks_enabled = self.video_toggle_var.get()
        self._style_track_toggle(self.video_toggle, self.video_tracks_enabled, 'video')
        self._draw_timeline()

    def _toggle_audio_tracks(self):
        """Toggle all audio tracks on/off."""
        self.audio_tracks_enabled = self.audio_toggle_var.get()
        self._style_track_toggle(self.audio_toggle, self.audio_tracks_enabled, 'audio')
        self._draw_timeline()

    def _scroll_timeline_y(self, *args):
        """Sync vertical scroll between track headers and timeline."""
        self.timeline_canvas.yview(*args)
        self.track_headers_canvas.yview(*args)

    def _sync_scroll_y(self, *args):
        """Sync scrollbar position from timeline canvas."""
        self.track_headers_canvas.yview_moveto(args[0])
        return True

    def _build_status_bar(self, parent):
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill=tk.X, pady=(10, 0))

        self.status_label = ttk.Label(status_frame, text="Ready - Add files to begin",
                                       style='SubHeader.TLabel')
        self.status_label.pack(side=tk.LEFT)

        # Shortcuts hint
        shortcuts_hint = tk.Label(status_frame,
                                  text="Shortcuts: Ctrl+O=Add | Ctrl+A=Analyze | Space=Play | +/-=Zoom | Del=Remove",
                                  font=('Segoe UI', 8), bg=self.COLORS['bg'], fg=self.COLORS['text_dim'])
        shortcuts_hint.pack(side=tk.LEFT, padx=30)

        # Total duration display
        self.total_duration_label = tk.Label(status_frame, text="Total: 0:00",
                                             font=('Segoe UI', 9, 'bold'),
                                             bg=self.COLORS['bg'], fg=self.COLORS['success'])
        self.total_duration_label.pack(side=tk.RIGHT, padx=(0, 15))

        self.progress = ttk.Progressbar(status_frame, length=200, mode='determinate')
        self.progress.pack(side=tk.RIGHT)

    def _update_total_duration(self):
        """Update the total duration display in the status bar."""
        total_secs = sum(c.duration for c in self.clips if c.duration > 0)
        total_mins = int(total_secs // 60)
        secs = int(total_secs % 60)

        if total_mins >= 60:
            hours = total_mins // 60
            mins = total_mins % 60
            duration_text = f"Total: {hours}:{mins:02d}:{secs:02d}"
        else:
            duration_text = f"Total: {total_mins}:{secs:02d}"

        self.total_duration_label.configure(text=duration_text)

        # Also update window title
        self._update_window_title()

    def _update_camera_legend(self):
        """Update the camera color legend in the timeline header."""
        # Clear existing legend items
        for widget in self.camera_legend_frame.winfo_children():
            widget.destroy()

        if not self.camera_color_map:
            return

        # Add "Cameras:" label
        label = tk.Label(self.camera_legend_frame, text="Cameras:", font=('Segoe UI', 9),
                        bg=self.COLORS['bg'], fg=self.COLORS['text_dim'])
        label.pack(side=tk.LEFT, padx=(5, 10))

        # Add each camera with its color
        for cam_id, color in sorted(self.camera_color_map.items()):
            # Color square
            color_box = tk.Frame(self.camera_legend_frame, bg=color, width=12, height=12)
            color_box.pack(side=tk.LEFT, padx=(0, 3))
            color_box.pack_propagate(False)

            # Camera name
            name_label = tk.Label(self.camera_legend_frame, text=cam_id, font=('Segoe UI', 9),
                                 bg=self.COLORS['bg'], fg=self.COLORS['text'])
            name_label.pack(side=tk.LEFT, padx=(0, 15))

    def _update_window_title(self):
        """Update the window title to reflect current project state."""
        base_title = self.APP_NAME

        if not self.clips:
            self.root.title(base_title)
            return

        clip_count = len(self.clips)
        tc_count = sum(1 for c in self.clips if c.start_tc)

        if self.synced:
            state = "Synced"
        elif tc_count > 0:
            state = f"{tc_count} TC detected"
        else:
            state = "Not synced"

        self.root.title(f"{base_title} - {clip_count} clips ({state})")

    # =========================================================================
    # File Operations
    # =========================================================================

    def _add_files(self):
        self._finalize_scrub_before_modal()
        try:
            # Force window to front and update
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
            self.root.update()

            self.status_label.configure(text="Opening file dialog...")
            self.root.update()

            files = filedialog.askopenfilenames(
                parent=self.root,
                title="Select Media Files",
                initialdir=self.last_directory,
                filetypes=[
                    ("All Media", "*.mp4;*.mov;*.mxf;*.avi;*.mkv;*.wav;*.mp3;*.aac;*.m4a"),
                    ("Video files", "*.mp4;*.mov;*.mxf;*.avi;*.mkv;*.m4v"),
                    ("Audio files", "*.wav;*.mp3;*.aac;*.m4a;*.flac"),
                    ("All files", "*.*")
                ]
            )

            if files:
                # Update last directory
                self.last_directory = os.path.dirname(files[0])

                # Get existing paths for fast duplicate check
                existing_paths = self._get_existing_paths()

                count = 0
                skipped = 0
                for f in files:
                    if self.analyzer.is_supported(f):
                        # Check for duplicate
                        normalized = os.path.normpath(os.path.abspath(f)).lower()
                        if normalized in existing_paths:
                            skipped += 1
                            continue

                        clip = MediaClip(path=f, filename=os.path.basename(f))
                        clip.is_audio_only = self.analyzer.is_audio_file(f)
                        clip.color = self.COLORS['clip_colors'][len(self.clips) % len(self.COLORS['clip_colors'])]
                        self.clips.append(clip)
                        existing_paths.add(normalized)  # Track newly added
                        self._add_to_recent_files(f)  # Add to recent files
                        count += 1

                self._refresh_clips_list()
                self.synced = False
                if skipped > 0:
                    self.status_label.configure(text=f"Added {count} file(s), skipped {skipped} duplicate(s)")
                else:
                    self.status_label.configure(text=f"Added {count} file(s)")
            else:
                self.status_label.configure(text="No files selected")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to add files:\n{e}")

    def _add_folder(self):
        try:
            # Force window to front and update
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
            self.root.update()

            self.status_label.configure(text="Opening folder dialog...")
            self.root.update()

            folder = filedialog.askdirectory(
                parent=self.root,
                title="Select Folder",
                initialdir=self.last_directory
            )

            if folder:
                # Update last directory
                self.last_directory = folder

                # Get existing paths for fast duplicate check
                existing_paths = self._get_existing_paths()

                count = 0
                skipped = 0
                added_files = []
                for root_dir, _, files in os.walk(folder):
                    for f in files:
                        path = os.path.join(root_dir, f)
                        if self.analyzer.is_supported(path):
                            # Check for duplicate
                            normalized = os.path.normpath(os.path.abspath(path)).lower()
                            if normalized in existing_paths:
                                skipped += 1
                                continue

                            clip = MediaClip(path=path, filename=f)
                            clip.is_audio_only = self.analyzer.is_audio_file(path)
                            clip.color = self.COLORS['clip_colors'][len(self.clips) % len(self.COLORS['clip_colors'])]
                            self.clips.append(clip)
                            existing_paths.add(normalized)  # Track newly added
                            added_files.append(path)
                            count += 1

                # Add first few files to recent (not entire folder)
                for path in added_files[:3]:
                    self._add_to_recent_files(path)
                self._refresh_clips_list()
                self.synced = False
                if skipped > 0:
                    self.status_label.configure(text=f"Added {count} file(s) from folder, skipped {skipped} duplicate(s)")
                else:
                    self.status_label.configure(text=f"Added {count} file(s) from folder")
            else:
                self.status_label.configure(text="No folder selected")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to add folder:\n{e}")

    def _is_duplicate_file(self, file_path: str) -> bool:
        """Check if a file is already in the clips list (by normalized path)."""
        normalized_path = os.path.normpath(os.path.abspath(file_path)).lower()
        for clip in self.clips:
            existing_path = os.path.normpath(os.path.abspath(clip.path)).lower()
            if normalized_path == existing_path:
                return True
        return False

    def _get_existing_paths(self) -> set:
        """Get set of normalized paths for all existing clips (for fast lookup)."""
        return {os.path.normpath(os.path.abspath(clip.path)).lower() for clip in self.clips}

    def _schedule_clips_list_refresh(self, delay_ms=1000):
        """Coalesce full clip-list rebuilds to at most ~1/s.

        _refresh_clips_list destroys and recreates every card widget; firing
        it once per analyzed clip made batch analysis O(n^2) in widget churn
        (200 clips = up to minutes of cumulative main-thread stalls)."""
        if getattr(self, '_clips_refresh_pending', False):
            return
        self._clips_refresh_pending = True

        def _do():
            self._clips_refresh_pending = False
            try:
                self._refresh_clips_list()
            except Exception:
                pass
        try:
            self.root.after(delay_ms, _do)
        except Exception:
            self._clips_refresh_pending = False

    def _update_clip_selection_display(self):
        """Show a SELECTION change in the clip list without rebuilding it.

        _refresh_clips_list destroys and recreates every card: on a 74-clip
        project that is ~1,300 Windows child windows and froze the app ~1.9s
        (0.7s Python + 1.2s Tk create/layout/paint) - and it ran on every
        selection change, e.g. each time a released playhead landed on
        another clip. Here only the cards whose state changed are recoloured
        (same rule as at creation: card + its own-coloured child frames).
        Falls back to a full rebuild if a clip has no live card."""
        selected = set(id(c) for c in self.selected_clips)
        visible = self._get_filtered_clips()
        for clip in visible:
            card = getattr(clip, '_card_widget', None)
            try:
                alive = card is not None and card.winfo_exists()
            except Exception:
                alive = False
            if not alive:
                self._refresh_clips_list()
                return
        for clip in visible:
            want = self.COLORS['bg_light'] if id(clip) in selected else self.COLORS['bg_card']
            old = getattr(clip, '_card_bg', None)
            if old == want:
                continue
            card = clip._card_widget
            try:
                card.configure(bg=want)
                for child in card.winfo_children():
                    if isinstance(child, tk.Frame) and child.cget('bg') == old:
                        child.configure(bg=want)
            except tk.TclError:
                continue
            clip._card_bg = want
        self._update_clip_count_label(len(visible))

    def _update_clip_count_label(self, filtered: int = None):
        total = len(self.clips)
        if filtered is None:
            filtered = len(self._get_filtered_clips())
        selected = len(self.selected_clips)
        if filtered < total:
            if selected > 0:
                self.clip_count_label.configure(text=f"{selected} sel / {filtered} shown / {total} total")
            else:
                self.clip_count_label.configure(text=f"{filtered}/{total} shown")
        elif selected > 0 and selected < total:
            self.clip_count_label.configure(text=f"{selected}/{total} selected")
        elif selected == total and total > 0:
            self.clip_count_label.configure(text=f"All {total} selected")
        else:
            self.clip_count_label.configure(text=f"{total} clips")

    def _refresh_clips_list(self):
        """Refresh the clips list display (full rebuild - use
        _update_clip_selection_display when only the selection changed)."""
        for widget in self.clips_frame.winfo_children():
            widget.destroy()

        # Get filtered clips for display
        filtered_clips = self._get_filtered_clips()

        if not self.clips:
            self._create_drop_zone()
        elif not filtered_clips:
            # Show "no matches" message when filter has no results
            no_match = ttk.Label(self.clips_frame,
                                 text="\n\nNo clips match the filter\n\n",
                                 font=('Segoe UI', 11), foreground=self.COLORS['text_dim'],
                                 anchor='center', justify='center')
            no_match.pack(pady=50, padx=20)
        else:
            for i, clip in enumerate(filtered_clips):
                # Use original index for proper selection handling
                original_index = self.clips.index(clip)
                self._create_clip_card(clip, original_index)

        # Update clip count label with filter and selection info
        total = len(self.clips)
        filtered = len(filtered_clips)
        selected = len(self.selected_clips)

        if filtered < total:
            # Filtering is active
            if selected > 0:
                self.clip_count_label.configure(text=f"{selected} sel / {filtered} shown / {total} total")
            else:
                self.clip_count_label.configure(text=f"{filtered}/{total} shown")
        elif selected > 0 and selected < total:
            self.clip_count_label.configure(text=f"{selected}/{total} selected")
        elif selected == total and total > 0:
            self.clip_count_label.configure(text=f"All {total} selected")
        else:
            self.clip_count_label.configure(text=f"{total} clips")

        self.clips_canvas.configure(scrollregion=self.clips_canvas.bbox('all'))

        # Update total duration display
        self._update_total_duration()

    def _on_card_hover_enter(self, card, clip, hover_color):
        """Handle mouse entering a clip card."""
        # Only change color if clip is not already selected
        if clip not in self.selected_clips:
            card.configure(bg=hover_color)
            # Also update child frames
            for child in card.winfo_children():
                if isinstance(child, tk.Frame) and child.cget('bg') == self.COLORS['bg_card']:
                    child.configure(bg=hover_color)

    def _on_card_hover_leave(self, card, clip, original_bg):
        """Handle mouse leaving a clip card."""
        # the card's colour can change after creation (selection updates)
        original_bg = getattr(clip, '_card_bg', original_bg)
        # Restore original color if clip is not selected
        if clip not in self.selected_clips:
            card.configure(bg=original_bg)
            # Also update child frames
            for child in card.winfo_children():
                if isinstance(child, tk.Frame) and child.cget('bg') == self.COLORS['bg_light']:
                    child.configure(bg=original_bg)

    def _clip_card_visible(self, clip) -> bool:
        """Is the clip's card fully inside the visible part of the list?"""
        card = getattr(clip, '_card_widget', None)
        try:
            if card is None or not card.winfo_exists():
                return False
            top = self.clips_canvas.canvasy(0)
            bottom = top + self.clips_canvas.winfo_height()
            y = card.winfo_y()
            return y >= top and y + card.winfo_height() <= bottom
        except Exception:
            return False

    def _scroll_to_clip_soon(self, clip, delay_ms: int = 400):
        """Scroll the list to a clip only if it is out of view, a moment
        later and never while playing/scrubbing: scrolling repaints the
        list's hundreds of child windows (~0.4s frozen), and on every
        playhead release it scrolled even when the card was already visible."""
        if self._clip_card_visible(clip):
            return
        if getattr(self, '_list_scroll_id', None):
            try:
                self.root.after_cancel(self._list_scroll_id)
            except Exception:
                pass

        def _do():
            self._list_scroll_id = None
            if not self.timeline_playing and not self._is_scrubbing and self.selected_clip is clip:
                self._scroll_to_clip(clip)
        self._list_scroll_id = self.root.after(delay_ms, _do)

    def _scroll_to_clip(self, clip: MediaClip):
        """Scroll the clips list to show the specified clip."""
        if not hasattr(clip, '_card_widget') or clip._card_widget is None:
            return

        try:
            # Get the clip card's position
            card = clip._card_widget
            card.update_idletasks()

            # Get card position relative to the canvas
            clip_index = self.clips.index(clip)
            card_height = 60  # Approximate height of each card
            card_y = clip_index * card_height

            # Get visible area of the canvas
            canvas_height = self.clips_canvas.winfo_height()
            scroll_region = self.clips_canvas.cget('scrollregion')

            if scroll_region:
                # Parse scroll region - it's a string like "0 0 width height"
                parts = scroll_region.split()
                if len(parts) == 4:
                    total_height = float(parts[3])
                    if total_height > canvas_height:
                        # Calculate scroll position to center the clip
                        scroll_pos = max(0, min(1.0, card_y / total_height))
                        self.clips_canvas.yview_moveto(scroll_pos)
        except Exception:
            pass  # Silently ignore scroll errors

    def _create_clip_card(self, clip: MediaClip, index: int):
        """Create a card widget for a clip."""
        # Check if clip is selected
        is_selected = clip in self.selected_clips
        bg_color = self.COLORS['bg_light'] if is_selected else self.COLORS['bg_card']

        card = tk.Frame(self.clips_frame, bg=bg_color, padx=10, pady=8)
        card.pack(fill=tk.X, pady=2, padx=5)

        # Store reference for highlighting
        clip._card_widget = card
        clip._card_bg = bg_color

        # Make card clickable with modifier support
        card.bind('<Button-1>', lambda e, c=clip, i=index: self._on_clip_click(e, c, i))
        # Double-click to copy timecode
        card.bind('<Double-1>', lambda e, c=clip: self._on_clip_double_click(c))
        # Right-click context menu
        card.bind('<Button-3>', lambda e, c=clip, i=index: self._show_clip_context_menu(e, c, i))

        # Hover effect (visual feedback)
        hover_color = self.COLORS['bg_light']
        card.bind('<Enter>', lambda e, w=card, c=clip, hc=hover_color: self._on_card_hover_enter(w, c, hc))
        card.bind('<Leave>', lambda e, w=card, c=clip, bg=bg_color: self._on_card_hover_leave(w, c, bg))

        # Tooltip on hover
        ToolTip(card, lambda c=clip: self._get_clip_tooltip(c))

        # Color indicator
        color_bar = tk.Frame(card, bg=clip.color, width=4)
        color_bar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        color_bar.bind('<Button-1>', lambda e, c=clip, i=index: self._on_clip_click(e, c, i))
        color_bar.bind('<Double-1>', lambda e, c=clip: self._on_clip_double_click(c))
        color_bar.bind('<Button-3>', lambda e, c=clip, i=index: self._show_clip_context_menu(e, c, i))

        # Helper function to bind click events to all child widgets
        def bind_card_events(widget):
            widget.bind('<Button-1>', lambda e, c=clip, i=index: self._on_clip_click(e, c, i))
            widget.bind('<Double-1>', lambda e, c=clip: self._on_clip_double_click(c))
            widget.bind('<Button-3>', lambda e, c=clip, i=index: self._show_clip_context_menu(e, c, i))

        # Info section
        info = tk.Frame(card, bg=bg_color)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bind_card_events(info)

        # Row 1: Filename + Camera ID
        row1 = tk.Frame(info, bg=self.COLORS['bg_card'])
        row1.pack(fill=tk.X)
        bind_card_events(row1)

        name_label = tk.Label(row1, text=clip.filename, font=('Segoe UI', 10, 'bold'),
                              bg=self.COLORS['bg_card'], fg=self.COLORS['text'], anchor='w')
        name_label.pack(side=tk.LEFT)
        bind_card_events(name_label)

        # Camera ID / Recording ID badge
        if clip.recording_id and clip.track_number is not None:
            # Multi-track recorder file - show recording ID and track
            track_badge = f" [{clip.recording_id} Tr{clip.track_number}]"
            track_label = tk.Label(row1, text=track_badge, font=('Segoe UI', 9),
                                  bg=self.COLORS['bg_card'], fg='#88aaff')
            track_label.pack(side=tk.LEFT)
            bind_card_events(track_label)
        elif clip.camera_id:
            # Use different color for user bits camera ID (more reliable than filename detection)
            # Green for cam ID from LTC user bits (01-99), blue for filename detection
            cam_color = '#00cc88' if clip.ub_camera_id and clip.ub_camera_id > 0 else self.COLORS['accent']
            cam_label = tk.Label(row1, text=f" [{clip.camera_id}]", font=('Segoe UI', 9),
                                bg=self.COLORS['bg_card'], fg=cam_color)
            cam_label.pack(side=tk.LEFT)
            bind_card_events(cam_label)

        # Row 2: Timecode + FPS
        row2 = tk.Frame(info, bg=self.COLORS['bg_card'])
        row2.pack(fill=tk.X)
        bind_card_events(row2)

        tc_text = clip.start_tc if clip.start_tc else "--:--:--:--"
        tc_color = self.COLORS['success'] if clip.start_tc else self.COLORS['text_dim']
        tc_label = tk.Label(row2, text=f"LTC: {tc_text}", font=('Consolas', 10),
                            bg=self.COLORS['bg_card'], fg=tc_color)
        tc_label.pack(side=tk.LEFT)
        bind_card_events(tc_label)

        # FPS display (only if we have valid timecode)
        if clip.start_tc and clip.fps > 0:
            # Show if FPS was manually overridden
            fps_text = clip.fps_display
            fps_color = self.COLORS['warning']
            if getattr(clip, 'fps_override', False):
                fps_text = f"{fps_text}*"  # Asterisk indicates override
                fps_color = '#ff88ff'  # Pink/magenta for override
            fps_label = tk.Label(row2, text=f"  @ {fps_text}", font=('Segoe UI', 9, 'bold'),
                                bg=self.COLORS['bg_card'], fg=fps_color)
            fps_label.pack(side=tk.LEFT)
            bind_card_events(fps_label)

        # Duration
        if clip.duration > 0:
            dur_str = f"{int(clip.duration // 60)}:{int(clip.duration % 60):02d}"
            dur_label = tk.Label(row2, text=f"  |  {dur_str}", font=('Segoe UI', 9),
                                bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
            dur_label.pack(side=tk.LEFT)
            bind_card_events(dur_label)

        # Row 2b: Embedded camera timecode with file frame rate
        # Show if embedded TC exists (even if same as LTC) OR if we have video fps to display
        # Only show file FPS after analysis is complete (not in pending/analyzing state)
        has_embedded_tc = clip.embedded_tc and clip.embedded_tc != clip.start_tc
        analysis_complete = clip.status not in ('pending', 'analyzing')
        has_video_fps = not clip.is_audio_only and getattr(clip, 'video_fps', None) and analysis_complete

        if has_embedded_tc or has_video_fps:
            row2b = tk.Frame(info, bg=self.COLORS['bg_card'])
            row2b.pack(fill=tk.X)
            bind_card_events(row2b)

            # Show embedded TC if different from LTC
            if has_embedded_tc:
                emb_label = tk.Label(row2b, text=f"CAM: {clip.embedded_tc}", font=('Consolas', 9),
                                    bg=self.COLORS['bg_card'], fg='#88ccff')  # Light blue
                emb_label.pack(side=tk.LEFT)
                bind_card_events(emb_label)

            # Show file frame rate from video metadata
            if has_video_fps:
                try:
                    if '/' in clip.video_fps:
                        num, denom = map(int, clip.video_fps.split('/'))
                        file_fps = num / denom
                        # Format nicely
                        if abs(file_fps - 23.976) < 0.01:
                            file_fps_str = "23.976"
                        elif abs(file_fps - 29.97) < 0.01:
                            file_fps_str = "29.97"
                        elif abs(file_fps - 59.94) < 0.01:
                            file_fps_str = "59.94"
                        elif file_fps == int(file_fps):
                            file_fps_str = str(int(file_fps))
                        else:
                            file_fps_str = f"{file_fps:.2f}"

                        # Highlight if file fps differs from LTC-detected fps
                        fps_mismatch = clip.fps > 0 and abs(file_fps - clip.fps) > 0.5
                        file_fps_color = '#ff6b6b' if fps_mismatch else '#88ccff'  # Red if mismatch

                        prefix = "  @ " if has_embedded_tc else "File: @ "
                        file_fps_label = tk.Label(row2b, text=f"{prefix}{file_fps_str}fps", font=('Segoe UI', 9),
                                                 bg=self.COLORS['bg_card'], fg=file_fps_color)
                        file_fps_label.pack(side=tk.LEFT)
                        bind_card_events(file_fps_label)
                except (ValueError, ZeroDivisionError):
                    pass

        # Row 3: Status indicators
        row3 = tk.Frame(info, bg=self.COLORS['bg_card'])
        row3.pack(fill=tk.X)
        bind_card_events(row3)

        # Status indicator with better messages
        status_text = clip.status.upper()
        status_colors = {
            'pending': self.COLORS['text_dim'],
            'analyzing': self.COLORS['warning'],
            'done': self.COLORS['success'],
            'no_ltc': self.COLORS['accent'],
            'no_audio': self.COLORS['accent'],
            'error': self.COLORS['accent']
        }

        # Show error message for failed clips
        if clip.status in ('no_ltc', 'no_audio', 'error') and clip.error:
            error_label = tk.Label(row3, text=f"⚠ {clip.error}", font=('Segoe UI', 8),
                                  bg=self.COLORS['bg_card'], fg=self.COLORS['accent'])
            error_label.pack(side=tk.LEFT)
            bind_card_events(error_label)

        if clip.status == 'done' and getattr(clip, 'ltc_warning', ''):
            warn_label = tk.Label(row3, text=f"⚠ {clip.ltc_warning}", font=('Segoe UI', 8),
                                  bg=self.COLORS['bg_card'], fg=self.COLORS['warning'])
            warn_label.pack(side=tk.LEFT)
            bind_card_events(warn_label)

        status_label = tk.Label(row3, text=f"[{status_text}]", font=('Segoe UI', 9),
                               bg=self.COLORS['bg_card'], fg=status_colors.get(clip.status, self.COLORS['text_dim']))
        status_label.pack(side=tk.RIGHT)
        bind_card_events(status_label)

        # Linked audio indicator
        if clip.has_linked_audio:
            linked_label = tk.Label(row3, text="[LINKED]  ", font=('Segoe UI', 9),
                                   bg=self.COLORS['bg_card'], fg=self.COLORS['warning'])
            linked_label.pack(side=tk.RIGHT)
            bind_card_events(linked_label)

        # LTC track indicator (for multi-track recorders)
        if clip.is_ltc_track:
            ltc_src_label = tk.Label(row3, text="[LTC SRC]  ", font=('Segoe UI', 9),
                                    bg=self.COLORS['bg_card'], fg='#ffaa00')
            ltc_src_label.pack(side=tk.RIGHT)
            bind_card_events(ltc_src_label)
        elif clip.linked_ltc_path:
            # Show link arrow for tracks following LTC
            link_label = tk.Label(row3, text="[→LTC]  ", font=('Segoe UI', 9),
                                 bg=self.COLORS['bg_card'], fg='#44aa88')
            link_label.pack(side=tk.RIGHT)
            bind_card_events(link_label)

        # Split file indicator
        if clip.split_part and clip.split_part > 1:
            split_label = tk.Label(row3, text=f"[Part {clip.split_part}]  ", font=('Segoe UI', 9),
                                  bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
            split_label.pack(side=tk.RIGHT)
            bind_card_events(split_label)

        # LTC channel
        if clip.ltc_channel >= 0:
            ltc_label = tk.Label(row3, text=f"LTC:Ch{clip.ltc_channel + 1}  ", font=('Segoe UI', 9),
                                bg=self.COLORS['bg_card'], fg=self.COLORS['text_dim'])
            ltc_label.pack(side=tk.RIGHT)
            bind_card_events(ltc_label)

    # =========================================================================
    # Analysis & Sync
    # =========================================================================

    def _get_expected_fps_value(self) -> float:
        """Parse the expected FPS selection from UI."""
        fps_str = self.expected_fps.get()
        if fps_str == "Auto":
            return None
        elif fps_str == "23.976":
            return 23.976
        elif fps_str == "24":
            return 24.0
        elif fps_str == "25":
            return 25.0
        elif fps_str in ("29.97 DF", "29.97 NDF"):
            return 29.97
        elif fps_str == "30":
            return 30.0
        return None

    def _analyze_clips(self):
        if self.analyzing:
            return

        pending = [c for c in self.clips if c.status == 'pending']
        if not pending:
            messagebox.showinfo("Info", "No pending clips to analyze")
            return

        self.analyzing = True
        self.status_label.configure(text="Analyzing clips...")

        # Get expected FPS from UI
        expected_fps = self._get_expected_fps_value()

        threading.Thread(target=self._analyze_thread, args=(pending, expected_fps), daemon=True).start()

    # Every MediaClip field analyze() produces. Re-analysis copies ALL of them
    # back - user_bits / ub_camera_id / ub_date / sample_count used to be left
    # out, so user bits never showed, TC+Date grouping had no date, and the
    # Resolve exporter read a stale sample count after a re-analysis.
    ANALYSIS_FIELDS = (
        'duration', 'original_duration', 'start_tc', 'end_tc', 'start_frames', 'fps', 'drop_frame',
        'ltc_channel', 'has_linked_audio', 'linked_audio_path', 'width', 'height', 'video_fps',
        'audio_channels', 'sample_rate', 'sample_count', 'status', 'error', 'ltc_warning', 'file_date',
        'embedded_tc', 'embedded_tc_frames', 'bwf_time_reference', 'camera_id', 'is_audio_only',
        'user_bits', 'ub_camera_id', 'ub_date',
        'ltc_offset')   # sample position of the first LTC frame: without it the sub-frame start fix (and LTC that begins late) was dropped in the GUI

    def _analysis_worker_count(self, total: int) -> int:
        """Files analysed at once. Auto = all CPU threads, capped at 8 (measured:
        12 threads is no faster than 8 - FFmpeg start-up and disk reads limit
        it beyond that)."""
        setting = str(getattr(self, 'analysis_threads', "Auto"))
        if setting != "Auto":
            try:
                return max(1, min(int(setting), total))
            except ValueError:
                pass
        return max(1, min(8, os.cpu_count() or 2, total))

    def _analyze_thread(self, clips, expected_fps=None):
        """Analyze clips in parallel - several files at once on a thread pool.

        Most of each file's time is FFmpeg/FFprobe subprocesses (no GIL held)
        and numpy; the remaining pure-Python bit decoding is short since the
        single-pass decode. MediaAnalyzer.analyze() keeps no shared state and
        every decode builds its own LTCDecoder, so it is safe to call
        concurrently. Results are copied onto the clip as each file finishes.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(clips)
        camera_id_source = self.camera_id_source
        workers = self._analysis_worker_count(total)
        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ltc-analyze') as pool:
            futures = {pool.submit(self.analyzer.analyze, clip.path, expected_fps, camera_id_source): clip
                       for clip in clips}
            for future in as_completed(futures):
                clip = futures[future]
                done += 1
                try:
                    result = future.result()
                    self.queue.put(('progress', (done, total, f"{clip.filename}  [{workers} at once]")))
                    # The clip may have been removed from the list meanwhile
                    if not any(c is clip for c in self.clips):
                        continue
                    # Copy results (including embedded_tc for FCPXML export, camera_id for track assignment)
                    for attr in self.ANALYSIS_FIELDS:
                        setattr(clip, attr, getattr(result, attr))
                    # analyze() already attempted the embedded-TC probe
                    self._embedded_probe_attempted.add(result.path)
                    self.queue.put(('update', None))
                except Exception as e:
                    self.queue.put(('error', str(e)))
        self.queue.put(('done', None))

    # =========================================================================
    # Audio Track Linking (Multi-track recorder support)
    # =========================================================================

    def _parse_recorder_filename(self, filename: str) -> dict:
        """
        Parse recorder filename patterns to extract recording ID, track number, and split part.

        Supports patterns like:
        - ZOOM0004_Tr3.WAV -> recording_id=ZOOM0004, track=3, split=None
        - ZOOM0004_Tr12.WAV -> recording_id=ZOOM0004, track=12, split=None
        - ZOOM0004_Tr12-0001.WAV -> recording_id=ZOOM0004, track=12, split=1
        - ZOOM0004_Tr12-0002.WAV -> recording_id=ZOOM0004, track=12, split=2
        - SD_A001_T01.WAV (Sound Devices) -> recording_id=SD_A001, track=1, split=None
        """
        import re

        result = {
            'recording_id': None,
            'track_number': None,
            'split_part': None,
            'base_name': None  # For grouping split files
        }

        # Remove extension
        name = os.path.splitext(filename)[0]

        # Pattern 1a: ZOOM format - ZOOM0004_Tr3, ZOOM0004_Tr12-0001, ZOOM0004_Tr2 - Copy
        # Allow optional suffix after the main pattern (e.g., " - Copy", "_backup", etc.)
        zoom_match = re.match(r'^(ZOOM\d+)_Tr(\d+)(?:-(\d+))?(?:\s*[-_].*)?$', name, re.IGNORECASE)
        if zoom_match:
            result['recording_id'] = zoom_match.group(1).upper()
            result['track_number'] = int(zoom_match.group(2))
            if zoom_match.group(3):
                result['split_part'] = int(zoom_match.group(3))
            result['base_name'] = f"{result['recording_id']}_Tr{result['track_number']}"
            return result

        # Pattern 1b: ZOOM stereo mix - ZOOM0004_LR (Left-Right stereo mix)
        # Allow optional suffix after the main pattern
        zoom_lr_match = re.match(r'^(ZOOM\d+)_LR(?:-(\d+))?(?:\s*[-_].*)?$', name, re.IGNORECASE)
        if zoom_lr_match:
            result['recording_id'] = zoom_lr_match.group(1).upper()
            result['track_number'] = 0  # Use 0 for stereo mix
            if zoom_lr_match.group(2):
                result['split_part'] = int(zoom_lr_match.group(2))
            result['base_name'] = f"{result['recording_id']}_LR"
            return result

        # Pattern 2: Sound Devices format - SD_A001_T01
        # Allow optional suffix after the main pattern
        sd_match = re.match(r'^([\w]+_[A-Z]\d+)_T(\d+)(?:-(\d+))?(?:\s*[-_].*)?$', name, re.IGNORECASE)
        if sd_match:
            result['recording_id'] = sd_match.group(1).upper()
            result['track_number'] = int(sd_match.group(2))
            if sd_match.group(3):
                result['split_part'] = int(sd_match.group(3))
            result['base_name'] = f"{result['recording_id']}_T{result['track_number']}"
            return result

        # Pattern 3: Generic format with Track/Ch - Recording_Track01, Recording_Ch1
        # Allow optional suffix after the main pattern
        generic_match = re.match(r'^(.+?)(?:_(?:Track|Tr|Ch|Channel))(\d+)(?:-(\d+))?(?:\s*[-_].*)?$', name, re.IGNORECASE)
        if generic_match:
            result['recording_id'] = generic_match.group(1)
            result['track_number'] = int(generic_match.group(2))
            if generic_match.group(3):
                result['split_part'] = int(generic_match.group(3))
            result['base_name'] = f"{result['recording_id']}_Tr{result['track_number']}"
            return result

        return result

    def _link_audio_tracks(self):
        """
        Link audio tracks from multi-track recorders.
        Finds LTC track and links other tracks from the same recording to follow its timecode.
        Also merges split files (-0001, -0002) into continuous clips.
        """
        # First, parse all filenames and reset/populate recording info
        for clip in self.clips:
            parsed = self._parse_recorder_filename(clip.filename)
            clip.recording_id = parsed['recording_id']
            clip.track_number = parsed['track_number']
            clip.split_part = parsed['split_part']
            # Reset linking flags - will be set correctly below
            clip.is_ltc_track = False
            clip.linked_ltc_path = None
            clip.linked_audio_tracks = None
            # Clear inherited timecodes for clips without detected LTC
            # This ensures they can inherit fresh values during sync
            if clip.ltc_channel < 0 and clip.recording_id:
                clip.start_tc = None
                clip.start_frames = 0

        # Group clips by recording_id
        recordings = {}  # {recording_id: [clips]}
        for clip in self.clips:
            if clip.recording_id:
                if clip.recording_id not in recordings:
                    recordings[clip.recording_id] = []
                recordings[clip.recording_id].append(clip)

        # Process each recording group
        for recording_id, recording_clips in recordings.items():
            # Find the LTC track - prioritize clips where LTC was actually DETECTED
            # (ltc_channel >= 0) over clips that just have start_tc (which could be inherited)
            ltc_clip = None

            # First pass: look for clip with detected LTC (ltc_channel >= 0)
            for clip in recording_clips:
                if clip.is_audio_only and clip.ltc_channel >= 0 and clip.start_tc:
                    ltc_clip = clip
                    break

            # Second pass: fall back to any clip with start_tc (might be inherited)
            if not ltc_clip:
                for clip in recording_clips:
                    if clip.start_tc and clip.is_audio_only:
                        ltc_clip = clip
                        break

            if not ltc_clip:
                continue

            ltc_clip.is_ltc_track = True

            # Group by track number to find split files
            tracks = {}  # {track_number: [clips]}
            for clip in recording_clips:
                if clip.track_number is not None:
                    if clip.track_number not in tracks:
                        tracks[clip.track_number] = []
                    tracks[clip.track_number].append(clip)

            # Sort split files and link to LTC
            for track_num, track_clips in tracks.items():
                # Sort by split part
                track_clips.sort(key=lambda c: c.split_part or 0)

                # Link all non-LTC tracks to the LTC track
                for clip in track_clips:
                    if clip != ltc_clip:
                        clip.linked_ltc_path = ltc_clip.path
                        # Don't override is_ltc_track here - keep the original detection result
                        # The clip's is_ltc_track was already set during analysis
                        clip.is_ltc_track = False

                        # If this clip has no timecode, inherit from LTC
                        if not clip.start_tc and ltc_clip.start_tc:
                            # Calculate offset based on split part
                            if clip.split_part and clip.split_part > 1:
                                # Find previous parts to calculate offset
                                prev_duration = 0.0
                                for prev_clip in track_clips:
                                    if prev_clip.split_part and prev_clip.split_part < clip.split_part:
                                        prev_duration += prev_clip.duration
                                # Offset timecode by previous parts' duration
                                # Use round() not int() to avoid cumulative frame drift
                                clip.start_tc = ltc_clip.start_tc
                                clip.start_frames = ltc_clip.start_frames + round(prev_duration * ltc_clip.fps)
                            else:
                                clip.start_tc = ltc_clip.start_tc
                                clip.start_frames = ltc_clip.start_frames
                                # same recorder take = same first sample: the
                                # LTC track's sub-frame offset applies here too
                                clip.ltc_offset = ltc_clip.ltc_offset
                            clip.fps = ltc_clip.fps
                            clip.drop_frame = ltc_clip.drop_frame
                            # Extract actual BWF embedded TC from the audio file for export
                            # DaVinci Resolve matches files by their actual embedded TC, not LTC
                            if not clip.embedded_tc and clip.path in self._embedded_probe_attempted:
                                # Analysis already probed this file and found
                                # nothing - don't repeat the ffprobe on the
                                # main thread; use the start-of-file fallback.
                                clip.embedded_tc = "00:00:00:00"
                                clip.embedded_tc_frames = 0
                            elif not clip.embedded_tc:
                                try:
                                    actual_tc, actual_frames, bwf_ref = self.analyzer._get_embedded_timecode(
                                        clip.path, clip.fps
                                    )
                                    if actual_tc:
                                        clip.embedded_tc = actual_tc
                                        clip.embedded_tc_frames = actual_frames
                                        clip.bwf_time_reference = bwf_ref
                                    else:
                                        # No BWF TC found - use 0 (start of file)
                                        clip.embedded_tc = "00:00:00:00"
                                        clip.embedded_tc_frames = 0
                                except Exception:
                                    # Fallback to start of file
                                    clip.embedded_tc = "00:00:00:00"
                                    clip.embedded_tc_frames = 0

            # Store linked tracks on the LTC clip
            ltc_clip.linked_audio_tracks = [c.path for c in recording_clips if c != ltc_clip]

        # Log results
        linked_count = sum(1 for c in self.clips if c.linked_ltc_path)
        if linked_count > 0:
            self.status_label.configure(text=f"Linked {linked_count} audio tracks to LTC references")

    def _handle_timecode_wrap(self, tc_clips: list):
        """
        Detect and handle 24-hour timecode wrap for recordings.

        Handles two cases:
        1. Split files (-0001, -0002): TC wraps between split parts
        2. Single long files: A 48h recording followed by a new recording
           where the new clip's TC appears earlier due to wrap

        For 48+ hour recordings, TC wraps at midnight (23:59:59 -> 00:00:00).
        This causes later recordings to have lower TC than earlier ones.

        Returns the number of clips adjusted.
        """
        adjusted_count = 0

        # ===== PART 1: Handle split files =====
        # Group split files by recording_id + track
        split_groups = {}  # {base_key: [clips]}

        for clip in tc_clips:
            if clip.recording_id and clip.track_number is not None and clip.split_part:
                if clip.track_number == 0:
                    base_key = f"{clip.recording_id}_LR"
                else:
                    base_key = f"{clip.recording_id}_Tr{clip.track_number}"
                if base_key not in split_groups:
                    split_groups[base_key] = []
                split_groups[base_key].append(clip)

        for base_key, group_clips in split_groups.items():
            if len(group_clips) <= 1:
                continue

            # Sort by split part
            group_clips.sort(key=lambda c: c.split_part or 0)

            # Track cumulative day wraps
            day_offset = 0

            for i in range(1, len(group_clips)):
                prev_clip = group_clips[i - 1]
                curr_clip = group_clips[i]

                # All comparisons in SECONDS via the effective TC - raw
                # start_frames is 0 for embedded-only clips and frame counts
                # mix bases across different-fps clips
                prev_start_sec = self._effective_tc_seconds(prev_clip)
                curr_start_sec = self._effective_tc_seconds(curr_clip)
                if prev_start_sec is None or curr_start_sec is None:
                    continue
                prev_end_sec = prev_start_sec + prev_clip.duration

                # Split parts of one recording are CONTIGUOUS by definition:
                # if this part's TC starts before the previous part ended, the
                # TC wrapped at 24h. (The old ">22h and <2h" heuristic missed
                # parts that themselves span midnight - e.g. part 1 running
                # 23:30-00:30 has an unwrapped end of 24.5h, so 24.5%24=0.5h
                # never satisfied ">=22".) 1s tolerance absorbs rounding.
                add_days = 0
                while (curr_start_sec + (day_offset + add_days) * 86400.0
                       < prev_end_sec - 1.0) and add_days < 10:
                    add_days += 1
                day_offset += add_days

                # Apply accumulated day offset (shifts BOTH TC representations)
                if day_offset > 0:
                    self._add_tc_day_offset(curr_clip, day_offset)
                    adjusted_count += 1

        # ===== PART 2: Handle single files spanning multiple days =====
        # Sort clips by file_date to get actual recording order
        clips_with_date = [(c, c.file_date) for c in tc_clips if c.file_date]
        if len(clips_with_date) < 2:
            return adjusted_count

        clips_with_date.sort(key=lambda x: x[1])

        # Track cumulative day offset for clips recorded later
        # Key insight: if clip A is 48h long and clip B starts 2h after A's TC,
        # clip B should actually be at A's end (48h) + 2h, not at 2h

        for i in range(1, len(clips_with_date)):
            prev_clip, prev_date = clips_with_date[i - 1]
            curr_clip, curr_date = clips_with_date[i]

            # Skip if same recording session (split files handled in Part 1)
            if prev_clip.recording_id and curr_clip.recording_id:
                if prev_clip.recording_id == curr_clip.recording_id:
                    continue

            # All range math in SECONDS via the effective TC (see Part 1)
            prev_start_sec = self._effective_tc_seconds(prev_clip)
            curr_start_sec = self._effective_tc_seconds(curr_clip)
            if prev_start_sec is None or curr_start_sec is None:
                continue

            prev_duration_hours = prev_clip.duration / 3600
            prev_end_sec = prev_start_sec + prev_clip.duration

            # Only check for wrap if previous clip is long (>20h)
            if prev_duration_hours > 20:
                # Verify current clip was recorded after previous using file dates
                time_diff = (curr_date - prev_date).total_seconds() if curr_date and prev_date else 0

                if time_diff > 0:
                    # Keep adding 24h to current clip until it's placed after previous ends
                    # Safety limit: max 10 days (240h recording)
                    safety_count = 0
                    while curr_start_sec < prev_end_sec and safety_count < 10:
                        self._add_tc_day_offset(curr_clip, 1)
                        curr_start_sec += 24 * 3600
                        adjusted_count += 1
                        safety_count += 1

        return adjusted_count

    def _merge_split_clips(self):
        """
        Merge split files (-0001, -0002) from the same track into virtual continuous clips.
        The first part keeps its timeline position, subsequent parts are positioned after.
        """
        # Group by base name (recording_id + track_number)
        split_groups = {}  # {base_key: [clips]}

        for clip in self.clips:
            if clip.recording_id and clip.track_number is not None:
                if clip.track_number == 0:
                    base_key = f"{clip.recording_id}_LR"
                else:
                    base_key = f"{clip.recording_id}_Tr{clip.track_number}"
                if base_key not in split_groups:
                    split_groups[base_key] = []
                split_groups[base_key].append(clip)

        # Process groups with multiple parts
        for base_key, group_clips in split_groups.items():
            if len(group_clips) <= 1:
                continue

            # Sort by split part (files without split_part go first with value 0)
            group_clips.sort(key=lambda c: c.split_part or 0)

            # Position subsequent parts after the first
            first_clip = group_clips[0]
            if first_clip.timeline_start < 0:
                # No TC yet (the -1 "place at the end" marker): chaining now
                # put parts 2+ at -1 + duration, i.e. near the timeline start.
                # _sync_clips chains them again after placing part 1.
                continue
            current_pos = first_clip.timeline_start + first_clip.duration

            for clip in group_clips[1:]:
                clip.timeline_start = current_pos
                current_pos += clip.duration

        merged_count = sum(1 for g in split_groups.values() if len(g) > 1)
        if merged_count > 0:
            self.status_label.configure(text=f"Merged {merged_count} split recordings")

    def _get_effective_tc(self, clip) -> tuple:
        """Get effective timecode for sync based on global TC source and per-clip override.

        Returns:
            tuple: (tc_string, tc_frames, source_used) where source_used is "ltc" or "embedded"
        """
        # Get global TC source preference
        global_source = getattr(self, 'global_tc_source', None)
        if global_source:
            global_source = global_source.get()
        else:
            global_source = "LTC (Audio)"

        # Per-clip override takes precedence
        clip_source = clip.tc_source if hasattr(clip, 'tc_source') else "auto"

        # Determine effective source
        if clip_source == "ltc":
            # Clip explicitly set to use LTC
            if clip.start_tc:
                return (clip.start_tc, clip.start_frames, "ltc")
            else:
                return (None, 0, None)
        elif clip_source == "embedded":
            # Clip explicitly set to use embedded
            if clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                return (clip.embedded_tc, clip.embedded_tc_frames, "embedded")
            else:
                return (None, 0, None)
        else:
            # Auto mode - use global preference
            if global_source == "LTC (Audio)":
                if clip.start_tc:
                    return (clip.start_tc, clip.start_frames, "ltc")
                else:
                    return (None, 0, None)
            elif global_source == "Embedded (File)":
                if clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                    return (clip.embedded_tc, clip.embedded_tc_frames, "embedded")
                else:
                    return (None, 0, None)
            else:
                # Auto (LTC>Emb) - prefer LTC, fall back to embedded
                if clip.start_tc:
                    return (clip.start_tc, clip.start_frames, "ltc")
                elif clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                    return (clip.embedded_tc, clip.embedded_tc_frames, "embedded")
                else:
                    return (None, 0, None)

    def _get_clip_effective_tc(self, clip):
        """Resolve the clip's effective sync TC under the current TC Source
        setting. Returns (tc_string, tc_frames, source) with source 'ltc',
        'embedded', or None; frames are in the clip's OWN fps base.

        Shared by _sync_clips, the TC+Date/TC+Filename range math and the
        24h-wrap handler so every mode resolves TC identically (embedded-only
        clips have start_frames == 0 - raw start_frames must never be used
        for positioning)."""
        source_setting = "LTC (Audio)"
        if hasattr(self, 'global_tc_source'):
            source_setting = self.global_tc_source.get()
        clip_source = getattr(clip, 'tc_source', 'auto')
        if clip_source == "ltc":
            effective_source = "LTC (Audio)"
        elif clip_source == "embedded":
            effective_source = "Embedded (File)"
        else:  # "auto" - use global setting
            effective_source = source_setting

        if effective_source == "LTC (Audio)":
            if clip.start_tc:
                return (clip.start_tc, clip.start_frames, 'ltc')
            return (None, 0, None)
        elif effective_source == "Embedded (File)":
            if clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                return (clip.embedded_tc, clip.embedded_tc_frames, 'embedded')
            return (None, 0, None)
        else:  # Auto (LTC>Emb)
            if clip.start_tc:
                return (clip.start_tc, clip.start_frames, 'ltc')
            elif clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
                return (clip.embedded_tc, clip.embedded_tc_frames, 'embedded')
            return (None, 0, None)

    def _effective_tc_seconds(self, clip):
        """Effective TC as SECONDS (frames / clip's own fps), or None.

        Seconds are the only base safe to compare across clips - frame counts
        mix bases when LTC clips (<=30fps) meet metadata-fps clips (e.g. 50)."""
        tc, frames, src = self._get_clip_effective_tc(clip)
        if not tc:
            return None
        fps = clip.fps if clip.fps and clip.fps > 0 else 30.0
        seconds = frames / fps
        # Timecode recorders without LTC audio (BWF time_reference): the
        # reference is SAMPLE-exact seconds since midnight - use it rather
        # than the frame-rounded TC, which lost up to half a frame and was
        # 0.1% off whenever clip.fps got corrected afterwards (30 -> 29.97).
        # Whole days added by the 24h-wrap handler are kept.
        if (src == 'embedded' and clip.is_audio_only and clip.bwf_time_reference > 0
                and clip.sample_rate and clip.sample_rate > 0):
            exact = clip.bwf_time_reference / clip.sample_rate
            return exact + round((seconds - exact) / 86400.0) * 86400.0
        return seconds

    def _add_tc_day_offset(self, clip, days=1):
        """Shift a clip's TC forward by whole days (24h-wrap adjustment).

        Both frame-count representations describe the same instant, so both
        move together, each in the clip's own fps base."""
        fps = clip.fps if clip.fps and clip.fps > 0 else 30.0
        day_frames = int(round(24 * 3600 * fps)) * days
        if clip.start_tc:
            clip.start_frames += day_frames
        if clip.embedded_tc and clip.embedded_tc != "00:00:00:00":
            clip.embedded_tc_frames += day_frames

    def _sync_clips(self):
        """Synchronize clips by timecode."""
        if not self.clips:
            return

        # Stop playback if running (sync completely rearranges timeline).
        # Also disarm any in-progress ruler scrub and close a soft-pause
        # audio stream: resuming over a rearranged timeline would play the
        # wrong content against stale state.
        self._resume_after_scrub = False
        self._is_scrubbing = False
        if self.timeline_playing:
            self._stop_timeline()
        else:
            self._stop_timeline_audio()
        if self.preview_playing:
            self._reset_preview()

        # Clear trim offsets - sync repositions clips so old trims are invalid
        self.clip_in_offsets.clear()

        # First, link audio tracks from multi-track recorders (ZOOM, Sound Devices, etc.)
        self._link_audio_tracks()

        # Effective-TC resolution is shared with the TC+Date / wrap helpers
        get_clip_tc = self._get_clip_effective_tc

        # Get clips with valid timecode based on TC Source setting
        tc_clips = [c for c in self.clips if get_clip_tc(c)[0]]
        if not tc_clips:
            messagebox.showwarning("Warning", "No clips with detected timecode.\n\n"
                                   "Check your TC Source setting:\n"
                                   "- LTC (Audio): Uses timecode from audio tracks\n"
                                   "- Embedded (File): Uses timecode from file metadata\n"
                                   "- Auto: Tries LTC first, falls back to embedded")
            return

        # Get actual frame rate from video metadata (more accurate than LTC detection)
        # This fixes 29.97 vs 30 fps sync drift issues
        actual_fps = None
        for clip in self.clips:
            if not clip.is_audio_only and clip.video_fps and '/' in clip.video_fps:
                try:
                    num, denom = map(int, clip.video_fps.split('/'))
                    actual_fps = num / denom
                    break
                except (ValueError, ZeroDivisionError):
                    pass

        # Get sync logic mode
        sync_mode = self.sync_logic.get() if hasattr(self, 'sync_logic') else "TC Only"

        # Handle 24-hour timecode wrap for split files (48+ hour recordings)
        # This must run before calculating min_frames or day_offsets
        wrap_adjusted = self._handle_timecode_wrap(tc_clips)
        if wrap_adjusted > 0:
            self.status_label.configure(text=f"Adjusted {wrap_adjusted} clips for 24h TC wrap")

        # For TC + Date mode: group clips by date and offset each day
        day_offsets = {}  # {date: offset_seconds}
        if sync_mode == "TC + Date":
            day_offsets = self._calculate_day_offsets(tc_clips, actual_fps)

        # For TC + Filename mode: group clips by filename pattern and offset each group
        filename_group_offsets = {}  # {group_key: offset_seconds}
        if sync_mode == "TC + Filename":
            filename_group_offsets = self._calculate_filename_group_offsets(tc_clips, actual_fps)

        # Find earliest timecode as SECONDS. Frame counts must be normalized
        # by each clip's OWN fps before comparing: an LTC clip (fps <= 30) and
        # a no-LTC clip whose fps now comes from video metadata (e.g. 50) have
        # frame counts on different bases - subtracting raw frames and dividing
        # by one clip's fps placed 50fps clips hours off the timeline.
        # (each clip's START, i.e. with its sub-frame LTC offset - without it
        # the earliest clip landed a fraction of a frame BELOW zero, which is
        # the "no TC, park at the end" marker)
        def ltc_offset_s(c):
            if (get_clip_tc(c)[2] == 'ltc' and getattr(c, 'ltc_offset', 0) > 0
                    and c.sample_rate and c.sample_rate > 0):
                return c.ltc_offset / c.sample_rate
            return 0.0
        def cam_shift_s(c):
            # Camera Offset dialog: this camera's clips sit that many of ITS frames later (+) or earlier (-)
            if c.is_audio_only or not self.camera_offsets:
                return 0.0
            frames = self.camera_offsets.get(XMLExporter.camera_audio_key(c), 0.0)
            return frames / (c.fps if c.fps and c.fps > 0 else 25.0) if frames else 0.0
        min_seconds = min(self._effective_tc_seconds(c) - ltc_offset_s(c) + cam_shift_s(c) for c in tc_clips)

        # Calculate timeline positions (counting TC sources for the status message)
        ltc_used_count = 0
        embedded_used_count = 0
        for clip in self.clips:
            clip_tc, clip_frames, clip_tc_src = get_clip_tc(clip)
            if clip_tc:
                if clip_tc_src == 'ltc':
                    ltc_used_count += 1
                elif clip_tc_src == 'embedded':
                    embedded_used_count += 1
                # Correct clip.fps to the video's real rate when they're close
                # (e.g. 29.97 vs 30); a large mismatch (25 vs 50) is left alone
                clip_fps = clip.fps if clip.fps and clip.fps > 0 else 30.0
                if (actual_fps and abs(actual_fps - clip.fps) < 0.5
                        and not getattr(clip, 'fps_override', False)):
                    clip.fps = actual_fps
                    clip_fps = actual_fps
                # Position in seconds relative to the earliest clip (BWF audio:
                # sample-exact, see _effective_tc_seconds)
                clip.timeline_start = self._effective_tc_seconds(clip) - min_seconds
                
                # Sub-frame offset: where the first LTC frame starts in the file
                # (LTC-placed clips only)
                clip.timeline_start -= ltc_offset_s(clip)
                clip.timeline_start += cam_shift_s(clip)   # per-camera offset
                if -0.001 < clip.timeline_start < 0:
                    clip.timeline_start = 0.0   # float rounding of the earliest clip (< 0 = "no TC")

                # TC + Date: add day offset to separate different days
                if sync_mode == "TC + Date":
                    # Use resolved date (from filename or file_date)
                    clip_date = getattr(clip, '_resolved_date', None)
                    if clip_date is None:
                        # Fallback: try to extract from filename
                        clip_date = self._extract_date_from_filename(clip.filename)
                    if clip_date is None and clip.file_date:
                        clip_date = clip.file_date.date()
                    if clip_date and clip_date in day_offsets:
                        clip.timeline_start += day_offsets[clip_date]

                # TC + Filename: add group offset to separate different filename groups
                if sync_mode == "TC + Filename":
                    # Use resolved group (from _calculate_filename_group_offsets)
                    group_key = getattr(clip, '_resolved_filename_group', None)
                    if group_key is None:
                        group_key = self._get_filename_group(clip)
                    if group_key and group_key in filename_group_offsets:
                        clip.timeline_start += filename_group_offsets[group_key]
            else:
                # For clips without TC that are linked, position them based on their LTC track
                if clip.linked_ltc_path:
                    ltc_clip = next((c for c in self.clips if c.path == clip.linked_ltc_path), None)
                    if ltc_clip and get_clip_tc(ltc_clip)[0]:
                        # Copy LTC clip's timeline_start (already includes day offset if TC+Date mode)
                        clip.timeline_start = ltc_clip.timeline_start
                else:
                    # Mark for later positioning (will be placed at end of timeline)
                    clip.timeline_start = -1  # Temporary marker

        # Merge split files (-0001, -0002, etc.) to continuous positions
        self._merge_split_clips()

        # Position clips without TC at the END of the timeline (not overlapping TC clips)
        # Find the end of all TC clips
        tc_end = 0
        for clip in self.clips:
            if clip.timeline_start >= 0:  # Has TC or is linked
                clip_end = clip.timeline_start + clip.duration
                if clip_end > tc_end:
                    tc_end = clip_end

        # Place clips without TC (marked with -1) at the end with 10 second gaps.
        # A split recording without TC is placed as ONE block: only its first
        # part gets a slot (sized for all parts); the re-run of
        # _merge_split_clips below chains the other parts after it.
        no_tc_clips = [c for c in self.clips if c.timeline_start < 0]
        if no_tc_clips:
            def split_key(c):
                return (c.recording_id, c.track_number) if c.recording_id and c.track_number is not None else None
            groups = {}
            for c in no_tc_clips:
                if split_key(c):
                    groups.setdefault(split_key(c), []).append(c)
            later_parts = set()
            block_extra = {}  # first part -> summed duration of its later parts
            for members in groups.values():
                if len(members) > 1:
                    members.sort(key=lambda c: c.split_part or 0)
                    later_parts.update(id(c) for c in members[1:])
                    block_extra[id(members[0])] = sum(c.duration for c in members[1:])
            next_pos = tc_end + 10  # 10 second gap after TC clips
            for clip in no_tc_clips:
                if id(clip) in later_parts:
                    continue
                clip.timeline_start = next_pos
                next_pos += clip.duration + block_extra.get(id(clip), 0.0) + 5  # 5 s gap between unsynced clips
            if later_parts:
                self._merge_split_clips()

        # Sort by timeline position
        self.clips.sort(key=lambda c: c.timeline_start)

        # Assign track indices for overlapping clips (multi-camera support)
        self._assign_track_indices()

        # Auto-mute LTC tracks (they contain timecode signal, not useful audio)
        # Must be called AFTER _assign_track_indices() so track_index is available
        self._auto_mute_ltc_tracks()

        self.synced = True
        self._invalidate_tc_cache()  # Reset timecode cache with new clips
        self._lock_timeline()  # Auto-lock timeline after sync
        self._refresh_clips_list()
        self._fit_timeline()  # Fit all clips in view and draw timeline

        no_tc_count = len(no_tc_clips) if no_tc_clips else 0

        # Build TC source info for status message
        tc_source_info = ""
        if ltc_used_count > 0 and embedded_used_count > 0:
            tc_source_info = f" [LTC:{ltc_used_count}, Emb:{embedded_used_count}]"
        elif embedded_used_count > 0:
            tc_source_info = " [Embedded TC]"
        # LTC only is the default, no need to show

        # Show sync mode in status (including warning about clips without TC)
        no_tc_warning = f" ({no_tc_count} without TC placed at end)" if no_tc_count > 0 else ""
        if sync_mode == "TC + Date" and day_offsets:
            day_count = len(day_offsets)
            self.status_label.configure(text=f"Synced {len(tc_clips)} clips across {day_count} days (TC+Date){tc_source_info}{no_tc_warning}")
        elif sync_mode == "TC + Filename" and filename_group_offsets:
            group_count = len(filename_group_offsets)
            self.status_label.configure(text=f"Synced {len(tc_clips)} clips across {group_count} groups (TC+Filename){tc_source_info}{no_tc_warning}")
        else:
            self.status_label.configure(text=f"Synced {len(tc_clips)} clips{tc_source_info} - Timeline locked{no_tc_warning}")

        # DISABLED: Old audio extraction - now using lazy loading during playback
        # self._prepare_timeline_audio()

    def _extract_date_from_filename(self, filename: str):
        """Extract shooting date from filename patterns like 251219 (YYMMDD) or 2025-12-19."""
        import re
        from datetime import date

        # Pattern 1: YYMMDD (e.g., A025C071_251219_DJ0B.MP4 -> 251219)
        match = re.search(r'[_\-](\d{6})[_\-]', filename)
        if match:
            try:
                yymmdd = match.group(1)
                yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
                year = 2000 + yy if yy < 50 else 1900 + yy
                return date(year, mm, dd)
            except (ValueError, IndexError):
                pass

        # Pattern 2: YYYY-MM-DD or YYYYMMDD
        match = re.search(r'(\d{4})[-_]?(\d{2})[-_]?(\d{2})', filename)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass

        return None

    def _calculate_day_offsets(self, tc_clips: list, actual_fps: float = None) -> dict:
        """
        Calculate timeline offsets for each day in TC + Date mode.

        Groups clips by date and calculates offsets so each day starts after
        the previous day ends, with a 1-hour gap between days.

        Date priority:
        1. Date extracted from filename (e.g., 251219 -> Dec 19, 2025)
        2. File modification date (fallback)

        Returns: {date: offset_seconds}
        """
        from collections import defaultdict

        # PASS 1: Extract dates from filenames (most reliable)
        clips_with_filename_date = []  # [(clip, date)]
        clips_without_filename_date = []  # [clip]

        for clip in tc_clips:
            filename_date = self._extract_date_from_filename(clip.filename)
            if filename_date:
                clips_with_filename_date.append((clip, filename_date))
            else:
                clips_without_filename_date.append(clip)

        # PASS 2: For clips without filename dates (e.g., ZOOM0014_LR.WAV),
        # find overlapping clips WITH dates and inherit their date

        def clips_overlap(clip1, clip2):
            """Check if two clips overlap in (effective) timecode, in seconds -
            base-safe across mixed fps and embedded-only clips."""
            s1 = self._effective_tc_seconds(clip1)
            s2 = self._effective_tc_seconds(clip2)
            if s1 is None or s2 is None:
                return False
            return s1 < s2 + clip2.duration and s2 < s1 + clip1.duration

        for clip in clips_without_filename_date:
            # Find a clip with filename date that overlaps
            inherited_date = None
            for other_clip, other_date in clips_with_filename_date:
                if clips_overlap(clip, other_clip):
                    inherited_date = other_date
                    break

            if inherited_date:
                clips_with_filename_date.append((clip, inherited_date))
            else:
                # No overlap found - fall back to file_date
                if clip.file_date:
                    clips_with_filename_date.append((clip, clip.file_date.date()))
                else:
                    clips_with_filename_date.append((clip, None))

        # Group clips by resolved date
        clips_by_date = defaultdict(list)
        for clip, clip_date in clips_with_filename_date:
            clips_by_date[clip_date].append(clip)
            # Store the resolved date on the clip for later use
            clip._resolved_date = clip_date

        if len(clips_by_date) <= 1:
            # Only one day (or no dates) - no offset needed
            return {}

        # Calculate timecode range for each day, in SECONDS (effective TC)
        day_ranges = {}  # {date: (min_seconds, max_end_seconds)}

        for date, clips in clips_by_date.items():
            if date is None:
                continue

            starts = [(c, self._effective_tc_seconds(c)) for c in clips]
            starts = [(c, s) for c, s in starts if s is not None]
            if not starts:
                continue
            min_sec = min(s for _c, s in starts)
            max_end_sec = max(s + c.duration for c, s in starts)
            day_ranges[date] = (min_sec, max_end_sec)

        # Sort dates chronologically
        sorted_dates = sorted([d for d in day_ranges.keys() if d is not None])

        if not sorted_dates:
            return {}

        # Calculate offsets for each day
        # Day 1: offset = 0
        # Day 2: offset = Day 1's duration + gap
        # Day 3: offset = Day 1's duration + Day 2's duration + 2*gap
        # etc.

        GAP_SECONDS = 3600  # 1-hour gap between days for visual clarity

        day_offsets = {}
        cumulative_offset = 0.0

        for i, date in enumerate(sorted_dates):
            min_sec, max_end_sec = day_ranges[date]

            if i == 0:
                # First day: no offset needed (relative to global min)
                day_offsets[date] = 0.0
            else:
                # Subsequent days: offset by cumulative duration + gap
                day_offsets[date] = cumulative_offset

            # This day's span, already in seconds
            day_duration = max(0.0, max_end_sec - min_sec)

            # Add this day's duration + gap to cumulative offset
            cumulative_offset += day_duration + GAP_SECONDS

        # Log the day groupings
        day_count = len(sorted_dates)
        total_clips = sum(len(clips_by_date[d]) for d in sorted_dates)
        self.status_label.configure(text=f"TC+Date: {total_clips} clips across {day_count} days")

        return day_offsets

    def _get_filename_group(self, clip) -> str:
        """
        Get the filename group for a clip (camera_id or recording_id).

        Used by TC + Filename mode to group clips by their source device.
        """
        # For video clips, use camera_id
        if not clip.is_audio_only and clip.camera_id:
            return clip.camera_id

        # For audio clips, use recording_id (e.g., "ZOOM0014")
        if clip.recording_id:
            return clip.recording_id

        # Fallback: extract pattern from filename
        import re
        filename = clip.filename

        # Try common patterns: A025C001, BMPCC_001, etc.
        match = re.match(r'^([A-Z]+\d+)', filename)
        if match:
            return match.group(1)

        # Try ZOOM/Sound Devices pattern: ZOOM0014_LR
        match = re.match(r'^(ZOOM\d+|SD\d+)', filename, re.IGNORECASE)
        if match:
            return match.group(1).upper()

        # Last resort: use first part before underscore/dash
        match = re.match(r'^([^_\-\.]+)', filename)
        if match:
            return match.group(1)

        return "UNKNOWN"

    def _calculate_filename_group_offsets(self, tc_clips: list, actual_fps: float = None) -> dict:
        """
        Calculate timeline offsets for each filename group in TC + Filename mode.

        Groups clips by filename pattern (camera_id, recording_id) and calculates
        offsets so each group starts after the previous group ends, with a 1-hour gap.

        Returns: {group_key: offset_seconds}
        """
        from collections import defaultdict

        # Group clips by filename pattern
        clips_by_group = defaultdict(list)

        for clip in tc_clips:
            group_key = self._get_filename_group(clip)
            clips_by_group[group_key].append(clip)
            # Store the resolved group on the clip for later use
            clip._resolved_filename_group = group_key

        if len(clips_by_group) <= 1:
            # Only one group - no offset needed
            return {}

        # Calculate timecode range for each group, in SECONDS (effective TC)
        group_ranges = {}  # {group_key: (min_seconds, max_end_seconds)}

        for group_key, clips in clips_by_group.items():
            starts = [(c, self._effective_tc_seconds(c)) for c in clips]
            starts = [(c, s) for c, s in starts if s is not None]
            if not starts:
                continue
            min_sec = min(s for _c, s in starts)
            max_end_sec = max(s + c.duration for c, s in starts)
            group_ranges[group_key] = (min_sec, max_end_sec)

        # Sort groups by their earliest timecode
        sorted_groups = sorted(group_ranges.keys(), key=lambda g: group_ranges[g][0])

        GAP_SECONDS = 3600  # 1-hour gap between groups for visual clarity

        group_offsets = {}
        cumulative_offset = 0.0

        for i, group_key in enumerate(sorted_groups):
            min_sec, max_end_sec = group_ranges[group_key]

            if i == 0:
                # First group: no offset needed
                group_offsets[group_key] = 0.0
            else:
                # Subsequent groups: offset by cumulative duration + gap
                group_offsets[group_key] = cumulative_offset

            # This group's span, already in seconds
            group_duration = max(0.0, max_end_sec - min_sec)

            # Add this group's duration + gap to cumulative offset
            cumulative_offset += group_duration + GAP_SECONDS

        # Log the groupings
        group_count = len(sorted_groups)
        total_clips = sum(len(clips_by_group[g]) for g in sorted_groups)
        self.status_label.configure(text=f"TC+Filename: {total_clips} clips across {group_count} groups")

        return group_offsets

    def _assign_track_indices(self):
        """
        Assign track indices for overlapping clips (multi-camera support).

        Rules:
        - Clips from the same camera stay on the same track
        - Overlapping clips from different cameras go to different tracks
        - Video track N pairs with Audio track N for embedded audio
        - Audio-only clips (LTC, external recorders) get their own tracks
        """
        # Separate video and audio clips
        video_clips = [c for c in self.clips if not c.is_audio_only]

        # === Assign video tracks ===
        # Group by camera_id first - same camera = same track
        camera_to_track = {}  # {camera_id: track_index}
        next_track = 0

        # Sort video clips by timeline position for proper lane assignment
        sorted_videos = sorted(video_clips, key=lambda c: c.timeline_start)

        # Track active time ranges for each track (to detect overlaps)
        track_ranges = {}  # {track_index: [(start, end), ...]}

        for clip in sorted_videos:
            camera_id = clip.camera_id or f"_no_cam_{id(clip)}"  # Unique ID if no camera
            clip_start = clip.timeline_start
            clip_end = clip_start + clip.duration

            if camera_id in camera_to_track:
                # Same camera - use existing track
                clip.track_index = camera_to_track[camera_id]
            else:
                # New camera - always gets its own dedicated track
                # Don't reuse other cameras' tracks even if they don't overlap
                assigned_track = next_track
                next_track += 1

                camera_to_track[camera_id] = assigned_track
                clip.track_index = assigned_track

            # Record this clip's time range on its track
            if clip.track_index not in track_ranges:
                track_ranges[clip.track_index] = []
            track_ranges[clip.track_index].append((clip_start, clip_end))

        self._assign_audio_track_indices()

    def _assign_audio_track_indices(self):
        """Audio-only clips get their own track numbering (recording ID groups the tracks of one
        recorder take). Touches ONLY audio clips - the 'Share Tracks' toggle calls this alone so the
        video tracks stay exactly as they are."""
        audio_clips = [c for c in self.clips if c.is_audio_only]
        def audio_key(clip):
            # A unique key for this audio source (recorder track of one take, or the file), per folder:
            # two recorders with the same file names (ZOOM0001 on both cards) stay apart
            folder = os.path.normcase(os.path.dirname(os.path.abspath(clip.path)))
            if clip.recording_id:
                if clip.track_number == 0 or clip.track_number is None:
                    return (folder, f"{clip.recording_id}_LR")
                return (folder, f"{clip.recording_id}_Tr{clip.track_number}")
            return (folder, Path(clip.path).stem)   # filename base for non-recorder audio

        # "Share tracks": takes of the same recorder track go on one track when they don't overlap
        pack = bool(getattr(self, 'pack_audio_tracks', None) and self.pack_audio_tracks.get())
        audio_track_map = XMLExporter.assign_audio_lanes(audio_clips, audio_key, pack)
        for clip in audio_clips:
            clip.track_index = audio_track_map[audio_key(clip)]

    # =========================================================================
    # Timeline
    # =========================================================================

    def _draw_timeline(self, event=None):
        """Draw NLE-style timeline with video tracks above and audio tracks below."""
        self.timeline_canvas.delete('all')
        self.track_headers_canvas.delete('all')
        self._clip_draw_positions.clear()  # Clear position cache for fresh rebuild
        self._clip_hit_rects.clear()

        w = self.timeline_canvas.winfo_width()
        h = self.timeline_canvas.winfo_height()

        if not self.clips:
            # Empty state message
            self.timeline_canvas.create_text(w//2, h//2,
                                             text="Add media files and sync to view timeline",
                                             fill=self.COLORS['text_dim'], font=('Segoe UI', 11))
            self.track_headers_canvas.create_text(self.track_header_width//2, h//2,
                                                   text="No tracks",
                                                   fill=self.COLORS['text_dim'], font=('Segoe UI', 9))
            return

        # Fix is_audio_only flag for any clips that might have incorrect values
        # (e.g., from old projects or failed analysis)
        for clip in self.clips:
            ext = Path(clip.path).suffix.lower()
            if ext in self.analyzer.SUPPORTED_AUDIO:
                clip.is_audio_only = True

        # Separate video and audio clips
        video_clips = [c for c in self.clips if not c.is_audio_only]
        audio_clips = [c for c in self.clips if c.is_audio_only]

        # Group video clips by track_index (assigned during sync)
        # This respects multi-camera layout with overlapping clips on different tracks
        video_tracks = {}  # {track_index: [clips]}
        video_track_names = {}  # {track_index: display_name}

        for clip in video_clips:
            track_idx = getattr(clip, 'track_index', 0)
            if track_idx not in video_tracks:
                video_tracks[track_idx] = []
                # Use camera_id for display name, or generic "Video N"
                video_track_names[track_idx] = clip.camera_id or f"Video {track_idx + 1}"
            video_tracks[track_idx].append(clip)

        # Group audio clips by track_index (assigned during sync)
        audio_tracks = {}  # {track_index: [clips]}
        audio_track_names = {}  # {track_index: display_name}

        for clip in audio_clips:
            track_idx = getattr(clip, 'track_index', 0)
            if track_idx not in audio_tracks:
                audio_tracks[track_idx] = []
                # Build display name from recording info
                if clip.recording_id and clip.track_number is not None:
                    # Shared tracks hold many takes: name them by recorder + track (ZOOM_LR), not by one take
                    rid = clip.recording_id
                    if getattr(self, 'pack_audio_tracks', None) and self.pack_audio_tracks.get():
                        rid = re.sub(r'\d+$', '', rid) or rid
                    if clip.track_number == 0:
                        name = f"{rid}_LR"
                    else:
                        name = f"{rid}_Tr{clip.track_number}"
                    if clip.is_ltc_track:
                        name += " (LTC)"
                else:
                    name = clip.camera_id or f"Audio {track_idx + 1}"
                audio_track_names[track_idx] = name
            else:
                # Update track name if this clip is the LTC track (may have been missed
                # if a split file was encountered first)
                if clip.is_ltc_track and clip.recording_id and clip.track_number is not None:
                    current_name = audio_track_names.get(track_idx, "")
                    if "(LTC)" not in current_name:
                        if clip.track_number == 0:
                            base_name = f"{clip.recording_id}_LR"
                        else:
                            base_name = f"{clip.recording_id}_Tr{clip.track_number}"
                        audio_track_names[track_idx] = base_name + " (LTC)"
            audio_tracks[track_idx].append(clip)

        # Create audio tracks for video clips' embedded audio (camera audio)
        # Each video track gets a paired audio track for its embedded audio
        camera_audio_tracks = {}  # {track_index: [clips]}
        camera_audio_names = {}  # {track_index: display_name}

        for clip in video_clips:
            track_idx = getattr(clip, 'track_index', 0)
            if track_idx not in camera_audio_tracks:
                camera_audio_tracks[track_idx] = []
                camera_audio_names[track_idx] = (clip.camera_id or f"Video {track_idx + 1}") + " Audio"
            camera_audio_tracks[track_idx].append(clip)

        # Calculate timeline duration
        if self.synced:
            all_ends = [(c.timeline_start + c.duration) for c in self.clips if c.duration > 0]
            max_end = max(all_ends) if all_ends else 60
            min_start = min((c.timeline_start for c in self.clips if c.duration > 0), default=0)
        else:
            max_end = sum(c.duration for c in self.clips if c.duration > 0) or 60
            min_start = 0

        # Actual content duration (not scaled by zoom)
        actual_duration = max(max_end - min_start, 60)

        # Pixels per second: base is 50pps at 100% zoom
        # Higher zoom = more pixels per second (zoomed in)
        # Lower zoom = fewer pixels per second (zoomed out)
        self.pixels_per_second = 50 * self.timeline_zoom

        # Store actual duration for other calculations
        total_duration = actual_duration

        # Layout constants
        ruler_height = 30
        track_spacing = 2
        section_spacing = 15
        y = ruler_height


        # Calculate total height
        total_height = ruler_height
        if self.video_tracks_enabled:
            total_height += len(video_tracks) * (self.track_height + track_spacing) + section_spacing
        if self.audio_tracks_enabled:
            # Include both external audio tracks and camera audio tracks
            total_height += (len(audio_tracks) + len(camera_audio_tracks)) * (self.track_height + track_spacing)
        total_height = max(total_height + 50, h)

        # Calculate canvas width based on content
        canvas_width = max(w, int(total_duration * self.pixels_per_second) + 100)

        # Set scroll regions
        self.timeline_canvas.configure(scrollregion=(0, 0, canvas_width, total_height))
        self.track_headers_canvas.configure(scrollregion=(0, 0, self.track_header_width, total_height))

        # Draw ruler on timeline canvas - VISIBLE width: items are placed
        # relative to timeline_offset, so only 0..w is ever on screen. Passing
        # the scroll width drew every label of the whole timeline (~5,700
        # labels + ticks on a multi-hour project, ~0.4s per redraw).
        self._draw_ruler(w, self.pixels_per_second, total_duration)

        # Draw ruler placeholder on headers
        self.track_headers_canvas.create_rectangle(0, 0, self.track_header_width, ruler_height,
                                                    fill=self.COLORS['timeline_ruler'], outline='')
        self.track_headers_canvas.create_text(10, ruler_height // 2, text="TRACKS",
                                               anchor='w', fill=self.COLORS['text_dim'],
                                               font=('Segoe UI', 8, 'bold'))

        y = ruler_height + 5

        # === DRAW VIDEO TRACKS ===
        if self.video_tracks_enabled and video_tracks:
            # Video section header
            self._draw_section_header(y, "VIDEO", len(video_tracks), canvas_width)
            y += 18

            sorted_video_tracks = sorted(video_tracks.keys())
            video_colors = self.COLORS['video_track_colors']

            for i, track_idx in enumerate(sorted_video_tracks):
                track_clips = video_tracks[track_idx]
                track_color = video_colors[i % len(video_colors)]
                track_name = video_track_names.get(track_idx, f"Video {track_idx + 1}")

                # Check if this track is hidden from preview (dims the clips)
                is_hidden_from_preview = not self._is_video_track_visible(track_name)

                # Draw track header
                self._draw_track_header(y, track_name, track_color, 'video', i)

                # Draw track background
                self.timeline_canvas.create_rectangle(0, y, canvas_width, y + self.track_height,
                                                      fill=self.COLORS['timeline_bg'], outline='')

                # Draw track divider line
                self.timeline_canvas.create_line(0, y + self.track_height, canvas_width, y + self.track_height,
                                                 fill=self.COLORS['track_divider'], width=1)

                # Draw clips on this track (dimmed if hidden from preview)
                for clip in track_clips:
                    if clip.duration > 0:
                        # Use integer pixels to avoid floating point rendering differences
                        x = int((clip.timeline_start - self.timeline_offset) * self.pixels_per_second)
                        # Scale minimum width with zoom to prevent clips from extending past their actual duration
                        # At high zoom use 20px min, at low zoom use 2px min
                        min_width = max(2, min(20, 20 * self.timeline_zoom))
                        clip_width = int(max(clip.duration * self.pixels_per_second, min_width))

                        if x + clip_width > 0 and x < canvas_width:
                            # Dim the color if hidden from preview
                            draw_color = self._darken_color(track_color, 0.4) if is_hidden_from_preview else track_color
                            self._draw_clip(x, y, clip_width, clip, draw_color, track_id=track_name)

                y += self.track_height + track_spacing

            y += section_spacing

        # === DRAW AUDIO TRACKS ===
        total_audio_track_count = len(audio_tracks) + len(camera_audio_tracks)
        if self.audio_tracks_enabled and (audio_tracks or camera_audio_tracks):
            # Audio section header (includes both external and camera audio)
            self._draw_section_header(y, "AUDIO", total_audio_track_count, canvas_width)
            y += 18

            audio_colors = self.COLORS['audio_track_colors']
            audio_track_index = 0

            # Draw camera audio tracks FIRST (paired with video tracks)
            sorted_camera_audio = sorted(camera_audio_tracks.keys())
            for track_idx in sorted_camera_audio:
                track_clips = camera_audio_tracks[track_idx]
                track_color = audio_colors[audio_track_index % len(audio_colors)]
                track_name = camera_audio_names.get(track_idx, f"Audio {track_idx + 1}")

                # Draw track header (camera audio)
                self._draw_track_header(y, track_name, track_color, 'audio', audio_track_index)

                # Draw track background
                self.timeline_canvas.create_rectangle(0, y, canvas_width, y + self.track_height,
                                                      fill='#101015', outline='')

                # Draw track divider line
                self.timeline_canvas.create_line(0, y + self.track_height, canvas_width, y + self.track_height,
                                                 fill=self.COLORS['track_divider'], width=1)

                # Draw clips on this track (video clips shown as audio waveform)
                for clip in track_clips:
                    if clip.duration > 0:
                        # Use integer pixels to avoid floating point rendering differences
                        x = int((clip.timeline_start - self.timeline_offset) * self.pixels_per_second)
                        min_width = max(2, min(20, 20 * self.timeline_zoom))
                        clip_width = int(max(clip.duration * self.pixels_per_second, min_width))

                        if x + clip_width > 0 and x < canvas_width:
                            # Draw as audio clip (shows waveform if available)
                            self._draw_clip(x, y, clip_width, clip, track_color, is_audio=True, track_id=track_name)

                y += self.track_height + track_spacing
                audio_track_index += 1

            # Draw external audio tracks (audio-only files)
            sorted_audio_tracks = sorted(audio_tracks.keys())
            for track_idx in sorted_audio_tracks:
                track_clips = audio_tracks[track_idx]
                track_color = audio_colors[audio_track_index % len(audio_colors)]
                track_name = audio_track_names.get(track_idx, f"Audio {track_idx + 1}")

                # Draw track header
                self._draw_track_header(y, track_name, track_color, 'audio', audio_track_index)

                # Draw track background (slightly different for audio)
                self.timeline_canvas.create_rectangle(0, y, canvas_width, y + self.track_height,
                                                      fill='#101015', outline='')

                # Draw track divider line
                self.timeline_canvas.create_line(0, y + self.track_height, canvas_width, y + self.track_height,
                                                 fill=self.COLORS['track_divider'], width=1)

                # Draw clips on this track - sort by timeline_start for consistent drawing
                sorted_track_clips = sorted(track_clips, key=lambda c: c.timeline_start)
                for clip in sorted_track_clips:
                    if clip.duration > 0:
                        # Use integer pixels to avoid floating point rendering differences
                        x = int((clip.timeline_start - self.timeline_offset) * self.pixels_per_second)
                        # Scale minimum width with zoom to prevent clips appearing longer at low zoom
                        min_width = max(2, min(20, 20 * self.timeline_zoom))
                        clip_width = int(max(clip.duration * self.pixels_per_second, min_width))

                        if x + clip_width > 0 and x < canvas_width:
                            self._draw_clip(x, y, clip_width, clip, track_color, is_audio=True, track_id=track_name)


                y += self.track_height + track_spacing
                audio_track_index += 1

        # Draw markers
        if self.timeline_markers:
            self._draw_markers(canvas_width, self.pixels_per_second)

        # Draw IN/OUT points
        self._draw_in_out_points(ruler_height, y, canvas_width)

        # Draw playhead
        self._draw_playhead(ruler_height, y)

        # Sync scrollbar to current timeline_offset
        self._sync_scrollbar_to_offset()

    def _draw_section_header(self, y, title, track_count, width):
        """Draw a section header (VIDEO or AUDIO) on both canvases."""
        # On timeline canvas
        self.timeline_canvas.create_rectangle(0, y, width, y + 16,
                                              fill=self.COLORS['bg_header'], outline='')

        # On headers canvas
        self.track_headers_canvas.create_rectangle(0, y, self.track_header_width, y + 16,
                                                    fill=self.COLORS['bg_header'], outline='')
        self.track_headers_canvas.create_text(10, y + 8, text=f"{title} ({track_count})",
                                               anchor='w', fill=self.COLORS['text_dim'],
                                               font=('Segoe UI', 8, 'bold'))

    def _draw_track_header(self, y, track_id, color, track_type, index):
        """Draw a track header with name, color indicator, and hide/mute/solo controls."""
        # For video tracks: use hide/solo, for audio tracks: use mute/solo
        if track_type == 'video':
            is_hidden = track_id in self.hidden_video_tracks
            is_soloed = track_id in self.soloed_video_tracks
            is_dimmed = is_hidden
        else:
            is_hidden = track_id in self.muted_tracks
            is_soloed = track_id in self.soloed_tracks
            is_dimmed = is_hidden

        # Track header background (dimmed if hidden/muted)
        bg_color = '#1a1a1a' if is_dimmed else self.COLORS['track_header']
        self.track_headers_canvas.create_rectangle(0, y, self.track_header_width, y + self.track_height,
                                                    fill=bg_color, outline='')

        # Color indicator bar (dimmed if hidden/muted)
        bar_color = self._darken_color(color, 0.5) if is_dimmed else color
        self.track_headers_canvas.create_rectangle(0, y, 4, y + self.track_height,
                                                    fill=bar_color, outline='')

        # Track label (V1, V2 for video, A1, A2 for audio) - like Sidus TC Sync
        track_label = f"V{index + 1}" if track_type == 'video' else f"A{index + 1}"
        label_bg = self._darken_color(color, 0.5) if is_dimmed else color
        self.track_headers_canvas.create_rectangle(8, y + 6, 32, y + self.track_height - 6,
                                                    fill=label_bg, outline='')
        self.track_headers_canvas.create_text(20, y + self.track_height // 2, text=track_label,
                                               fill='white', font=('Segoe UI', 9, 'bold'))

        # Source name (camera ID or recording info, truncated if too long)
        # Show smaller below the main label
        display_name = track_id[:12] + '..' if len(track_id) > 14 else track_id
        name_color = self.COLORS['text_dim'] if is_dimmed else '#aaaaaa'
        self.track_headers_canvas.create_text(38, y + self.track_height // 2,
                                               text=display_name, anchor='w',
                                               fill=name_color, font=('Segoe UI', 7))

        btn_y = y + self.track_height // 2

        if track_type == 'video':
            # Hide button (H) for video tracks
            hide_x = self.track_header_width - 50
            hide_bg = '#ff4444' if is_hidden else '#333333'
            hide_fg = '#ffffff' if is_hidden else '#888888'
            self.track_headers_canvas.create_rectangle(
                hide_x - 10, btn_y - 8, hide_x + 10, btn_y + 8,
                fill=hide_bg, outline='#555555', width=1,
                tags=(f'hide_{track_id}', 'hide_btn')
            )
            self.track_headers_canvas.create_text(
                hide_x, btn_y, text='H', fill=hide_fg, font=('Segoe UI', 8, 'bold'),
                tags=(f'hide_{track_id}', 'hide_btn')
            )

            # Solo button (S) for video tracks
            solo_x = self.track_header_width - 22
            solo_bg = '#ffcc00' if is_soloed else '#333333'
            solo_fg = '#000000' if is_soloed else '#888888'
            self.track_headers_canvas.create_rectangle(
                solo_x - 10, btn_y - 8, solo_x + 10, btn_y + 8,
                fill=solo_bg, outline='#555555', width=1,
                tags=(f'video_solo_{track_id}', 'video_solo_btn')
            )
            self.track_headers_canvas.create_text(
                solo_x, btn_y, text='S', fill=solo_fg, font=('Segoe UI', 8, 'bold'),
                tags=(f'video_solo_{track_id}', 'video_solo_btn')
            )
        else:
            # Mute button (M) for audio tracks
            mute_x = self.track_header_width - 50
            mute_bg = '#ff4444' if is_hidden else '#333333'
            mute_fg = '#ffffff' if is_hidden else '#888888'
            self.track_headers_canvas.create_rectangle(
                mute_x - 10, btn_y - 8, mute_x + 10, btn_y + 8,
                fill=mute_bg, outline='#555555', width=1,
                tags=(f'mute_{track_id}', 'mute_btn')
            )
            self.track_headers_canvas.create_text(
                mute_x, btn_y, text='M', fill=mute_fg, font=('Segoe UI', 8, 'bold'),
                tags=(f'mute_{track_id}', 'mute_btn')
            )

            # Solo button (S) for audio tracks
            solo_x = self.track_header_width - 22
            solo_bg = '#ffcc00' if is_soloed else '#333333'
            solo_fg = '#000000' if is_soloed else '#888888'
            self.track_headers_canvas.create_rectangle(
                solo_x - 10, btn_y - 8, solo_x + 10, btn_y + 8,
                fill=solo_bg, outline='#555555', width=1,
                tags=(f'solo_{track_id}', 'solo_btn')
            )
            self.track_headers_canvas.create_text(
                solo_x, btn_y, text='S', fill=solo_fg, font=('Segoe UI', 8, 'bold'),
                tags=(f'solo_{track_id}', 'solo_btn')
            )

    def _draw_clip(self, x, y, width, clip, color, is_audio=False, track_id=None):
        """Draw a single clip on the timeline with professional NLE styling."""
        clip_top = y + 2
        clip_bottom = y + self.track_height - 2
        clip_height = clip_bottom - clip_top

        # Clip drawing to visible area to prevent color leak when scrolled
        canvas_width = self.timeline_canvas.winfo_width()
        draw_x = max(0, x)
        draw_end_x = min(x + width, canvas_width)
        draw_width = draw_end_x - draw_x

        if draw_width <= 0:
            return  # Nothing visible to draw

        # Cache clip position for efficient selection updates. A video clip is drawn twice (picture row,
        # then its camera-audio row): the first draw - the picture - is the one selection highlights use.
        self._clip_draw_positions.setdefault(id(clip), (x, y, width))
        self._clip_hit_rects.append((x, y, width, clip))

        # Check if track is muted (dim the clip if so)
        is_muted = track_id and not self._is_track_audible(track_id)
        if is_muted:
            color = self._darken_color(color, 0.5)

        # Clip body - darker base (clipped to visible area)
        darker_color = self._darken_color(color, 0.3)
        self.timeline_canvas.create_rectangle(draw_x, clip_top, draw_end_x, clip_bottom,
                                              fill=darker_color, outline='')

        if is_audio:
            # Audio clip: Draw waveform visualization (clipped to visible area)
            # Pass original x and width for proper waveform portion calculation
            self._draw_audio_waveform(draw_x, clip_top, draw_width, clip_height, clip, color, x, width)
        else:
            # Video clip: Draw video thumbnails/filmstrip (clipped to visible area)
            # Pass original x and width for proper thumbnail portion calculation
            self._draw_video_filmstrip(draw_x, clip_top, draw_width, clip_height, clip, color, x, width)

        # Top edge highlight (clipped to visible area)
        self.timeline_canvas.create_line(draw_x + 1, clip_top + 1, draw_end_x - 1, clip_top + 1,
                                         fill=self._lighten_color(color, 0.4), width=1)

        # Left edge highlight (only if left edge is visible)
        if x >= 0:
            self.timeline_canvas.create_line(draw_x + 1, clip_top + 1, draw_x + 1, clip_bottom - 1,
                                             fill=self._lighten_color(color, 0.2), width=1)

        # Bottom edge shadow (clipped to visible area)
        self.timeline_canvas.create_line(draw_x + 1, clip_bottom - 1, draw_end_x - 1, clip_bottom - 1,
                                         fill=self._darken_color(color, 0.5), width=1)

        # Group indicator (colored border based on group ID) - clipped
        if clip.path in self.clip_to_group:
            group_id = self.clip_to_group[clip.path]
            group_colors = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#95e1d3', '#f38181',
                           '#aa96da', '#fcbad3', '#a8d8ea', '#ffb6b9', '#fae3d9']
            group_color = group_colors[(group_id - 1) % len(group_colors)]

            # Draw group indicator border (clipped to visible area)
            self.timeline_canvas.create_rectangle(
                draw_x, clip_top + 1, draw_end_x, clip_bottom - 1,
                fill='', outline=group_color, width=2, dash=(4, 2)
            )

            # Small group badge in top-left corner (only if visible)
            if x >= 0:
                badge_size = 12
                self.timeline_canvas.create_oval(
                    draw_x + 2, clip_top + 2, draw_x + 2 + badge_size, clip_top + 2 + badge_size,
                    fill=group_color, outline='#ffffff', width=1
                )
                self.timeline_canvas.create_text(
                    draw_x + 2 + badge_size // 2, clip_top + 2 + badge_size // 2,
                    text=str(group_id), fill='#ffffff', font=('Segoe UI', 6, 'bold')
                )

        # LTC or Link indicator badge (top-right corner for audio clips, only if visible)
        if is_audio and draw_width > 50:
            badge_x = draw_end_x - 22
            if badge_x > draw_x and clip.is_ltc_track:
                # LTC source track - show "LTC" badge
                self.timeline_canvas.create_rectangle(
                    badge_x, clip_top + 2, badge_x + 20, clip_top + 12,
                    fill='#ffaa00', outline='#ffffff', width=1
                )
                self.timeline_canvas.create_text(
                    badge_x + 10, clip_top + 7,
                    text='LTC', fill='#000000', font=('Segoe UI', 6, 'bold')
                )
            elif badge_x > draw_x and clip.linked_ltc_path:
                # Linked to LTC track - show link icon
                self.timeline_canvas.create_rectangle(
                    badge_x, clip_top + 2, badge_x + 14, clip_top + 12,
                    fill='#44aa88', outline='#ffffff', width=1
                )
                self.timeline_canvas.create_text(
                    badge_x + 7, clip_top + 7,
                    text='\u2192', fill='#ffffff', font=('Segoe UI', 6, 'bold')  # Arrow symbol
                )

        # Selection highlight if selected (clipped to visible area)
        if clip in self.selected_clips:
            self.timeline_canvas.create_rectangle(draw_x, y + 1, draw_end_x, y + self.track_height - 1,
                                                  fill='', outline='#ffffff', width=2,
                                                  tags=('selection', f'sel_{id(clip)}'))

        # Clip name label (with background for readability) - only if left edge visible
        if draw_width > 30 and x >= 0:
            display_name = clip.filename
            max_chars = max(3, int(draw_width / 7))
            if len(display_name) > max_chars:
                display_name = display_name[:max_chars - 2] + '..'

            # Semi-transparent label background
            label_width = min(len(display_name) * 6 + 8, draw_width - 10)
            self.timeline_canvas.create_rectangle(draw_x + 3, clip_top + 2, draw_x + 3 + label_width, clip_top + 14,
                                                  fill=darker_color, outline='', stipple='gray50')

            self.timeline_canvas.create_text(draw_x + 5, clip_top + 8,
                                             text=display_name, anchor='w',
                                             fill='white', font=('Segoe UI', 8, 'bold'))

        # Timecode at bottom left (only if left edge visible)
        if draw_width > 80 and clip.start_tc and x >= 0:
            self.timeline_canvas.create_text(draw_x + 5, clip_bottom - 6,
                                             text=clip.start_tc, anchor='w',
                                             fill='#aaaaaa', font=('Consolas', 7))

        # Duration and FPS at bottom right (only if right edge visible)
        if draw_width > 100 and x + width <= canvas_width:
            dur_mins = int(clip.duration // 60)
            dur_secs = int(clip.duration % 60)
            dur_text = f"{dur_mins}:{dur_secs:02d}"
            self.timeline_canvas.create_text(draw_end_x - 5, clip_bottom - 6,
                                             text=dur_text, anchor='e',
                                             fill='#ffcc66', font=('Segoe UI', 7))

        if draw_width > 140 and clip.fps > 0 and x + width <= canvas_width:
            self.timeline_canvas.create_text(draw_end_x - 40, clip_bottom - 6,
                                             text=clip.fps_display, anchor='e',
                                             fill='#88ff88', font=('Segoe UI', 7))

        # Trim handles (visible when clip is selected or hovered) - clipped to visible area
        if clip in self.selected_clips:
            handle_width = 6
            handle_color = '#ffaa00'

            # Left trim handle (IN point) - only if left edge is visible
            if x >= 0:
                self.timeline_canvas.create_rectangle(
                    draw_x, clip_top, draw_x + handle_width, clip_bottom,
                    fill=handle_color, outline='#ffffff', width=1,
                    tags=('trim_handle', f'trim_in_{id(clip)}')
                )

            # Right trim handle (OUT point) - only if right edge is visible
            if x + width <= canvas_width:
                self.timeline_canvas.create_rectangle(
                    draw_end_x - handle_width, clip_top, draw_end_x, clip_bottom,
                    fill=handle_color, outline='#ffffff', width=1,
                    tags=('trim_handle', f'trim_out_{id(clip)}')
                )

        # Lock indicator (small lock icon in top-right corner) - only if right edge visible
        if self._is_clip_locked(clip):
            lock_size = 10
            lock_x = draw_end_x - lock_size - 4
            lock_y = clip_top + 4

            if x + width <= canvas_width and lock_x > draw_x:
                # Lock body (rounded rectangle approximation)
                self.timeline_canvas.create_rectangle(
                    lock_x, lock_y + 4, lock_x + lock_size, lock_y + lock_size + 2,
                    fill='#ff6666', outline='#ffffff', width=1
                )
                # Lock shackle (arc at top)
                self.timeline_canvas.create_arc(
                    lock_x + 2, lock_y, lock_x + lock_size - 2, lock_y + 8,
                    start=0, extent=180, style='arc',
                    outline='#ffffff', width=1
                )

            # Dim the entire clip slightly to indicate locked state (clipped to visible area)
            self.timeline_canvas.create_rectangle(
                draw_x + 1, clip_top, draw_end_x - 1, clip_bottom,
                fill='#000000', outline='', stipple='gray50'
            )

    def _draw_video_filmstrip(self, x, y, width, height, clip, color, orig_x=None, orig_width=None):
        """Draw film strip pattern for video clips with thumbnail support.

        Args:
            x, y, width, height: Visible/clipped drawing area
            clip: The clip object
            color: Base color
            orig_x: Original (unclipped) x position of the clip (can be negative)
            orig_width: Original (unclipped) full width of the clip
        """
        # Use original dimensions if provided, otherwise fall back to visible
        if orig_x is None:
            orig_x = x
        if orig_width is None:
            orig_width = width

        thumbnails = self._get_clip_thumbnails(clip.path)

        if thumbnails and width > 40:
            # Draw thumbnails across the clip
            self._draw_clip_thumbnails(x, y, width, height, clip, thumbnails, color, orig_x, orig_width)
        else:
            # Queue for thumbnail extraction if not available
            self._queue_thumbnail_extraction(clip)
            # Fall back to film strip pattern
            self._draw_filmstrip_pattern(x, y, width, height, clip, color)

    def _draw_filmstrip_pattern(self, x, y, width, height, clip, color):
        """Draw fallback film strip pattern when thumbnails not available."""
        # Film sprocket holes along top and bottom
        hole_size = 3
        hole_spacing = 12
        hole_color = self._darken_color(color, 0.6)

        for hx in range(int(x + 6), int(x + width - 6), hole_spacing):
            # Top sprocket holes
            self.timeline_canvas.create_rectangle(hx, y + 2, hx + hole_size, y + 2 + hole_size,
                                                  fill=hole_color, outline='')
            # Bottom sprocket holes
            self.timeline_canvas.create_rectangle(hx, y + height - 2 - hole_size, hx + hole_size, y + height - 2,
                                                  fill=hole_color, outline='')

        # Frame dividers (vertical lines suggesting frames)
        if width > 60:
            frame_spacing = max(20, int(width / max(1, clip.duration / 2)))
            frame_spacing = min(frame_spacing, 40)
            divider_color = self._darken_color(color, 0.4)

            for fx in range(int(x + frame_spacing), int(x + width - 10), frame_spacing):
                self.timeline_canvas.create_line(fx, y + 8, fx, y + height - 8,
                                                 fill=divider_color, width=1)

        # Gradient overlay for depth (simulate film)
        mid_y = y + height // 2
        self.timeline_canvas.create_rectangle(x + 2, y + 7, x + width - 2, mid_y,
                                              fill=color, outline='')

    def _draw_clip_thumbnails(self, x, y, width, height, clip, thumbnails, color, orig_x=None, orig_width=None):
        """Draw actual video thumbnails across the clip area.

        Args:
            x, y, width, height: Visible/clipped drawing area
            clip: The clip object
            thumbnails: List of thumbnail data
            color: Base color
            orig_x: Original (unclipped) x position of the clip (can be negative)
            orig_width: Original (unclipped) full width of the clip
        """
        if not thumbnails:
            return

        # Use original dimensions if provided, otherwise fall back to visible
        if orig_x is None:
            orig_x = x
        if orig_width is None:
            orig_width = width

        # Get first thumbnail dimensions
        first_thumb = thumbnails[0]
        thumb_width = first_thumb['width']
        thumb_height = first_thumb['height']

        if thumb_width <= 0 or thumb_height <= 0:
            return

        # Calculate aspect-correct scaling
        thumb_area_height = height - 4
        scale = min(thumb_area_height / thumb_height, 1.0)
        display_thumb_width = int(thumb_width * scale)
        display_thumb_height = int(thumb_height * scale)

        if display_thumb_width <= 0:
            display_thumb_width = 40

        # Calculate total number of thumbnails that would fit in the FULL clip
        total_thumb_slots = max(1, int((orig_width - 4) / (display_thumb_width + 1)))

        # Calculate which portion of the clip is visible
        visible_start_ratio = max(0, (x - orig_x) / orig_width) if orig_width > 0 else 0
        visible_end_ratio = min(1, (x + width - orig_x) / orig_width) if orig_width > 0 else 1

        # Calculate the starting thumbnail slot index based on scroll position
        start_slot = int(visible_start_ratio * total_thumb_slots)
        end_slot = int(visible_end_ratio * total_thumb_slots) + 1

        # Calculate visible area boundaries
        thumb_area_y = y + 2
        clip_left_edge = x + 2

        # Draw thumbnails that fall within the visible area
        for slot_idx in range(start_slot, min(end_slot + 1, total_thumb_slots)):
            # Calculate the x position of this thumbnail in the FULL clip
            full_clip_thumb_x = orig_x + 2 + slot_idx * (display_thumb_width + 1)

            # Skip if thumbnail is not visible
            if full_clip_thumb_x + display_thumb_width < x:
                continue
            if full_clip_thumb_x > x + width:
                break

            # Map slot index to thumbnail data index
            if total_thumb_slots > 1:
                thumb_idx = int(slot_idx * (len(thumbnails) - 1) / max(1, total_thumb_slots - 1))
            else:
                thumb_idx = 0
            thumb_idx = min(thumb_idx, len(thumbnails) - 1)

            if thumb_idx < len(thumbnails):
                thumb_data = thumbnails[thumb_idx]
                photo = thumb_data.get('photo')
                if photo is None:  # made here, on the main thread (see _extract_clip_thumbnails)
                    photo = thumb_data['photo'] = ImageTk.PhotoImage(thumb_data['pil'])

                # Clamp position to visible area
                draw_thumb_x = max(clip_left_edge, full_clip_thumb_x)

                # Center vertically
                thumb_y = thumb_area_y + (thumb_area_height - display_thumb_height) // 2

                try:
                    self.timeline_canvas.create_image(
                        draw_thumb_x, thumb_y,
                        image=photo,
                        anchor='nw',
                        tags='thumbnail'
                    )
                except:
                    pass  # Image may have been garbage collected

        # Draw semi-transparent film frame overlay
        overlay_color = self._darken_color(color, 0.8)

        # Top border
        self.timeline_canvas.create_rectangle(x, y, x + width, y + 2,
                                              fill=overlay_color, outline='')
        # Bottom border
        self.timeline_canvas.create_rectangle(x, y + height - 2, x + width, y + height,
                                              fill=overlay_color, outline='')

        # Mask any thumbnail overflow at right edge (crop effect)
        # Draw a rectangle matching the background to cover overflow
        # Start at exactly x + width so we don't cover any part of the clip body
        mask_width = display_thumb_width + 5  # Cover potential overflow
        self.timeline_canvas.create_rectangle(
            x + width, y, x + width + mask_width, y + height,
            fill=self.COLORS['timeline_bg'], outline=''
        )

    def _draw_audio_waveform(self, x, y, width, height, clip, color, orig_x=None, orig_width=None):
        """Draw audio waveform visualization with real waveform data.

        Args:
            x, y, width, height: Visible/clipped drawing area
            clip: The clip object
            color: Base color for the waveform
            orig_x: Original (unclipped) x position of the clip (can be negative)
            orig_width: Original (unclipped) full width of the clip
        """
        wave_color = self._lighten_color(color, 0.6)
        fill_color = self._lighten_color(color, 0.3)
        center_y = y + height // 2

        # Use original dimensions if provided, otherwise fall back to visible
        if orig_x is None:
            orig_x = x
        if orig_width is None:
            orig_width = width

        # Draw center line (minimal padding to allow edge-to-edge waveform)
        self.timeline_canvas.create_line(x + 1, center_y, x + width - 1, center_y,
                                         fill=self._darken_color(color, 0.3), width=1)

        if width < 10:
            return

        max_wave_height = max(4, int(height * 0.4))

        # Try to get real waveform data
        waveform_data = self._get_clip_waveform(clip.path)

        if waveform_data and len(waveform_data) > 0:
            # Check if waveform data seems incomplete and needs re-extraction
            # Waveform should have ~4 samples per second of original file duration (max 1000)
            original_duration = getattr(clip, 'original_duration', None) or clip.duration
            expected_samples = min(1000, max(100, int(original_duration * 4)))
            if len(waveform_data) < expected_samples * 0.5:  # Less than 50% of expected
                # Waveform was likely extracted with wrong duration or old format, queue re-extraction
                self._invalidate_and_reextract_waveform(clip)

            # Calculate which portion of the waveform is visible
            # visible_start_ratio: what fraction of the clip is off-screen to the left
            # visible_end_ratio: what fraction of the clip is visible through the right edge
            if orig_width > 0:
                visible_start_ratio = max(0, (x - orig_x) / orig_width)
                visible_end_ratio = min(1, (x + width - orig_x) / orig_width)
            else:
                visible_start_ratio = 0
                visible_end_ratio = 1

            # Map visible portion to waveform data indices
            # Waveform data is stretched/compressed to fill the displayed clip
            data_start_idx = int(visible_start_ratio * len(waveform_data))
            data_end_idx = int(visible_end_ratio * len(waveform_data))
            data_start_idx = max(0, min(data_start_idx, len(waveform_data) - 1))
            data_end_idx = max(data_start_idx + 1, min(data_end_idx, len(waveform_data)))

            visible_data = waveform_data[data_start_idx:data_end_idx]

            if len(visible_data) < 2:
                return

            # Use real waveform data - draw as filled polygon for smooth waveform look
            num_points = min(int(width), len(visible_data))
            if num_points < 2:
                return

            # Build polygon points for upper and lower waveform (edge-to-edge)
            # Clamp all x-coordinates to stay within clip boundaries
            upper_points = []
            lower_points = []
            clip_right = x + width - 1  # Right boundary (exclusive)

            for i in range(num_points):
                wx = x + int(i * width / num_points)
                # Clamp to clip boundaries
                wx = max(x, min(wx, clip_right))

                # Map to visible waveform data index
                data_idx = int(i * len(visible_data) / num_points)
                data_idx = min(data_idx, len(visible_data) - 1)

                # Get peak value and scale to wave height
                peak = visible_data[data_idx]
                wave_h = max(1, int(peak * max_wave_height))

                upper_points.append((wx, center_y - wave_h))
                lower_points.append((wx, center_y + wave_h))

            # Draw filled polygon for waveform (smooth appearance)
            if len(upper_points) >= 2:
                # Add explicit end points at clip boundary for clean edges
                last_wave_h = max(1, int(visible_data[-1] * max_wave_height)) if visible_data else 1
                upper_points.append((clip_right, center_y - last_wave_h))
                lower_points.append((clip_right, center_y + last_wave_h))

                # Combine upper and lower points to form a closed polygon
                polygon_points = []
                for px, py in upper_points:
                    # Final clamp to ensure nothing exceeds boundary
                    px = max(x, min(px, clip_right))
                    polygon_points.extend([px, py])
                for px, py in reversed(lower_points):
                    px = max(x, min(px, clip_right))
                    polygon_points.extend([px, py])

                self.timeline_canvas.create_polygon(polygon_points, fill=fill_color,
                                                   outline=wave_color, width=1)
        else:
            # Queue waveform extraction and use fallback visualization
            self._queue_waveform_extraction(clip)

            # Fallback: draw simple bars while loading (edge-to-edge)
            clip_hash = hash(clip.path)
            step = max(2, int(width / 100))

            for wx in range(int(x + 1), int(x + width - 1), step):
                seed = clip_hash + wx
                wave_h = 2 + abs(hash(str(seed)) % int(height * 0.35))
                self.timeline_canvas.create_line(wx, center_y - wave_h, wx, center_y + wave_h,
                                                 fill=wave_color, width=max(1, step - 1))

    def _draw_video_audio_waveform(self, x, y, width, height, clip, color):
        """Draw audio waveform visualization for video clips (compact version)."""
        # Use a distinct color for video audio (slightly blue-tinted)
        wave_color = '#55aaff'  # Blue-ish for audio track
        fill_color = '#334466'  # Darker fill for polygon
        bg_color = self._darken_color(color, 0.5)

        # Draw audio section background (edge-to-edge)
        self.timeline_canvas.create_rectangle(x, y, x + width, y + height,
                                              fill=bg_color, outline='')

        # Draw separator line between video and audio
        self.timeline_canvas.create_line(x + 1, y, x + width - 1, y,
                                         fill='#333333', width=1)

        if width < 10 or height < 8:
            return

        center_y = y + height // 2

        # Draw center line (quieter, edge-to-edge)
        self.timeline_canvas.create_line(x + 1, center_y, x + width - 1, center_y,
                                         fill='#333344', width=1)

        max_wave_height = max(2, int(height * 0.4))

        # Try to get real waveform data
        waveform_data = self._get_clip_waveform(clip.path)

        if waveform_data and len(waveform_data) > 0:
            # Use real waveform data - draw as filled polygon for smooth waveform look
            num_points = min(int(width), len(waveform_data))
            if num_points < 2:
                return

            # Build polygon points for upper and lower waveform (edge-to-edge)
            # Clamp all x-coordinates to stay within clip boundaries
            upper_points = []
            lower_points = []
            clip_right = x + width - 1

            for i in range(num_points):
                wx = x + int(i * width / num_points)
                # Clamp to clip boundaries
                wx = max(x, min(wx, clip_right))

                # Map to waveform data index
                data_idx = int(i * len(waveform_data) / num_points)
                data_idx = min(data_idx, len(waveform_data) - 1)

                # Get normalized peak value (0-1) and scale to height
                peak = waveform_data[data_idx]
                wave_h = max(1, int(peak * max_wave_height))

                upper_points.append((wx, center_y - wave_h))
                lower_points.append((wx, center_y + wave_h))

            # Draw filled polygon for waveform (smooth appearance)
            if len(upper_points) >= 2:
                # Add explicit end points at clip boundary for clean edges
                last_wave_h = max(1, int(waveform_data[-1] * max_wave_height)) if waveform_data else 1
                upper_points.append((clip_right, center_y - last_wave_h))
                lower_points.append((clip_right, center_y + last_wave_h))

                polygon_points = []
                for px, py in upper_points:
                    px = max(x, min(px, clip_right))
                    polygon_points.extend([px, py])
                for px, py in reversed(lower_points):
                    px = max(x, min(px, clip_right))
                    polygon_points.extend([px, py])

                self.timeline_canvas.create_polygon(polygon_points, fill=fill_color,
                                                   outline=wave_color, width=1)
        else:
            # Queue waveform extraction and use fallback hash-based visualization
            self._queue_waveform_extraction(clip)

            # Fallback: generate waveform based on clip hash (edge-to-edge)
            clip_hash = hash(clip.path + "_audio")
            step = max(1, int(width / 150))

            for wx in range(int(x + 1), int(x + width - 1), step):
                seed = clip_hash + wx * 7
                wave_h = 1 + abs(hash(str(seed)) % max_wave_height)
                self.timeline_canvas.create_line(
                    wx, center_y - wave_h, wx, center_y + wave_h,
                    fill='#445566', width=max(1, step - 1)  # Dimmer for placeholder
                )

        # Draw small audio icon indicator
        if width > 60:
            # Small speaker icon at left
            icon_x = x + 6
            icon_y = y + height // 2
            icon_size = min(6, height // 2 - 1)

            # Speaker body (small rectangle)
            self.timeline_canvas.create_rectangle(
                icon_x, icon_y - icon_size // 2,
                icon_x + icon_size // 2, icon_y + icon_size // 2,
                fill='#888888', outline=''
            )
            # Speaker cone (small triangle)
            self.timeline_canvas.create_polygon(
                icon_x + icon_size // 2, icon_y - icon_size // 2,
                icon_x + icon_size, icon_y - icon_size,
                icon_x + icon_size, icon_y + icon_size,
                icon_x + icon_size // 2, icon_y + icon_size // 2,
                fill='#888888', outline=''
            )

    def _darken_color(self, hex_color, factor):
        """Darken a hex color by a factor (0-1)."""
        try:
            hex_color = hex_color.lstrip('#')
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            r = int(r * (1 - factor))
            g = int(g * (1 - factor))
            b = int(b * (1 - factor))
            return f'#{r:02x}{g:02x}{b:02x}'
        except:
            return hex_color

    def _lighten_color(self, hex_color, factor):
        """Lighten a hex color by a factor (0-1)."""
        try:
            hex_color = hex_color.lstrip('#')
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            r = min(255, int(r + (255 - r) * factor))
            g = min(255, int(g + (255 - g) * factor))
            b = min(255, int(b + (255 - b) * factor))
            return f'#{r:02x}{g:02x}{b:02x}'
        except:
            return hex_color

    def _draw_ruler(self, width, pps, duration):
        """Draw timeline ruler."""
        # Ruler background
        self.timeline_canvas.create_rectangle(0, 0, width, 30, fill=self.COLORS['bg_card'], outline='')

        # Time markers - calculate interval based on pixels per second
        # Ensure labels don't overlap (need ~60-80 pixels between labels)
        min_label_spacing = 70  # pixels between labels

        # Calculate seconds needed for minimum spacing
        if pps > 0:
            min_seconds_per_label = min_label_spacing / pps
        else:
            min_seconds_per_label = 60

        # Choose nice interval values (1s, 5s, 10s, 30s, 1m, 5m, 10m, 30m, 1h)
        intervals = [1, 5, 10, 30, 60, 300, 600, 1800, 3600]
        interval = 1
        for iv in intervals:
            if iv >= min_seconds_per_label:
                interval = iv
                break
        else:
            interval = 3600  # Default to 1 hour if nothing else fits

        # Only the visible window (offset .. offset + width/pps)
        first = max(0, int(self.timeline_offset // interval) * interval)
        last = min(int(duration) + interval, int(self.timeline_offset + width / max(pps, 1e-6)) + interval)
        for t in range(first, last + 1, interval):
            x = (t - self.timeline_offset) * pps
            if 0 <= x < width:
                self.timeline_canvas.create_line(x, 20, x, 30, fill=self.COLORS['text_dim'])

                # Time label
                mins, secs = divmod(t, 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    time_str = f"{hours}:{mins:02d}:{secs:02d}"
                else:
                    time_str = f"{mins}:{secs:02d}"
                self.timeline_canvas.create_text(x, 12, text=time_str,
                                                 fill=self.COLORS['text_dim'], font=('Segoe UI', 8))

    def _fit_timeline(self):
        """Fit all clips in the timeline view."""
        if not self.clips:
            self.timeline_zoom = 1.0
            self.timeline_offset = 0.0
        else:
            # Get clips with duration
            valid_clips = [c for c in self.clips if c.duration > 0]
            if valid_clips:
                max_end = max((c.timeline_start + c.duration) for c in valid_clips)
                min_start = min(c.timeline_start for c in valid_clips)
                total_duration = max_end - min_start

                # Get canvas width
                w = self.timeline_canvas.winfo_width() - 100  # margin for labels
                if w > 0 and total_duration > 0:
                    # Calculate zoom to fit all clips
                    # pixels_per_second = 50 * timeline_zoom, so:
                    # We want: w = total_duration * 50 * timeline_zoom
                    # Therefore: timeline_zoom = w / (total_duration * 50)
                    self.timeline_zoom = w / (total_duration * 50)
                    self.timeline_zoom = max(0.01, min(10.0, self.timeline_zoom))  # Clamp to 1%-1000%
                    self.timeline_offset = min_start
                else:
                    self.timeline_zoom = 1.0
                    self.timeline_offset = 0.0
            else:
                self.timeline_zoom = 1.0
                self.timeline_offset = 0.0
        self._update_zoom_label()
        self._draw_timeline()

    def _on_timeline_scroll(self, event):
        """Vertical zoom with mouse wheel."""
        if event.delta > 0:
            self._zoom_timeline(1.1, event)
        else:
            self._zoom_timeline(0.9, event)

    def _on_h_scroll(self, *args):
        """Handle horizontal scrollbar commands and sync timeline_offset.

        Scrollbar passes commands like:
        - ("moveto", fraction) - when dragging the scrollbar
        - ("scroll", number, what) - when clicking scroll arrows/trough

        We calculate timeline_offset directly WITHOUT using canvas xview.
        """
        # Skip during scrubbing - playhead position is authoritative
        if getattr(self, '_is_scrubbing', False):
            return

        try:
            scrollregion = self.timeline_canvas.cget('scrollregion')
            if not scrollregion or not hasattr(self, 'pixels_per_second') or self.pixels_per_second <= 0:
                return

            coords = [float(x) for x in scrollregion.split()]
            total_width = coords[2] - coords[0]
            if total_width <= 0:
                return

            canvas_width = self.timeline_canvas.winfo_width()
            max_offset_pixels = max(0, total_width - canvas_width)
            max_offset_seconds = max_offset_pixels / self.pixels_per_second

            if args[0] == 'moveto':
                # Dragging scrollbar - fraction is the new position
                fraction = float(args[1])
                scroll_x = fraction * total_width
                self.timeline_offset = max(0, min(max_offset_seconds, scroll_x / self.pixels_per_second))
            elif args[0] == 'scroll':
                # Scroll arrows or trough click
                amount = int(args[1])
                unit = args[2]
                if unit == 'units':
                    # Scroll by small amount (arrow click)
                    scroll_seconds = amount * 2.0 / max(0.01, self.timeline_zoom)
                else:
                    # Scroll by page (trough click)
                    visible_seconds = canvas_width / self.pixels_per_second
                    scroll_seconds = amount * visible_seconds * 0.9
                self.timeline_offset = max(0, min(max_offset_seconds, self.timeline_offset + scroll_seconds))

            # Redraw timeline with new offset and update scrollbar
            self._draw_timeline()
            self._sync_scrollbar_to_offset()
        except Exception:
            pass  # Silently handle scroll errors

    def _on_timeline_h_scroll(self, event):
        """Horizontal scroll with Shift+mouse wheel."""
        # Skip during scrubbing
        if getattr(self, '_is_scrubbing', False):
            return
        # Use _on_h_scroll to keep timeline_offset in sync
        if event.delta > 0:
            self._on_h_scroll('scroll', -3, 'units')
        else:
            self._on_h_scroll('scroll', 3, 'units')

    def _sync_scrollbar_to_offset(self):
        """Sync the horizontal scrollbar position to match timeline_offset.

        Updates the scrollbar directly WITHOUT scrolling the canvas internally.
        This avoids double-offset issues since we use timeline_offset for rendering.
        """
        try:
            if not hasattr(self, 'timeline_h_scroll'):
                return

            scrollregion = self.timeline_canvas.cget('scrollregion')
            if scrollregion and hasattr(self, 'pixels_per_second') and self.pixels_per_second > 0:
                coords = [float(x) for x in scrollregion.split()]
                total_width = coords[2] - coords[0]
                canvas_width = self.timeline_canvas.winfo_width()

                if total_width > 0 and canvas_width > 0:
                    # Calculate scroll position based on timeline_offset
                    scroll_x = self.timeline_offset * self.pixels_per_second
                    first = scroll_x / total_width
                    last = (scroll_x + canvas_width) / total_width
                    first = max(0, min(1, first))
                    last = max(0, min(1, last))
                    # Update scrollbar directly without scrolling canvas
                    self.timeline_h_scroll.set(first, last)
        except Exception:
            pass  # Silently ignore scrollbar sync errors

    def _ensure_playhead_visible(self):
        """Auto-scroll timeline to keep the playhead visible during playback.

        Uses timeline_offset adjustment for smooth scrolling during playback.
        OPTIMIZED: Uses deferred redraw to avoid stuttering.
        """
        if not hasattr(self, 'pixels_per_second') or self.pixels_per_second <= 0:
            return

        try:
            canvas_width = self.timeline_canvas.winfo_width()
            if canvas_width <= 0:
                return

            # Calculate visible time range
            visible_duration = canvas_width / self.pixels_per_second
            visible_start = self.timeline_offset
            visible_end = visible_start + visible_duration

            # Calculate maximum offset (can't scroll too far past end of content)
            if self.clips:
                total_duration = max(c.timeline_start + c.duration for c in self.clips)
            else:
                total_duration = 0
            # Add 20% padding at the end for visual comfort
            end_padding = visible_duration * 0.2
            max_offset = max(0, total_duration - visible_duration + end_padding)

            # Check if playhead is within visible area with margin
            margin = visible_duration * 0.15  # 15% margin
            needs_scroll = False
            new_offset = self.timeline_offset

            if self.playhead_position < visible_start + margin:
                # Playhead is too far left - scroll left
                new_offset = max(0, self.playhead_position - margin)
                needs_scroll = True
            elif self.playhead_position > visible_end - margin:
                # Playhead is too far right - scroll right (but stop at end)
                new_offset = self.playhead_position - visible_duration + margin
                new_offset = min(max_offset, new_offset)
                needs_scroll = True

            if needs_scroll and new_offset != self.timeline_offset:
                self.timeline_offset = new_offset
                # OPTIMIZATION: Schedule deferred redraw instead of immediate
                # This coalesces multiple scroll events into one redraw
                self._schedule_deferred_redraw()

        except Exception:
            pass  # Silently handle scroll errors for smooth playback

    def _schedule_deferred_redraw(self):
        """Schedule a deferred timeline redraw to coalesce multiple updates."""
        # Cancel any pending deferred redraw
        if self._deferred_redraw_id:
            try:
                self.root.after_cancel(self._deferred_redraw_id)
            except:
                pass
        # Schedule redraw after 16ms (one frame) - fast enough to avoid visual disconnect
        # between playhead and clips, but still coalesces rapid offset changes
        self._deferred_redraw_id = self.root.after(16, self._execute_deferred_redraw)

    def _execute_deferred_redraw(self):
        """Execute the deferred timeline redraw."""
        self._deferred_redraw_id = None
        try:
            self._draw_timeline()
            self._sync_scrollbar_to_offset()
        except:
            pass

    def _draw_playhead(self, y_start, total_height):
        """Draw the playhead indicator."""
        if not hasattr(self, 'pixels_per_second'):
            return

        # Store total height for efficient updates
        self.timeline_total_height = total_height

        # No label offset needed - track headers are separate panel
        x = (self.playhead_position - self.timeline_offset) * self.pixels_per_second

        # Playhead line - store item ID for efficient updates
        self.playhead_item_line = self.timeline_canvas.create_line(
            x, 30, x, total_height,
            fill=self.COLORS['accent'], width=2, tags='playhead'
        )

        # Playhead handle (triangle at top) - store item ID
        self.playhead_item_handle = self.timeline_canvas.create_polygon(
            x-8, 25, x+8, 25, x, 35,
            fill=self.COLORS['accent'], outline='white', tags='playhead'
        )

        # Draw snap indicator if active
        if self.snap_indicator_pos is not None:
            self._draw_snap_indicator(total_height)

    def _update_playhead_only(self):
        """Efficiently update only the playhead position without redrawing entire timeline."""
        if not hasattr(self, 'pixels_per_second') or self.pixels_per_second <= 0:
            return  # Timeline not ready yet

        x = (self.playhead_position - self.timeline_offset) * self.pixels_per_second
        total_height = getattr(self, 'timeline_total_height', 500)

        # Try to update existing playhead items
        try:
            # Check if playhead items exist on canvas
            playhead_items = self.timeline_canvas.find_withtag('playhead')
            if playhead_items and self.playhead_item_line:
                self.timeline_canvas.coords(self.playhead_item_line, x, 30, x, total_height)
                self.timeline_canvas.coords(self.playhead_item_handle, x-8, 25, x+8, 25, x, 35)
                # Ensure playhead stays on top of other elements
                self.timeline_canvas.tag_raise('playhead')
            else:
                # Delete old playhead and create new one (faster than full redraw)
                self.timeline_canvas.delete('playhead')
                self.playhead_item_line = self.timeline_canvas.create_line(
                    x, 30, x, total_height,
                    fill=self.COLORS['accent'], width=2, tags='playhead'
                )
                self.playhead_item_handle = self.timeline_canvas.create_polygon(
                    x-8, 25, x+8, 25, x, 35,
                    fill=self.COLORS['accent'], outline='white', tags='playhead'
                )
        except tk.TclError:
            pass  # Canvas might be in invalid state, skip this frame

    def _update_selection_only(self):
        """Efficiently update only selection highlights without redrawing entire timeline.

        Uses cached clip positions from _draw_clip to redraw only selection rectangles
        and trim handles. Much faster than full timeline redraw for selection changes.
        """
        try:
            # Delete existing selection items
            self.timeline_canvas.delete('selection')
            self.timeline_canvas.delete('trim_handle')

            canvas_width = self.timeline_canvas.winfo_width()

            # Redraw selection highlights and trim handles for selected clips
            for clip in self.selected_clips:
                clip_id = id(clip)
                if clip_id not in self._clip_draw_positions:
                    continue  # Clip not visible/drawn, skip

                x, y, width = self._clip_draw_positions[clip_id]
                clip_top = y + 2
                clip_bottom = y + self.track_height - 2

                # Clip to visible area
                draw_x = max(0, x)
                draw_end_x = min(x + width, canvas_width)
                draw_width = draw_end_x - draw_x

                if draw_width <= 0:
                    continue  # Not visible

                # Selection highlight rectangle
                self.timeline_canvas.create_rectangle(
                    draw_x, y + 1, draw_end_x, y + self.track_height - 1,
                    fill='', outline='#ffffff', width=2,
                    tags=('selection', f'sel_{clip_id}')
                )

                # Trim handles (only if timeline unlocked)
                handle_width = 6
                handle_color = '#ffaa00'

                # Left trim handle (IN point) - only if left edge is visible
                if x >= 0:
                    self.timeline_canvas.create_rectangle(
                        draw_x, clip_top, draw_x + handle_width, clip_bottom,
                        fill=handle_color, outline='#ffffff', width=1,
                        tags=('trim_handle', f'trim_in_{clip_id}')
                    )

                # Right trim handle (OUT point) - only if right edge is visible
                if x + width <= canvas_width:
                    self.timeline_canvas.create_rectangle(
                        draw_end_x - handle_width, clip_top, draw_end_x, clip_bottom,
                        fill=handle_color, outline='#ffffff', width=1,
                        tags=('trim_handle', f'trim_out_{clip_id}')
                    )

            # Ensure selection stays on top but below playhead
            self.timeline_canvas.tag_raise('selection')
            self.timeline_canvas.tag_raise('trim_handle')
            self.timeline_canvas.tag_raise('playhead')

        except tk.TclError:
            pass  # Canvas might be in invalid state, skip

    def _draw_snap_indicator(self, total_height):
        """Draw the snap indicator line."""
        if self.snap_indicator_pos is None:
            return

        x = (self.snap_indicator_pos - self.timeline_offset) * self.pixels_per_second

        # Different colors based on snap type
        snap_colors = {
            'playhead': '#ff6b6b',
            'marker': '#ffe66d',
            'clip_start': '#4ecdc4',
            'clip_end': '#4ecdc4',
            'in_point': '#ff9ff3',
            'out_point': '#ff9ff3',
        }
        color = snap_colors.get(self.snap_indicator_type, '#ffffff')

        # Draw dashed snap line
        self.timeline_canvas.create_line(
            x, 30, x, total_height,
            fill=color, width=1, dash=(4, 4), tags='snap_indicator'
        )

        # Small label showing snap type
        label_text = {
            'playhead': 'PLAYHEAD',
            'marker': 'MARKER',
            'clip_start': 'CLIP',
            'clip_end': 'CLIP END',
            'in_point': 'IN',
            'out_point': 'OUT',
        }.get(self.snap_indicator_type, 'SNAP')

        self.timeline_canvas.create_text(
            x, 42, text=label_text, fill=color,
            font=('Segoe UI', 6, 'bold'), anchor='n', tags='snap_indicator'
        )

    def _on_timeline_click(self, event):
        """Handle click on timeline to move playhead and trigger preview."""
        if not self.clips or not hasattr(self, 'pixels_per_second'):
            return

        # Use widget x directly since our rendering uses timeline_offset, not canvas xview
        widget_x = event.x
        widget_y = self.timeline_canvas.canvasy(event.y)  # Keep canvasy for vertical scroll
        ruler_height = 30

        # Check if clicking in ruler area - always allow playhead movement
        if widget_y < ruler_height:
            # Soft-pause playback while scrubbing (keeps the audio stream and WAV
            # cache alive); release auto-resumes from the new position
            self._resume_after_scrub = self.timeline_playing
            if self.timeline_playing:
                self._pause_timeline_for_scrub()
            self._is_scrubbing = True
            time_pos = widget_x / self.pixels_per_second + self.timeline_offset

            # Unified seek: clamps, syncs clock/scheduler/mixer, updates visuals
            self._seek_playhead(time_pos)
            self._scrub_preview_update()
            return

        # Below ruler area - a fresh non-ruler press means no scrub is in
        # progress; clear any scrub state left latched by a swallowed
        # ButtonRelease (e.g. a modal dialog grabbing mid-drag), so a later
        # release can't spuriously auto-resume playback.
        self._is_scrubbing = False
        self._resume_after_scrub = False

        # Check if clicking on a clip or blank space
        clicked_clip = self._find_clip_at_position(widget_x, widget_y)

        if clicked_clip:
            # Clicked on a clip - select it
            if event.state & 0x4:  # Ctrl held
                # Toggle selection
                if clicked_clip in self.selected_clips:
                    self.selected_clips.remove(clicked_clip)
                else:
                    self.selected_clips.append(clicked_clip)
            else:
                # Single select
                self.selected_clips = [clicked_clip]
            self.selected_clip = clicked_clip if self.selected_clips else None
            self._update_selection_only()  # Optimized: only update selection, not full redraw
            self._update_clip_selection_ui()

            # Check if clicking on trim handle (only if timeline unlocked)
            if not self.timeline_locked:
                trim_result = self._check_trim_handle(widget_x, widget_y)
                if trim_result:
                    # Pause playback if running (trimming modifies clips)
                    if self.timeline_playing:
                        self._toggle_timeline_playback()  # Pause playback
                    clip, mode = trim_result
                    self.trim_mode = mode
                    self.trim_clip = clip
                    self.trim_start_x = widget_x
                    self.trim_original_start = clip.timeline_start
                    self.trim_original_duration = clip.duration
                    return
        else:
            # Clicked on blank space - deselect all clips
            self.selected_clips = []
            self.selected_clip = None
            self._update_selection_only()  # Optimized: only clear selection, not full redraw
            self._update_clip_selection_ui()

    def _on_timeline_drag(self, event):
        """Handle drag on timeline (scrubbing or trimming) - OPTIMIZED."""
        if self.trim_mode and self.trim_clip:
            self._handle_trim_drag(event)
        elif self._is_scrubbing:
            # Only allow scrubbing if it started from the ruler area
            if not self.clips or not hasattr(self, 'pixels_per_second'):
                return

            # If playback was restarted mid-drag (Space), re-enter the soft
            # pause: dragging must always own the playhead, otherwise the tick
            # loop rubber-bands it back to the wall-clock position every 16ms.
            if self.timeline_playing:
                self._pause_timeline_for_scrub()
                self._resume_after_scrub = True

            # pointer dragged outside the timeline counts as its edge
            widget_x = max(0, min(event.x, self.timeline_canvas.winfo_width()))

            # Auto-scroll FIRST when dragging near edges (returns True if scrolled)
            scrolled = self._auto_scroll_during_scrub(widget_x)

            # THEN calculate playhead position with potentially updated offset
            # This ensures playhead stays under cursor after auto-scroll
            time_pos = widget_x / self.pixels_per_second + self.timeline_offset
            time_pos = max(0, time_pos)
            self.playhead_position = time_pos

            # FRAME BUFFER SYSTEM: Only sync clock during scrub (skip heavy decoder seek)
            if self.use_frame_buffer_system and self.audio_master_clock:
                self.audio_master_clock.seek(time_pos)
            # Note: Don't call frame_display_scheduler.seek() during drag - too heavy

            # Now draw with the CORRECT playhead position
            if scrolled:
                # Throttle full redraws during auto-scroll to 30fps for smoothness
                current_time = time.perf_counter()
                if current_time - self._last_auto_scroll_time >= 0.033:  # ~30fps
                    self._last_auto_scroll_time = current_time
                    # Full redraw needed because clips shifted
                    self._draw_timeline()
                    self._sync_scrollbar_to_offset()
                else:
                    # Between redraws, just update playhead
                    self._update_playhead_only()
            else:
                # Just update playhead position
                self._update_playhead_only()

            # Throttled preview update during scrub
            self._scrub_preview_update()

    def _auto_scroll_during_scrub(self, widget_x):
        """Auto-scroll timeline when scrubbing near edges - faster near edge.

        Returns True if scrolled, False otherwise. Does NOT redraw - caller handles that.
        """
        try:
            canvas_width = self.timeline_canvas.winfo_width()
            edge_zone = 60  # Pixels from edge to trigger scroll
            # Real time since the last auto-scroll step (mouse events arrive
            # at 60-1000/s; scrolling per EVENT made the speed depend on the
            # mouse and, scaled by 1/zoom, threw the view across the project)
            now = time.perf_counter()
            last = getattr(self, '_scrub_scroll_last', 0.0)
            dt = min(0.05, max(0.0, now - last)) if now - last < 0.5 else 0.016
            self._scrub_scroll_last = now

            # Calculate maximum offset (can't scroll too far past end of content)
            if self.clips:
                total_duration = max(c.timeline_start + c.duration for c in self.clips)
            else:
                total_duration = 0
            visible_duration = canvas_width / self.pixels_per_second if self.pixels_per_second > 0 else 0
            # Add 20% padding at the end for visual comfort
            end_padding = visible_duration * 0.2
            max_offset = max(0, total_duration - visible_duration + end_padding)

            # At most 1.5 screen-widths per second at the very edge, gentler
            # further in - the same feel at every zoom level (the old fixed
            # 2/zoom seconds PER MOUSE EVENT was ~1,000 s/s at 17% zoom)
            max_speed = visible_duration * 1.5  # timeline seconds per real second

            if widget_x < edge_zone:
                # Near left edge - scroll left
                if self.timeline_offset <= 0:
                    return False
                ratio = min(1.0, (edge_zone - widget_x) / edge_zone)  # beyond the edge counts as the edge
                scroll_amount = (ratio ** 2) * max_speed * dt
                self.timeline_offset = max(0, self.timeline_offset - scroll_amount)
                return True
            elif widget_x > canvas_width - edge_zone:
                # Near right edge - scroll right (but stop at end of content)
                if self.timeline_offset >= max_offset:
                    return False  # Already at the end, don't scroll further
                ratio = min(1.0, (widget_x - (canvas_width - edge_zone)) / edge_zone)
                scroll_amount = (ratio ** 2) * max_speed * dt
                self.timeline_offset = min(max_offset, self.timeline_offset + scroll_amount)
                return True
            return False
        except Exception:
            return False

    def _cancel_scrub_rest(self):
        if getattr(self, '_scrub_rest_id', None):
            try:
                self.root.after_cancel(self._scrub_rest_id)
            except Exception:
                pass
            self._scrub_rest_id = None

    def _scrub_rest_still(self):
        """The pointer rested on a clip that isn't open yet: show it now."""
        self._scrub_rest_id = None
        target = getattr(self, '_scrub_rest_target', None)
        if target and self._is_scrubbing:
            clip, pos = target
            if self._direct_ok(clip):
                self._direct_show_still(clip, pos)

    def _scrub_preview_trailing(self):
        """Deferred scrub render for the final position of a drag."""
        self._scrub_trailing_id = None
        if self._is_scrubbing:
            self._scrub_preview_update()

    def _scrub_preview_update(self):
        """Throttled preview update during scrubbing for smooth performance."""
        # Only update every ~33ms (30fps) for responsive scrubbing
        current_time = time.perf_counter()

        wait = 0.033 - (current_time - self._last_scrub_update)
        if wait > 0:
            # Trailing edge: without it the moves inside the last 33ms window
            # were never rendered, so when the drag stopped VLC showed a frame
            # behind the playhead (measured 1.8s of media on a fast drag).
            if not getattr(self, '_scrub_trailing_id', None):
                self._scrub_trailing_id = self.root.after(
                    int(wait * 1000) + 1, self._scrub_preview_trailing)
            return

        self._last_scrub_update = current_time
        self._update_playhead_tc_display()

        # Find topmost visible video clip at playhead (layer system)
        clip = self._get_topmost_visible_video_clip(self.playhead_position)

        if clip:
            position_in_clip = self.playhead_position - clip.timeline_start
            # Add trim offset for actual file position
            split_key = clip.path + f"_split_{id(clip)}"
            in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(clip.path, 0))
            position_in_file = position_in_clip + in_offset

            # Direct GPU engine (no proxy yet, GPU decodes it in hardware)
            if self._direct_ok(clip):
                self.selected_clip = clip
                if self.hw_player.is_warm(clip.path):
                    self._cancel_scrub_rest()
                    self._direct_show_still(clip, position_in_file)
                else:
                    # A clip not open yet costs a cold Intel start (~0.7 s,
                    # memory, a driver-level UI stall). Dragging across dozens
                    # of clips started one per clip: up to 1.5 s freezes and
                    # ~1.4 GB per sweep. Open it only when the pointer rests.
                    self._scrub_rest_target = (clip, position_in_file)
                    self._cancel_scrub_rest()
                    self._scrub_rest_id = self.root.after(150, self._scrub_rest_still)
                return
            self._direct_release_display()

            # Update VLC to show frame
            if self.use_vlc and self.vlc_player:
                if self.vlc_media_path != clip.path or self._vlc_switch_busy:
                    # Different clip (or a switch still in flight): libVLC's
                    # stop()/set_media() block until the old decoder is torn
                    # down - measured 0.46-0.92s per clip boundary on the UI
                    # thread, the "clunky" scrub. Hand it to the worker.
                    self.selected_clip = clip
                    self._vlc_switch_media_for_scrub(clip.path, position_in_file)
                else:
                    # Same clip - VLC needs brief play to render frame when paused
                    # Just setting time while paused doesn't update the display
                    self._vlc_seek_and_render(position_in_file)

    def _on_timeline_release(self, event):
        """Handle mouse release on timeline."""
        was_scrubbing = self._is_scrubbing
        self._is_scrubbing = False

        if self.trim_mode and self.trim_clip:
            # Finalize trim
            self.trim_mode = None
            self.trim_clip = None
            # Clear snap indicator
            self.snap_indicator_pos = None
            self.snap_indicator_type = None
            self._draw_timeline()
        else:
            # Only pause VLC after actual scrubbing ended (not just clicking)
            # and only if timeline is not playing
            if (was_scrubbing and self.use_vlc and self.vlc_player and not self.timeline_playing
                    and not self._vlc_switch_busy):  # (the worker pauses VLC itself; blocking here froze up to 2.3 s)
                # Cancel any pending pause callbacks first
                if hasattr(self, '_vlc_pause_after_id') and self._vlc_pause_after_id:
                    try:
                        self.root.after_cancel(self._vlc_pause_after_id)
                    except Exception:
                        pass
                    self._vlc_pause_after_id = None
                # Force pause VLC immediately - use set_pause(1) not pause()
                # because pause() is a TOGGLE which would unpause if already paused!
                try:
                    self.vlc_player.set_pause(1)  # Explicitly set pause state
                except Exception:
                    pass

            # Released inside the 33ms throttle window: the trailing render
            # won't fire any more (no longer scrubbing), so show the final
            # position now - otherwise VLC stays on an earlier frame
            if was_scrubbing and getattr(self, '_scrub_trailing_id', None):
                try:
                    self.root.after_cancel(self._scrub_trailing_id)
                except Exception:
                    pass
                self._scrub_trailing_id = None
                if not self._resume_after_scrub:
                    self._last_scrub_update = 0
                    self._scrub_preview_update()

            # Auto-resume playback from the new position if scrub interrupted it
            if was_scrubbing and self._resume_after_scrub:
                self._resume_after_scrub = False
                self._resume_timeline_after_scrub()
                return

            # Update full preview on release
            self._update_playhead_preview()
            self._schedule_direct_warm()

    def _on_timeline_motion(self, event):
        """Handle mouse motion for cursor changes - OPTIMIZED."""
        if not self.clips or not hasattr(self, 'pixels_per_second'):
            return

        # Use widget x directly since our rendering uses timeline_offset, not canvas xview
        widget_x = event.x
        widget_y = self.timeline_canvas.canvasy(event.y)  # Keep canvasy for vertical scroll

        trim_result = self._check_trim_handle(widget_x, widget_y)
        new_cursor = 'sb_h_double_arrow' if trim_result else ''

        # OPTIMIZATION: Only change cursor if it actually needs to change
        # configure() is expensive and causes widget updates even with same value
        if not hasattr(self, '_current_cursor'):
            self._current_cursor = ''
        if new_cursor != self._current_cursor:
            self._current_cursor = new_cursor
            self.timeline_canvas.configure(cursor=new_cursor)

    def _check_trim_handle(self, x, y):
        """Check if mouse is over a trim handle. Returns (clip, 'in'/'out') or None."""
        handle_width = 8  # Slightly larger hit area

        for clip in self.selected_clips:
            if clip.duration <= 0:
                continue

            clip_x = (clip.timeline_start - self.timeline_offset) * self.pixels_per_second
            clip_width = clip.duration * self.pixels_per_second
            clip_y = self._get_clip_y_position(clip)

            if clip_y is None:
                continue

            clip_top = clip_y + 2
            clip_bottom = clip_y + self.track_height - 2

            # Check if y is within clip bounds
            if not (clip_top <= y <= clip_bottom):
                continue

            # Check left handle (IN point)
            if clip_x <= x <= clip_x + handle_width:
                return (clip, 'in')

            # Check right handle (OUT point)
            if clip_x + clip_width - handle_width <= x <= clip_x + clip_width:
                return (clip, 'out')

        return None

    def _get_clip_y_position(self, target_clip):
        """Get the Y position of a clip on the timeline."""
        if not self.clips:
            return None
        # Where it was actually drawn (track rows follow track_index, Share Tracks, hidden tracks...)
        pos = self._clip_draw_positions.get(id(target_clip))
        if pos:
            return pos[1]

        ruler_height = 30
        track_spacing = 2
        section_spacing = 15
        y = ruler_height

        video_clips = [c for c in self.clips if not c.is_audio_only]
        audio_clips = [c for c in self.clips if c.is_audio_only]

        video_tracks = {}
        for clip in video_clips:
            track_id = clip.camera_id or "Video"
            if track_id not in video_tracks:
                video_tracks[track_id] = []
            video_tracks[track_id].append(clip)

        audio_tracks = {}
        for clip in audio_clips:
            # Use recording_id + track_number for multi-track recorder files
            if clip.recording_id and clip.track_number is not None:
                if clip.track_number == 0:
                    track_id = f"{clip.recording_id}_LR"  # Stereo mix
                else:
                    track_id = f"{clip.recording_id}_Tr{clip.track_number}"
                if clip.is_ltc_track:
                    track_id += " (LTC)"
            else:
                track_id = clip.camera_id or "Audio"
            if track_id not in audio_tracks:
                audio_tracks[track_id] = []
            audio_tracks[track_id].append(clip)

        # Video tracks
        if self.video_tracks_enabled and video_tracks:
            y += 16  # Section header
            for track_id, clips in video_tracks.items():
                if target_clip in clips:
                    return y
                y += self.track_height + track_spacing
            y += section_spacing

        # Audio tracks
        if self.audio_tracks_enabled and audio_tracks:
            y += 16  # Section header
            for track_id, clips in audio_tracks.items():
                if target_clip in clips:
                    return y
                y += self.track_height + track_spacing

        return None

    def _find_clip_at_position(self, canvas_x, canvas_y):
        """Find which clip (if any) is at the given canvas position."""
        if not self.clips:
            return None

        # Test against what _draw_timeline actually drew. The old rebuild below grouped tracks by camera /
        # recording NAME, while drawing groups them by track_index - clips without a camera ID, shared audio
        # tracks and a 16/18 px header difference made clicks select the wrong clip or none.
        if self._clip_hit_rects:
            for x, y, width, clip in reversed(self._clip_hit_rects):   # topmost (last drawn) first
                if x <= canvas_x < x + width and y <= canvas_y < y + self.track_height:
                    return clip
            return None

        ruler_height = 30
        track_spacing = 2
        section_spacing = 15

        # Convert x to time
        time_pos = canvas_x / self.pixels_per_second + self.timeline_offset

        # Build track layout (same as in _draw_timeline)
        video_clips = [c for c in self.clips if not c.is_audio_only]
        audio_clips = [c for c in self.clips if c.is_audio_only]

        video_tracks = {}
        camera_audio_tracks = {}
        audio_tracks = {}

        for clip in video_clips:
            track_id = clip.camera_id or "Video"
            if track_id not in video_tracks:
                video_tracks[track_id] = []
            video_tracks[track_id].append(clip)
            # Camera audio track
            audio_track_id = track_id + " Audio"
            if audio_track_id not in camera_audio_tracks:
                camera_audio_tracks[audio_track_id] = []
            camera_audio_tracks[audio_track_id].append(clip)

        for clip in audio_clips:
            if clip.recording_id and clip.track_number is not None:
                if clip.track_number == 0:
                    track_id = f"{clip.recording_id}_LR"
                else:
                    track_id = f"{clip.recording_id}_Tr{clip.track_number}"
                if clip.is_ltc_track:
                    track_id += " (LTC)"
            else:
                track_id = clip.camera_id or "Audio"
            if track_id not in audio_tracks:
                audio_tracks[track_id] = []
            audio_tracks[track_id].append(clip)

        y = ruler_height

        # Check video tracks
        if self.video_tracks_enabled and video_tracks:
            y += 18  # Section header
            for track_id in sorted(video_tracks.keys()):
                track_clips = video_tracks[track_id]
                if y <= canvas_y < y + self.track_height:
                    # Check clips in this track
                    for clip in track_clips:
                        if clip.timeline_start <= time_pos < clip.timeline_start + clip.duration:
                            return clip
                y += self.track_height + track_spacing
            y += section_spacing

        # Check audio tracks (camera audio + external audio)
        if self.audio_tracks_enabled and (audio_tracks or camera_audio_tracks):
            y += 18  # Section header

            # Camera audio tracks
            for track_id in sorted(camera_audio_tracks.keys()):
                track_clips = camera_audio_tracks[track_id]
                if y <= canvas_y < y + self.track_height:
                    for clip in track_clips:
                        if clip.timeline_start <= time_pos < clip.timeline_start + clip.duration:
                            return clip
                y += self.track_height + track_spacing

            # External audio tracks
            for track_id in sorted(audio_tracks.keys()):
                track_clips = audio_tracks[track_id]
                if y <= canvas_y < y + self.track_height:
                    for clip in track_clips:
                        if clip.timeline_start <= time_pos < clip.timeline_start + clip.duration:
                            return clip
                y += self.track_height + track_spacing

        return None

    def _update_clip_selection_ui(self):
        """Update UI to reflect current clip selection."""
        # Update clip list selection if available
        if hasattr(self, 'clips_tree') and self.clips_tree:
            self.clips_tree.selection_remove(*self.clips_tree.selection())
            for clip in self.selected_clips:
                if clip in self.clips:
                    idx = self.clips.index(clip)
                    item_id = self.clips_tree.get_children()[idx] if idx < len(self.clips_tree.get_children()) else None
                    if item_id:
                        self.clips_tree.selection_add(item_id)

    def _handle_trim_drag(self, event):
        """Handle dragging a trim handle with snapping support."""
        if not self.trim_clip or not self.trim_mode:
            return

        # Use widget x directly since our rendering uses timeline_offset, not canvas xview
        widget_x = event.x
        delta_x = widget_x - self.trim_start_x
        delta_time = delta_x / self.pixels_per_second

        min_duration = 0.5  # Minimum clip duration in seconds

        # Clear snap indicator by default
        self.snap_indicator_pos = None
        self.snap_indicator_type = None

        if self.trim_mode == 'in':
            # Trimming IN point - changes both start and duration
            new_start = self.trim_original_start + delta_time
            new_duration = self.trim_original_duration - delta_time

            # Apply snapping to the IN point
            if self.snap_enabled:
                snap_result = self._find_snap_target(new_start, self.trim_clip)
                if snap_result:
                    snap_type, snap_time = snap_result
                    # Adjust new_start to snap target
                    new_start = snap_time
                    new_duration = self.trim_original_duration - (new_start - self.trim_original_start)
                    # Show snap indicator
                    self.snap_indicator_pos = snap_time
                    self.snap_indicator_type = snap_type

            # Prevent moving IN point earlier than previous clip end (overlap prevention)
            prev_clip_end = self._find_prev_clip_end(self.trim_clip)
            if prev_clip_end is not None and new_start < prev_clip_end:
                new_start = prev_clip_end
                new_duration = self.trim_original_duration - (new_start - self.trim_original_start)

            # Clamp to valid range
            if new_duration >= min_duration and new_start >= 0:
                self.trim_clip.timeline_start = new_start
                self.trim_clip.duration = new_duration

                # Store the in-point offset for export purposes
                in_offset = self.clip_in_offsets.get(self.trim_clip.path, 0) + (new_start - self.trim_original_start)
                self.clip_in_offsets[self.trim_clip.path] = in_offset

        elif self.trim_mode == 'out':
            # Trimming OUT point - changes duration only
            new_duration = self.trim_original_duration + delta_time
            new_end = self.trim_original_start + new_duration

            # Apply snapping to the OUT point (clip end)
            if self.snap_enabled:
                snap_result = self._find_snap_target(new_end, self.trim_clip)
                if snap_result:
                    snap_type, snap_time = snap_result
                    new_end = snap_time
                    new_duration = new_end - self.trim_clip.timeline_start
                    # Show snap indicator
                    self.snap_indicator_pos = snap_time
                    self.snap_indicator_type = snap_type

            # Cap duration to not exceed original file duration (accounting for in-trim)
            original_dur = self.trim_clip.original_duration
            if original_dur <= 0:
                # For old projects without original_duration, use the duration at trim start
                # This prevents stretching beyond current size
                original_dur = self.trim_original_duration
                # Also set it on the clip so it persists
                self.trim_clip.original_duration = original_dur

            in_offset = self.clip_in_offsets.get(self.trim_clip.path, 0)
            max_duration = original_dur - in_offset
            if max_duration > 0:
                new_duration = min(new_duration, max_duration)

            # Prevent overlap with next clip on same track
            next_clip_start = self._find_next_clip_start(self.trim_clip)
            if next_clip_start is not None:
                max_duration_for_overlap = next_clip_start - self.trim_clip.timeline_start
                new_duration = min(new_duration, max_duration_for_overlap)

            if new_duration >= min_duration:
                self.trim_clip.duration = new_duration

        self._draw_timeline()

    def _find_next_clip_start(self, clip):
        """Find the start time of the next clip on the same track (for overlap prevention).

        Returns the start time of the nearest clip that begins after this clip's start,
        so we know the maximum extent to which we can extend the OUT point.
        """
        next_start = None

        for c in self.clips:
            if c is clip:
                continue
            # Same track only (same kind AND same track_index) - another camera's track doesn't limit this one
            if clip.is_audio_only != c.is_audio_only or getattr(c, 'track_index', 0) != getattr(clip, 'track_index', 0):
                continue
            # Find clips that start after our clip's start position
            if c.timeline_start > clip.timeline_start:
                if next_start is None or c.timeline_start < next_start:
                    next_start = c.timeline_start

        return next_start

    def _find_prev_clip_end(self, clip):
        """Find the end time of the previous clip on the same track (for overlap prevention).

        Returns the end time of the nearest clip that ends before this clip's start,
        so we know the minimum position to which we can move the IN point.
        """
        prev_end = None

        for c in self.clips:
            if c is clip:
                continue
            # Same track only (same kind AND same track_index)
            if clip.is_audio_only != c.is_audio_only or getattr(c, 'track_index', 0) != getattr(clip, 'track_index', 0):
                continue
            # Find clips that end before or at our original clip's start position
            c_end = c.timeline_start + c.duration
            if c_end <= self.trim_original_start:
                if prev_end is None or c_end > prev_end:
                    prev_end = c_end

        return prev_end

    def _reset_clip_trim(self):
        """Reset trim for selected clips back to original duration."""
        if not self.selected_clips:
            return

        # Pause playback if running (reset trim affects clip timing)
        if self.timeline_playing:
            self._toggle_timeline_playback()

        for clip in self.selected_clips:
            # Remove any stored trim offsets
            if clip.path in self.clip_in_offsets:
                del self.clip_in_offsets[clip.path]
            if clip.path in self.clip_out_offsets:
                del self.clip_out_offsets[clip.path]

            # Re-analyze to get original duration would be needed here
            # For now, just notify user
            self.status_label.configure(text=f"Trim reset for: {clip.filename}")

        self._draw_timeline()

    # =========================================================================
    # Clip Splitting
    # =========================================================================

    def _split_clip_at_playhead(self):
        """Split clips at the playhead position (S or Ctrl+B)."""
        if not self.clips:
            return

        # Pause playback if running (split during playback can cause sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Find clips that span the playhead position
        clips_to_split = []
        for clip in self.clips:
            if clip.path in self.locked_clips:
                continue
            if clip.timeline_start < self.playhead_position < (clip.timeline_start + clip.duration):
                clips_to_split.append(clip)

        if not clips_to_split:
            self.status_label.configure(text="No clips at playhead position")
            return

        # Save state for undo
        self._save_undo_state('split')

        split_count = 0
        for clip in clips_to_split:
            # Calculate split point relative to clip
            split_time_in_clip = self.playhead_position - clip.timeline_start

            # Two FULL copies of the original (every field: embedded TC, recording info, sample rate...).
            # (Was MediaClip(clip.path) - missing the filename argument, so Split always crashed.)
            import copy as _copy
            orig_key = clip.path + f"_split_{id(clip)}"
            orig_in = self.clip_in_offsets.get(orig_key, self.clip_in_offsets.get(clip.path, 0)) or 0

            # First part: from original start to split point (keeps the original's IN point)
            clip1 = _copy.copy(clip)
            clip1.filename = f"{clip.filename}_A"
            clip1.duration = split_time_in_clip
            if orig_key in self.clip_in_offsets:   # the original was itself a split part
                self.clip_in_offsets[clip1.path + f"_split_{id(clip1)}"] = self.clip_in_offsets.pop(orig_key)

            # Second part: from split point to original end
            clip2 = _copy.copy(clip)
            clip2.filename = f"{clip.filename}_B"
            clip2.start_tc = self._offset_timecode(clip.start_tc, split_time_in_clip, clip.fps)
            if clip2.start_tc:
                clip2.start_frames = clip2.tc_to_frames(clip2.start_tc)
            clip2.duration = clip.duration - split_time_in_clip
            clip2.timeline_start = self.playhead_position

            # In-point of the second part: the original's IN point + the split position
            self.clip_in_offsets[clip2.path + f"_split_{id(clip2)}"] = orig_in + split_time_in_clip

            # Track split clips
            if clip.path not in self.split_clips:
                self.split_clips[clip.path] = []
            self.split_clips[clip.path].append((clip1, clip2))

            # Remove original clip and add the two new ones
            idx = self.clips.index(clip)
            self.clips.remove(clip)
            self.clips.insert(idx, clip1)
            self.clips.insert(idx + 1, clip2)

            # Update selection
            if clip in self.selected_clips:
                self.selected_clips.remove(clip)
                self.selected_clips.append(clip2)

            split_count += 1

        self.status_label.configure(text=f"Split {split_count} clip(s) at playhead")
        self._refresh_clips_list()
        self._draw_timeline()

    def _offset_timecode(self, tc_string, offset_seconds, fps):
        """Calculate new timecode by adding offset seconds."""
        if not tc_string or fps <= 0:
            return tc_string

        try:
            parts = tc_string.replace(';', ':').split(':')
            if len(parts) != 4:
                return tc_string

            h, m, s, f = map(int, parts)
            total_frames = int(h * 3600 * fps + m * 60 * fps + s * fps + f)
            offset_frames = int(offset_seconds * fps)
            new_total = total_frames + offset_frames

            new_h = int(new_total // (3600 * fps))
            new_m = int((new_total % (3600 * fps)) // (60 * fps))
            new_s = int((new_total % (60 * fps)) // fps)
            new_f = int(new_total % fps)

            separator = ';' if ';' in tc_string else ':'
            return f"{new_h:02d}:{new_m:02d}:{new_s:02d}{separator}{new_f:02d}"
        except:
            return tc_string

    # =========================================================================
    # In/Out Points
    # =========================================================================

    def _set_in_point(self):
        """Set the IN point at the current playhead position (I key)."""
        self.in_point = self.playhead_position
        self.status_label.configure(text=f"IN point set at {self._format_time(self.in_point)}")
        # Use deferred redraw to avoid stuttering during playback
        if self.timeline_playing:
            self._schedule_deferred_redraw()
        else:
            self._draw_timeline()

    def _set_out_point(self):
        """Set the OUT point at the current playhead position (O key)."""
        self.out_point = self.playhead_position
        self.status_label.configure(text=f"OUT point set at {self._format_time(self.out_point)}")
        # Use deferred redraw to avoid stuttering during playback
        if self.timeline_playing:
            self._schedule_deferred_redraw()
        else:
            self._draw_timeline()

    def _go_to_in_point(self):
        """Jump playhead to IN point (Ctrl+I). Keeps playing if playback is active."""
        if self.in_point is not None:
            self._jump_playhead(self.in_point, "Jumped to IN point")

    def _go_to_out_point(self):
        """Jump playhead to OUT point (Alt+O). Keeps playing if playback is active."""
        if self.out_point is not None:
            self._jump_playhead(self.out_point, "Jumped to OUT point")

    def _toggle_loop_playback(self):
        """Toggle loop playback (P key). Loops between IN/OUT points if set,
        otherwise the whole timeline. Great for checking sync at a cut point."""
        self.loop_playback = not self.loop_playback
        if self.loop_playback:
            if self.in_point is not None or self.out_point is not None:
                self.status_label.configure(text="Loop playback ON (IN/OUT range)")
            else:
                self.status_label.configure(text="Loop playback ON (full timeline)")
        else:
            self.status_label.configure(text="Loop playback OFF")

    def _clear_in_out_points(self):
        """Clear both IN and OUT points (X key)."""
        # Pause playback if running (clearing in/out triggers timeline redraw)
        if self.timeline_playing:
            self._toggle_timeline_playback()
        self.in_point = None
        self.out_point = None
        self.status_label.configure(text="IN/OUT points cleared")
        self._draw_timeline()

    def _get_in_out_duration(self):
        """Get the duration between IN and OUT points."""
        if self.in_point is not None and self.out_point is not None:
            return abs(self.out_point - self.in_point)
        return None

    # =========================================================================
    # Undo/Redo System
    # =========================================================================

    def _save_undo_state(self, action_name):
        """Save current state for undo."""
        state = {
            'action': action_name,
            'clips': [(c.path, c.timeline_start, c.duration, c.filename) for c in self.clips],
            'selected': [c.path for c in self.selected_clips],
            'markers': list(self.timeline_markers),
            'in_point': self.in_point,
            'out_point': self.out_point,
            'playhead': self.playhead_position,
            'clip_in_offsets': dict(self.clip_in_offsets),
            'clip_out_offsets': dict(self.clip_out_offsets),
            'locked_clips': set(self.locked_clips)
        }

        self.undo_stack.append(state)

        # Limit undo stack size
        if len(self.undo_stack) > self.max_undo_levels:
            self.undo_stack.pop(0)

        # Clear redo stack when new action is performed
        self.redo_stack.clear()

    def _undo(self):
        """Undo the last action (Ctrl+Z)."""
        if not self.undo_stack:
            self.status_label.configure(text="Nothing to undo")
            return

        # Pause playback if running (undo can modify clips causing sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Save current state for redo
        current_state = {
            'action': 'redo',
            'clips': [(c.path, c.timeline_start, c.duration, c.filename) for c in self.clips],
            'selected': [c.path for c in self.selected_clips],
            'markers': list(self.timeline_markers),
            'in_point': self.in_point,
            'out_point': self.out_point,
            'playhead': self.playhead_position,
            'clip_in_offsets': dict(self.clip_in_offsets),
            'clip_out_offsets': dict(self.clip_out_offsets),
            'locked_clips': set(self.locked_clips)
        }
        self.redo_stack.append(current_state)

        # Restore previous state
        state = self.undo_stack.pop()
        self._restore_state(state)
        self.status_label.configure(text=f"Undone: {state['action']}")

    def _redo(self):
        """Redo the last undone action (Ctrl+Y or Ctrl+Shift+Z)."""
        if not self.redo_stack:
            self.status_label.configure(text="Nothing to redo")
            return

        # Pause playback if running (redo can modify clips causing sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Save current state for undo
        current_state = {
            'action': 'undo',
            'clips': [(c.path, c.timeline_start, c.duration, c.filename) for c in self.clips],
            'selected': [c.path for c in self.selected_clips],
            'markers': list(self.timeline_markers),
            'in_point': self.in_point,
            'out_point': self.out_point,
            'playhead': self.playhead_position,
            'clip_in_offsets': dict(self.clip_in_offsets),
            'clip_out_offsets': dict(self.clip_out_offsets),
            'locked_clips': set(self.locked_clips)
        }
        self.undo_stack.append(current_state)

        # Restore redo state
        state = self.redo_stack.pop()
        self._restore_state(state)
        self.status_label.configure(text="Redone action")

    def _restore_state(self, state):
        """Restore a saved state."""
        # Restore clip positions and durations
        clip_data = {path: (start, dur, name) for path, start, dur, name in state['clips']}

        for clip in self.clips:
            if clip.path in clip_data:
                start, dur, name = clip_data[clip.path]
                clip.timeline_start = start
                clip.duration = dur
                clip.filename = name

        # Restore selection
        self.selected_clips = [c for c in self.clips if c.path in state['selected']]

        # Restore other state
        self.timeline_markers = state['markers']
        self.in_point = state['in_point']
        self.out_point = state['out_point']
        # Unified seek keeps wall clock / audio mixer / scheduler in step even
        # when undo/redo fires during playback
        self._seek_playhead(state['playhead'])
        self.clip_in_offsets = state['clip_in_offsets']
        self.clip_out_offsets = state['clip_out_offsets']
        self.locked_clips = state['locked_clips']

        self._refresh_clips_list()
        self._draw_timeline()

    # =========================================================================
    # Clip Snapping
    # =========================================================================

    def _toggle_snapping(self):
        """Toggle snapping on/off (N key)."""
        self.snap_enabled = not self.snap_enabled
        state = "enabled" if self.snap_enabled else "disabled"
        self.status_label.configure(text=f"Snapping {state}")

    def _get_snap_points(self, exclude_clip=None):
        """Get all snap points on the timeline."""
        snap_points = []

        # Playhead
        if self.snap_to_playhead:
            snap_points.append(('playhead', self.playhead_position))

        # Markers
        if self.snap_to_markers:
            for marker in self.timeline_markers:
                snap_points.append(('marker', marker['time']))

        # Clip edges
        if self.snap_to_clips:
            for clip in self.clips:
                if clip == exclude_clip:
                    continue
                snap_points.append(('clip_start', clip.timeline_start))
                snap_points.append(('clip_end', clip.timeline_start + clip.duration))

        # In/Out points
        if self.in_point is not None:
            snap_points.append(('in_point', self.in_point))
        if self.out_point is not None:
            snap_points.append(('out_point', self.out_point))

        return snap_points

    def _find_snap_target(self, time_pos, exclude_clip=None):
        """Find the nearest snap target within threshold."""
        if not self.snap_enabled:
            return None

        snap_points = self._get_snap_points(exclude_clip)
        threshold_time = self.snap_threshold / self.pixels_per_second

        nearest = None
        min_dist = float('inf')

        for snap_type, snap_time in snap_points:
            dist = abs(time_pos - snap_time)
            if dist < threshold_time and dist < min_dist:
                min_dist = dist
                nearest = (snap_type, snap_time)

        return nearest

    # =========================================================================
    # Clip Locking
    # =========================================================================

    def _toggle_clip_lock(self):
        """Toggle lock state for selected clips (L key)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected to lock/unlock")
            return

        for clip in self.selected_clips:
            if clip.path in self.locked_clips:
                self.locked_clips.discard(clip.path)
                self.status_label.configure(text=f"Unlocked: {clip.filename}")
            else:
                self.locked_clips.add(clip.path)
                self.status_label.configure(text=f"Locked: {clip.filename}")

        self._draw_timeline()

    def _is_clip_locked(self, clip):
        """Check if a clip is locked."""
        return clip.path in self.locked_clips

    # =========================================================================
    # Ripple Delete
    # =========================================================================

    def _ripple_delete(self):
        """Delete selected clips and ripple (shift) remaining clips left (Shift+Delete)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected for ripple delete")
            return

        # Pause playback if running (ripple delete can cause sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Check for locked clips
        locked_selected = [c for c in self.selected_clips if self._is_clip_locked(c)]
        if locked_selected:
            self.status_label.configure(text="Cannot ripple delete locked clips")
            return

        # Save state for undo
        self._save_undo_state('ripple_delete')

        # Sort selected clips by timeline_start
        sorted_selected = sorted(self.selected_clips, key=lambda c: c.timeline_start)

        for clip in sorted_selected:
            # Calculate the gap that will be created
            gap_start = clip.timeline_start
            gap_duration = clip.duration

            # Remove the clip
            if clip in self.clips:
                self.clips.remove(clip)

            # Find clips that start after this clip and shift them left
            for other_clip in self.clips:
                if other_clip.timeline_start > gap_start and not self._is_clip_locked(other_clip):
                    other_clip.timeline_start -= gap_duration

        # Clear selection
        self.selected_clips.clear()
        self.selected_clip = None

        self.status_label.configure(text=f"Ripple deleted {len(sorted_selected)} clip(s)")
        self._refresh_clips_list()
        self._draw_timeline()

    # =========================================================================
    # Clip Grouping
    # =========================================================================

    def _group_selected_clips(self):
        """Group selected clips together (Ctrl+G)."""
        if len(self.selected_clips) < 2:
            self.status_label.configure(text="Select at least 2 clips to group")
            return

        # Save state for undo
        self._save_undo_state('group')

        # Create new group
        group_id = self.next_group_id
        self.next_group_id += 1

        clip_paths = []
        for clip in self.selected_clips:
            # Remove from existing group if any
            if clip.path in self.clip_to_group:
                old_group = self.clip_to_group[clip.path]
                if old_group in self.clip_groups:
                    self.clip_groups[old_group].remove(clip.path)
                    if not self.clip_groups[old_group]:
                        del self.clip_groups[old_group]

            clip_paths.append(clip.path)
            self.clip_to_group[clip.path] = group_id

        self.clip_groups[group_id] = clip_paths

        self.status_label.configure(text=f"Created group {group_id} with {len(clip_paths)} clips")
        self._draw_timeline()

    def _ungroup_selected_clips(self):
        """Ungroup selected clips (Ctrl+Shift+G)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected to ungroup")
            return

        # Save state for undo
        self._save_undo_state('ungroup')

        ungrouped_count = 0
        groups_to_remove = set()

        for clip in self.selected_clips:
            if clip.path in self.clip_to_group:
                group_id = self.clip_to_group[clip.path]
                groups_to_remove.add(group_id)

        # Remove all clips from these groups
        for group_id in groups_to_remove:
            if group_id in self.clip_groups:
                for clip_path in self.clip_groups[group_id]:
                    if clip_path in self.clip_to_group:
                        del self.clip_to_group[clip_path]
                        ungrouped_count += 1
                del self.clip_groups[group_id]

        if ungrouped_count > 0:
            self.status_label.configure(text=f"Ungrouped {ungrouped_count} clips")
        else:
            self.status_label.configure(text="Selected clips were not grouped")
        self._draw_timeline()

    def _get_group_clips(self, clip):
        """Get all clips in the same group as the given clip."""
        if clip.path not in self.clip_to_group:
            return [clip]

        group_id = self.clip_to_group[clip.path]
        if group_id not in self.clip_groups:
            return [clip]

        # Find all clip objects that belong to this group
        group_clips = []
        for c in self.clips:
            if c.path in self.clip_groups[group_id]:
                group_clips.append(c)

        return group_clips if group_clips else [clip]

    def _select_grouped_clips(self, clip):
        """Select all clips in the same group as the clicked clip."""
        if clip.path in self.clip_to_group:
            group_clips = self._get_group_clips(clip)
            for gc in group_clips:
                if gc not in self.selected_clips:
                    self.selected_clips.append(gc)

    # =========================================================================
    # Clip Duplication
    # =========================================================================

    def _duplicate_selected_clips(self):
        """Duplicate selected clips (Ctrl+D)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected to duplicate")
            return

        # Pause playback if running (duplicate modifies clips causing sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Save state for undo
        self._save_undo_state('duplicate')

        new_clips = []
        for clip in self.selected_clips:
            # A full copy of the clip (was MediaClip(clip.path) without filename - Duplicate always crashed)
            import copy as _copy
            new_clip = _copy.copy(clip)
            new_clip.filename = f"{clip.filename}_copy"
            new_clip.timeline_start = clip.timeline_start + clip.duration + 0.1  # Offset slightly
            split_in = self.clip_in_offsets.get(clip.path + f"_split_{id(clip)}")
            if split_in is not None:   # a split part keeps its own IN point
                self.clip_in_offsets[new_clip.path + f"_split_{id(new_clip)}"] = split_in

            new_clips.append(new_clip)
            self.clips.append(new_clip)

        # Select the new clips
        self.selected_clips = new_clips
        self.selected_clip = new_clips[0] if new_clips else None

        self.status_label.configure(text=f"Duplicated {len(new_clips)} clip(s)")
        self._refresh_clips_list()
        self._draw_timeline()

    # =========================================================================
    # Clip Nudging
    # =========================================================================

    def _nudge_clips(self, frames):
        """Move selected clips by a number of frames (Alt+Arrow keys)."""
        if not self.selected_clips:
            self.status_label.configure(text="No clips selected to nudge")
            return

        # Pause playback if running (nudge modifies clip positions causing sync issues)
        if self.timeline_playing:
            self._toggle_timeline_playback()  # Pause playback

        # Check for locked clips
        unlocked = [c for c in self.selected_clips if not self._is_clip_locked(c)]
        if not unlocked:
            self.status_label.configure(text="All selected clips are locked")
            return

        # Save state for undo
        self._save_undo_state('nudge')

        # Calculate time offset (use first clip's FPS or default to 25)
        fps = self.selected_clips[0].fps if self.selected_clips[0].fps > 0 else 25.0
        time_offset = frames / fps

        # If clips are grouped, get all clips in their groups
        clips_to_move = set()
        for clip in unlocked:
            if clip.path in self.clip_to_group:
                group_clips = self._get_group_clips(clip)
                for gc in group_clips:
                    if not self._is_clip_locked(gc):
                        clips_to_move.add(gc)
            else:
                clips_to_move.add(clip)

        # Apply offset with snapping
        for clip in clips_to_move:
            new_start = clip.timeline_start + time_offset

            # Apply snapping if enabled
            if self.snap_enabled:
                snap_result = self._find_snap_target(new_start, clip)
                if snap_result:
                    new_start = snap_result[1]

            clip.timeline_start = max(0, new_start)

        direction = "right" if frames > 0 else "left"
        self.status_label.configure(text=f"Nudged {len(clips_to_move)} clip(s) {abs(frames)} frame(s) {direction}")
        self._draw_timeline()

    # =========================================================================
    # Clip Navigation
    # =========================================================================

    def _go_to_clip_start(self):
        """Move playhead to start of selected clip ([ key). Keeps playing if active."""
        if not self.selected_clip:
            self.status_label.configure(text="No clip selected")
            return

        self._jump_playhead(self.selected_clip.timeline_start,
                            f"Jumped to start of {self.selected_clip.filename}")

    def _go_to_clip_end(self):
        """Move playhead to end of selected clip (] key). Keeps playing if active."""
        if not self.selected_clip:
            self.status_label.configure(text="No clip selected")
            return

        self._jump_playhead(self.selected_clip.timeline_start + self.selected_clip.duration,
                            f"Jumped to end of {self.selected_clip.filename}")

    # =========================================================================
    # Auto-Save
    # =========================================================================

    def _start_auto_save(self):
        """Start auto-save timer."""
        if self.auto_save_enabled and self.current_project_path:
            self._schedule_auto_save()

    def _schedule_auto_save(self):
        """Schedule the next auto-save."""
        if self.auto_save_id:
            self.root.after_cancel(self.auto_save_id)

        if self.auto_save_enabled and self.current_project_path:
            self.auto_save_id = self.root.after(self.auto_save_interval, self._perform_auto_save)

    def _perform_auto_save(self):
        """Perform auto-save of current project."""
        if not self.current_project_path or not self.clips:
            self._schedule_auto_save()
            return

        try:
            # Create auto-save backup path
            backup_path = self.current_project_path.replace('.ltcproj', '_autosave.ltcproj')

            # Same schema as Save (_project_data), so crash recovery loads everything
            project_data = self._project_data()
            project_data['auto_save_time'] = datetime.now().isoformat()

            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(project_data, f, indent=2)

            self.last_auto_save_time = datetime.now().timestamp()
            self.status_label.configure(text=f"Auto-saved at {datetime.now().strftime('%H:%M:%S')}")

        except Exception:
            pass

        # Schedule next auto-save
        self._schedule_auto_save()

    def _toggle_auto_save(self):
        """Toggle auto-save on/off."""
        self.auto_save_enabled = not self.auto_save_enabled
        state = "enabled" if self.auto_save_enabled else "disabled"
        self.status_label.configure(text=f"Auto-save {state}")

        if self.auto_save_enabled:
            self._start_auto_save()
        elif self.auto_save_id:
            self.root.after_cancel(self.auto_save_id)
            self.auto_save_id = None

    # =========================================================================
    # Track Mute/Solo (Audio) and Hide/Solo (Video)
    # =========================================================================

    def _toggle_video_hide(self, track_id):
        """Toggle hide state for a video track (hides from preview, not timeline)."""
        if track_id in self.hidden_video_tracks:
            self.hidden_video_tracks.discard(track_id)
            self.status_label.configure(text=f"Preview enabled: {track_id}")
        else:
            self.hidden_video_tracks.add(track_id)
            self.status_label.configure(text=f"Hidden from preview: {track_id}")
        # Redraw timeline to show dimmed clips
        self.root.after(1, self._draw_timeline)
        # Force VLC to load the new topmost visible clip
        self.root.after(10, self._force_vlc_layer_update)

    def _toggle_video_solo(self, track_id):
        """Toggle solo state for a video track. When soloed, only this track shows in preview."""
        if track_id in self.soloed_video_tracks:
            self.soloed_video_tracks.discard(track_id)
            self.status_label.configure(text=f"Unsolo preview: {track_id}")
        else:
            self.soloed_video_tracks.add(track_id)
            self.status_label.configure(text=f"Solo preview: {track_id}")
        # Redraw timeline to show dimmed clips
        self.root.after(1, self._draw_timeline)
        # Force VLC to load the new topmost visible clip
        self.root.after(10, self._force_vlc_layer_update)

    def _force_vlc_layer_update(self):
        """Force VLC to reload based on current layer visibility."""
        clip = self._get_topmost_visible_video_clip(self.playhead_position)

        if clip:
            # Calculate position in file
            position_in_clip = self.playhead_position - clip.timeline_start
            split_key = clip.path + f"_split_{id(clip)}"
            in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(clip.path, 0))
            position_in_file = position_in_clip + in_offset

            # Update selected clip
            self.selected_clip = clip
            self.selected_clips = [clip]

            if self._direct_ok(clip):
                self._direct_show_still(clip, position_in_file)
                return
            self._direct_release_display()

            # Force VLC to show this clip - on the clip-switch worker so the
            # UI never waits for libVLC (load + first picture took 1.5-2.3s on
            # 4K HEVC 4:2:2 before proxies, all of it with the window frozen)
            if self.use_vlc and self.vlc_player:
                self._vlc_switch_media_for_scrub(clip.path, position_in_file)
        else:
            # No visible clip - pause VLC
            # Use set_pause(1) not pause() - pause() is a TOGGLE!
            if self.use_vlc and self.vlc_player:
                try:
                    self.vlc_player.set_pause(1)
                except:
                    pass

    def _is_video_track_visible(self, track_id):
        """Check if a video track should be visible in preview (considering hide/solo)."""
        # If any video tracks are soloed, only soloed tracks are visible
        if self.soloed_video_tracks:
            return track_id in self.soloed_video_tracks and track_id not in self.hidden_video_tracks
        # Otherwise, check if track is not hidden
        return track_id not in self.hidden_video_tracks

    def _get_topmost_visible_video_clip(self, position):
        """Get the topmost visible video clip at the given timeline position.

        Video tracks work like layers:
        - V1 is the top layer, V2 is below, etc.
        - Returns the highest priority (lowest track_index) visible clip
        - If V1 is hidden, V2 shows through
        """
        # Collect all video clips at this position
        clips_at_position = []
        for clip in self.clips:
            if clip.duration > 0 and not clip.is_audio_only:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration
                if clip_start <= position < clip_end:
                    clips_at_position.append(clip)

        if not clips_at_position:
            return None

        # Sort by track_index (lower = higher priority, V1 is on top)
        clips_at_position.sort(key=lambda c: getattr(c, 'track_index', 999))

        # Find the topmost visible clip
        for clip in clips_at_position:
            track_name = clip.camera_id or f"Video {getattr(clip, 'track_index', 0) + 1}"
            if self._is_video_track_visible(track_name):
                return clip

        return None  # All clips at this position are hidden

    def _toggle_track_mute(self, track_id):
        """Toggle mute state for a track."""
        if track_id in self.muted_tracks:
            self.muted_tracks.discard(track_id)
            self.status_label.configure(text=f"Unmuted track: {track_id}")
        else:
            self.muted_tracks.add(track_id)
            self.status_label.configure(text=f"Muted track: {track_id}")
            # If currently playing audio from this track, stop it
            if self.selected_clip and self.selected_clip.is_audio_only:
                clip_track_id = self._get_audio_track_id(self.selected_clip)
                if clip_track_id == track_id:
                    self._stop_audio_playback()
        # Update VLC audio mute state immediately
        self._update_vlc_audio_mute()
        # Use after() for non-blocking redraw
        self.root.after(1, self._draw_timeline)

    def _toggle_track_solo(self, track_id):
        """Toggle solo state for a track."""
        if track_id in self.soloed_tracks:
            self.soloed_tracks.discard(track_id)
            self.status_label.configure(text=f"Unsolo track: {track_id}")
        else:
            self.soloed_tracks.add(track_id)
            self.status_label.configure(text=f"Solo track: {track_id}")
        # If currently playing audio from a non-soloed track, stop it
        if self.selected_clip and self.selected_clip.is_audio_only:
            clip_track_id = self._get_audio_track_id(self.selected_clip)
            if not self._is_track_audible(clip_track_id):
                self._stop_audio_playback()
        # Update VLC audio mute state immediately
        self._update_vlc_audio_mute()
        # Use after() for non-blocking redraw
        self.root.after(1, self._draw_timeline)

    def _is_track_audible(self, track_id):
        """Check if a track should be audible (considering mute/solo)."""
        # If any tracks are soloed, only soloed tracks are audible
        if self.soloed_tracks:
            return track_id in self.soloed_tracks and track_id not in self.muted_tracks
        # Otherwise, check if track is not muted
        return track_id not in self.muted_tracks

    def _auto_mute_ltc_tracks(self):
        """Automatically mute tracks containing LTC signal after analysis.

        Mutes:
        - Dedicated LTC tracks (is_ltc_track=True)
        - Single-channel audio files with LTC (purely LTC files)
        - Video/camera clips that have LTC detected on their audio
          (cameras often record LTC on audio which is not useful for listening)

        Does NOT mute external audio recorders' stereo files with LTC on one channel
        (they typically have usable dialogue/music on the other channel).
        """
        muted_count = 0
        for clip in self.clips:
            # Dedicated LTC reference track
            if clip.is_ltc_track:
                track_id = self._get_audio_track_id(clip)
                if track_id not in self.muted_tracks:
                    self.muted_tracks.add(track_id)
                    muted_count += 1

            # Single-channel audio file with LTC detected (purely LTC file)
            elif clip.is_audio_only and clip.ltc_channel >= 0:
                num_ch = clip.audio_channels if hasattr(clip, 'audio_channels') and clip.audio_channels else 2
                if num_ch == 1:
                    # Single channel = purely LTC file, mute it
                    track_id = self._get_audio_track_id(clip)
                    if track_id not in self.muted_tracks:
                        self.muted_tracks.add(track_id)
                        muted_count += 1
                # For stereo external audio files with LTC on one channel, don't mute
                # The user may still want to hear the non-LTC channel

            # Video/camera clip with LTC detected on audio track
            elif not clip.is_audio_only and clip.ltc_channel >= 0:
                # Camera audio with LTC - mute this track as it contains LTC signal
                # The camera's LTC audio is not useful for listening
                track_idx = getattr(clip, 'track_index', 0)
                track_id = (clip.camera_id or f"Video {track_idx + 1}") + " Audio"
                if track_id not in self.muted_tracks:
                    self.muted_tracks.add(track_id)
                    muted_count += 1

        if muted_count > 0:
            self.status_label.configure(text=f"Auto-muted {muted_count} LTC track(s)")

    def _toggle_timeline_lock(self):
        """Toggle timeline lock state (locked = playback only, unlocked = edit mode)."""
        self.timeline_locked = not self.timeline_locked
        if self.timeline_locked:
            self.lock_btn.configure(text="Unlock", bg=self.COLORS['bg_card'])
            self.status_label.configure(text="Timeline locked - playback only")
        else:
            self.lock_btn.configure(text="Lock", bg='#ff9800')
            self.status_label.configure(text="Timeline unlocked - editing enabled")
        self._draw_timeline()

    def _lock_timeline(self):
        """Lock the timeline (called after sync)."""
        self.timeline_locked = True
        if hasattr(self, 'lock_btn'):
            self.lock_btn.configure(text="Unlock", bg=self.COLORS['bg_card'])

    def _update_playhead_preview(self):
        """Update preview based on playhead position - auto-select clip under playhead."""
        if not self.clips:
            return

        # Find clip under playhead using layer system for video
        # Video clips: use topmost visible (layer system)
        # Audio clips: find first audio clip at playhead
        video_clip_at_playhead = self._get_topmost_visible_video_clip(self.playhead_position)
        audio_clip_at_playhead = None

        for clip in self.clips:
            if clip.duration > 0 and clip.is_audio_only:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration
                if clip_start <= self.playhead_position < clip_end:
                    audio_clip_at_playhead = clip
                    break

        # Prefer video clips for preview
        clip_at_playhead = video_clip_at_playhead or audio_clip_at_playhead

        if clip_at_playhead:
            # Calculate position within the clip
            position_in_clip = self.playhead_position - clip_at_playhead.timeline_start

            # Update selected clip if different
            if self.selected_clip != clip_at_playhead:
                self.selected_clip = clip_at_playhead
                # Also update the selection list and refresh highlights
                self.selected_clips = [clip_at_playhead]
                self._update_clip_selection_display()

                # Auto-scroll to show the selected clip
                self._scroll_to_clip_soon(clip_at_playhead)

                self.preview_clip_label.configure(text=clip_at_playhead.filename)
                self.preview_fps_label.configure(text=clip_at_playhead.fps_display if clip_at_playhead.start_tc else "--")
                self.preview_cam_label.configure(text=clip_at_playhead.camera_id if clip_at_playhead.camera_id else "--")

                if clip_at_playhead.duration > 0:
                    self.position_slider.configure(to=clip_at_playhead.duration)

                # Don't overwrite status during audio extraction
                if not self.audio_extraction_running:
                    self.status_label.configure(text=f"Selected: {clip_at_playhead.filename}")

            # Update position within clip
            self.preview_position = position_in_clip
            self.position_var.set(position_in_clip)
            self._update_position_label()
            self._update_preview_tc()
            self._update_preview_frame()

        # Update playhead timecode display
        self._update_playhead_tc_display()

    def _invalidate_tc_cache(self):
        """Invalidate the timecode reference cache (call when clips change)."""
        self._tc_ref_clip = None
        self._tc_ref_fps = 30.0
        self._tc_min_frames = 0

    def _update_playhead_tc_display(self):
        """Update the timecode display above the timeline.

        OPTIMIZED: Caches reference clip and min frames to avoid iterating clips on every update.
        """
        if not self.clips or not self.synced:
            self.playhead_tc_label.configure(text="TC: --:--:--:--")
            return

        # Cache reference clip and min frames for performance during playback
        if not hasattr(self, '_tc_ref_clip') or self._tc_ref_clip is None:
            # Find the first clip with timecode to use as reference
            for clip in self.clips:
                if clip.start_tc:
                    self._tc_ref_clip = clip
                    self._tc_ref_fps = clip.fps if clip.fps > 0 else 30.0
                    self._tc_min_frames = min(c.start_frames for c in self.clips if c.start_tc)
                    break
            else:
                self._tc_ref_clip = None

        if not self._tc_ref_clip:
            self.playhead_tc_label.configure(text="TC: --:--:--:--")
            return

        # Calculate timecode at playhead position using cached values
        fps = self._tc_ref_fps
        current_frames = self._tc_min_frames + int(self.playhead_position * fps)

        # Convert to timecode
        total_secs, frames = divmod(current_frames, int(round(fps)))
        total_mins, secs = divmod(total_secs, 60)
        hours, mins = divmod(total_mins, 60)

        tc_str = f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"
        self.playhead_tc_label.configure(text=f"TC: {tc_str}")

    # =========================================================================
    # Timeline Playback
    # =========================================================================

    def _toggle_timeline_playback(self):
        """Toggle timeline playback (play/pause)."""
        if not self.clips or not self.synced:
            self.status_label.configure(text="Sync clips first to enable timeline playback")
            return

        # Any explicit transport action supersedes an armed scrub auto-resume
        # (e.g. Space pressed mid-drag): disarm so a later mouse release can't
        # re-toggle playback against the user's intent.
        self._resume_after_scrub = False

        self.timeline_playing = not self.timeline_playing

        if self.timeline_playing:
            # Stop any clip-preview session first: its tick chain would later
            # call _stop_audio_playback() and close the TIMELINE mixer stream
            # out from under us (both sessions share self.audio_stream).
            # _force_stop_preview works even when selected_clip is None
            # (where _toggle_playback would silently no-op).
            if self.preview_playing:
                self._force_stop_preview()
            # IMMEDIATELY mute VLC - before anything else
            # This prevents doubled audio at playback start
            if self.use_vlc and self.vlc_player:
                try:
                    self.vlc_player.audio_set_mute(True)
                except:
                    pass

            # Calculate timeline start and end time
            tc_clips = [c for c in self.clips if c.start_tc and c.duration > 0]
            if tc_clips:
                self.timeline_start_time = min(c.timeline_start for c in tc_clips)
                self.timeline_end_time = max(c.timeline_start + c.duration for c in tc_clips)
            else:
                self.timeline_start_time = 0.0
                self.timeline_end_time = 60.0

            # Ensure timeline is drawn and pixels_per_second is set
            if not hasattr(self, 'pixels_per_second') or self.pixels_per_second <= 0:
                self._draw_timeline()

            # IMPORTANT: Ensure playhead is within valid range
            current_pos = self.playhead_position
            if current_pos < self.timeline_start_time:
                current_pos = self.timeline_start_time
            if current_pos >= self.timeline_end_time:
                current_pos = self.timeline_start_time  # Reset to start if at/past end

            self.playhead_position = current_pos
            self.timeline_playback_start_time = time.perf_counter()
            self.timeline_playback_start_pos = current_pos

            # Initialize counter for optimized updates
            self._ui_update_counter = 0

            # Initialize audio master clock for precise A/V sync timing
            # Video is still displayed via VLC, but timing comes from audio
            if self.use_frame_buffer_system:
                # Create audio master clock if not exists
                if self.audio_master_clock is None:
                    self.audio_master_clock = AudioMasterClock(sample_rate=48000)

                # Set initial clock position
                self.audio_master_clock.seek(current_pos)

            # Pre-load audio for clips at current playhead position
            # This ensures audio is ready when playback starts (fixes silent start bug)
            self._preload_audio_at_playhead(current_pos)

            self.timeline_audio_ready = True

            # Start multi-track audio stream for timeline
            # (it arms _audio_seek_pending, which snaps _audio_smooth_pos to the
            # latency-compensated playhead on the first callback)
            self._start_timeline_audio_stream()

            # Prepare VLC for VIDEO (audio handled by multi-track mixer)
            # VLC is used for video display in both legacy and frame buffer modes
            if self.use_vlc and self.vlc_player:
                # (not when the direct GPU engine plays the clip under the
                # playhead: it draws on the canvas, which must stay visible)
                clip_here = self._find_clip_at_playhead()
                if not (clip_here and self._direct_ok(clip_here)):
                    self._show_vlc_frame()
                    self._vlc_set_window()

                # MUTE VLC - we handle all audio via multi-track mixer
                self.vlc_player.audio_set_mute(True)

                # If we already have the correct clip loaded, just resume playback
                # Otherwise reset timeline_last_clip to force loading
                current_clip = self._find_clip_at_playhead()
                if current_clip and self._direct_ok(current_clip):
                    self.timeline_last_clip = None  # engine restarts at the playhead
                elif current_clip and self.vlc_media_path == current_clip.path:
                    # Same clip - just resume VLC playback (muted)
                    self.vlc_player.audio_set_mute(True)  # Mute before play
                    self.vlc_player.play()
                    self.vlc_player.audio_set_mute(True)  # Mute after play (in case it resets)
                else:
                    # Different clip - force reload
                    self.timeline_last_clip = None

            self.tl_play_btn.configure(text="⏸", bg='#fbbf24')

            # Start the playback tick loop immediately
            self._timeline_playback_tick()
        else:
            self.tl_play_btn.configure(text="▶", bg=self.COLORS['accent'])
            if self.timeline_update_id:
                self.root.after_cancel(self.timeline_update_id)
                self.timeline_update_id = None

            # Stop frame display scheduler
            if self.use_frame_buffer_system and self.frame_display_scheduler:
                self.frame_display_scheduler.stop()

            # Stop direct GPU playback (the last frame stays on screen) and
            # warm the spot it stopped at, so resuming is instant
            self._direct_stop_playback()
            self._schedule_direct_warm()

            # Pause VLC (don't stop - keeps buffer for quick resume)
            # Use set_pause(1) not pause() - pause() is a TOGGLE!
            if self.use_vlc and self.vlc_player:
                try:
                    self.vlc_player.set_pause(1)
                except:
                    pass

            # Stop audio playback
            self._stop_audio_playback()

            # Flush any timeline redraw deferred during playback (waveforms/
            # thumbnails that finished extracting while we were playing)
            if getattr(self, '_extraction_redraw_deferred', False):
                self._extraction_redraw_deferred = False
                self._draw_timeline()

    def _pause_timeline_for_scrub(self):
        """Lightweight pause for ruler scrubbing.

        Unlike _toggle_timeline_playback's pause branch, this keeps the audio
        output stream and lazy WAV cache alive (the audio callback outputs
        silence while timeline_playing is False), so resume is instant instead
        of a full device close/open + cache rebuild per scrub."""
        self.timeline_playing = False
        if self.timeline_update_id:
            self.root.after_cancel(self.timeline_update_id)
            self.timeline_update_id = None
        if self.use_frame_buffer_system and self.frame_display_scheduler:
            self.frame_display_scheduler.stop()
        self._direct_stop_playback()
        if self.use_vlc and self.vlc_player:
            try:
                self.vlc_player.set_pause(1)
            except Exception:
                pass
        self.tl_play_btn.configure(text="▶", bg=self.COLORS['accent'])

    def _resume_timeline_after_scrub(self):
        """Resume playback after a ruler scrub without tearing down audio.

        Falls back to a full playback start if something else already resumed
        playback during the drag (e.g. spacebar) or the audio stream is gone."""
        if self.timeline_playing:
            return  # Already playing (user resumed mid-drag) - don't toggle it off
        # NOTE: any explicit transport action mid-drag (Space, Stop, sync)
        # disarms _resume_after_scrub, so reaching here means the scrub is the
        # only thing that paused playback.
        if self.audio_stream is None:
            # No stream survived (e.g. sounddevice unavailable) - full start path
            self._toggle_timeline_playback()
            return

        # Recompute timeline bounds: clips may have been edited mid-drag by
        # paths that don't disarm the resume (undo shortcut, deferred analysis)
        tc_clips = [c for c in self.clips if c.start_tc and c.duration > 0]
        if tc_clips:
            self.timeline_start_time = min(c.timeline_start for c in tc_clips)
            self.timeline_end_time = max(c.timeline_start + c.duration for c in tc_clips)
        if self.playhead_position >= self.timeline_end_time:
            self.playhead_position = self.timeline_start_time

        self.timeline_playing = True
        self.tl_play_btn.configure(text="⏸", bg='#fbbf24')
        # Re-anchor clock, snap audio mixer, force VLC reload on next tick
        self._seek_playhead(self.playhead_position)
        self._timeline_playback_tick()

    def _finalize_scrub_before_modal(self):
        """Finalize an armed ruler scrub before opening any modal dialog.

        Modals swallow the ButtonRelease, which would otherwise strand the
        armed auto-resume and its open (silent) audio stream."""
        if self._resume_after_scrub:
            self._is_scrubbing = False
            self._resume_after_scrub = False
            self._resume_timeline_after_scrub()

    def _find_clip_at_playhead(self):
        """Find the video clip at current playhead position."""
        for clip in self.clips:
            if clip.duration > 0 and not clip.is_audio_only:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration
                if clip_start <= self.playhead_position < clip_end:
                    return clip
        return None

    def _stop_timeline(self):
        """Stop timeline playback and reset to beginning."""
        self.timeline_playing = False
        self._resume_after_scrub = False  # explicit stop supersedes armed scrub-resume
        self.tl_play_btn.configure(text="▶", bg=self.COLORS['accent'])

        if self.timeline_update_id:
            self.root.after_cancel(self.timeline_update_id)
            self.timeline_update_id = None

        # Reset audio master clock
        if self.use_frame_buffer_system and self.audio_master_clock:
            self.audio_master_clock.seek(0.0)

        # Stop audio
        self._stop_timeline_audio()
        self._direct_stop_playback()

        # Reset playhead to start
        self.playhead_position = 0.0
        self.timeline_last_clip = None
        self._draw_timeline()
        self._update_playhead_preview()

    def _timeline_playback_tick(self):
        """Advance timeline playback by one tick - HIGHLY OPTIMIZED version.

        Uses audio master clock for timing when frame buffer system is enabled,
        otherwise falls back to wall-clock timing. Minimizes UI updates.
        """
        if not self.timeline_playing:
            return

        # Keep the audio pieces ahead of the playhead queued (twice a second)
        now_pf = time.monotonic()
        if now_pf - getattr(self, '_seg_prefetch_at', 0.0) > 0.5:
            self._seg_prefetch_at = now_pf
            self._preload_audio_at_playhead(self.playhead_position)

        # WALL-CLOCK TIMING: Playhead advances based on elapsed time
        # This is more reliable than VLC-driven timing which can stall on large files
        elapsed = time.perf_counter() - self.timeline_playback_start_time
        new_position = self.timeline_playback_start_pos + elapsed

        # Check if user has seeked (clicked on timeline during playback)
        # If current playhead is significantly different from expected, user seeked
        # (Normal seeks go through _seek_playhead which re-anchors the reference;
        #  this is a fallback for any code path that moved the playhead directly.)
        if abs(self.playhead_position - new_position) > 0.5:
            # Direct playhead write or a main-thread stall - re-anchor the wall
            # clock AND snap the audio mixer, otherwise audio (which kept running
            # on the sound-card clock through the stall) stays offset from video
            # by the stall duration.
            self.timeline_playback_start_time = time.perf_counter()
            self.timeline_playback_start_pos = self.playhead_position
            new_position = self.playhead_position
            self._audio_seek_pending = True

        # Loop playback: wrap at OUT point (clamped to timeline end so an OUT
        # past the last clip still loops) back to IN point/start
        if self.loop_playback:
            loop_end = self.out_point if self.out_point is not None else self.timeline_end_time
            loop_end = min(loop_end, self.timeline_end_time)
            # Clamp loop start into the playable range too: an IN point set
            # before the first clip would otherwise make wrap_target land at
            # timeline_start_time >= loop_end and wrap again every tick.
            loop_start = self.in_point if self.in_point is not None else self.timeline_start_time
            loop_start = max(loop_start, self.timeline_start_time)
            if loop_end > loop_start and new_position >= loop_end:
                # PRE-ROLL by the audio output latency: video briefly replays
                # pre-IN context while the audio pipeline fills, so the first
                # audible sample lands exactly on the IN point - the cut being
                # auditioned is never swallowed by the latency gap.
                wrap_target = max(loop_start - self._audio_output_latency,
                                  self.timeline_start_time)
                self._seek_playhead(wrap_target)
                self._update_timeline_playback_state()
                self.timeline_update_id = self.root.after(16, self._timeline_playback_tick)
                return

        # Check if reached end
        if new_position >= self.timeline_end_time:
            self.timeline_playing = False
            self._seek_playhead(self.timeline_end_time)  # syncs clock/scheduler too
            self.tl_play_btn.configure(text="▶", bg=self.COLORS['accent'])
            self._stop_timeline_audio()
            return

        # Update playhead position
        self.playhead_position = new_position

        # OPTIMIZATION: Only update playhead visual (move existing canvas items)
        self._update_playhead_only()

        # OPTIMIZATION: Handle VLC and preview in a streamlined way
        self._update_timeline_playback_state()

        # Schedule next tick (~60fps for smooth playhead movement)
        self.timeline_update_id = self.root.after(16, self._timeline_playback_tick)

    def _update_timeline_playback_state(self):
        """Update VLC playback and UI state during timeline playback - optimized."""
        if not self.clips:
            return

        # Find topmost visible video clip under playhead (layer system)
        current_video_clip = self._get_topmost_visible_video_clip(self.playhead_position)

        # Calculate position within current clip (accounting for trim offset)
        position_in_clip = 0.0
        position_in_file = 0.0  # Actual position in source file (for VLC)
        if current_video_clip:
            position_in_clip = self.playhead_position - current_video_clip.timeline_start
            # Add trim offset for actual file position
            split_key = current_video_clip.path + f"_split_{id(current_video_clip)}"
            in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(current_video_clip.path, 0))
            position_in_file = position_in_clip + in_offset

        # Direct GPU engine plays this clip: show its frame, skip VLC entirely
        if current_video_clip and self._direct_ok(current_video_clip):
            if current_video_clip != self.timeline_last_clip:
                self.timeline_last_clip = current_video_clip
                self.selected_clip = current_video_clip
                self.selected_clips = [current_video_clip]
                self._update_timeline_audio_for_clip(current_video_clip)
                self._direct_stop_playback()  # new clip or a seek: restart decoding
            self._direct_playback_frame(current_video_clip, position_in_file)
            # (this early return used to skip the block below: in direct mode
            # the view never followed the playhead and the TC display froze)
            self._playback_ui_tick(current_video_clip, position_in_clip)
            return
        if self._direct_play_id is not None:
            self._direct_stop_playback()
            self._direct_release_display()
            self.timeline_last_clip = None  # make VLC load the clip below
        # Over empty timeline (or a VLC clip): still warm up the next engine
        # clip, else its first picture waits for a cold start (1.4-2s)
        if self.hw_player is not None and DIRECT_GPU_MODE != "Off":
            now_t = time.perf_counter()
            if now_t >= self._direct_next_check:
                self._direct_next_check = now_t + 0.25
                self._direct_prepare_next(current_video_clip)

        # Handle clip transitions
        clip_changed = current_video_clip != self.timeline_last_clip

        if clip_changed:
            self.timeline_last_clip = current_video_clip

            # Give VLC a grace period before drift correction - get_time() reports
            # stale values while a newly loaded clip is still spinning up
            self._last_vlc_drift_check = time.perf_counter()

            if current_video_clip:
                # Update selected clip reference (for UI)
                self.selected_clip = current_video_clip
                self.selected_clips = [current_video_clip]

                # Load new clip in VLC (use position_in_file for correct seek)
                if self.use_vlc:
                    self._vlc_timeline_load_clip(current_video_clip, position_in_file)

                # Handle audio for audio-only clips
                self._update_timeline_audio_for_clip(current_video_clip)

            else:
                # No clip at playhead - pause VLC
                # Use set_pause(1) not pause() - pause() is a TOGGLE!
                if self.use_vlc and self.vlc_player:
                    try:
                        self.vlc_player.set_pause(1)
                    except:
                        pass

        elif current_video_clip and self.use_vlc and self.vlc_player:
            # Ensure VLC is playing
            try:
                if not self.vlc_player.is_playing():
                    self.vlc_player.play()
            except:
                pass

            # DRIFT CORRECTION: VLC free-runs on its own clock, which slowly
            # drifts from the timeline (wall) clock within long clips. Check
            # every ~2s and re-seek only if drift exceeds 150ms, so corrections
            # are rare and lip-sync stays locked.
            now_t = time.perf_counter()
            if now_t - self._last_vlc_drift_check > 2.0:
                self._last_vlc_drift_check = now_t
                try:
                    vlc_ms = self.vlc_player.get_time()
                    if vlc_ms is not None and vlc_ms >= 0:
                        drift = (vlc_ms / 1000.0) - position_in_file
                        # Threshold must exceed libVLC's get_time() update
                        # granularity (~250-300ms) or the corrector itself
                        # causes periodic jumps on perfectly-synced playback.
                        if abs(drift) > 0.5:
                            self.vlc_player.set_time(int(position_in_file * 1000))
                except Exception:
                    pass

        self._playback_ui_tick(current_video_clip, position_in_clip)

    def _playback_ui_tick(self, current_video_clip, position_in_clip):
        """Per-tick UI during timeline playback (every path - VLC or direct)."""
        # Update position slider and labels (every ~2 ticks = ~32ms for smooth updates)
        self._ui_update_counter = (self._ui_update_counter + 1) % 2

        if self._ui_update_counter == 0:
            if current_video_clip:
                # Update position display
                self.preview_position = position_in_clip
                self.position_var.set(position_in_clip)
                self._update_position_label()

            # Update timecode display (every tick for smoothness)
            self._update_playhead_tc_display()

            # Auto-scroll check (uses deferred redraw so OK to check frequently)
            self._ensure_playhead_visible()

    def _sync_frame_buffer_seek(self):
        """Sync audio master clock and frame display scheduler after a seek operation."""
        if self.use_frame_buffer_system:
            if self.audio_master_clock:
                self.audio_master_clock.seek(self.playhead_position)
            if self.frame_display_scheduler:
                self.frame_display_scheduler.seek(self.playhead_position)

    def _seek_playhead(self, time_pos):
        """Unified playhead seek - keeps the wall-clock reference, audio mixer,
        frame scheduler and VLC in step, whether paused or playing.

        All playhead jumps (timeline click, nav keys, loop wrap, frame step)
        should go through here so every subsystem sees the same position.
        Preview refresh for the paused case is left to the caller.
        """
        time_pos = max(0.0, time_pos)
        self.playhead_position = time_pos
        if not self.timeline_playing:
            self._schedule_direct_warm()

        # Audio mixer: snap to the new position on the next callback
        self._audio_seek_pending = True

        # Frame buffer clock/scheduler (existing helper reads playhead_position)
        self._sync_frame_buffer_seek()

        if self.timeline_playing:
            # Re-anchor the wall-clock reference so the tick loop continues from here
            self.timeline_playback_start_time = time.perf_counter()
            self.timeline_playback_start_pos = time_pos
            # Force the playback state updater to reload/seek video on the next tick
            self.timeline_last_clip = None
            self._ensure_playhead_visible()

        self._update_playhead_only()
        self._update_playhead_tc_display()

    def _jump_playhead(self, time_pos, status=None):
        """Navigation jump: unified seek plus the standard UI refresh.

        Keeps playing if playback is active (the tick loop handles video);
        when paused, does the full timeline + preview refresh."""
        self._seek_playhead(time_pos)
        if not self.timeline_playing:
            self._draw_timeline()
            self._update_playhead_preview()
        if status:
            self.status_label.configure(text=status)

    # NOTE: _display_frame is defined once, near the other preview-canvas methods.
    # (A second, shadowed definition used to live here and was removed.)

    def _update_frame_buffer_ui_state(self):
        """Update UI state during frame buffer playback - lightweight updates only."""
        if not self.clips:
            return

        # Find video clip under playhead
        current_video_clip = None
        for clip in self.clips:
            if clip.duration > 0 and not clip.is_audio_only:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration
                if clip_start <= self.playhead_position < clip_end:
                    current_video_clip = clip
                    break

        # Calculate position within current clip
        position_in_clip = 0.0
        if current_video_clip:
            position_in_clip = self.playhead_position - current_video_clip.timeline_start

        # Handle clip transitions
        clip_changed = current_video_clip != self.timeline_last_clip

        if clip_changed:
            self.timeline_last_clip = current_video_clip
            if current_video_clip:
                # Update selected clip reference (for UI)
                self.selected_clip = current_video_clip
                self.selected_clips = [current_video_clip]

        # Update position slider and labels (every ~2 ticks = ~32ms for smooth updates)
        if not hasattr(self, '_fb_ui_update_counter'):
            self._fb_ui_update_counter = 0
        self._fb_ui_update_counter = (self._fb_ui_update_counter + 1) % 2

        if self._fb_ui_update_counter == 0:
            if current_video_clip:
                # Update position display
                self.preview_position = position_in_clip
                self.position_var.set(position_in_clip)
                self._update_position_label()

            # Update timecode display
            self._update_playhead_tc_display()

            # Auto-scroll check
            self._ensure_playhead_visible()

    def _vlc_timeline_load_clip(self, clip, position_in_clip):
        """Load a clip for timeline playback - optimized for smooth transitions."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            # ALWAYS mute VLC during timeline playback - do this FIRST
            self.vlc_player.audio_set_mute(True)

            ms = int(max(0.0, position_in_clip) * 1000)

            # Check if we need to load a different file
            self._vlc_wait_scrub_switch()
            current_path = getattr(self, 'vlc_media_path', None)
            if current_path != clip.path:
                # New file. libVLC ignores set_time() while the player is
                # stopped (no input thread yet), so a plain
                # set_media/set_time/play would start the clip from frame 0 -
                # the "moving to next clip resets playback" bug. Instead, seed
                # the start position via the media's start-time option so VLC
                # begins decoding at the right spot, then play.
                self.vlc_player.stop()
                media = self.vlc_instance.media_new(preview_source(clip.path))
                media.add_option(self._vlc_hw_option())
                if media:
                    media.add_option(f':start-time={ms / 1000.0:.3f}')
                    self.vlc_player.set_media(media)
                    self.vlc_media_path = clip.path
                    self.vlc_media = media
                self.vlc_player.audio_set_mute(True)
                self.vlc_player.play()
                self.vlc_player.audio_set_mute(True)
                # Belt-and-suspenders: once the input thread exists, set_time
                # takes effect and corrects any start-time rounding
                self.root.after(30, lambda m=ms: self._vlc_safe_set_time(m))
            else:
                # Same file already loaded - a plain seek works (it's playing)
                self.vlc_player.audio_set_mute(True)
                self.vlc_player.set_time(ms)
                if not self.vlc_player.is_playing():
                    self.vlc_player.play()
                self.vlc_player.audio_set_mute(True)

        except Exception:
            pass  # Silently ignore VLC errors during playback

    def _vlc_safe_set_time(self, ms):
        """Set VLC time only if still in timeline playback (deferred correction)."""
        if self.timeline_playing and self.use_vlc and self.vlc_player:
            try:
                self.vlc_player.set_time(int(ms))
                self.vlc_player.audio_set_mute(True)
            except Exception:
                pass

    def _update_vlc_audio_mute_silent(self):
        """Update VLC mute state without logging.

        During timeline playback, VLC is ALWAYS muted because we handle
        all audio through the multi-track audio mixer for proper mixing.
        """
        if not self.use_vlc or not self.vlc_player:
            return
        try:
            # During timeline playback, always mute VLC (audio handled by mixer)
            if self.timeline_playing:
                self.vlc_player.audio_set_mute(True)
                return

            # For single clip preview, use track mute state
            should_mute = False
            if self.selected_clip and not self.selected_clip.is_audio_only:
                track_idx = getattr(self.selected_clip, 'track_index', 0)
                camera_audio_track = (self.selected_clip.camera_id or f"Video {track_idx + 1}") + " Audio"
                should_mute = not self._is_track_audible(camera_audio_track)
            self.vlc_player.audio_set_mute(should_mute)
        except:
            pass

    def _update_timeline_audio_for_clip(self, clip):
        """Update audio playback when clip changes during timeline playback."""
        # When using multi-track audio mode, the mixer handles ALL audio
        # Don't start single-clip playback as it would overwrite the multi-track stream
        if hasattr(self, 'timeline_audio_mode') and self.timeline_audio_mode:
            return  # Multi-track mixer handles everything

        if clip.is_audio_only:
            # Check if track is muted
            track_id = self._get_audio_track_id(clip)
            if self._is_track_audible(track_id):
                self.selected_clip = clip
                position_in_clip = self.playhead_position - clip.timeline_start
                self.preview_position = position_in_clip
                self._start_audio_playback()
            else:
                self._stop_audio_playback()

    def _start_timeline_audio(self):
        """Start audio playback for timeline - use VLC for video clips."""
        # When using multi-track audio mode, the mixer handles ALL audio
        if hasattr(self, 'timeline_audio_mode') and self.timeline_audio_mode:
            return  # Multi-track mixer handles everything

        # Find current clip under playhead
        for clip in self.clips:
            if clip.duration > 0:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration
                if clip_start <= self.playhead_position < clip_end:
                    if not clip.is_audio_only and self.use_vlc:
                        # VLC handles audio for video clips - check mute state
                        track_idx = getattr(clip, 'track_index', 0)
                        camera_audio_track = (clip.camera_id or f"Video {track_idx + 1}") + " Audio"
                        if not self._is_track_audible(camera_audio_track):
                            # Camera audio track is muted, mute VLC
                            if self.vlc_player:
                                self.vlc_player.audio_set_mute(True)
                        return
                    elif clip.is_audio_only:
                        # Check if this audio track is muted
                        track_id = self._get_audio_track_id(clip)
                        if not self._is_track_audible(track_id):
                            # Track is muted, don't play audio
                            return
                        # Use sounddevice for audio-only clips
                        self.selected_clip = clip
                        position_in_clip = self.playhead_position - clip.timeline_start
                        self.preview_position = position_in_clip
                        self._start_audio_playback()
                        return

    def _get_audio_track_id(self, clip):
        """Get the track ID for an audio clip (matching timeline track naming)."""
        if clip.recording_id and clip.track_number is not None:
            if clip.track_number == 0:
                track_id = f"{clip.recording_id}_LR"  # Stereo mix
            else:
                track_id = f"{clip.recording_id}_Tr{clip.track_number}"
            # Mark LTC track
            if clip.is_ltc_track:
                track_id += " (LTC)"
        else:
            track_id = clip.camera_id or "Audio"
        return track_id

    def _stop_timeline_audio(self):
        """Stop timeline audio playback."""
        if self.use_vlc and self.vlc_player:
            self._vlc_pause()
        self._stop_audio_playback()

    # =========================================================================
    # Multi-Select Support
    # =========================================================================

    def _on_clip_click(self, event, clip: MediaClip, index: int):
        """Handle clip click with modifier key support for multi-selection."""
        # Check modifier keys
        ctrl_pressed = event.state & 0x4  # Control key
        shift_pressed = event.state & 0x1  # Shift key

        if ctrl_pressed:
            # Ctrl+Click: Toggle selection
            if clip in self.selected_clips:
                self.selected_clips.remove(clip)
            else:
                self.selected_clips.append(clip)
            self.last_clicked_index = index

        elif shift_pressed and self.last_clicked_index >= 0:
            # Shift+Click: Range selection
            start_idx = min(self.last_clicked_index, index)
            end_idx = max(self.last_clicked_index, index)

            # Add all clips in range to selection
            for i in range(start_idx, end_idx + 1):
                if i < len(self.clips) and self.clips[i] not in self.selected_clips:
                    self.selected_clips.append(self.clips[i])

        else:
            # Regular click: Single selection (clear others)
            self.selected_clips = [clip]
            self.last_clicked_index = index

        # Update the primary selected clip for preview
        if self.selected_clips:
            self.selected_clip = self.selected_clips[-1]  # Most recently selected
            self._update_preview_for_clip(self.selected_clip)
        else:
            self.selected_clip = None

        # Update selection count in status (don't overwrite during audio extraction)
        if not self.audio_extraction_running:
            count = len(self.selected_clips)
            if count > 1:
                self.status_label.configure(text=f"Selected {count} clips")
            elif count == 1:
                self.status_label.configure(text=f"Selected: {self.selected_clips[0].filename}")

        # Show the selection highlights (recolour only - no rebuild)
        self._update_clip_selection_display()

    def _on_clip_double_click(self, clip: MediaClip):
        """Handle double-click on clip card - copies timecode to clipboard."""
        if clip.start_tc:
            self._copy_to_clipboard(clip.start_tc)
        else:
            self.status_label.configure(text=f"No timecode available for {clip.filename}")

    def _get_clip_tooltip(self, clip: MediaClip) -> str:
        """Generate tooltip text for a clip card."""
        lines = []
        lines.append(f"📁 {clip.filename}")
        lines.append("")

        if clip.start_tc:
            lines.append(f"⏱ Start TC: {clip.start_tc}")
            if clip.end_tc:
                lines.append(f"⏱ End TC:   {clip.end_tc}")
            lines.append(f"🎬 FPS: {clip.fps_display}")
            if clip.drop_frame:
                lines.append("⚡ Drop Frame: Yes")
        else:
            lines.append("⚠ No timecode detected")

        if clip.duration > 0:
            dur_mins = int(clip.duration // 60)
            dur_secs = int(clip.duration % 60)
            lines.append(f"⏰ Duration: {dur_mins}:{dur_secs:02d}")

        if clip.camera_id:
            lines.append(f"📷 Camera: {clip.camera_id}")
            # Show user bits source if camera ID came from LTC user bits
            if clip.ub_camera_id is not None:
                lines.append("   (from LTC user bits)")

        # Show raw user bits if available
        if clip.user_bits:
            lines.append(f"🔢 User Bits: {clip.user_bits}")
            if clip.ub_date:
                lines.append(f"   Date: {clip.ub_date[:2]}/{clip.ub_date[2:4]}/20{clip.ub_date[4:6]}")

        if clip.ltc_channel >= 0:
            lines.append(f"🔊 LTC Channel: {clip.ltc_channel + 1}")

        if clip.has_linked_audio:
            lines.append("🔗 Has linked audio")

        # Multi-track recorder info
        if clip.recording_id and clip.track_number is not None:
            lines.append(f"🎤 Recording: {clip.recording_id} Track {clip.track_number}")
            if clip.split_part:
                lines.append(f"📎 Split file: Part {clip.split_part}")

        if clip.is_ltc_track:
            lines.append("⏱ LTC Reference Track")
        elif clip.linked_ltc_path:
            ltc_name = os.path.basename(clip.linked_ltc_path)
            lines.append(f"🔗 TC from: {ltc_name}")

        lines.append("")
        lines.append(f"📂 {clip.path}")

        return "\n".join(lines)

    def _update_preview_for_clip(self, clip: MediaClip):
        """Update preview panel for a clip without affecting selection state."""
        # Don't interfere with timeline playback - only update for single clip preview
        if self.timeline_playing:
            return

        self.preview_position = 0.0
        self.preview_playing = False

        # Stop any VLC playback and load new media for the clip
        if self.use_vlc and self.vlc_player:
            self._vlc_stop()
            if not clip.is_audio_only:
                self._vlc_load_media(clip.path)
                self._show_canvas()  # Show canvas initially for thumbnail

        # Clear CV frame cache for new clip
        self.cv_frame_cache.clear()
        self.cv_playback_running = False
        self._clear_cv_frame_queue()
        # Close any existing capture for clean start
        if self.cv_capture is not None:
            try:
                self.cv_capture.release()
            except:
                pass
            self.cv_capture = None
            self.cv_capture_path = None

        # Update preview UI
        self.preview_clip_label.configure(text=clip.filename)
        self.preview_fps_label.configure(text=clip.fps_display if clip.start_tc else "--")
        self.preview_cam_label.configure(text=clip.camera_id if clip.camera_id else "--")

        # Update slider range
        if clip.duration > 0:
            self.position_slider.configure(to=clip.duration)
        self.position_var.set(0)

        # Update position label
        self._update_position_label()

        # Update play button text
        self.play_btn.configure(text="▶ Play")

        # Extract and display first frame
        self._update_preview_frame()

        # Update timecode display
        self._update_preview_tc()

    # =========================================================================
    # Video Preview
    # =========================================================================

    # -------------------------------------------------------------------------
    # VLC Player Methods (Hardware-Accelerated Playback)
    # -------------------------------------------------------------------------

    def _direct_ok(self, clip) -> bool:
        """Use the direct GPU engine for this clip? Only when it is on, the
        clip has no ready proxy (hybrid: proxies scrub fastest) and a GPU can
        decode its format IN HARDWARE (tested once per format; untested ->
        not yet, the test runs in the background)."""
        if (self.hw_player is None or DIRECT_GPU_MODE == "Off" or DECODE_DEVICE == "CPU"
                or clip is None or clip.is_audio_only):
            return False
        if PREVIEW_PROXY_MODE != "Off" and PROXIES.get(clip.path):
            return False
        return bool(self.hw_player.route_for(clip.path))

    def _direct_use_canvas(self):
        # Check what is REALLY on screen, not only our flag: Play, the preview
        # Play button and J/K/L map the VLC area themselves - the flag stayed
        # True, the canvas was never re-shown, and every later frame went to
        # a hidden canvas (black preview after the first playback).
        self._direct_display = True
        if not self.preview_canvas.winfo_ismapped():
            self._show_canvas()
        if self.use_vlc and self.vlc_player:
            try:
                self.vlc_player.set_pause(1)
            except Exception:
                pass

    def _direct_release_display(self):
        """Hand the preview back to VLC (a clip the engine doesn't play)."""
        self._direct_still_id = None
        was_ours = self._direct_display
        self._direct_display = False
        if was_ours and not self.vlc_frame.winfo_ismapped():
            self._show_vlc_frame()

    def _direct_show_still(self, clip, position_in_file: float):
        """Show the exact frame at position_in_file (async, latest wins)."""
        self._direct_use_canvas()
        self._direct_still_id = self.hw_player.show(clip.path, position_in_file)
        self._direct_poll()

    def _direct_poll(self):
        """UI thread: put finished engine stills on the preview canvas."""
        self._direct_poll_id = None
        item = self.hw_player.take_still() if self.hw_player else None
        if item and item[0] == self._direct_still_id and self._direct_display:
            self._display_frame(ImageTk.PhotoImage(Image.fromarray(item[3])))
        if self.hw_player and self._direct_still_id is not None and self.hw_player.busy():
            self._direct_poll_id = self.root.after(10, self._direct_poll)
        elif self.hw_player and item is None and self._direct_still_id is not None:
            # request may not have started yet - look once more shortly
            self._direct_poll_id = self.root.after(15, self._direct_poll_once_more)

    def _direct_poll_once_more(self):
        self._direct_poll_id = None
        if self.hw_player and (self.hw_player.busy() or self.hw_player._still is not None):
            self._direct_poll()

    def _direct_stop_playback(self):
        if self._direct_play_id is not None and self.hw_player:
            self.hw_player.stop()
        self._direct_play_id = None
        self._direct_play_path = None
        self._direct_last_frame_t = None

    def _direct_playback_frame(self, clip, position_in_file: float):
        """Timeline tick: show the engine frame for the playhead; (re)start
        decoding only when the clip changes or the playhead really jumped.

        Being LATE is not a reason to restart: the decoder runs >3x real time
        and catches up (late frames are dropped by take_play_frame). The old
        rule restarted whenever the picture lagged 0.5s - but a cold start
        (QSV init, uncached file) takes longer than that, so every restart
        triggered the next one: seconds of black/frozen picture."""
        now = time.perf_counter()
        restart = self._direct_play_id is None or self._direct_play_path != clip.path
        if not restart:
            frame, earliest = self.hw_player.take_play_frame(self._direct_play_id, position_in_file)
            if frame is not None:
                if self._direct_first_pending:
                    self._direct_first_pending = False
                    # learn the start-up delay (request -> first frame shown)
                    lat = min(1.5, max(0.05, now - self._direct_play_started))
                    self._direct_start_latency = 0.7 * self._direct_start_latency + 0.3 * lat
                self._direct_last_frame_t = frame[0]
                self._display_frame(ImageTk.PhotoImage(Image.fromarray(frame[1])))
            last = self._direct_last_frame_t
            if earliest is not None and earliest > position_in_file + 0.75:
                restart = True   # jumped backwards: everything queued is ahead
            elif last is not None and position_in_file - last > 3.0:
                restart = True   # jumped far forwards
            elif last is None and now - self._direct_play_started > 4.0:
                restart = True   # nothing at all for 4s: try again
        if restart:
            self._direct_use_canvas()
            # start a little ahead so the first frame lands on time - unless a
            # warm session is ready right here (paused warm-up / prepare-ahead)
            lead = 0.0 if self.hw_player.is_prepared(clip.path, position_in_file) else self._direct_start_latency
            self._direct_play_id = self.hw_player.play(clip.path, position_in_file + lead)
            self._direct_play_path = clip.path
            self._direct_last_frame_t = None
            self._direct_play_started = now
            self._direct_first_pending = True
        # every ~0.25s: warm up the clip the playhead reaches next
        if now >= self._direct_next_check:
            self._direct_next_check = now + 0.25
            self._direct_prepare_next(clip)

    def _direct_warm_at_playhead(self):
        """Paused: open the clip under the playhead in the background with the
        frame AT the playhead ready, so pressing Play starts instantly even in
        a clip never shown before (a cold Intel start is ~0.6-0.8s, up to 2s
        with other GPU work). Debounced: only once the playhead rests."""
        self._direct_warm_id = None
        if self.timeline_playing or self.hw_player is None or DIRECT_GPU_MODE == "Off":
            return
        clip = self._get_topmost_visible_video_clip(self.playhead_position)
        if clip is None:
            return
        if not self._direct_ok(clip):
            # format test still running (e.g. right after loading a project):
            # try again shortly instead of giving up
            if self.hw_player.route_for(clip.path) is False:
                self._schedule_direct_warm(300)
            return
        split_key = clip.path + f"_split_{id(clip)}"
        in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(clip.path, 0))
        pos = self.playhead_position - clip.timeline_start + in_offset
        # prepared AT the playhead: play() then starts right here, no lead time
        self.hw_player.prepare(clip.path, pos)

    def _schedule_direct_warm(self, delay_ms: int = 150):
        if self.hw_player is None or DIRECT_GPU_MODE == "Off":
            return
        if getattr(self, '_direct_warm_id', None):
            try:
                self.root.after_cancel(self._direct_warm_id)
            except Exception:
                pass
        self._direct_warm_id = self.root.after(delay_ms, self._direct_warm_at_playhead)

    def _direct_prepare_next(self, current_clip):
        """Open the next clip (topmost layer 0.5-2s ahead) before it starts."""
        for ahead in (0.5, 1.0, 1.5, 2.0):
            t = self.playhead_position + ahead
            nxt = self._get_topmost_visible_video_clip(t)
            if nxt is not None and nxt is not current_clip:
                if self._direct_ok(nxt):
                    split_key = nxt.path + f"_split_{id(nxt)}"
                    in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(nxt.path, 0))
                    start_in_file = max(0.0, t - nxt.timeline_start) + in_offset
                    # where its first shown frame will be: its start (or the
                    # moment it becomes the top layer)
                    self.hw_player.prepare(nxt.path, max(in_offset, start_in_file - ahead))
                return

    def _sync_proxy_dir(self):
        """The scratch disk (the cache folder chosen in Settings) holds
        waveforms, proxies/ and temp/ (this session's temporary files)."""
        if getattr(self, 'waveform_cache_dir', None):
            PROXIES.set_directory(os.path.join(self.waveform_cache_dir, 'proxies'))
            PROXIES.notify = self.queue.put  # thread-safe; handled in _update_loop
            if getattr(self, 'analyzer', None) is not None:
                self.analyzer.set_temp_root(os.path.join(self.waveform_cache_dir, 'temp'))
            self._scratch_housekeeping()

    def _scratch_housekeeping(self):
        """Background: remove leftover temp folders and keep waveforms +
        proxies under the size limit (it was never enforced before)."""
        scratch = getattr(self, 'waveform_cache_dir', None)

        def work():
            roots = [tempfile.gettempdir()]
            if scratch:
                roots.append(os.path.join(scratch, 'temp'))
            removed, freed = cleanup_stale_temp_dirs(roots)
            self._enforce_scratch_limit()
            if removed:
                self.queue.put(('status', f"Scratch disk: removed {removed} leftover temp folder(s), "
                                          f"{freed / 2**20:.0f} MB freed"))
        threading.Thread(target=work, daemon=True, name='scratch-housekeeping').start()

    def _scratch_usage(self, scratch: str = None) -> dict:
        """Bytes and file counts per kind on the scratch disk."""
        scratch = scratch or getattr(self, 'waveform_cache_dir', None)
        out = {'waveforms': [0, 0], 'proxies': [0, 0], 'temp': [0, 0]}
        if not scratch or not os.path.isdir(scratch):
            return out
        for name in os.listdir(scratch):
            if name.endswith('.waveform'):
                try:
                    out['waveforms'][0] += os.path.getsize(os.path.join(scratch, name)); out['waveforms'][1] += 1
                except OSError:
                    pass
        for kind, sub, ext in (('proxies', 'proxies', '.mp4'), ('temp', 'temp', '')):
            for dirpath, _, files in os.walk(os.path.join(scratch, sub)):
                for f in files:
                    if f.endswith(ext):
                        try:
                            out[kind][0] += os.path.getsize(os.path.join(dirpath, f)); out[kind][1] += 1
                        except OSError:
                            pass
        return out

    def _enforce_scratch_limit(self):
        """Keep waveforms + proxies under the Settings size limit, least
        recently used first (files in use can't be deleted and are skipped)."""
        scratch = getattr(self, 'waveform_cache_dir', None)
        if not scratch or not os.path.isdir(scratch):
            return
        limit = float(getattr(self, 'waveform_cache_max_size_gb', 5)) * 2**30
        files = []
        for name in os.listdir(scratch):
            if name.endswith('.waveform'):
                files.append(os.path.join(scratch, name))
        pdir = os.path.join(scratch, 'proxies')
        if os.path.isdir(pdir):
            files += [os.path.join(pdir, f) for f in os.listdir(pdir)
                      if f.endswith('.mp4') and not f.endswith('.part.mp4')]
        stats = []
        for f in files:
            try:
                st = os.stat(f)
                stats.append((max(st.st_atime, st.st_mtime), st.st_size, f))
            except OSError:
                pass
        total = sum(s for _, s, _ in stats)
        if total <= limit:
            return
        for _, size, f in sorted(stats):
            if total <= limit * 0.9:
                break
            try:
                os.remove(f)
                total -= size
            except OSError:
                pass

    def _queue_preview_proxies(self):
        """Build proxies for video clips, nearest the playhead first (and test
        their formats for direct GPU playback)."""
        if PREVIEW_PROXY_MODE == "Off" and (self.hw_player is None or DIRECT_GPU_MODE == "Off"):
            return
        clips = [c for c in self.clips if not c.is_audio_only and c.status != 'error' and os.path.exists(c.path)]
        clips.sort(key=lambda c: abs(c.timeline_start + c.duration / 2 - self.playhead_position))
        if self.hw_player and DIRECT_GPU_MODE != "Off":
            self.hw_player.probe([c.path for c in clips])
        PROXIES.enqueue([c.path for c in clips])

    def _on_proxy_ready(self, original_path: str):
        """A proxy finished: make the NEXT preview of that clip use it."""
        if getattr(self, 'cv_capture_path', None) == original_path:
            self._close_cv_capture()
        if self.vlc_media_path == original_path and not self.timeline_playing and not self._is_scrubbing:
            self.vlc_media_path = None  # forces a reload (from the proxy) on next show
            if self.selected_clip and self.selected_clip.path == original_path:
                self._update_preview_frame()

    def _vlc_hw_option(self) -> str:
        """Per-media VLC decoder option for the Decode setting (verified in
        VLC's log: ':avcodec-hw=none' -> "no hw decoder modules matched")."""
        return ':avcodec-hw=none' if DECODE_DEVICE == "CPU" else ':avcodec-hw=any'

    def _gpu_decoder_text(self) -> str:
        if _GPU_LIST is None:
            return "detected on first use"
        gpu = selected_gpu(ignore_cpu_mode=True)
        return gpu['name'] if gpu else "none found (using CPU)"

    def _on_decode_device_change(self, *args):
        """Apply the Decode setting. FFmpeg grabs and new VLC media pick it up
        immediately; open OpenCV captures are reopened so they switch too."""
        global DECODE_DEVICE
        value = self.decode_device.get()
        if value not in DECODE_DEVICES or value == DECODE_DEVICE:
            return
        DECODE_DEVICE = value
        if getattr(self, 'cv_capture', None) is not None:
            self._close_cv_capture()
        if value == "CPU":
            msg = "Decode: CPU - no GPU video decoding"
        else:
            if value == "GPU" and _GPU_LIST is not None and not selected_gpu():
                msg = "Decode: GPU requested, but no working GPU decoder was found - using CPU"
            else:
                msg = f"Decode: {value} - takes effect from the next clip loaded"
        if hasattr(self, 'status_label'):
            self.status_label.configure(text=msg)

    def _init_vlc_player(self):
        """Initialize VLC instance and media player."""
        if not VLC_AVAILABLE:
            return

        try:
            # Create VLC instance with hardware acceleration and optimized for large files
            self.vlc_instance = vlc.Instance([
                '--no-xlib',  # No X11 dependency
                '--quiet',  # Less console output
                '--avcodec-hw=any',  # Enable hardware decoding
                '--no-video-title-show',  # Don't show filename overlay
                '--file-caching=5000',  # 5 second file cache for large files
                '--disc-caching=3000',  # 3 second disc cache
                '--network-caching=3000',  # 3 second network cache
                '--clock-jitter=0',  # Reduce clock jitter
                '--avcodec-skiploopfilter=4',  # Skip loop filter for faster decode
                '--avcodec-fast',  # Faster decoding (less accurate)
                '--avcodec-threads=0',  # Auto thread count
                '--sout-mux-caching=5000',  # Output mux caching
            ])
            if not self.vlc_instance:
                self.use_vlc = False
                return

            self.vlc_player = self.vlc_instance.media_player_new()
            if not self.vlc_player:
                self.use_vlc = False
                return

            self.use_vlc = True

            # Force update the frame to ensure it has a valid window handle
            self.vlc_frame.update_idletasks()

        except Exception:
            self.use_vlc = False

    def _vlc_switch_media_for_scrub(self, path: str, position_seconds: float):
        """Show `path` at `position_seconds` without blocking the UI thread.

        The UI thread only records the target; one worker thread does every
        libVLC call (stop, set_media, play, pause, seek). Targets that arrive
        while it works overwrite each other - only the latest is shown, so a
        fast drag across several clips loads at most what is needed.
        """
        with self._vlc_switch_lock:
            self._vlc_switch_target = (path, position_seconds)
            self.vlc_media_path = path
            if self._vlc_switch_busy:
                return  # the running worker picks up the new target
            self._vlc_switch_busy = True
        # Tk work stays on the UI thread; only once per worker (it calls
        # update(), which must not run on every 30Hz scrub tick)
        self._vlc_set_window()
        self._vlc_switch_thread = threading.Thread(
            target=self._vlc_scrub_switch_worker, name='vlc-scrub-switch', daemon=True)
        self._vlc_switch_thread.start()

    def _vlc_scrub_switch_worker(self):
        """Worker for _vlc_switch_media_for_scrub - never touches Tk."""
        loaded, shown = None, None
        try:
            while True:
                with self._vlc_switch_lock:
                    path, pos = self._vlc_switch_target
                    if path == loaded and pos == shown:
                        self._vlc_switch_busy = False
                        return
                if path != loaded:
                    self.vlc_player.stop()
                    media = self.vlc_instance.media_new(preview_source(path))
                    media.add_option(self._vlc_hw_option())
                    # Open AT the target instead of at 0 then seeking: one
                    # decode instead of two before the first picture
                    media.add_option(f':start-time={max(0.0, pos):.3f}')
                    self.vlc_player.set_media(media)
                    self.vlc_media = media
                    loaded = path
                    # VLC needs to play briefly to decode and display a frame;
                    # set_time is ignored until the input thread exists
                    self.vlc_player.play()
                    time.sleep(0.05)
                    continue  # target may have moved on while we loaded
                if not self.timeline_playing:
                    self.vlc_player.set_pause(1)  # set_pause, not the pause() toggle
                self.vlc_player.set_time(int(max(0.0, pos) * 1000))
                shown = pos
        except Exception:
            with self._vlc_switch_lock:
                self._vlc_switch_busy = False

    def _vlc_wait_scrub_switch(self, timeout: float = 2.0):
        """Let an in-flight scrub switch finish before another path drives
        the player (two threads loading media would fight over it)."""
        worker = self._vlc_switch_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout)

    def _vlc_load_media(self, path: str):
        """Load a media file into VLC player."""
        if not self.use_vlc or not self.vlc_player:
            return False

        self._vlc_wait_scrub_switch()
        try:
            # Stop any current playback
            self.vlc_player.stop()

            # Create new media
            self.vlc_media = self.vlc_instance.media_new(preview_source(path))
            self.vlc_media.add_option(self._vlc_hw_option())
            if not self.vlc_media:
                return False

            self.vlc_player.set_media(self.vlc_media)
            self.vlc_media_path = path  # Store path for later
            return True
        except Exception:
            return False

    def _vlc_set_window(self):
        """Set VLC output window handle (must be called after frame is visible)."""
        if not self.vlc_player:
            return False

        try:
            # Ensure the VLC frame is packed and realized
            self._show_vlc_frame()
            self.vlc_frame.update()
            self.root.update_idletasks()

            hwnd = self.vlc_frame.winfo_id()

            # Embed VLC output into our frame
            if os.name == 'nt':  # Windows
                self.vlc_player.set_hwnd(hwnd)
            else:  # Linux/Mac
                self.vlc_player.set_xwindow(hwnd)

            return True
        except Exception:
            return False  # Silent failure

    def _vlc_play(self):
        """Start VLC playback."""
        if not self.use_vlc or not self.vlc_player:
            return False

        try:
            # Set window handle AFTER frame is visible
            self._vlc_set_window()

            # Start playback
            self.vlc_player.play()

            # Apply mute state AFTER playback starts (VLC requires this)
            self.root.after(50, self._update_vlc_audio_mute)

            # Start position update timer
            self._vlc_start_position_update()
            return True
        except Exception:
            return False  # Silent failure

    def _update_vlc_audio_mute(self):
        """Update VLC audio mute state based on camera audio track mute."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            # During timeline playback, always mute VLC (audio handled by mixer)
            if self.timeline_playing:
                self.vlc_player.audio_set_mute(True)
                return

            # Check if current clip's camera audio track is muted
            should_mute = False
            if self.selected_clip and not self.selected_clip.is_audio_only:
                track_idx = getattr(self.selected_clip, 'track_index', 0)
                camera_audio_track = (self.selected_clip.camera_id or f"Video {track_idx + 1}") + " Audio"
                should_mute = not self._is_track_audible(camera_audio_track)

            self.vlc_player.audio_set_mute(should_mute)
        except Exception:
            pass  # Silently ignore mute errors

    def _vlc_check_state(self):
        """Check VLC player state (disabled in production for performance)."""
        # Disabled - was causing console spam during playback
        pass

    def _vlc_pause(self):
        """Pause VLC playback."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            # Use set_pause(1) not pause() - pause() is a TOGGLE!
            self.vlc_player.set_pause(1)
            self._vlc_stop_position_update()
        except Exception:
            pass  # Silent failure

    def _vlc_stop(self):
        """Stop VLC playback."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            self.vlc_player.stop()
            self._vlc_stop_position_update()
        except Exception:
            pass  # Silent failure

    def _vlc_seek(self, position_seconds: float):
        """Seek VLC to a specific position in seconds."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            # VLC set_time expects milliseconds
            ms = int(position_seconds * 1000)
            self.vlc_player.set_time(ms)
        except Exception:
            pass  # Silently ignore seek errors during playback

    def _vlc_scrub_frame(self, position_seconds: float):
        """Pause VLC and seek to position - called after brief play to display frame."""
        if not self.use_vlc or not self.vlc_player:
            return
        try:
            # Always pause unless timeline is actually playing
            # Use set_pause(1) not pause() - pause() is a TOGGLE!
            if not self.timeline_playing:
                self.vlc_player.set_pause(1)  # Explicitly set pause state
            ms = int(position_seconds * 1000)
            self.vlc_player.set_time(ms)
        except Exception:
            pass

    def _vlc_seek_and_render(self, position_seconds: float):
        """Seek to position and force VLC to render the frame.

        VLC doesn't update display when paused and set_time is called.
        We briefly play, seek, then immediately pause to render the new frame.
        This is called during scrubbing for frame-accurate preview.
        """
        if not self.use_vlc or not self.vlc_player:
            return
        try:
            ms = int(position_seconds * 1000)

            # Cancel any pending pause callback to avoid race conditions
            if hasattr(self, '_vlc_pause_after_id') and self._vlc_pause_after_id:
                try:
                    self.root.after_cancel(self._vlc_pause_after_id)
                except Exception:
                    pass
                self._vlc_pause_after_id = None

            # Always seek first
            self.vlc_player.set_time(ms)

            # Check VLC state - if paused/stopped, need to play briefly to render
            state = self.vlc_player.get_state()
            if state == vlc.State.Paused or state == vlc.State.Stopped:
                self.vlc_player.play()
                # Schedule pause very quickly - track the ID to cancel if needed
                self._vlc_pause_after_id = self.root.after(20, self._vlc_pause_after_seek)
            # If already playing, the seek alone will update the display
        except Exception:
            pass

    def _vlc_pause_after_seek(self):
        """Pause VLC after seek - helper for seek_and_render."""
        self._vlc_pause_after_id = None
        # Pause VLC after rendering frame (unless timeline is actually playing)
        # Use set_pause(1) not pause() - pause() is a TOGGLE!
        if self.use_vlc and self.vlc_player and not self.timeline_playing:
            try:
                self.vlc_player.set_pause(1)  # Explicitly set pause state
            except Exception:
                pass

    def _vlc_set_rate(self, rate: float):
        """Set VLC playback rate."""
        if not self.use_vlc or not self.vlc_player:
            return

        try:
            self.vlc_player.set_rate(rate)
        except Exception:
            pass  # Silent failure

    def _vlc_get_position(self) -> float:
        """Get current VLC position in seconds."""
        if not self.use_vlc or not self.vlc_player:
            return 0.0

        try:
            # VLC get_time returns milliseconds
            return self.vlc_player.get_time() / 1000.0
        except:
            return 0.0

    def _vlc_start_position_update(self):
        """Start timer to update UI position from VLC."""
        self._vlc_stop_position_update()  # Cancel any existing timer

        def update():
            if not self.preview_playing or not self.use_vlc:
                return

            try:
                # Get VLC position
                pos = self._vlc_get_position()
                if pos >= 0:
                    self.preview_position = pos
                    self.position_var.set(pos)
                    self._update_position_label()
                    self._update_preview_tc()

                # Check if reached end
                state = self.vlc_player.get_state()
                if state == vlc.State.Ended:
                    self.preview_playing = False
                    self.play_btn.configure(text="▶ Play")
                    self._vlc_stop_position_update()
                    return

                # Schedule next update
                self.vlc_position_update_id = self.root.after(33, update)

            except Exception:
                pass  # Silent failure to avoid console spam

        self.vlc_position_update_id = self.root.after(33, update)

    def _vlc_stop_position_update(self):
        """Stop the VLC position update timer."""
        if self.vlc_position_update_id:
            self.root.after_cancel(self.vlc_position_update_id)
            self.vlc_position_update_id = None

    def _show_vlc_frame(self):
        """Show VLC frame and hide canvas."""
        self.preview_canvas.pack_forget()
        self.vlc_frame.pack(fill=tk.BOTH, expand=True)

    def _show_canvas(self):
        """Show canvas and hide VLC frame."""
        self.vlc_frame.pack_forget()
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)

    def _force_stop_preview(self):
        """Unconditionally stop a clip-preview session.

        Unlike _toggle_playback (which bails when selected_clip is None and
        can therefore leave preview_playing latched), this always clears the
        preview state and its tick chain. It deliberately does NOT touch VLC
        or close the audio stream - callers that start timeline playback next
        will take those over; other callers can follow with _reset_preview."""
        self.preview_playing = False
        self.playback_speed = 1.0
        if getattr(self, 'preview_update_id', None):
            try:
                self.root.after_cancel(self.preview_update_id)
            except Exception:
                pass
            self.preview_update_id = None
        if hasattr(self, 'play_btn'):
            self.play_btn.configure(text="▶ Play")

    def _reset_preview(self):
        """Reset preview to empty state."""
        # Stop any playback
        self.preview_playing = False
        if hasattr(self, 'play_btn'):
            self.play_btn.configure(text="▶ Play")

        # Stop VLC
        if self.use_vlc and self.vlc_player:
            self._vlc_wait_scrub_switch()
            try:
                self.vlc_player.stop()
                self._vlc_stop_position_update()
            except:
                pass
            self.vlc_media_path = None
            self.vlc_media = None

        # Stop audio
        self._stop_audio_playback()

        # Show canvas instead of VLC frame
        self._show_canvas()

        # Clear and draw placeholder
        self.preview_canvas.delete('all')
        w = self.preview_canvas.winfo_width()
        h = self.preview_canvas.winfo_height()
        self.preview_canvas.create_text(
            w // 2 if w > 1 else 240,
            h // 2 if h > 1 else 135,
            text="Click a clip to preview",
            fill=self.COLORS['text_dim'],
            font=('Segoe UI', 11)
        )

        # Reset position display
        self.preview_position = 0
        self.position_var.set(0)
        if hasattr(self, 'duration_label'):
            self.duration_label.configure(text="Duration: --:--")
        if hasattr(self, 'position_label'):
            self.position_label.configure(text="00:00:00:00")

    def _vlc_cleanup(self):
        """Clean up VLC resources."""
        try:
            self._vlc_stop_position_update()
            if self.vlc_player:
                self.vlc_player.stop()
                self.vlc_player.release()
            if self.vlc_instance:
                self.vlc_instance.release()
        except:
            pass

    # -------------------------------------------------------------------------
    # Clip Selection and Preview
    # -------------------------------------------------------------------------

    def _select_clip(self, clip: MediaClip):
        """Select a clip for preview (single selection mode)."""
        self.selected_clip = clip
        self.selected_clips = [clip]  # Also update multi-select list
        self.preview_position = 0.0
        self.preview_playing = False

        # Stop any VLC playback and load new media
        if self.use_vlc and self.vlc_player:
            self._vlc_stop()
            if not clip.is_audio_only:
                self._vlc_load_media(clip.path)
                self._show_canvas()  # Show canvas initially for thumbnail

        # Clear CV frame cache for new clip
        self.cv_frame_cache.clear()
        self.cv_playback_running = False
        self._clear_cv_frame_queue()
        # Close any existing capture for clean start
        if self.cv_capture is not None:
            try:
                self.cv_capture.release()
            except:
                pass
            self.cv_capture = None
            self.cv_capture_path = None

        # Update preview UI
        self.preview_clip_label.configure(text=clip.filename)
        self.preview_fps_label.configure(text=clip.fps_display if clip.start_tc else "--")
        self.preview_cam_label.configure(text=clip.camera_id if clip.camera_id else "--")

        # Update slider range
        if clip.duration > 0:
            self.position_slider.configure(to=clip.duration)
        self.position_var.set(0)

        # Update position label
        self._update_position_label()

        # Update play button text
        self.play_btn.configure(text="▶ Play")

        # Update timecode display
        self._update_preview_tc()

        # Don't overwrite status during audio extraction
        if not self.audio_extraction_running:
            self.status_label.configure(text=f"Selected: {clip.filename}")

        # Extract first frame asynchronously to avoid blocking UI
        self.root.after(10, self._update_preview_frame)

    def _toggle_playback(self):
        """Toggle video playback."""
        if not self.selected_clip:
            return

        self.preview_playing = not self.preview_playing

        if self.preview_playing and self.timeline_playing:
            # Symmetric guard: starting a clip preview while the timeline is
            # playing would fight over self.audio_stream - stop the timeline.
            self._toggle_timeline_playback()

        if self.preview_playing:
            self.play_btn.configure(text="⏸ Pause")

            # Use VLC for video playback if available
            if self.use_vlc and not self.selected_clip.is_audio_only:
                # IMPORTANT: Show VLC frame BEFORE playing
                self._show_vlc_frame()
                # Seek to current position and play
                self.status_label.configure(text=f"Playing (VLC): {self.selected_clip.filename}")
                self._vlc_seek(self.preview_position)
                self._vlc_play()
            else:
                # Fallback to OpenCV/FFmpeg playback
                self.status_label.configure(text=f"Playing (OpenCV): {self.selected_clip.filename}")
                if OPENCV_AVAILABLE and not self.selected_clip.is_audio_only:
                    fps = self.selected_clip.fps if self.selected_clip.fps and self.selected_clip.fps > 0 else 30.0
                    start_frame = int(self.preview_position * fps)
                    self._ensure_cv_extraction_running(start_frame)
                # Start audio playback (only needed for non-VLC)
                self._start_audio_playback()
                self._playback_tick()
        else:
            self.play_btn.configure(text="▶ Play")

            # Pause VLC if using it
            if self.use_vlc and not self.selected_clip.is_audio_only:
                self._vlc_pause()
                # Get position from VLC
                self.preview_position = self._vlc_get_position()
            else:
                if self.preview_update_id:
                    self.root.after_cancel(self.preview_update_id)
                    self.preview_update_id = None
                # Stop CV extraction thread
                self.cv_playback_running = False
                # Clear frame queue
                self._clear_cv_frame_queue()
                # Stop audio playback
                self._stop_audio_playback()

    def _start_audio_playback(self):
        """Start audio playback for the current clip."""
        if not SOUNDDEVICE_AVAILABLE or not self.selected_clip:
            return

        try:
            # Check if we have cached audio for this clip
            if self.selected_clip.path not in self.audio_cache:
                # Extract audio in a thread to avoid blocking
                self.status_label.configure(text="Loading audio...")
                thread = threading.Thread(target=self._extract_clip_audio,
                                         args=(self.selected_clip,), daemon=True)
                thread.start()
                return

            # Get cached audio data
            audio_data, sample_rate = self.audio_cache[self.selected_clip.path]

            if audio_data is None or len(audio_data) == 0:
                return

            # Calculate starting sample position
            self.audio_sample_rate = sample_rate
            start_sample = int(self.preview_position * sample_rate)
            start_sample = max(0, min(start_sample, len(audio_data) - 1))
            self.audio_position = start_sample
            self.audio_data = audio_data

            # Stop any existing stream
            if self.audio_stream is not None:
                try:
                    self.audio_stream.stop()
                    self.audio_stream.close()
                except:
                    pass
                self.audio_stream = None

            # Get selected audio device
            device_id = None
            if hasattr(self, 'audio_device_var') and hasattr(self, 'audio_device_ids'):
                device_name = self.audio_device_var.get()
                if device_name in self.audio_device_ids:
                    device_id = self.audio_device_ids[device_name]

            # Always use stereo output for compatibility
            channels = 2

            # Create output stream with callback
            self.audio_stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=channels,
                device=device_id,
                callback=self._audio_callback,
                blocksize=4096,  # Larger buffer for stability (~85ms at 48kHz)
                latency='high'  # Higher latency for more stable playback
            )
            self.audio_stream.start()

        except Exception:
            pass  # Silent failure to avoid console spam

    def _audio_callback(self, outdata, frames, time_info, status):
        """Callback for audio stream - fills output buffer for SINGLE CLIP preview only.

        NOTE: This callback is for single clip preview, NOT timeline playback.
        Timeline audio is handled by _timeline_audio_callback.
        """
        # Only play during single clip preview, NOT timeline mode
        # Timeline has its own audio callback (_timeline_audio_callback)
        if self.timeline_playing or self.audio_data is None or not self.preview_playing:
            outdata.fill(0)
            return

        # Sync audio position with video position for better sync
        expected_pos = int(self.preview_position * self.audio_sample_rate)
        if abs(expected_pos - self.audio_position) > self.audio_sample_rate // 4:
            # Re-sync if more than 0.25 seconds off (tighter sync for timeline)
            self.audio_position = expected_pos

        start = self.audio_position
        end = start + frames

        if start < 0:
            start = 0
        if start >= len(self.audio_data):
            outdata.fill(0)
            return

        try:
            # Get audio chunk
            if len(self.audio_data.shape) > 1 and self.audio_data.shape[1] >= 2:
                # Stereo source
                chunk = self.audio_data[start:end]
                if len(chunk) < frames:
                    # Pad with zeros if at end
                    padded = np.zeros((frames, 2), dtype=np.float32)
                    padded[:len(chunk)] = chunk[:, :2]
                    chunk = padded
                else:
                    chunk = chunk[:, :2]  # Take first 2 channels only
                outdata[:] = chunk
            else:
                # Mono - duplicate to both channels
                if len(self.audio_data.shape) > 1:
                    chunk = self.audio_data[start:end, 0]
                else:
                    chunk = self.audio_data[start:end]
                if len(chunk) < frames:
                    padded = np.zeros(frames, dtype=np.float32)
                    padded[:len(chunk)] = chunk
                    chunk = padded
                outdata[:, 0] = chunk
                outdata[:, 1] = chunk

            self.audio_position = end
        except Exception:
            outdata.fill(0)  # Silent on errors to avoid callback spam

    def _stop_audio_playback(self):
        """Stop audio playback."""
        if self.audio_stream is not None:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except:
                pass
            self.audio_stream = None

        # Close lazy-loaded WAV file handles
        self._close_wav_file_cache()

    # =========================================================================
    # Multi-Track Timeline Audio Mixing
    # =========================================================================

    def _prepare_timeline_audio(self):
        """Prepare audio for ALL clips before timeline playback starts.

        During timeline playback, VLC is muted and the Python audio mixer handles
        all audio mixing. This includes both external audio files AND camera audio
        from video clips.
        """
        if not SOUNDDEVICE_AVAILABLE or not FFMPEG_AVAILABLE or not NUMPY_AVAILABLE:
            return

        # Find ALL clips that need audio extraction (both audio-only and video)
        # During timeline playback, VLC is muted, so we need to mix camera audio too
        clips_needing_audio = []
        for clip in self.clips:
            if clip.duration > 0 and clip.path not in self.audio_cache:
                clips_needing_audio.append(clip)

        if not clips_needing_audio:
            self.timeline_audio_ready = True
            return

        # Queue all clips for extraction
        self.audio_extraction_queue = clips_needing_audio.copy()
        self.timeline_audio_ready = False

        # Start extraction in background
        if not self.audio_extraction_running:
            self.audio_extraction_running = True
            thread = threading.Thread(target=self._process_audio_extraction_queue, daemon=True)
            thread.start()

    def _process_audio_extraction_queue(self):
        """Process all queued clips for audio extraction using parallel processing."""
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
        import os

        clips_to_extract = self.audio_extraction_queue.copy()
        total = len(clips_to_extract)

        if total == 0:
            self.audio_extraction_running = False
            self.timeline_audio_ready = True
            return

        # Use up to 4 parallel workers (balance speed vs system load)
        max_workers = min(4, os.cpu_count() or 2, total)
        completed_count = 0

        def extract_and_track(clip):
            """Extract audio and return clip path for tracking."""
            self._extract_clip_audio_silent(clip)
            return clip.path

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all extraction tasks
                futures = {executor.submit(extract_and_track, clip): clip for clip in clips_to_extract}

                # Process completed tasks with timeout
                for future in as_completed(futures, timeout=300):  # 5 min total timeout
                    try:
                        future.result(timeout=60)  # 60 sec per file timeout
                        completed_count += 1
                    except (TimeoutError, Exception):
                        completed_count += 1

                    # Update status
                    try:
                        count = completed_count
                        self.root.after(0, lambda c=count, t=total: self.status_label.configure(
                            text=f"Loading audio... ({c}/{t})"))
                    except:
                        pass

        except (TimeoutError, Exception):
            pass  # Silent failure

        self.audio_extraction_running = False
        self.timeline_audio_ready = True

        # Update status
        try:
            self.root.after(0, lambda: self.status_label.configure(text="Audio ready for playback"))
        except:
            pass

    def _extract_clip_audio_silent(self, clip):
        """Extract audio from a clip via memory streaming (no disk I/O).

        Uses FFmpeg stdout piping to extract audio directly to memory,
        avoiding temp file writes which are slow for batch processing.
        """
        if clip.path in self.audio_cache:
            return  # Already cached

        try:
            # Extract audio directly to stdout as raw PCM (no file I/O)
            cmd = [
                FFMPEG_PATH, '-y', '-i', clip.path,
                '-vn',  # No video
                '-ac', '2',  # Stereo
                '-ar', '48000',  # 48kHz sample rate
                '-f', 's16le',  # Raw PCM format
                '-acodec', 'pcm_s16le',
                'pipe:1'  # Output to stdout
            ]

            kwargs = {'capture_output': True, 'timeout': 120}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            if result.returncode == 0 and len(result.stdout) >= 4:
                # Convert raw bytes directly to numpy array
                audio_data = np.frombuffer(result.stdout, dtype=np.int16)
                audio_data = audio_data.reshape(-1, 2)
                audio_data = audio_data.astype(np.float32) / 32768.0
                self.audio_cache[clip.path] = (audio_data, 48000)

        except Exception:
            pass  # Silent failure

    def _lazy_read_audio_chunk(self, clip, start_sample: int, num_frames: int) -> np.ndarray:
        """Read audio from cache, or trigger background loading if not cached.

        Uses streaming read for large files to avoid UI freeze.
        Returns numpy array of shape (num_frames, 2) with float32 audio data.
        """
        try:
            # Check if already fully loaded in audio_cache (works for any file type)
            # Video clips are pre-extracted by _prepare_timeline_audio
            if clip.path in self.audio_cache:
                cache_entry = self.audio_cache[clip.path]
                if cache_entry is not None:
                    audio_data, cached_rate = cache_entry
                    if audio_data is not None and len(audio_data) > 0:
                        # Serve from cache
                        audio_start = int(start_sample * cached_rate / 48000) if cached_rate != 48000 else start_sample
                        if 0 <= audio_start < len(audio_data):
                            audio_end = min(audio_start + num_frames, len(audio_data))
                            chunk = audio_data[audio_start:audio_end]
                            if len(chunk) < num_frames:
                                padded = np.zeros((num_frames, 2), dtype=np.float32)
                                padded[:len(chunk)] = chunk
                                return padded
                            return chunk
                return None

            # Not fully loaded: read from the segment cache - camera MP4/MOV and WAV alike. (Before, only WAV
            # files were loaded here - camera audio stayed silent unless that clip had been previewed - and a
            # WAV was loaded WHOLE, gigabytes for a long recording.)
            return self.seg_audio.read(clip.path, start_sample, num_frames)

        except Exception:
            return None

    def _load_wav_background(self, path: str, filename: str):
        """Load WAV file in background thread to avoid UI freeze."""
        try:
            wf = wave.open(path, 'rb')
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            n_frames_total = wf.getnframes()

            # Read entire file
            raw_data = wf.readframes(n_frames_total)
            wf.close()

            if len(raw_data) == 0:
                return

            # Convert to numpy array
            if sample_width == 2:  # 16-bit
                audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
            elif sample_width == 3:  # 24-bit
                raw_bytes = np.frombuffer(raw_data, dtype=np.uint8)
                b0 = raw_bytes[0::3].astype(np.int32)
                b1 = raw_bytes[1::3].astype(np.int32)
                b2 = raw_bytes[2::3].astype(np.int32)
                vals = b0 | (b1 << 8) | (b2 << 16)
                vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
                audio = vals.astype(np.float32) / 8388608.0
            elif sample_width == 4:  # 32-bit
                audio = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                return

            # Reshape to stereo
            if n_channels == 1:
                audio = np.column_stack([audio, audio])
            elif n_channels >= 2:
                audio = audio.reshape(-1, n_channels)[:, :2]

            # Resample to 48kHz if needed
            if sample_rate != 48000:
                target_len = int(len(audio) * 48000 / sample_rate)
                if target_len > 0:
                    indices = np.linspace(0, len(audio) - 1, target_len)
                    audio = np.column_stack([
                        np.interp(indices, np.arange(len(audio)), audio[:, 0]),
                        np.interp(indices, np.arange(len(audio)), audio[:, 1])
                    ]).astype(np.float32)
                sample_rate = 48000

            # Store in audio_cache
            self.audio_cache[path] = (audio.astype(np.float32), sample_rate)

        except Exception:
            pass  # Silent failure
        finally:
            # Remove from loading set
            if hasattr(self, '_wav_loading_set'):
                self._wav_loading_set.discard(path)
                # If all loading complete, update status from main thread
                if len(self._wav_loading_set) == 0:
                    try:
                        self.root.after(0, lambda: self.status_label.configure(text="Audio ready"))
                    except Exception:
                        pass

    def _close_wav_file_cache(self):
        """Close all cached WAV file handles and buffers."""
        if hasattr(self, '_wav_buffer_cache'):
            for entry in self._wav_buffer_cache.values():
                try:
                    if entry[3]:  # wf handle
                        entry[3].close()
                except:
                    pass
            self._wav_buffer_cache.clear()
        if hasattr(self, '_wav_file_cache'):
            for wf in self._wav_file_cache.values():
                try:
                    wf.close()
                except:
                    pass
            self._wav_file_cache.clear()

    def _preload_audio_at_playhead(self, playhead_pos: float):
        """Queue the audio pieces of every clip under / just after the playhead (camera and recorder audio),
        so playback has sound right away. Cheap: only queues work for the segment-cache worker."""
        if not self.clips:
            return
        lookahead = 20.0
        for clip in list(self.clips):
            if clip.duration <= 0 or clip.path in self.audio_cache:
                continue
            clip_start = clip.timeline_start
            clip_end = clip_start + clip.duration
            if clip_end <= playhead_pos or clip_start > playhead_pos + lookahead:
                continue
            in_offset = self.clip_in_offsets.get(clip.path + f"_split_{id(clip)}", self.clip_in_offsets.get(clip.path, 0)) or 0
            a = max(playhead_pos, clip_start) - clip_start + in_offset
            b = min(playhead_pos + lookahead, clip_end) - clip_start + in_offset
            self.seg_audio.prefetch(clip.path, a, b)

    def _start_timeline_audio_stream(self):
        """Start the multi-track audio stream for timeline playback."""
        if not SOUNDDEVICE_AVAILABLE:
            return

        try:
            # Stop any existing stream
            if self.audio_stream is not None:
                try:
                    self.audio_stream.stop()
                    self.audio_stream.close()
                except:
                    pass
                self.audio_stream = None

            # Initialize audio position tracker to current playhead
            self.timeline_audio_position = self.playhead_position

            # Get selected audio device
            device_id = None
            if hasattr(self, 'audio_device_var') and hasattr(self, 'audio_device_ids'):
                device_name = self.audio_device_var.get()
                if device_name in self.audio_device_ids:
                    device_id = self.audio_device_ids[device_name]

            # Create output stream with multi-track callback
            # Use larger buffer for more stable playback with lazy loading
            self.audio_stream = sd.OutputStream(
                samplerate=48000,
                channels=2,
                device=device_id,
                callback=self._timeline_audio_callback,
                blocksize=8192,  # ~170ms buffer for lazy loading
                latency='high'
            )

            # Measure output latency and arm the mixer snap BEFORE start(), so
            # even the very first callback mixes the latency-compensated position
            try:
                self._audio_output_latency = min(float(self.audio_stream.latency or 0.0), 0.5)
            except Exception:
                self._audio_output_latency = 0.0
            self._audio_seek_pending = True

            self.audio_stream.start()

        except Exception:
            pass  # Silent failure to avoid console spam

    def _timeline_audio_callback(self, outdata, frames, time_info, status):
        """Multi-track audio callback - mixes all audible tracks at playhead position."""
        if not self.timeline_playing:
            outdata.fill(0)
            return

        try:
            sample_rate = 48000
            frame_duration = frames / sample_rate

            # SMOOTH AUDIO: Track own position, resync on seeks.
            # Free-running between seeks prevents artifacts from playhead jitter.

            # LATENCY COMPENSATION: samples mixed now are heard ~output-latency
            # later, so mix slightly ahead of the on-screen playhead to keep
            # heard audio aligned with the displayed video frame.
            latency = self._audio_output_latency

            # ORDER MATTERS (cross-thread): clear the seek flag BEFORE reading
            # the playhead. _seek_playhead writes playhead first, flag second -
            # this ordering guarantees we never consume the flag with a stale
            # pre-seek playhead value (worst case: the flag is set again right
            # after we clear it, and the next callback re-snaps harmlessly).
            if self._audio_seek_pending:
                self._audio_seek_pending = False
                audio_pos = self.playhead_position + latency
                self._audio_smooth_pos = audio_pos
            else:
                ui_playhead = self.playhead_position
                audio_pos = self._audio_smooth_pos
                if abs(ui_playhead + latency - audio_pos) > 1.0:
                    # Failsafe for direct playhead writes that bypassed _seek_playhead
                    audio_pos = ui_playhead + latency
                    self._audio_smooth_pos = audio_pos

            # Use smooth audio position
            playhead_time = audio_pos

            # Advance audio position for next callback
            self._audio_smooth_pos = audio_pos + frame_duration

            # Initialize mixed output
            mixed = np.zeros((frames, 2), dtype=np.float32)
            active_tracks = 0

            # Take a snapshot of clips list to prevent race conditions
            # (main thread may modify self.clips during playback)
            clips_snapshot = list(self.clips)

            # Find all clips at the current playhead position
            # Mix both audio-only clips AND video clips' embedded audio (camera audio)
            # During timeline playback, VLC is muted, so we handle all audio here
            for clip in clips_snapshot:
                if clip.duration <= 0:
                    continue

                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration

                # Check if clip is at playhead position
                if not (clip_start <= playhead_time < clip_end):
                    continue

                # Get track ID for mute check
                # For video clips, use the camera audio track format
                if clip.is_audio_only:
                    track_id = self._get_audio_track_id(clip)
                else:
                    # Camera audio track format matches timeline track names
                    track_idx = getattr(clip, 'track_index', 0)
                    track_id = (clip.camera_id or f"Video {track_idx + 1}") + " Audio"

                # Check if track is muted
                if not self._is_track_audible(track_id):
                    continue

                # Calculate position in clip's audio
                # Account for trim offset (if clip was trimmed at IN point)
                position_in_clip = playhead_time - clip_start
                split_key = clip.path + f"_split_{id(clip)}"
                in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(clip.path, 0))
                audio_position_in_file = position_in_clip + in_offset

                # Try cached audio first, then fall back to lazy loading
                cache_entry = self.audio_cache.get(clip.path)
                chunk = None

                if cache_entry is not None:
                    # Use cached audio
                    audio_data, cached_rate = cache_entry
                    if audio_data is not None and len(audio_data) > 0:
                        audio_start = int(audio_position_in_file * cached_rate)
                        if 0 <= audio_start < len(audio_data):
                            audio_end = min(audio_start + frames, len(audio_data))
                            chunk = audio_data[audio_start:audio_end]

                if chunk is None or len(chunk) == 0:
                    # LAZY LOADING: Read directly from WAV file
                    audio_start = int(audio_position_in_file * 48000)
                    chunk = self._lazy_read_audio_chunk(clip, audio_start, frames)

                if chunk is None or len(chunk) == 0:
                    continue

                # Pad if necessary
                if len(chunk) < frames:
                    padded = np.zeros((frames, 2), dtype=np.float32)
                    padded[:len(chunk)] = chunk
                    chunk = padded

                # Add to mix
                mixed += chunk
                active_tracks += 1

            # Normalize if multiple tracks to prevent clipping
            if active_tracks > 1:
                # Simple normalization - divide by sqrt of track count for headroom
                mixed = mixed / np.sqrt(active_tracks)

            # Clip to valid range
            mixed = np.clip(mixed, -1.0, 1.0)
            outdata[:] = mixed

        except Exception:
            outdata.fill(0)
            # Silently handle audio errors to avoid console spam during playback

    def _get_clip_track_id(self, clip):
        """Get the track ID for any clip (audio or video)."""
        if clip.is_audio_only:
            return self._get_audio_track_id(clip)
        else:
            track_idx = getattr(clip, 'track_index', 0)
            return (clip.camera_id or f"Video {track_idx + 1}") + " Audio"

    def _extract_clip_audio(self, clip):
        """Extract audio from a clip for playback via memory streaming.

        Uses FFmpeg stdout piping to avoid disk I/O. Runs in background thread.
        """
        if not FFMPEG_AVAILABLE or not NUMPY_AVAILABLE:
            return

        try:

            # Extract full audio directly to stdout as raw PCM (no file I/O)
            cmd = [
                FFMPEG_PATH, '-y', '-i', clip.path,
                '-vn',  # No video
                '-ac', '2',  # Stereo
                '-ar', '48000',  # 48kHz sample rate
                '-f', 's16le',  # Raw PCM signed 16-bit little-endian
                '-acodec', 'pcm_s16le',
                'pipe:1'  # Output to stdout
            ]

            kwargs = {'capture_output': True, 'timeout': 120}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            if result.returncode == 0 and len(result.stdout) >= 4:
                # Convert raw bytes directly to numpy array
                audio_data = np.frombuffer(result.stdout, dtype=np.int16)
                # Reshape to stereo (2 channels)
                audio_data = audio_data.reshape(-1, 2)
                # Normalize to float32 in range -1 to 1
                audio_data = audio_data.astype(np.float32) / 32768.0

                # Cache the audio data
                self.audio_cache[clip.path] = (audio_data, 48000)

                # Update status and start playback if still playing
                self.root.after(0, lambda: self._on_audio_extracted())

        except Exception:
            pass  # Silent failure to avoid console spam

    def _on_audio_extracted(self):
        """Called when audio extraction completes."""
        self.status_label.configure(text="Audio ready")
        # If still playing, start the audio
        if self.preview_playing:
            self._start_audio_playback()

    def _playback_tick(self):
        """Update playback position with variable speed support."""
        if not self.preview_playing or not self.selected_clip:
            return

        # Advance position based on playback speed (approximately 30fps update)
        self.preview_position += (1.0 / 30.0) * self.playback_speed

        # Check if reached start (reverse playback)
        if self.preview_position <= 0:
            self.preview_position = 0
            self.preview_playing = False
            self.playback_speed = 1.0
            self.play_btn.configure(text="▶ Play")
            self._stop_audio_playback()
            self.status_label.configure(text="Start of clip")
            return

        # Check if reached end
        if self.preview_position >= self.selected_clip.duration:
            self.preview_position = self.selected_clip.duration
            self.preview_playing = False
            self.playback_speed = 1.0
            self.play_btn.configure(text="▶ Play")
            self._stop_audio_playback()
            return

        # Update UI
        self.position_var.set(self.preview_position)
        self._update_position_label()
        self._update_preview_tc()

        # Update frame - with buffer, this should be fast
        # For video clips, try to update every tick; for audio, no update needed
        if not self.selected_clip.is_audio_only:
            self._update_preview_frame()

        # Schedule next tick (faster at higher speeds for smoother playback)
        tick_interval = max(16, int(33 / abs(self.playback_speed))) if abs(self.playback_speed) > 1 else 33
        self.preview_update_id = self.root.after(tick_interval, self._playback_tick)

    # =========================================================================
    # J/K/L Playback Controls
    # =========================================================================

    def _jkl_reverse(self):
        """J key - Play reverse / increase reverse speed."""
        if self.timeline_playing:
            return  # J/K/L drive the clip PREVIEW; don't hijack timeline playback
        if not self.selected_clip:
            return

        # VLC doesn't support reverse playback well, so just step back
        if self.use_vlc and not self.selected_clip.is_audio_only:
            # Step back 1 second
            self.preview_position = max(0, self.preview_position - 1)
            self._vlc_seek(self.preview_position)
            self.position_var.set(self.preview_position)
            self._update_position_label()
            self._update_preview_tc()
            self.status_label.configure(text="Step back 1s")
            return

        if self.playback_speed > 0:
            # Currently forward, switch to reverse
            self.playback_speed = -1.0
        else:
            # Already reverse, increase speed
            current_idx = 0
            for i, speed in enumerate(self.playback_speed_levels):
                if abs(self.playback_speed) <= speed:
                    current_idx = i
                    break
            next_idx = min(current_idx + 1, len(self.playback_speed_levels) - 1)
            self.playback_speed = -self.playback_speed_levels[next_idx]

        # Start playback if not playing
        if not self.preview_playing:
            self.preview_playing = True
            self.play_btn.configure(text="⏸ Pause")
            # Start audio (only for 1x forward speed)
            if self.playback_speed == 1.0:
                self._start_audio_playback()
            self._playback_tick()

        self._update_speed_indicator()

    def _jkl_pause(self):
        """K key - Pause playback (timeline if playing, else preview)."""
        if self.timeline_playing:
            self._toggle_timeline_playback()
            return
        if self.preview_playing:
            self.preview_playing = False
            self.playback_speed = 1.0
            self.play_btn.configure(text="▶ Play")

            # Pause VLC if using it
            if self.use_vlc and self.selected_clip and not self.selected_clip.is_audio_only:
                self._vlc_pause()
                self.preview_position = self._vlc_get_position()
            else:
                if self.preview_update_id:
                    self.root.after_cancel(self.preview_update_id)
                    self.preview_update_id = None
                # Stop audio playback
                self._stop_audio_playback()

        self.status_label.configure(text="Paused")

    def _jkl_forward(self):
        """L key - Play forward / increase forward speed."""
        if self.timeline_playing:
            return  # J/K/L drive the clip PREVIEW; don't hijack timeline playback
        if not self.selected_clip:
            return

        # Use VLC for playback
        if self.use_vlc and not self.selected_clip.is_audio_only:
            if self.playback_speed < 0:
                self.playback_speed = 1.0
            else:
                # Increase speed
                current_idx = 0
                for i, speed in enumerate(self.playback_speed_levels):
                    if self.playback_speed <= speed:
                        current_idx = i
                        break
                next_idx = min(current_idx + 1, len(self.playback_speed_levels) - 1)
                self.playback_speed = self.playback_speed_levels[next_idx]

            # Set VLC rate
            self._vlc_set_rate(self.playback_speed)

            # Start playback if not playing
            if not self.preview_playing:
                self.preview_playing = True
                self.play_btn.configure(text="⏸ Pause")
                self._show_vlc_frame()  # Show VLC frame before playing
                self._vlc_seek(self.preview_position)
                self._vlc_play()

            self._update_speed_indicator()
            return

        if self.playback_speed < 0:
            # Currently reverse, switch to forward
            self.playback_speed = 1.0
        else:
            # Already forward, increase speed
            current_idx = 0
            for i, speed in enumerate(self.playback_speed_levels):
                if self.playback_speed <= speed:
                    current_idx = i
                    break
            next_idx = min(current_idx + 1, len(self.playback_speed_levels) - 1)
            self.playback_speed = self.playback_speed_levels[next_idx]

        # Start playback if not playing
        if not self.preview_playing:
            self.preview_playing = True
            self.play_btn.configure(text="⏸ Pause")
            # Start audio (only for 1x forward speed)
            if self.playback_speed == 1.0:
                self._start_audio_playback()
            self._playback_tick()

        self._update_speed_indicator()

    def _update_speed_indicator(self):
        """Update status to show current playback speed."""
        if self.playback_speed == 1.0:
            self.status_label.configure(text="Playing 1x")
        elif self.playback_speed == -1.0:
            self.status_label.configure(text="Reverse 1x")
        elif self.playback_speed > 0:
            self.status_label.configure(text=f"Playing {self.playback_speed}x")
        else:
            self.status_label.configure(text=f"Reverse {abs(self.playback_speed)}x")

    def _seek_start(self):
        """Seek to start of clip."""
        if not self.selected_clip:
            return
        self.preview_position = 0.0
        self.position_var.set(0)
        self._update_position_label()
        self._update_preview_tc()
        self._update_preview_frame()

    def _seek_end(self):
        """Seek to end of clip."""
        if not self.selected_clip:
            return
        self.preview_position = max(0, self.selected_clip.duration - 0.1)
        self.position_var.set(self.preview_position)
        self._update_position_label()
        self._update_preview_tc()
        self._update_preview_frame()

    def _on_slider_change(self, value):
        """Handle slider position change."""
        if not self.selected_clip:
            return

        # Skip during timeline playback - position is controlled by playback tick
        # This avoids redundant updates and VLC seek conflicts
        if self.timeline_playing:
            return

        self.preview_position = float(value)
        self._update_position_label()
        self._update_preview_tc()

        # Seek VLC if using it
        if self.use_vlc and not self.selected_clip.is_audio_only:
            self._vlc_seek(self.preview_position)

        # Update frame (debounced - only when not playing)
        if not self.preview_playing:
            self._update_preview_frame()

    def _update_position_label(self):
        """Update the position time label."""
        if not self.selected_clip:
            self.position_label.configure(text="0:00 / 0:00")
            return

        current = int(self.preview_position)
        total = int(self.selected_clip.duration)

        cur_min, cur_sec = divmod(current, 60)
        tot_min, tot_sec = divmod(total, 60)

        self.position_label.configure(text=f"{cur_min}:{cur_sec:02d} / {tot_min}:{tot_sec:02d}")

    def _update_preview_tc(self):
        """Update the timecode display based on current position."""
        if not self.selected_clip or not self.selected_clip.start_tc:
            self.preview_tc_label.configure(text="--:--:--:--")
            return

        # Calculate current timecode
        fps = self.selected_clip.fps if self.selected_clip.fps > 0 else 30.0
        start_frames = self.selected_clip.start_frames
        current_frames = start_frames + int(self.preview_position * fps)

        # Convert to timecode
        total_secs, frames = divmod(current_frames, int(round(fps)))
        total_mins, secs = divmod(total_secs, 60)
        hours, mins = divmod(total_mins, 60)

        tc_str = f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"
        self.preview_tc_label.configure(text=tc_str)

    def _update_preview_frame(self):
        """Extract and display current frame with hardware acceleration."""
        if not self.selected_clip:
            return

        # For audio-only clips, show audio waveform placeholder
        if self.selected_clip.is_audio_only:
            self._display_audio_preview()
            return

        if not PIL_AVAILABLE:
            self._display_placeholder("PIL not available")
            return

        fps = self.selected_clip.fps if self.selected_clip.fps and self.selected_clip.fps > 0 else 30.0
        # Add trim offset to get actual file position
        split_key = self.selected_clip.path + f"_split_{id(self.selected_clip)}"
        in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(self.selected_clip.path, 0))
        actual_position = self.preview_position + in_offset
        frame_num = int(actual_position * fps)

        # During playback - non-blocking frame display
        if self.preview_playing:
            if OPENCV_AVAILABLE:
                # Process any frames from the queue (non-blocking, max 2 per tick)
                self._process_cv_frame_queue()

                # Display from cache if available (try current frame or nearest)
                if frame_num in self.cv_frame_cache:
                    self._display_frame(self.cv_frame_cache[frame_num])
                else:
                    # Try to find nearest cached frame
                    for offset in range(1, 5):
                        if frame_num - offset in self.cv_frame_cache:
                            self._display_frame(self.cv_frame_cache[frame_num - offset])
                            break

                # Ensure background extraction is running
                self._ensure_cv_extraction_running(frame_num)
            # Don't block or use FFmpeg during playback - audio continues regardless
            return

        # Direct GPU engine: exact frame, decoded off the UI thread
        if self._direct_ok(self.selected_clip):
            self._direct_show_still(self.selected_clip, actual_position)
            return

        # For scrubbing/paused state - can use blocking operations
        # Check OpenCV cache first
        if OPENCV_AVAILABLE and frame_num in self.cv_frame_cache:
            self._display_frame(self.cv_frame_cache[frame_num])
            return

        # For paused state, extract single frame (blocking is OK when paused)
        if OPENCV_AVAILABLE:
            cv_frame = self._get_cv_frame_sync(self.preview_position)
            if cv_frame:
                self._display_frame(cv_frame)
                return

        # Fallback to FFmpeg if OpenCV not available or failed
        if not FFMPEG_AVAILABLE:
            self._display_placeholder("ffmpeg or OpenCV not available")
            return

        # Check cache (for scrubbing/single frame display)
        cache_key = f"{self.selected_clip.path}_{self.preview_position:.2f}"
        if cache_key in self.preview_frame_cache:
            self._display_frame(self.preview_frame_cache[cache_key])
            return

        # Extract frame using ffmpeg (GPU or CPU per the Decode setting)
        try:
            hw = ffmpeg_frame_hwaccel()  # lazy detection; None = CPU
            temp_path = os.path.join(self.analyzer.temp_dir, "preview_frame.jpg")
            hw_in, scale_vf = ffmpeg_frame_grab_args(hw, '480', '-2')

            # Input seeking (before input for fast seeking)
            cmd = [FFMPEG_PATH, '-y', *hw_in,
                   '-ss', str(self.preview_position),
                   '-i', preview_source(self.selected_clip.path),
                   '-vframes', '1', '-q:v', '2', *scale_vf,
                   temp_path]

            kwargs = {'capture_output': True, 'text': True, 'timeout': 5}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            # Fallback to software decoding if hardware fails
            if result.returncode != 0 and hw:
                cmd = [
                    FFMPEG_PATH, '-y', '-ss', str(self.preview_position),
                    '-i', preview_source(self.selected_clip.path),
                    '-vframes', '1', '-q:v', '2',
                    '-vf', 'scale=480:-1',
                    temp_path
                ]
                result = subprocess.run(cmd, **kwargs)

            if result.returncode == 0 and os.path.exists(temp_path):
                img = Image.open(temp_path)
                # Resize to fit canvas while maintaining aspect ratio
                img.thumbnail((480, 270), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)

                # Cache the frame (limit cache size)
                if len(self.preview_frame_cache) > 100:
                    # Remove oldest entries
                    keys = list(self.preview_frame_cache.keys())[:50]
                    for k in keys:
                        del self.preview_frame_cache[k]
                self.preview_frame_cache[cache_key] = photo

                self._display_frame(photo)

                try:
                    os.remove(temp_path)
                except:
                    pass

        except Exception:
            pass  # Silent failure

    def _display_frame(self, photo):
        """Display a frame on the preview canvas with optional timecode overlay.

        Called both by direct preview code and by FrameDisplayScheduler."""
        if not hasattr(self, 'preview_canvas') or not self.preview_canvas:
            return
        self.preview_canvas.delete('all')
        self.preview_canvas.create_image(240, 135, image=photo, anchor='center')
        # Keep reference to prevent garbage collection
        self.preview_canvas.image = photo

        # Draw timecode overlay if enabled
        if hasattr(self, 'show_tc_overlay') and self.show_tc_overlay.get():
            self._draw_tc_overlay()

    def _display_placeholder(self, message: str = "No preview"):
        """Display a placeholder message on the preview canvas."""
        self.preview_canvas.delete('all')
        self.preview_canvas.image = None

        # Draw dark background
        self.preview_canvas.create_rectangle(0, 0, 480, 270, fill=self.COLORS['timeline_bg'], outline='')

        # Draw message
        self.preview_canvas.create_text(240, 135, text=message,
                                        fill=self.COLORS['text_dim'], font=('Segoe UI', 11))

    def _display_audio_preview(self):
        """Display audio waveform visualization for audio-only clips."""
        self.preview_canvas.delete('all')
        self.preview_canvas.image = None

        # Dark background
        self.preview_canvas.create_rectangle(0, 0, 480, 270, fill='#1a1a2e', outline='')

        # Audio icon
        self.preview_canvas.create_text(240, 80, text="🎵", font=('Segoe UI', 48), fill='#4a9eff')

        # Clip name
        if self.selected_clip:
            name = self.selected_clip.filename
            if len(name) > 40:
                name = name[:37] + "..."
            self.preview_canvas.create_text(240, 150, text=name,
                                            fill=self.COLORS['text'], font=('Segoe UI', 11, 'bold'))

            # Show recording info if available
            if self.selected_clip.recording_id and self.selected_clip.track_number is not None:
                info = f"{self.selected_clip.recording_id} Track {self.selected_clip.track_number}"
                if self.selected_clip.is_ltc_track:
                    info += " (LTC)"
                self.preview_canvas.create_text(240, 175, text=info,
                                                fill='#ffaa00', font=('Segoe UI', 10))

            # Duration
            if self.selected_clip.duration > 0:
                dur_mins = int(self.selected_clip.duration // 60)
                dur_secs = int(self.selected_clip.duration % 60)
                dur_text = f"Duration: {dur_mins}:{dur_secs:02d}"
                self.preview_canvas.create_text(240, 200, text=dur_text,
                                                fill=self.COLORS['text_dim'], font=('Segoe UI', 9))

        # Draw simple waveform visualization
        self._draw_audio_waveform_preview()

    def _draw_audio_waveform_preview(self):
        """Draw a stylized waveform visualization in the preview."""
        import random
        center_y = 230
        width = 400
        start_x = 40
        bar_width = 3
        max_height = 30

        # Generate simple waveform bars
        num_bars = width // (bar_width + 2)
        for i in range(num_bars):
            x = start_x + i * (bar_width + 2)
            # Vary height for visual interest
            height = random.randint(5, max_height)
            color = '#4a9eff' if i % 3 == 0 else '#3a7abf'
            self.preview_canvas.create_rectangle(
                x, center_y - height, x + bar_width, center_y + height,
                fill=color, outline=''
            )

    def _draw_tc_overlay(self):
        """Draw timecode overlay on the preview canvas."""
        if not self.selected_clip or not self.selected_clip.start_tc:
            return

        # Calculate current timecode
        fps = self.selected_clip.fps if self.selected_clip.fps > 0 else 30.0
        start_frames = self.selected_clip.start_frames
        current_frames = start_frames + int(self.preview_position * fps)

        # Convert to timecode
        total_secs, frames = divmod(current_frames, int(round(fps)))
        total_mins, secs = divmod(total_secs, 60)
        hours, mins = divmod(total_mins, 60)

        tc_str = f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"

        # Draw background box
        bbox_width = 180
        bbox_height = 30
        x1 = 240 - bbox_width // 2
        y1 = 250 - bbox_height
        x2 = 240 + bbox_width // 2
        y2 = 250

        self.preview_canvas.create_rectangle(x1, y1, x2, y2,
                                             fill='#000000', outline='#333333',
                                             stipple='gray50', tags='tc_overlay')

        # Draw timecode text
        self.preview_canvas.create_text(240, y1 + bbox_height // 2,
                                        text=tc_str, fill='#00ff88',
                                        font=('Consolas', 16, 'bold'),
                                        anchor='center', tags='tc_overlay')

        # Draw FPS badge
        fps_str = self.selected_clip.fps_display
        self.preview_canvas.create_text(x2 - 5, y1 + bbox_height // 2,
                                        text=fps_str, fill='#ffaa00',
                                        font=('Consolas', 8),
                                        anchor='e', tags='tc_overlay')

    # =========================================================================
    # Frame Buffer for Smooth Playback
    # =========================================================================

    def _start_frame_buffer_extraction(self, clip, start_frame: int = 0):
        """Start extracting frames into the buffer in a background thread."""
        if not FFMPEG_AVAILABLE or not PIL_AVAILABLE:
            return

        # If already extracting for this clip and position, skip
        if self.frame_buffer_extracting:
            return

        # If different clip, clear the buffer
        if self.frame_buffer_clip != clip.path:
            with self.frame_buffer_lock:
                self.frame_buffer.clear()
                self.frame_buffer_clip = clip.path

        self.frame_buffer_extracting = True
        self.frame_buffer_target_start = start_frame

        thread = threading.Thread(
            target=self._extract_frame_batch,
            args=(clip, start_frame),
            daemon=True
        )
        thread.start()

    def _extract_frame_batch(self, clip, start_frame: int):
        """Extract a batch of frames using FFmpeg (runs in background thread)."""
        try:
            # Determine FPS - use clip FPS if available, else default to 30
            fps = clip.fps if clip.fps and clip.fps > 0 else 30.0
            self.frame_buffer_fps = fps

            # Extract frames for the next few seconds
            frames_to_extract = min(self.frame_buffer_size, 90)  # Max 3 seconds at 30fps
            start_time = start_frame / fps

            # Don't extract past the end of the clip
            max_frames = int(clip.duration * fps) if clip.duration > 0 else 10000
            end_frame = min(start_frame + frames_to_extract, max_frames)

            if start_frame >= end_frame:
                self.frame_buffer_extracting = False
                return

            # Create temp directory for frames
            temp_dir = os.path.join(self.analyzer.temp_dir, f"frames_{id(clip)}")
            os.makedirs(temp_dir, exist_ok=True)

            # Use FFmpeg to extract frames as image sequence
            output_pattern = os.path.join(temp_dir, "frame_%05d.jpg")

            cmd = [FFMPEG_PATH, '-y']

            # Input seeking (before input for fast seeking)
            cmd.extend(['-ss', f'{start_time:.3f}'])
            cmd.extend(['-i', preview_source(clip.path)])

            # Limit frames and output as image sequence
            cmd.extend(['-frames:v', str(end_frame - start_frame)])
            cmd.extend(['-q:v', '3'])  # Quality (2-5 is good)
            cmd.extend(['-vf', 'scale=480:-1'])  # Scale to preview size
            cmd.extend(['-r', str(fps)])  # Ensure correct frame rate
            cmd.append(output_pattern)

            kwargs = {'capture_output': True, 'text': True, 'timeout': 30}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            if result.returncode == 0:
                # Load extracted frames into buffer
                frame_files = sorted([f for f in os.listdir(temp_dir) if f.startswith('frame_')])

                for i, frame_file in enumerate(frame_files):
                    frame_path = os.path.join(temp_dir, frame_file)
                    frame_num = start_frame + i

                    try:
                        img = Image.open(frame_path)
                        img.thumbnail((480, 270), Image.Resampling.LANCZOS)

                        # Schedule PhotoImage creation on main thread
                        self.root.after(0, self._add_frame_to_buffer, frame_num, img.copy())

                        # Clean up immediately
                        os.remove(frame_path)
                    except Exception:
                        continue  # Skip problematic frame

                # Clean up buffer - remove old frames outside the window
                self._cleanup_frame_buffer(start_frame)

            # Clean up temp directory
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass

        except Exception:
            pass  # Silent failure
        finally:
            self.frame_buffer_extracting = False

    def _add_frame_to_buffer(self, frame_num: int, img):
        """Add a frame to the buffer (called on main thread)."""
        try:
            photo = ImageTk.PhotoImage(img)
            with self.frame_buffer_lock:
                self.frame_buffer[frame_num] = photo
                # Limit buffer size
                if len(self.frame_buffer) > self.frame_buffer_size * 2:
                    self._cleanup_frame_buffer(self.frame_buffer_target_start)
        except Exception:
            pass  # Silent failure

    def _cleanup_frame_buffer(self, current_frame: int):
        """Remove frames outside the buffer window."""
        with self.frame_buffer_lock:
            frames_to_remove = []
            for frame_num in self.frame_buffer:
                # Keep frames from current_frame - 10 to current_frame + buffer_size
                if frame_num < current_frame - 10 or frame_num > current_frame + self.frame_buffer_size:
                    frames_to_remove.append(frame_num)

            for frame_num in frames_to_remove:
                del self.frame_buffer[frame_num]

    def _get_buffered_frame(self, position: float):
        """Get a frame from the buffer, or return None if not available."""
        if not self.selected_clip or self.frame_buffer_clip != self.selected_clip.path:
            return None

        fps = self.frame_buffer_fps if self.frame_buffer_fps > 0 else 30.0
        frame_num = int(position * fps)

        with self.frame_buffer_lock:
            return self.frame_buffer.get(frame_num)

    def _ensure_frame_buffer_ahead(self, position: float):
        """Ensure the frame buffer has frames ahead of the current position."""
        if not self.selected_clip:
            return

        fps = self.frame_buffer_fps if self.frame_buffer_fps > 0 else 30.0
        current_frame = int(position * fps)

        # Check if we need more frames
        with self.frame_buffer_lock:
            max_buffered = max(self.frame_buffer.keys()) if self.frame_buffer else -1

        # If buffer is running low, start extraction
        buffer_ahead = max_buffered - current_frame
        if buffer_ahead < 15 and not self.frame_buffer_extracting:
            # Start extracting from the next needed frame
            start_frame = max(0, max_buffered + 1) if max_buffered >= 0 else current_frame
            self._start_frame_buffer_extraction(self.selected_clip, start_frame)

    # =========================================================================
    # OpenCV Video Playback (Fast Hardware-Accelerated)
    # =========================================================================

    def _open_cv_capture(self, clip_path: str) -> bool:
        """Open a video file with OpenCV for fast playback."""
        if not OPENCV_AVAILABLE:
            return False

        # Close existing capture if different file
        if self.cv_capture is not None and self.cv_capture_path != clip_path:
            self._close_cv_capture()

        if self.cv_capture is None:
            try:
                self.cv_capture = open_video_capture(clip_path)

                if self.cv_capture.isOpened():
                    self.cv_capture_path = clip_path
                    # Clear frame cache for new video
                    self.cv_frame_cache.clear()
                    return True
                else:
                    self.cv_capture = None
                    return False
            except Exception:
                self.cv_capture = None
                return False  # Silent failure

        return self.cv_capture is not None and self.cv_capture.isOpened()

    def _close_cv_capture(self):
        """Close the OpenCV video capture."""
        if self.cv_capture is not None:
            try:
                self.cv_capture.release()
            except:
                pass
            self.cv_capture = None
            self.cv_capture_path = None
            self.cv_frame_cache.clear()
            self.cv_next_frame = None
            self.cv_next_frame_num = -1

    def _get_cv_frame_sync(self, position: float) -> ImageTk.PhotoImage:
        """Get a frame synchronously (for scrubbing when paused)."""
        if not OPENCV_AVAILABLE or not self.selected_clip:
            return None

        if self.selected_clip.is_audio_only:
            return None

        try:
            # Reuse existing capture if same file
            if self.cv_capture is None or self.cv_capture_path != self.selected_clip.path:
                if self.cv_capture is not None:
                    try:
                        self.cv_capture.release()
                    except:
                        pass
                self.cv_capture = open_video_capture(self.selected_clip.path)  # proxy-aware
                self.cv_capture_path = self.selected_clip.path

            if not self.cv_capture.isOpened():
                self.cv_capture = None
                return None

            fps = self.cv_capture.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = self.selected_clip.fps if self.selected_clip.fps > 0 else 30.0

            # Add trim offset to get actual file position
            split_key = self.selected_clip.path + f"_split_{id(self.selected_clip)}"
            in_offset = self.clip_in_offsets.get(split_key, self.clip_in_offsets.get(self.selected_clip.path, 0))
            actual_position = position + in_offset
            frame_num = int(actual_position * fps)

            self.cv_capture.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ret, frame = self.cv_capture.read()

            if ret and frame is not None:
                photo = self._cv_frame_to_photo(frame)
                # Cache the frame
                if photo:
                    self.cv_frame_cache[frame_num] = photo
                return photo
        except Exception:
            pass  # Silent failure

        return None

    def _process_cv_frame_queue(self):
        """Process frames from background thread queue (non-blocking)."""
        # Process up to 3 frames per tick to avoid blocking
        for _ in range(3):
            try:
                frame_num, frame_rgb = self.cv_frame_queue.get_nowait()
                if frame_num not in self.cv_frame_cache:
                    img = Image.fromarray(frame_rgb)
                    photo = ImageTk.PhotoImage(img)
                    self.cv_frame_cache[frame_num] = photo
            except queue.Empty:
                break
            except Exception:
                break  # Silent failure

    def _ensure_cv_extraction_running(self, current_frame: int):
        """Ensure background frame extraction is running."""
        if not OPENCV_AVAILABLE or not self.selected_clip:
            return

        # Update target frame for the extractor
        self.cv_last_requested_frame = current_frame

        # Start extractor thread if not running
        if not self.cv_playback_running:
            self.cv_playback_running = True
            thread = threading.Thread(
                target=self._cv_frame_extractor_loop,
                args=(self.selected_clip.path,),
                daemon=True
            )
            thread.start()

    def _cv_frame_extractor_loop(self, clip_path: str):
        """Continuous frame extraction loop (runs in background thread)."""
        cap = None
        try:
            cap = open_video_capture(clip_path)  # proxy-aware
            if not cap.isOpened():
                return

            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            last_extracted_frame = -1

            while self.preview_playing and self.cv_playback_running:
                target = self.cv_last_requested_frame

                # Determine what frames to extract (ahead of current position)
                frames_ahead = sum(1 for f in self.cv_frame_cache
                                   if target <= f < target + self.cv_cache_size)

                if frames_ahead >= self.cv_cache_size // 2:
                    # We have enough frames cached, sleep briefly
                    time.sleep(0.005)
                    continue

                # Find next frame to extract
                extract_frame = target
                for f in range(target, min(target + self.cv_cache_size, total_frames)):
                    if f not in self.cv_frame_cache:
                        extract_frame = f
                        break
                else:
                    # All frames in range are cached
                    time.sleep(0.01)
                    continue

                # Seek if needed (only if far from current position)
                if abs(extract_frame - last_extracted_frame) > 1:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, extract_frame)

                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                last_extracted_frame = extract_frame

                # Convert and resize frame
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = frame_rgb.shape[:2]
                scale = min(480 / w, 270 / h)
                new_w, new_h = int(w * scale), int(h * scale)
                if new_w != w or new_h != h:
                    frame_rgb = cv2.resize(frame_rgb, (new_w, new_h),
                                           interpolation=cv2.INTER_LINEAR)

                # Put in queue (non-blocking, drop if full)
                try:
                    self.cv_frame_queue.put_nowait((extract_frame, frame_rgb))
                except queue.Full:
                    pass  # Queue full, skip this frame

                # Clean up old cache entries
                self._cleanup_cv_cache_background(target)

        except Exception:
            pass  # Silent failure
        finally:
            if cap is not None:
                cap.release()
            self.cv_playback_running = False

    def _cleanup_cv_cache_background(self, current_frame: int):
        """Clean up old frames (thread-safe)."""
        if len(self.cv_frame_cache) <= self.cv_cache_size:
            return

        # Remove frames far behind current position
        frames_to_remove = [f for f in list(self.cv_frame_cache.keys())
                           if f < current_frame - 10]
        for f in frames_to_remove[:max(1, len(frames_to_remove) // 2)]:
            try:
                del self.cv_frame_cache[f]
            except KeyError:
                pass

    def _clear_cv_frame_queue(self):
        """Clear the CV frame queue."""
        while True:
            try:
                self.cv_frame_queue.get_nowait()
            except queue.Empty:
                break

    def _cv_frame_to_photo(self, frame) -> ImageTk.PhotoImage:
        """Convert OpenCV frame to PhotoImage for display."""
        if not PIL_AVAILABLE:
            return None

        try:
            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Resize to preview size (480x270)
            h, w = frame_rgb.shape[:2]
            target_w, target_h = 480, 270

            # Maintain aspect ratio
            scale = min(target_w / w, target_h / h)
            new_w = int(w * scale)
            new_h = int(h * scale)

            if new_w != w or new_h != h:
                frame_rgb = cv2.resize(frame_rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

            # Convert to PIL Image, then to PhotoImage
            img = Image.fromarray(frame_rgb)
            photo = ImageTk.PhotoImage(img)
            return photo

        except Exception:
            return None  # Silent failure

    # =========================================================================
    # Timeline Thumbnails
    # =========================================================================

    def _queue_thumbnail_extraction(self, clip):
        """Queue a clip for thumbnail extraction."""
        if not PIL_AVAILABLE or not FFMPEG_AVAILABLE:
            return
        if clip.is_audio_only:
            return
        if clip.path in self.timeline_thumbnails:
            return

        if clip.path not in self.thumbnail_extraction_queue:
            self.thumbnail_extraction_queue.append(clip.path)
            if not self.thumbnail_extraction_running:
                self._start_thumbnail_extraction()

    def _start_thumbnail_extraction(self):
        """Start background thumbnail extraction thread."""
        if self.thumbnail_extraction_running:
            return
        self.thumbnail_extraction_running = True
        thread = threading.Thread(target=self._process_thumbnail_queue, daemon=True)
        thread.start()

    def _process_thumbnail_queue(self):
        """Process thumbnail extraction queue in background."""
        while self.thumbnail_extraction_queue:
            while self.timeline_playing:  # never compete with playback
                time.sleep(0.25)
            clip_path = self.thumbnail_extraction_queue.pop(0)

            # Find the clip object
            clip = None
            for c in self.clips:
                if c.path == clip_path:
                    clip = c
                    break

            if not clip or clip.is_audio_only:
                continue

            try:
                self._extract_clip_thumbnails(clip)
            except Exception:
                pass  # Silently handle thumbnail extraction errors

        self.thumbnail_extraction_running = False

        # Coalesced redraw (deferred if playback is active, to protect audio)
        self._request_extraction_redraw(final=True)

    def _extract_clip_thumbnails(self, clip):
        """Timeline thumbnails for a clip - ONE FFmpeg run for all of them.

        Used to be one FFmpeg per thumbnail, each first tried on the Auto GPU
        (NVIDIA) and retried on the CPU: on 4K HEVC 4:2:2 that was ~2s of
        full-CPU decoding per thumbnail, ~40s per clip, running during
        playback. Now: the hardware route the direct engine found for the file
        (Intel QSV / CUDA / D3D11VA), frames scaled on the GPU and every
        STEP-th second kept; files no GPU can decode use keyframes only on the
        CPU. Low priority, and it waits while the timeline plays."""
        if not PIL_AVAILABLE or not FFMPEG_AVAILABLE:
            return
        duration = clip.duration if clip.duration > 0 else 10.0
        num_thumbs = min(20, max(3, int(duration / 5)))  # one per ~5s, max 20
        thumb_height = 40
        aspect = (clip.width / clip.height) if clip.width and clip.height else 16 / 9
        thumb_width = max(2, int(round(thumb_height * aspect / 2)) * 2)
        step = duration / max(1, num_thumbs - 1) * 0.98  # a touch short so the last one fits

        # Never compete with playback
        while self.timeline_playing:
            time.sleep(0.25)

        source = preview_source(clip.path)
        route = None
        if source == clip.path and self.hw_player is not None and DECODE_DEVICE != "CPU":
            waited = 0.0
            route = self.hw_player.route_for(clip.path)
            while route is False and waited < 5.0:   # format test still running
                time.sleep(0.1); waited += 0.1
                route = self.hw_player.route_for(clip.path)
            route = route or None

        pick = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step:.3f})'"
        cpu_in = ['-skip_frame', 'nokey']  # keyframes only: cheap on the CPU
        cpu_vf = f"{pick},scale={thumb_width}:{thumb_height}"
        attempts = []
        if route and route['kind'] == 'qsv':
            attempts.append((['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv'],
                             f"vpp_qsv=w={thumb_width}:h={thumb_height}:format=nv12,hwdownload,format=nv12,{pick}"))
        elif route and route.get('type') == 'cuda':
            attempts.append((['-hwaccel', 'cuda', '-hwaccel_device', str(route['device']),
                              '-hwaccel_output_format', 'cuda'],
                             f"scale_cuda={thumb_width}:{thumb_height}:format=nv12,hwdownload,format=nv12,{pick}"))
        elif route:
            attempts.append((['-hwaccel', route['type'], '-hwaccel_device', str(route['device'])], cpu_vf))
        attempts.append((cpu_in, cpu_vf))

        out_dir = os.path.join(self.analyzer.temp_dir, f"thumbs_{id(clip)}")
        os.makedirs(out_dir, exist_ok=True)
        pattern = os.path.join(out_dir, 't_%03d.jpg')
        if duration > 60:
            # Long clip: the single-run filter below DECODES THE WHOLE FILE to
            # keep 20 frames - a 5 h / 190 GB recording ran for hours. Jump to
            # each thumbnail instead and decode one keyframe there (CPU, a few
            # MB read each): ~0.3 s per thumbnail whatever the clip length.
            self._seek_thumbnails(source, out_dir, num_thumbs, step, thumb_width, thumb_height)
            attempts = []
        kwargs = {'capture_output': True, 'timeout': max(30, duration * 2 + 30)}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | 0x00004000  # below normal
        files = []
        popen_kwargs = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            popen_kwargs['creationflags'] = kwargs['creationflags']
        attempt = 0
        files = sorted(f for f in os.listdir(out_dir) if f.endswith('.jpg')) if not attempts else []
        while attempt < len(attempts) and not getattr(self, '_closing', False):
            hw_in, vf = attempts[attempt]
            for f in os.listdir(out_dir):
                os.remove(os.path.join(out_dir, f))
            cmd = [FFMPEG_PATH, '-v', 'error', '-y', *hw_in, '-i', source, '-an', '-vf', vf,
                   '-fps_mode', 'vfr', '-frames:v', str(num_thumbs), '-q:v', '5', pattern]
            interrupted = False
            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
                self._thumb_proc = proc
                deadline = time.time() + kwargs['timeout']
                while proc.poll() is None:
                    if self.timeline_playing and _suspend_process(proc, True):
                        # playback started: give the GPU/CPU back NOW by
                        # SUSPENDING the job (killing it tore down its Intel
                        # device while playback created one - the UI froze
                        # ~0.7 s); it continues where it was afterwards
                        while self.timeline_playing:
                            time.sleep(0.25)
                        _suspend_process(proc, False)
                        deadline = time.time() + kwargs['timeout']
                    elif self.timeline_playing or time.time() > deadline:
                        interrupted = self.timeline_playing  # can't suspend: kill + redo
                        proc.kill()
                        proc.wait()
                        break
                    time.sleep(0.05)
            except Exception:
                pass
            if interrupted:
                while self.timeline_playing:
                    time.sleep(0.25)
                continue  # same attempt again
            files = sorted(f for f in os.listdir(out_dir) if f.endswith('.jpg'))
            if files:
                break
            attempt += 1

        thumbnails = []
        for i, name in enumerate(files):
            path = os.path.join(out_dir, name)
            try:
                img = Image.open(path)
                img.load()
                # Plain PIL image only: this runs on a worker thread, and
                # creating a Tk PhotoImage here locked the main thread out of
                # Tcl (UI froze 0.6-1.3 s while thumbnails finished). The Tk
                # image is made on the main thread when first drawn.
                thumbnails.append({'pil': img.copy(), 'width': img.width, 'height': img.height,
                                   'timestamp': min(i * step, duration - 0.1)})
                img.close()
                os.remove(path)
            except Exception:
                continue
        try:
            os.rmdir(out_dir)
        except OSError:
            pass
        if thumbnails:
            self.timeline_thumbnails[clip.path] = thumbnails

    def _seek_thumbnails(self, source, out_dir, count, step, width, height):
        """Thumbnails of a long clip, one short FFmpeg per thumbnail: seek to
        it, decode the first keyframe there. Waits while the timeline plays;
        stops when the app closes."""
        flags = (subprocess.CREATE_NO_WINDOW | 0x00004000) if os.name == 'nt' else 0  # below normal
        for i in range(count):
            while self.timeline_playing and not getattr(self, '_closing', False):
                time.sleep(0.25)
            if getattr(self, '_closing', False):
                return
            out = os.path.join(out_dir, f't_{i + 1:03d}.jpg')
            cmd = [FFMPEG_PATH, '-v', 'error', '-y', '-skip_frame', 'nokey', '-ss', f'{i * step:.3f}',
                   '-i', source, '-an', '-frames:v', '1', '-vf', f'scale={width}:{height}', '-q:v', '5', out]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        **({'creationflags': flags} if flags else {}))
                self._thumb_proc = proc
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            except Exception:
                continue

    def _get_clip_thumbnails(self, clip_path):
        """Get cached thumbnails for a clip."""
        return self.timeline_thumbnails.get(clip_path, [])

    def _clear_thumbnail_cache(self):
        """Clear all cached thumbnails."""
        self.timeline_thumbnails.clear()
        self.thumbnail_extraction_queue.clear()

    # =========================================================================
    # Audio Waveform Extraction
    # =========================================================================

    def _queue_waveform_extraction(self, clip):
        """Queue a clip for waveform extraction if not already cached."""
        # Check memory cache first
        if clip.path in self.audio_waveform_cache:
            return

        # Check disk cache before queueing for extraction
        cached_data = self._load_waveform_from_disk(clip.path)
        if cached_data:
            self.audio_waveform_cache[clip.path] = cached_data
            return

        # Check if clip is already in queue (compare by path)
        if any(c.path == clip.path for c in self.waveform_extraction_queue):
            return
        self.waveform_extraction_queue.append(clip)
        self._start_waveform_extraction()

    def _start_waveform_extraction(self):
        """Start background waveform extraction if not already running."""
        if self.waveform_extraction_running:
            return
        if not self.waveform_extraction_queue:
            return

        self.waveform_extraction_running = True
        thread = threading.Thread(target=self._process_waveform_queue, daemon=True)
        thread.start()

    def _process_waveform_queue(self):
        """Process queued clips for waveform extraction."""
        processed = 0

        while self.waveform_extraction_queue:
            clip = self.waveform_extraction_queue.pop(0)
            processed += 1

            # Update status to show progress
            try:
                remaining = len(self.waveform_extraction_queue)
                self.root.after(0, lambda r=remaining: self.status_label.configure(
                    text=f"Extracting waveforms... ({r} remaining)"))
            except:
                pass

            try:
                self._extract_audio_waveform(clip)
                # Coalesced redraw (never a full redraw per clip - that stalls
                # the audio callback's GIL and stutters playback)
                self._request_extraction_redraw()
            except Exception:
                pass

        self.waveform_extraction_running = False

        # Final redraw once the batch is done (deferred if still playing)
        self._request_extraction_redraw(final=True)

        # Clear status message
        try:
            self.root.after(0, lambda: self.status_label.configure(text="Waveforms ready"))
        except:
            pass

    # A full _draw_timeline() clears and rebuilds the whole canvas; its cost
    # grows with clip count. Below this many clips a redraw is a few tens of ms
    # (well inside the audio buffer), so waveforms can refresh LIVE during
    # playback. Above it, redraws are deferred until playback stops so they
    # can't starve the pure-Python audio-mixer callback (GIL) and stutter.
    _LIVE_REDRAW_MAX_CLIPS = 10

    def _request_extraction_redraw(self, final=False):
        """Coalesce timeline redraws requested by background extraction threads.

        Extraction (ffmpeg + numpy) already runs concurrently with playback in
        a worker thread and releases the GIL - it never blocked audio. What
        stuttered playback was firing a FULL main-thread redraw per extracted
        clip. This coalesces those to at most one per interval; on normal
        timelines the redraw runs live during playback (waveforms appear while
        playing), and only large timelines defer to the pause/stop flush.
        `final` re-arms so the last batch of a queue always shows.
        """
        if getattr(self, '_extraction_redraw_pending', False) and not final:
            return

        # Large timeline + playing: too expensive to redraw live - defer.
        if self.timeline_playing and len(self.clips) > self._LIVE_REDRAW_MAX_CLIPS:
            self._extraction_redraw_deferred = True
            return

        self._extraction_redraw_pending = True
        # Space redraws out a bit more during playback for extra audio headroom
        delay = 800 if self.timeline_playing else 300

        def _do():
            self._extraction_redraw_pending = False
            # Re-check size at fire time (clips may have grown during the wait)
            if self.timeline_playing and len(self.clips) > self._LIVE_REDRAW_MAX_CLIPS:
                self._extraction_redraw_deferred = True
                return
            self._extraction_redraw_deferred = False
            try:
                self._draw_timeline()
            except Exception:
                pass
        try:
            self.root.after(delay, _do)
        except Exception:
            self._extraction_redraw_pending = False

    def _extract_audio_waveform(self, clip):
        """Extract audio waveform data from a video clip using fast seek-based sampling."""
        if not FFMPEG_AVAILABLE or not NUMPY_AVAILABLE:
            return

        # Use original_duration (actual file length) for waveform extraction, not clip.duration
        # clip.duration may be trimmed by IN/OUT points or LTC boundaries
        duration = getattr(clip, 'original_duration', None) or clip.duration
        if duration <= 0:
            duration = 10.0

        # Number of samples to extract (one per ~0.25 seconds, max 1000 for better detail)
        num_samples = min(1000, max(100, int(duration * 4)))

        # Sample duration at each seek point (50ms is enough for RMS calculation)
        sample_duration = 0.05

        try:
            temp_dir = self.analyzer.temp_dir
            peaks = [0.0] * num_samples

            def extract_sample(args):
                """Extract a single audio sample at a specific timestamp."""
                idx, timestamp = args
                temp_path = os.path.join(temp_dir, f"wf_{id(clip)}_{idx}.raw")

                try:
                    # Seek-based extraction with hardware acceleration
                    cmd = [
                        FFMPEG_PATH, '-y',
                        '-ss', str(timestamp),  # Seek BEFORE input (fast seek)
                        '-i', clip.path,
                        '-t', str(sample_duration),  # Only extract tiny sample
                        '-vn',  # No video
                        '-ac', '1',  # Mono
                        '-ar', '4000',  # Lower sample rate (4kHz enough for visualization)
                        '-f', 's16le',
                        '-acodec', 'pcm_s16le',
                        temp_path
                    ]

                    kwargs = {'capture_output': True, 'text': True, 'timeout': 10}
                    if os.name == 'nt':
                        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

                    result = subprocess.run(cmd, **kwargs)

                    if result.returncode == 0 and os.path.exists(temp_path):
                        with open(temp_path, 'rb') as f:
                            raw_data = f.read()

                        try:
                            os.remove(temp_path)
                        except:
                            pass

                        if len(raw_data) >= 4:
                            audio_data = np.frombuffer(raw_data, dtype=np.int16)
                            if len(audio_data) > 0:
                                # Use peak detection instead of RMS for more accurate waveform
                                # This captures the actual peak amplitude in the sample
                                abs_data = np.abs(audio_data.astype(np.float32))
                                peak_value = np.max(abs_data)
                                normalized = min(1.0, peak_value / 32767.0)
                                return idx, normalized

                    return idx, 0.0

                except Exception:
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except:
                        pass
                    return idx, 0.0

            # Calculate timestamps for each sample point
            sample_args = []
            for i in range(num_samples):
                timestamp = (i / max(1, num_samples - 1)) * (duration - sample_duration) if num_samples > 1 else 0
                timestamp = max(0, min(timestamp, duration - sample_duration))
                sample_args.append((i, timestamp))

            # Parallel extraction using thread pool (4-8 workers)
            num_workers = min(8, max(4, num_samples // 10))

            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {executor.submit(extract_sample, args): args for args in sample_args}

                for future in as_completed(futures):
                    try:
                        idx, value = future.result()
                        peaks[idx] = value
                    except Exception:
                        pass

            # Check if we got valid data
            if any(p > 0 for p in peaks):
                self.audio_waveform_cache[clip.path] = peaks
                # Save to disk cache for future sessions
                self._save_waveform_to_disk(clip.path, peaks)
            else:
                # Fallback: try simple full extraction for short clips
                if duration < 60:
                    self._extract_audio_waveform_simple(clip)

        except Exception:
            pass  # Silently handle waveform extraction errors

    def _extract_audio_waveform_simple(self, clip):
        """Fallback: Simple full audio extraction for short clips."""
        if not FFMPEG_AVAILABLE or not NUMPY_AVAILABLE:
            return

        duration = clip.duration if clip.duration > 0 else 10.0
        num_samples = min(500, max(50, int(duration * 2)))

        try:
            temp_path = os.path.join(self.analyzer.temp_dir, f"waveform_{id(clip)}.raw")

            cmd = [
                FFMPEG_PATH, '-y',
                '-i', clip.path,
                '-vn', '-ac', '1', '-ar', '4000',
                '-f', 's16le', '-acodec', 'pcm_s16le',
                temp_path
            ]

            kwargs = {'capture_output': True, 'text': True, 'timeout': 60}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            if result.returncode != 0 or not os.path.exists(temp_path):
                return

            with open(temp_path, 'rb') as f:
                raw_data = f.read()

            try:
                os.remove(temp_path)
            except:
                pass

            if len(raw_data) < 4:
                return

            audio_data = np.frombuffer(raw_data, dtype=np.int16)
            samples_per_peak = max(1, len(audio_data) // num_samples)
            peaks = []

            for i in range(num_samples):
                start = i * samples_per_peak
                end = min(start + samples_per_peak, len(audio_data))
                if start >= len(audio_data):
                    break
                chunk = audio_data[start:end]
                if len(chunk) > 0:
                    rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
                    normalized = min(1.0, rms / 32767.0 * 4.0)
                    peaks.append(normalized)
                else:
                    peaks.append(0.0)

            if peaks:
                self.audio_waveform_cache[clip.path] = peaks
                # Save to disk cache for future sessions
                self._save_waveform_to_disk(clip.path, peaks)

        except Exception:
            pass  # Silently handle simple waveform extraction errors

    def _get_clip_waveform(self, clip_path):
        """Get cached waveform data for a clip."""
        return self.audio_waveform_cache.get(clip_path, None)

    def _clear_waveform_cache(self):
        """Clear all cached waveforms."""
        self.audio_waveform_cache.clear()

    def _invalidate_and_reextract_waveform(self, clip):
        """Invalidate cached waveform and queue for re-extraction with correct duration."""
        # Track which clips we've already queued to avoid infinite re-extraction loops
        if not hasattr(self, '_waveform_reextract_queued'):
            self._waveform_reextract_queued = set()

        if clip.path in self._waveform_reextract_queued:
            return  # Already queued for re-extraction

        self._waveform_reextract_queued.add(clip.path)

        # Remove from memory cache
        if clip.path in self.audio_waveform_cache:
            del self.audio_waveform_cache[clip.path]

        # Remove from disk cache
        if self.waveform_cache_dir:
            cache_key = self._get_waveform_cache_key(clip.path)
            if cache_key:
                cache_file = os.path.join(self.waveform_cache_dir, f"{cache_key}.waveform")
                try:
                    if os.path.exists(cache_file):
                        os.remove(cache_file)
                except Exception:
                    pass  # Silently handle cache removal errors

        # Queue for re-extraction
        self._queue_waveform_extraction(clip)

    # =========================================================================
    # Persistent Waveform Disk Cache
    # =========================================================================

    def _init_waveform_cache_dir(self, custom_dir=None):
        """Initialize the persistent waveform cache directory."""
        try:
            # Use custom directory if provided and valid
            if custom_dir and os.path.isdir(os.path.dirname(custom_dir) if not os.path.exists(custom_dir) else custom_dir):
                cache_dir = custom_dir
            elif self.waveform_cache_custom_dir and os.path.isdir(os.path.dirname(self.waveform_cache_custom_dir) if not os.path.exists(self.waveform_cache_custom_dir) else self.waveform_cache_custom_dir):
                cache_dir = self.waveform_cache_custom_dir
            else:
                # Use AppData/Local on Windows, ~/.cache on Unix
                if os.name == 'nt':
                    base_dir = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
                    cache_dir = os.path.join(base_dir, 'LTC_Sync', 'waveform_cache')
                else:
                    cache_dir = os.path.expanduser('~/.cache/ltc_sync/waveforms')

            os.makedirs(cache_dir, exist_ok=True)
            return cache_dir
        except Exception:
            return None

    def _get_waveform_cache_key(self, file_path):
        """Generate a unique cache key based on file path and modification time."""
        import hashlib
        try:
            mtime = os.path.getmtime(file_path)
            file_size = os.path.getsize(file_path)
            # Create hash from path + mtime + size for uniqueness
            key_data = f"{file_path}|{mtime}|{file_size}"
            return hashlib.md5(key_data.encode()).hexdigest()
        except Exception:
            return None

    def _load_waveform_from_disk(self, file_path):
        """Try to load waveform data from disk cache."""
        if not self.waveform_cache_dir:
            return None

        cache_key = self._get_waveform_cache_key(file_path)
        if not cache_key:
            return None

        cache_file = os.path.join(self.waveform_cache_dir, f"{cache_key}.waveform")

        try:
            if os.path.exists(cache_file):
                import pickle
                with open(cache_file, 'rb') as f:
                    data = pickle.load(f)
                # Update access time for LRU cleanup
                os.utime(cache_file, None)
                return data
        except Exception:
            # Cache file corrupted or incompatible, delete it
            try:
                os.remove(cache_file)
            except:
                pass
        return None

    def _save_waveform_to_disk(self, file_path, waveform_data):
        """Save waveform data to disk cache."""
        if not self.waveform_cache_dir or not waveform_data:
            return

        cache_key = self._get_waveform_cache_key(file_path)
        if not cache_key:
            return

        cache_file = os.path.join(self.waveform_cache_dir, f"{cache_key}.waveform")

        try:
            import pickle
            with open(cache_file, 'wb') as f:
                pickle.dump(waveform_data, f)

            # Check cache size and cleanup if needed (in background)
            threading.Thread(target=self._cleanup_waveform_cache, daemon=True).start()
        except Exception:
            pass  # Silently handle cache save errors

    def _cleanup_waveform_cache(self):
        """Remove oldest cache files when cache exceeds size limit."""
        if not self.waveform_cache_dir:
            return

        try:
            max_size_bytes = self.waveform_cache_max_size_gb * 1024 * 1024 * 1024

            # Get all cache files with their stats
            cache_files = []
            total_size = 0

            for filename in os.listdir(self.waveform_cache_dir):
                if filename.endswith('.waveform'):
                    filepath = os.path.join(self.waveform_cache_dir, filename)
                    try:
                        stat = os.stat(filepath)
                        cache_files.append((filepath, stat.st_atime, stat.st_size))
                        total_size += stat.st_size
                    except:
                        pass

            # If under limit, no cleanup needed
            if total_size <= max_size_bytes:
                return

            # Sort by access time (oldest first) for LRU eviction
            cache_files.sort(key=lambda x: x[1])

            # Remove oldest files until under limit
            removed_count = 0
            for filepath, atime, size in cache_files:
                if total_size <= max_size_bytes * 0.8:  # Leave 20% headroom
                    break
                try:
                    os.remove(filepath)
                    total_size -= size
                    removed_count += 1
                except:
                    pass

        except Exception:
            pass  # Silently handle cache cleanup errors

    def _get_waveform_cache_size_mb(self):
        """Get current cache size in MB."""
        if not self.waveform_cache_dir:
            return 0

        try:
            total_size = 0
            for filename in os.listdir(self.waveform_cache_dir):
                if filename.endswith('.waveform'):
                    filepath = os.path.join(self.waveform_cache_dir, filename)
                    try:
                        total_size += os.path.getsize(filepath)
                    except:
                        pass
            return total_size / (1024 * 1024)
        except:
            return 0

    # =========================================================================
    # Timeline Markers
    # =========================================================================

    def _add_marker_at_playhead(self):
        """Add a marker at the current playhead position."""
        if not hasattr(self, 'playhead_position'):
            return

        self._finalize_scrub_before_modal()

        # Pause playback if running (marker dialog is modal and blocks interaction)
        if self.timeline_playing:
            self._toggle_timeline_playback()

        time_pos = self.playhead_position

        # Check if marker already exists near this position
        for marker in self.timeline_markers:
            if abs(marker['time'] - time_pos) < 0.1:
                # Edit existing marker
                self._edit_marker(marker)
                return

        # Create new marker
        color = self.marker_colors[self.next_marker_color % len(self.marker_colors)]
        self.next_marker_color += 1

        marker = {
            'time': time_pos,
            'color': color,
            'label': f"Marker {len(self.timeline_markers) + 1}",
            'note': ""
        }

        self.timeline_markers.append(marker)
        self._draw_timeline()

        # Show quick label dialog
        self._edit_marker(marker)

    def _edit_marker(self, marker):
        """Edit marker label and note."""
        # Pause playback if running (modal dialog blocks interaction)
        if self.timeline_playing:
            self._toggle_timeline_playback()

        dialog = tk.Toplevel(self.root)
        dialog.title("Edit Marker")
        dialog.geometry("350x200")
        dialog.configure(bg=self.COLORS['bg_card'])
        dialog.transient(self.root)
        dialog.grab_set()

        # Center on parent
        dialog.geometry(f"+{self.root.winfo_x() + 200}+{self.root.winfo_y() + 200}")

        # Label
        ttk.Label(dialog, text="Marker Label:").pack(anchor='w', padx=10, pady=(10, 2))
        label_var = tk.StringVar(value=marker['label'])
        label_entry = ttk.Entry(dialog, textvariable=label_var, width=40)
        label_entry.pack(padx=10, fill=tk.X)
        label_entry.focus_set()
        label_entry.select_range(0, tk.END)

        # Note
        ttk.Label(dialog, text="Note:").pack(anchor='w', padx=10, pady=(10, 2))
        note_text = tk.Text(dialog, height=4, width=40, bg=self.COLORS['bg'],
                            fg=self.COLORS['text_primary'])
        note_text.pack(padx=10, fill=tk.BOTH, expand=True)
        note_text.insert('1.0', marker.get('note', ''))

        # Color selection
        color_frame = ttk.Frame(dialog)
        color_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(color_frame, text="Color:").pack(side=tk.LEFT)

        for c in self.marker_colors:
            btn = tk.Button(color_frame, bg=c, width=2, height=1,
                           command=lambda col=c: self._set_marker_color(marker, col, dialog))
            btn.pack(side=tk.LEFT, padx=2)

        # Buttons
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        def save_marker():
            marker['label'] = label_var.get()
            marker['note'] = note_text.get('1.0', 'end-1c')
            self._draw_timeline()
            dialog.destroy()

        def delete_marker():
            if marker in self.timeline_markers:
                self.timeline_markers.remove(marker)
            self._draw_timeline()
            dialog.destroy()

        ttk.Button(btn_frame, text="Save", command=save_marker).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Delete", command=delete_marker).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        # Enter to save
        dialog.bind('<Return>', lambda e: save_marker())
        dialog.bind('<Escape>', lambda e: dialog.destroy())

    def _set_marker_color(self, marker, color, dialog=None):
        """Set marker color."""
        marker['color'] = color
        self._draw_timeline()

    def _delete_nearest_marker(self):
        """Delete the marker nearest to the playhead."""
        if not hasattr(self, 'playhead_position') or not self.timeline_markers:
            return

        # Pause playback if running (modifying markers during playback can cause UI stutter)
        if self.timeline_playing:
            self._toggle_timeline_playback()

        time_pos = self.playhead_position

        # Find nearest marker
        nearest = None
        min_dist = float('inf')
        for marker in self.timeline_markers:
            dist = abs(marker['time'] - time_pos)
            if dist < min_dist:
                min_dist = dist
                nearest = marker

        # Only delete if within 5 seconds
        if nearest and min_dist < 5.0:
            self.timeline_markers.remove(nearest)
            self._draw_timeline()

    def _go_to_next_marker(self):
        """Jump to the next marker after current position. Keeps playing if active."""
        if not hasattr(self, 'playhead_position') or not self.timeline_markers:
            return

        current = self.playhead_position
        sorted_markers = sorted(self.timeline_markers, key=lambda m: m['time'])

        for marker in sorted_markers:
            if marker['time'] > current + 0.1:
                self._jump_playhead(marker['time'])
                return

        # Wrap to first marker
        if sorted_markers:
            self._jump_playhead(sorted_markers[0]['time'])

    def _go_to_prev_marker(self):
        """Jump to the previous marker before current position. Keeps playing if active."""
        if not hasattr(self, 'playhead_position') or not self.timeline_markers:
            return

        current = self.playhead_position
        sorted_markers = sorted(self.timeline_markers, key=lambda m: m['time'], reverse=True)

        for marker in sorted_markers:
            if marker['time'] < current - 0.1:
                self._jump_playhead(marker['time'])
                return

        # Wrap to last marker
        if sorted_markers:
            self._jump_playhead(sorted_markers[0]['time'])

    def _draw_markers(self, canvas_width, pps):
        """Draw markers on the timeline."""
        for marker in self.timeline_markers:
            x = (marker['time'] - self.timeline_offset) * pps

            if 0 <= x <= canvas_width:
                # Marker line
                self.timeline_canvas.create_line(x, 30, x, 30 + self.track_height * 2 + 20,
                                                 fill=marker['color'], width=2, tags='marker')

                # Marker flag (triangle at top)
                self.timeline_canvas.create_polygon(
                    x, 20, x + 10, 15, x + 10, 25,
                    fill=marker['color'], outline='', tags='marker'
                )

                # Label
                if marker.get('label'):
                    self.timeline_canvas.create_text(x + 12, 20,
                                                     text=marker['label'][:15],
                                                     anchor='w',
                                                     fill=marker['color'],
                                                     font=('Segoe UI', 7),
                                                     tags='marker')

    def _draw_in_out_points(self, ruler_height, total_height, canvas_width):
        """Draw IN and OUT point indicators on the timeline."""
        in_color = '#00ff88'  # Green for IN
        out_color = '#ff8844'  # Orange for OUT

        # Draw IN point
        if self.in_point is not None:
            x = (self.in_point - self.timeline_offset) * self.pixels_per_second
            if 0 <= x <= canvas_width:
                # IN point line
                self.timeline_canvas.create_line(x, ruler_height, x, total_height,
                                                 fill=in_color, width=2,
                                                 dash=(4, 2), tags='in_out')
                # IN bracket at top
                self.timeline_canvas.create_line(x, ruler_height - 5, x + 8, ruler_height - 5,
                                                 fill=in_color, width=2, tags='in_out')
                self.timeline_canvas.create_line(x, ruler_height - 5, x, ruler_height + 5,
                                                 fill=in_color, width=2, tags='in_out')
                # "I" label
                self.timeline_canvas.create_text(x + 3, ruler_height - 12,
                                                 text="I", fill=in_color,
                                                 font=('Segoe UI', 8, 'bold'), tags='in_out')

        # Draw OUT point
        if self.out_point is not None:
            x = (self.out_point - self.timeline_offset) * self.pixels_per_second
            if 0 <= x <= canvas_width:
                # OUT point line
                self.timeline_canvas.create_line(x, ruler_height, x, total_height,
                                                 fill=out_color, width=2,
                                                 dash=(4, 2), tags='in_out')
                # OUT bracket at top
                self.timeline_canvas.create_line(x - 8, ruler_height - 5, x, ruler_height - 5,
                                                 fill=out_color, width=2, tags='in_out')
                self.timeline_canvas.create_line(x, ruler_height - 5, x, ruler_height + 5,
                                                 fill=out_color, width=2, tags='in_out')
                # "O" label
                self.timeline_canvas.create_text(x - 3, ruler_height - 12,
                                                 text="O", fill=out_color,
                                                 font=('Segoe UI', 8, 'bold'), anchor='e', tags='in_out')

        # Draw selection range highlight if both are set
        if self.in_point is not None and self.out_point is not None:
            in_x = (min(self.in_point, self.out_point) - self.timeline_offset) * self.pixels_per_second
            out_x = (max(self.in_point, self.out_point) - self.timeline_offset) * self.pixels_per_second

            if in_x < canvas_width and out_x > 0:
                in_x = max(0, in_x)
                out_x = min(canvas_width, out_x)

                # Semi-transparent selection highlight
                self.timeline_canvas.create_rectangle(
                    in_x, ruler_height, out_x, total_height,
                    fill='#4488ff', outline='', stipple='gray25', tags='in_out'
                )

    def _clear_all_markers(self):
        """Clear all markers."""
        # Pause playback if running (clearing markers triggers timeline redraw)
        if self.timeline_playing:
            self._toggle_timeline_playback()
        self.timeline_markers.clear()
        self._draw_timeline()

    def _export_markers(self):
        """Export markers to a text file."""
        if not self.timeline_markers:
            messagebox.showinfo("Info", "No markers to export")
            return

        output_path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv")],
            title="Export Markers"
        )

        if not output_path:
            return

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                if output_path.endswith('.csv'):
                    f.write("Time,Label,Note\n")
                    for marker in sorted(self.timeline_markers, key=lambda m: m['time']):
                        time_str = self._format_time(marker['time'])
                        label = marker.get('label', '').replace(',', ';')
                        note = marker.get('note', '').replace(',', ';').replace('\n', ' ')
                        f.write(f"{time_str},{label},{note}\n")
                else:
                    f.write("Timeline Markers\n")
                    f.write("=" * 50 + "\n\n")
                    for marker in sorted(self.timeline_markers, key=lambda m: m['time']):
                        time_str = self._format_time(marker['time'])
                        f.write(f"[{time_str}] {marker.get('label', '')}\n")
                        if marker.get('note'):
                            f.write(f"    {marker['note']}\n")
                        f.write("\n")

            messagebox.showinfo("Success", f"Markers exported to:\n{output_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to export markers: {e}")

    def _format_time(self, seconds):
        """Format seconds as timecode string."""
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        frames = int((seconds % 1) * 25)
        return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"

    # =========================================================================
    # Export
    # =========================================================================

    def _on_pack_audio_changed(self):
        """'Share Tracks' toggled: re-lay the audio tracks on the timeline (exports read the option directly)."""
        if self.clips and self.synced:   # before a sync there is no layout to change (Sync applies the option)
            self._assign_audio_track_indices()   # audio only: never re-lay the video tracks
            self._draw_timeline()

    def _export_xml(self):
        self._finalize_scrub_before_modal()
        if not self.clips:
            messagebox.showinfo("Info", "No clips to export")
            return

        if not self.synced:
            if not messagebox.askyesno("Not Synced", "Clips are not synced. Export anyway?"):
                return

        format_map = {
            "Premiere Pro": ("Adobe Premiere Pro XML", ".xml", XMLExporter.export_premiere),
            "DaVinci Resolve": ("DaVinci Resolve XML", ".fcpxml", XMLExporter.export_resolve),
            "Final Cut Pro X": ("Final Cut Pro X XML", ".fcpxml", XMLExporter.export_fcpx)
        }

        fmt = self.export_format.get()
        desc, ext, exporter = format_map.get(fmt, format_map["Premiere Pro"])

        # Force window to front
        self.root.lift()
        self.root.focus_force()
        self.root.update_idletasks()
        self.root.update()

        output_path = filedialog.asksaveasfilename(
            parent=self.root,
            title=f"Export {fmt} XML",
            defaultextension=ext,
            initialdir=self.last_directory,
            filetypes=[(desc, f"*{ext}"), ("All files", "*.*")]
        )

        if output_path:
            # Update last directory
            self.last_directory = os.path.dirname(output_path)

            try:
                fps = self.clips[0].fps if self.clips else 30.0

                # Filter out LTC tracks if mute_ltc is enabled
                # Include both clips marked as is_ltc_track and audio-only clips with LTC detected
                mute_ltc_enabled = self.mute_ltc.get()
                include_camera_audio = self.include_camera_audio.get()
                split_stereo_enabled = self.split_stereo.get()
                multicam_enabled = self.multicam_export.get()
                pack_audio = self.pack_audio_tracks.get()
                clips_to_export = self.clips
                if mute_ltc_enabled:
                    def is_ltc_only_clip(c):
                        """Check if clip is an LTC-only track that should be excluded."""
                        if c.is_ltc_track:
                            return True
                        # Audio-only clip with LTC detected on a channel
                        if c.is_audio_only and c.ltc_channel >= 0:
                            # If single channel audio file with LTC, it's purely LTC
                            num_ch = c.audio_channels if hasattr(c, 'audio_channels') and c.audio_channels else 2
                            if num_ch == 1:
                                return True
                        return False
                    clips_to_export = [c for c in self.clips if not is_ltc_only_clip(c)]

                # All three exporters apply IN-trim/split offsets on regular
                # spine clips; only the MULTICAM structures don't yet.
                if fmt != "Premiere Pro" and multicam_enabled:
                    has_trims = any(abs(v) > 0.01 for v in self.clip_in_offsets.values())
                    if has_trims:
                        if not messagebox.askyesno(
                                "Trims Not Applied In Multicam",
                                f"This timeline contains trimmed/split clips. The "
                                f"{fmt} MULTICAM structure does not apply trim "
                                f"offsets - multicam angles will reference their "
                                f"ORIGINAL start.\n\n"
                                f"Export anyway? (Disable multicam export to get "
                                f"trims applied.)"):
                            return

                close_gaps = self.close_gaps.get()
                try:
                    keep_gap = float(self.close_gaps_keep.get())
                except ValueError:
                    keep_gap = 0.0

                def run_exporter(clips_out, offsets_out, note):
                    fitted = XMLExporter.fit_to_grid(clips_out)   # 50p / 59.94p clips: source range inside the file
                    moved = []
                    if close_gaps:
                        shifts = XMLExporter.gap_shifts(clips_out, fps, keep_gap)
                        for c in clips_out:
                            if shifts.get(id(c)):
                                moved.append((c, c.timeline_start))
                                c.timeline_start -= shifts[id(c)]
                        if moved:
                            note += f"\n\nGaps removed: {max(shifts.values()) / 60:.1f} min of empty time left out."
                    try:
                        if fmt == "DaVinci Resolve":
                            exporter(clips_out, output_path, fps,
                                     mute_ltc=mute_ltc_enabled,
                                     include_camera_audio=include_camera_audio,
                                     split_stereo=split_stereo_enabled,
                                     multicam_export=multicam_enabled,
                                     in_offsets=offsets_out,
                                     camera_audio_selection=dict(self.camera_audio_selection),
                                     pack_audio=pack_audio)
                        elif fmt == "Premiere Pro":
                            exporter(clips_out, output_path, fps,
                                     mute_ltc=mute_ltc_enabled,
                                     include_camera_audio=include_camera_audio,
                                     split_stereo=split_stereo_enabled,
                                     in_offsets=offsets_out,
                                     camera_audio_selection=dict(self.camera_audio_selection),
                                     pack_audio=pack_audio)
                        elif fmt == "Final Cut Pro X":
                            exporter(clips_out, output_path, fps,
                                     mute_ltc=mute_ltc_enabled,
                                     include_camera_audio=include_camera_audio,
                                     split_stereo=split_stereo_enabled,
                                     multicam_export=multicam_enabled,
                                     in_offsets=offsets_out,
                                     camera_audio_selection=dict(self.camera_audio_selection),
                                     pack_audio=pack_audio)
                        else:
                            exporter(clips_out, output_path, fps)
                        csv_note = ""
                        if fmt == "DaVinci Resolve" and getattr(XMLExporter, 'last_csv', None):
                            csv_note = ("\n\n" + XMLExporter.last_csv +
                                        "\n\nIn Resolve: File > Import Metadata To > Media Pool, pick this CSV"
                                        ",\ntick \"Ignore file extension\" and untick \"Match using clip start and end timecode\"\n"
                                        "(fills Camera # and Angle).")
                        messagebox.showinfo("Success", f"Exported to:\n{output_path}{note}{csv_note}")
                        self.status_label.configure(text=f"Exported {len(clips_out)} clips to {fmt}")
                    except Exception as e:
                        messagebox.showerror("Error", f"Export failed:\n{str(e)}")
                    finally:
                        for c, start in moved:   # the app's timeline keeps the real positions
                            c.timeline_start = start
                        for c, start, dur, emb, orig in fitted:
                            c.timeline_start, c.duration, c.embedded_tc_frames, c.original_duration = start, dur, emb, orig

                if self.drift_correct.get() and fmt == "DaVinci Resolve":
                    self._drift_correct_async(clips_to_export, dict(self.clip_in_offsets), run_exporter)
                else:
                    note = ("\n\n(Drift fix is applied in the DaVinci Resolve export only.)"
                            if self.drift_correct.get() else "")
                    run_exporter(clips_to_export, dict(self.clip_in_offsets), note)
            except Exception as e:
                messagebox.showerror("Error", f"Export failed:\n{str(e)}")

    def _drift_segments(self, clips, offsets, minutes, progress=None):
        """Recorder drift fix: (clips, offsets, report lines) with every long
        audio recording that has LTC on a track replaced by segments of
        `minutes`, each placed by the LTC at its own start (see ltc_seconds_at).

        A recording = all audio tracks of one recorder take (recording_id; a
        plain file is its own recording), its split parts chained by sample
        count. The LTC is read from the take's LTC track even when that track
        isn't exported (Mute LTC). Consecutive segments of a track meet
        exactly; where the recorder runs slow a segment plays a few ms longer
        into the next one's start (never past its file). Segments point at
        the original files - nothing is written. Runs on a worker thread."""
        def off(c):
            return offsets.get(c.path + f"_split_{id(c)}", offsets.get(c.path, 0)) or 0
        def key_of(c):
            return c.recording_id or c.path
        exported = [c for c in clips if c.is_audio_only and XMLExporter._has_sync_tc(c)]
        keys = []
        for c in exported:
            if key_of(c) not in keys:
                keys.append(key_of(c))
        out_clips, out_offsets, report = list(clips), dict(offsets), []
        import copy
        for n_rec, key in enumerate(keys):
            members = [c for c in self.clips if c.is_audio_only and key_of(c) == key]
            tracks = {}
            for c in members:
                tracks.setdefault(c.track_number if c.recording_id else 0, []).append(c)
            for t in tracks.values():
                t.sort(key=lambda c: c.split_part or 0)
            ltc_track = next((t for t in tracks.values() if t[0].ltc_channel >= 0 and t[0].start_tc), None)
            if ltc_track is None:
                continue
            sr = ltc_track[0].sample_rate or 48000
            fps, df = ltc_track[0].fps or 25.0, bool(ltc_track[0].drop_frame)
            parts, pos = [], 0
            for c in ltc_track:
                parts.append((c, pos))
                pos += c.sample_count
            rec_len = pos
            seg = int(minutes * 60 * sr)
            if rec_len <= seg or any(c.sample_rate != sr for c in members):
                continue
            grid = list(range(0, rec_len, seg)) + [rec_len - 1]
            tc = {}
            for k, g in enumerate(grid):
                if progress:
                    progress(n_rec, len(keys), k, len(grid), os.path.basename(ltc_track[0].filename))
                part, base = next(((c, b) for c, b in reversed(parts) if g >= b), parts[0])
                tc[g] = ltc_seconds_at(part.path, g - base, part.ltc_channel, fps, df, total=part.sample_count)
            pts = [g for g in grid if tc[g] is not None]
            if len(pts) < 2:
                report.append(f"{key}: no readable LTC along the recording - not corrected")
                continue
            for g in pts[1:]:
                if tc[g] < tc[pts[0]] - 43200:      # LTC passed midnight
                    tc[g] += 86400.0
            def tc_at(s):
                if s <= pts[0]:
                    return tc[pts[0]] + (s - pts[0]) / sr
                if s >= pts[-1]:
                    return tc[pts[-1]] + (s - pts[-1]) / sr
                i = max(j for j in range(len(pts)) if pts[j] <= s)
                a, b = pts[i], pts[i + 1]
                return tc[a] + (tc[b] - tc[a]) * (s - a) / (b - a)
            drift = (rec_len / sr) - (tc_at(rec_len) - tc_at(0))
            n_cuts = 0
            for t in tracks.values():
                rec_start, base = {}, 0
                for c in t:
                    rec_start[id(c)] = base
                    base += c.sample_count
                anchor = t[0].timeline_start - off(t[0])      # timeline time of recording sample 0
                for c in t:
                    if c not in out_clips:
                        continue                               # not exported (e.g. muted LTC track)
                    r0 = rec_start[id(c)]
                    u0 = r0 + int(round(off(c) * sr))
                    u1 = min(r0 + c.sample_count, u0 + int(round(c.duration * sr)))
                    cuts = [u0] + [g for g in range(0, rec_len, seg) if u0 < g < u1] + [u1]
                    pieces = []
                    for a_, b_ in zip(cuts, cuts[1:]):
                        s = copy.copy(c)
                        s.timeline_start = anchor + (tc_at(a_) - tc_at(0))
                        end = anchor + (tc_at(b_) - tc_at(0))
                        s.duration = min(end - s.timeline_start, (r0 + c.sample_count - a_) / sr)
                        s.split_part = None
                        s._exact_audio = True
                        out_offsets[s.path + f"_split_{id(s)}"] = (a_ - r0) / sr
                        pieces.append(s)
                    i = out_clips.index(c)
                    out_clips[i:i + 1] = pieces
                    n_cuts += len(pieces) - 1
            hrs = rec_len / sr / 3600
            report.append(f"{key}: drift {drift * 1000:+.0f} ms over {hrs:.1f} h ({drift / (rec_len / sr) * 1e6:+.1f} ppm"
                          f" vs its LTC) - corrected with {n_cuts} cut(s)")
        return out_clips, out_offsets, report

    def _drift_correct_async(self, clips, offsets, done):
        """Run _drift_segments on a worker thread with a progress window, then
        done(clips, offsets, note)."""
        try:
            minutes = float(self.drift_segment_min.get())
        except (TypeError, ValueError):
            minutes = 5.0
        win = tk.Toplevel(self.root)
        win.title("Recorder drift fix")
        win.configure(bg=self.COLORS['bg'])
        win.transient(self.root)
        win.resizable(False, False)
        label = tk.Label(win, text="Reading LTC along the recordings...", bg=self.COLORS['bg'],
                         fg=self.COLORS['text'], font=('Segoe UI', 10), anchor='w', justify='left')
        label.pack(fill='x', padx=16, pady=(14, 6))
        bar = ttk.Progressbar(win, length=420, mode='determinate', maximum=1000)
        bar.pack(padx=16, pady=(0, 14))
        win.grab_set()
        state = {'p': None, 'result': None, 'error': None}

        def work():
            try:
                state['result'] = self._drift_segments(
                    clips, offsets, minutes, progress=lambda *a: state.__setitem__('p', a))
            except Exception as e:
                state['error'] = e

        threading.Thread(target=work, daemon=True, name="drift-fix").start()

        def poll():
            if state['result'] is None and state['error'] is None:
                if state['p']:
                    r, nr, k, nk, name = state['p']
                    label.configure(text=f"Reading LTC along {name} ({k + 1}/{nk})")
                    bar['value'] = int(1000 * (r + (k + 1) / max(1, nk)) / max(1, nr))
                win.after(100, poll)
                return
            win.grab_release()
            win.destroy()
            if state['error'] is not None:
                messagebox.showerror("Drift fix", f"Drift fix failed, exporting without it:\n{state['error']}")
                done(clips, offsets, "")
                return
            out_clips, out_offsets, report = state['result']
            note = ("\n\nRecorder drift fix:\n" + "\n".join(report)) if report else \
                   "\n\nRecorder drift fix: no long recording with LTC on a track - nothing to correct."
            done(out_clips, out_offsets, note)
        poll()

    def _export_edl(self):
        """Export an EDL (Edit Decision List) file."""
        if not self.clips:
            messagebox.showinfo("Info", "No clips to export")
            return

        if not self.synced:
            if not messagebox.askyesno("Not Synced", "Clips are not synced. Export anyway?"):
                return

        # Force window to front
        self.root.lift()
        self.root.focus_force()
        self.root.update_idletasks()
        self.root.update()

        output_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export EDL",
            defaultextension=".edl",
            initialdir=self.last_directory,
            filetypes=[("Edit Decision List", "*.edl"), ("All files", "*.*")]
        )

        if not output_path:
            return

        # Update last directory
        self.last_directory = os.path.dirname(output_path)

        try:
            # Get reference FPS
            ref_fps = 30.0
            for clip in self.clips:
                if clip.fps > 0:
                    ref_fps = clip.fps
                    break

            # Determine if drop frame
            is_df = any(c.drop_frame for c in self.clips if c.drop_frame)
            tc_sep = ';' if is_df else ':'

            # Filter out LTC tracks if mute_ltc is enabled, then sort by timeline position
            # Include both clips marked as is_ltc_track and audio-only clips with LTC detected
            clips_to_export = self.clips
            if self.mute_ltc.get():
                def is_ltc_only_clip(c):
                    if c.is_ltc_track:
                        return True
                    if c.is_audio_only and c.ltc_channel >= 0:
                        num_ch = c.audio_channels if hasattr(c, 'audio_channels') and c.audio_channels else 2
                        if num_ch == 1:
                            return True
                    return False
                clips_to_export = [c for c in self.clips if not is_ltc_only_clip(c)]
            sorted_clips = sorted([c for c in clips_to_export if c.start_tc], key=lambda c: c.timeline_start)

            with open(output_path, 'w', encoding='utf-8') as f:
                # EDL Header
                f.write("TITLE: LTC Sync Export\n")
                f.write(f"FCM: {'DROP FRAME' if is_df else 'NON-DROP FRAME'}\n\n")

                edit_num = 1
                record_in_frames = 0

                for clip in sorted_clips:
                    if clip.duration <= 0:
                        continue

                    # Calculate timecodes

                    # Source timecode (from clip)
                    src_in = clip.start_tc.replace(':', tc_sep) if is_df else clip.start_tc
                    src_out_frames = clip.start_frames + int(clip.duration * ref_fps)
                    src_out = self._frames_to_tc(src_out_frames, ref_fps, is_df)

                    # Record timecode (in timeline)
                    rec_in = self._frames_to_tc(record_in_frames, ref_fps, is_df)
                    rec_out_frames = record_in_frames + int(clip.duration * ref_fps)
                    rec_out = self._frames_to_tc(rec_out_frames, ref_fps, is_df)

                    # Reel name (from filename, max 8 chars for compatibility)
                    reel = clip.filename[:8].replace(' ', '_').upper()
                    if len(reel) < 8:
                        reel = reel.ljust(8)

                    # Write EDL entry
                    f.write(f"{edit_num:03d}  {reel} V     C        {src_in} {src_out} {rec_in} {rec_out}\n")
                    f.write(f"* FROM CLIP NAME: {clip.filename}\n")
                    if clip.camera_id:
                        f.write(f"* CAMERA: {clip.camera_id}\n")
                    f.write(f"* SOURCE FILE: {clip.path}\n")
                    f.write("\n")

                    edit_num += 1
                    record_in_frames = rec_out_frames

            messagebox.showinfo("Success", f"EDL exported to:\n{output_path}")
            self.status_label.configure(text=f"Exported EDL with {edit_num - 1} edits")

        except Exception as e:
            messagebox.showerror("Error", f"EDL export failed:\n{str(e)}")

    def _frames_to_tc(self, total_frames, fps, drop_frame=False):
        """Convert frame count to timecode string."""
        fps_int = int(round(fps))
        sep = ';' if drop_frame else ':'

        if drop_frame and fps_int == 30:
            # Drop frame calculation for 29.97 fps
            # Drop 2 frames every minute except every 10th minute
            D = total_frames // 17982  # Number of 10-minute blocks
            M = total_frames % 17982  # Frames in current 10-min block
            if M < 2:
                M = 2
            frames = total_frames + 18 * D + 2 * ((M - 2) // 1798)
        else:
            frames = total_frames

        f = frames % fps_int
        s = (frames // fps_int) % 60
        m = (frames // (fps_int * 60)) % 60
        h = (frames // (fps_int * 3600)) % 24

        return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"

    def _export_report(self):
        """Export a detailed text report of all clips and their timecode info."""
        if not self.clips:
            messagebox.showinfo("Info", "No clips to export")
            return

        # Force window to front
        self.root.lift()
        self.root.focus_force()
        self.root.update_idletasks()
        self.root.update()

        output_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Timecode Report",
            defaultextension=".txt",
            initialdir=self.last_directory,
            filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")]
        )

        if not output_path:
            return

        # Update last directory
        self.last_directory = os.path.dirname(output_path)

        try:
            is_csv = output_path.lower().endswith('.csv')

            with open(output_path, 'w', encoding='utf-8') as f:
                if is_csv:
                    # CSV format
                    f.write("Filename,Start TC,End TC,Duration,FPS,Drop Frame,Camera,LTC Channel,Status,Path\n")
                    for clip in self.clips:
                        df = "Yes" if clip.drop_frame else "No"
                        cam = clip.camera_id or ""
                        ltc_ch = str(clip.ltc_channel) if clip.ltc_channel >= 0 else "N/A"
                        f.write(f'"{clip.filename}","{clip.start_tc or "N/A"}","{clip.end_tc or "N/A"}",'
                               f'{clip.duration:.2f},{clip.fps_display},{df},"{cam}",{ltc_ch},'
                               f'{clip.status},"{clip.path}"\n')
                else:
                    # Text format
                    f.write("=" * 80 + "\n")
                    f.write("LTC TIMECODE SYNC REPORT\n")
                    f.write("=" * 80 + "\n\n")
                    f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Total Clips: {len(self.clips)}\n")

                    # Summary by status
                    done = sum(1 for c in self.clips if c.status == 'done')
                    failed = sum(1 for c in self.clips if c.status in ('no_ltc', 'no_audio', 'error'))
                    f.write(f"With Timecode: {done}\n")
                    f.write(f"Failed: {failed}\n\n")

                    # Group by camera
                    camera_groups = {}
                    for clip in self.clips:
                        cam_id = clip.camera_id or "Unknown"
                        if cam_id not in camera_groups:
                            camera_groups[cam_id] = []
                        camera_groups[cam_id].append(clip)

                    for cam_id in sorted(camera_groups.keys()):
                        f.write("-" * 80 + "\n")
                        f.write(f"CAMERA: {cam_id}\n")
                        f.write("-" * 80 + "\n\n")

                        for clip in camera_groups[cam_id]:
                            f.write(f"  File: {clip.filename}\n")
                            f.write(f"    Start TC: {clip.start_tc or 'Not detected'}\n")
                            f.write(f"    End TC:   {clip.end_tc or 'N/A'}\n")
                            f.write(f"    Duration: {clip.duration:.2f}s\n")
                            f.write(f"    FPS:      {clip.fps_display}\n")
                            f.write(f"    Drop Frame: {'Yes' if clip.drop_frame else 'No'}\n")
                            if clip.ltc_channel >= 0:
                                f.write(f"    LTC Channel: {clip.ltc_channel}\n")
                            f.write(f"    Status:   {clip.status}\n")
                            if clip.error:
                                f.write(f"    Error:    {clip.error}\n")
                            f.write(f"    Path:     {clip.path}\n")
                            f.write("\n")

                    f.write("=" * 80 + "\n")
                    f.write("END OF REPORT\n")
                    f.write("=" * 80 + "\n")

            messagebox.showinfo("Success", f"Report exported to:\n{output_path}")
            self.status_label.configure(text=f"Exported report for {len(self.clips)} clips")

        except Exception as e:
            messagebox.showerror("Error", f"Export failed:\n{str(e)}")

    # =========================================================================
    # Update Loop
    # =========================================================================

    def _update_loop(self):
        try:
            while True:
                msg_type, data = self.queue.get_nowait()

                if msg_type == 'progress':
                    current, total, name = data
                    self.progress['value'] = (current / total) * 100
                    self.status_label.configure(text=f"Analyzing ({current}/{total}): {name}")

                elif msg_type == 'update':
                    # Coalesced: a full list rebuild per analyzed clip is
                    # O(clips^2) widget churn on big batches
                    self._schedule_clips_list_refresh()

                elif msg_type == 'done':
                    self.analyzing = False
                    self.progress['value'] = 0
                    done_count = sum(1 for c in self.clips if c.start_tc)
                    failed_count = sum(1 for c in self.clips if c.status in ('no_ltc', 'no_audio', 'error'))
                    warned = sum(1 for c in self.clips if getattr(c, 'ltc_warning', ''))
                    self.status_label.configure(text=f"Analysis complete - {done_count} with TC, {failed_count} failed"
                                                     + (f", {warned} with a timecode warning" if warned else ""))

                    # Assign colors based on camera ID (same camera = same color)
                    self._assign_camera_colors()
                    self._refresh_clips_list()

                    # Auto-sync clips after analysis
                    if done_count > 0:
                        self._sync_clips()
                    else:
                        self._draw_timeline()

                    # Proxies for heavy footage (positions are known after sync)
                    self._queue_preview_proxies()

                elif msg_type == 'status':
                    if not self.analyzing and not self.timeline_playing:
                        self.status_label.configure(text=data)

                elif msg_type == 'proxy':
                    done, total, name, original, built = data
                    if built:
                        self._on_proxy_ready(original)
                    if total and done >= total:
                        threading.Thread(target=self._enforce_scratch_limit, daemon=True).start()
                    if not self.analyzing:
                        if total and done < total:
                            self.status_label.configure(
                                text=f"Making preview proxies {done}/{total} ({name}) - preview gets smooth as they finish")
                        elif total:
                            self.status_label.configure(text="Preview proxies ready - smooth preview for all clips")

                elif msg_type == 'error':
                    pass

        except queue.Empty:
            pass

        self.root.after(100, self._update_loop)

    def on_closing(self):
        """Handle application close - save settings and cleanup."""
        proc = getattr(self, '_apply_ltc_current_process', None)
        if proc is not None and proc.poll() is None:
            if not messagebox.askyesno(
                    "Apply LTC Still Running",
                    "Timecode is still being written to a file.\n\n"
                    "Stop it and close? The unfinished file is deleted."):
                return
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            out = getattr(self, '_apply_ltc_current_output', None)
            if out and os.path.exists(out):
                try:
                    os.remove(out)
                except Exception:
                    pass
        # Stop background FFmpeg work right away (the job object also ends
        # any child process when the app exits)
        self._closing = True
        self.thumbnail_extraction_queue.clear()
        thumb = getattr(self, '_thumb_proc', None)
        if thumb is not None and thumb.poll() is None:
            try:
                thumb.kill()
            except Exception:
                pass
        if getattr(self, 'hw_player', None):
            self.hw_player.shutdown()
        # Stop all playback first
        if self.timeline_playing:
            self.timeline_playing = False
            if self.timeline_update_id:
                self.root.after_cancel(self.timeline_update_id)
                self.timeline_update_id = None
        # Tear down the timeline audio stream UNCONDITIONALLY: during a soft
        # scrub pause the stream is still open while timeline_playing is False,
        # and it must not keep firing callbacks into a destroyed app.
        try:
            self._stop_timeline_audio()
        except Exception:
            pass

        if self.preview_playing:
            self.preview_playing = False
            if self.preview_update_id:
                self.root.after_cancel(self.preview_update_id)
                self.preview_update_id = None
            self._stop_audio_playback()

        # Cancel any pending deferred redraws
        if hasattr(self, '_deferred_redraw_id') and self._deferred_redraw_id:
            self.root.after_cancel(self._deferred_redraw_id)
            self._deferred_redraw_id = None

        # Release VLC resources
        if self.use_vlc:
            try:
                if self.vlc_player:
                    self.vlc_player.stop()
                    self.vlc_player.release()
                if self.vlc_instance:
                    self.vlc_instance.release()
            except Exception:
                pass  # Ignore errors during cleanup

        self._save_settings()  # Save settings before closing
        self.analyzer.cleanup()
        self.root.destroy()


def check_dependencies():
    """Check for required dependencies and show helpful installation dialog if missing."""
    missing = []
    install_instructions = []

    # Check FFmpeg
    if not FFMPEG_AVAILABLE:
        missing.append("FFmpeg")
        install_instructions.append(
            "FFmpeg (required for media analysis):\n"
            "  - Download from: https://ffmpeg.org/download.html\n"
            "  - Or install via winget: winget install FFmpeg\n"
            "  - Or install via chocolatey: choco install ffmpeg\n"
            "  - Make sure ffmpeg.exe and ffprobe.exe are in your PATH\n"
            "    or in C:\\ffmpeg\\bin\\"
        )

    # Check VLC
    if not VLC_AVAILABLE:
        missing.append("VLC")
        install_instructions.append(
            "VLC (recommended for video playback):\n"
            "  - Download from: https://www.videolan.org/vlc/\n"
            "  - Install 64-bit version to match Python\n"
            "  - Then: pip install python-vlc"
        )

    if missing:
        # Create a temporary root for the dialog
        temp_root = tk.Tk()
        temp_root.withdraw()

        if "FFmpeg" in missing:
            # FFmpeg is required - show error and exit
            message = (
                "Missing required dependency: FFmpeg\n\n"
                "This application requires FFmpeg for media analysis.\n\n"
                + install_instructions[0] + "\n\n"
                "The application will now exit."
            )
            if "VLC" in missing:
                message = (
                    "Missing dependencies:\n\n"
                    + "\n\n".join(install_instructions) + "\n\n"
                    "FFmpeg is required. VLC is optional but recommended.\n"
                    "The application will now exit."
                )
            messagebox.showerror("Missing Dependencies", message)
            temp_root.destroy()
            return False
        else:
            # Only VLC missing - show warning but continue
            message = (
                "Optional dependency missing: VLC\n\n"
                + install_instructions[0] + "\n\n"
                "The app will work but video playback may be limited.\n"
                "Continue anyway?"
            )
            result = messagebox.askyesno("Missing Optional Dependency", message)
            temp_root.destroy()
            if not result:
                return False

    return True


_CHILD_JOB = None  # kept open for the app's lifetime (closing it kills the children)


def end_child_processes_with_app() -> bool:
    """Windows: put this process in a job object that KILLS every child
    process (FFmpeg/ffprobe thumbnails, waveforms, proxies, ...) when the app
    exits - normally, by crash or from Task Manager. Without it, a thumbnail
    FFmpeg on a 190 GB file kept decoding for hours after the app was closed.
    Programs meant to outlive the app are started with
    CREATE_BREAKAWAY_FROM_JOB (e.g. Explorer for "Open file location")."""
    global _CHILD_JOB
    if os.name != 'nt' or _CHILD_JOB:
        return bool(_CHILD_JOB)
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXTENDED_LIMIT()
        # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        info.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000800
        if (k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))   # 9 = extended limits
                and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())):
            _CHILD_JOB = job
            return True
        k32.CloseHandle(job)
    except Exception:
        pass
    return False


def main():
    # Child processes (FFmpeg etc.) must end with the app
    end_child_processes_with_app()

    # Check dependencies before starting
    if not check_dependencies():
        return

    # Use TkinterDnD if available for drag-and-drop support
    global DND_AVAILABLE
    root = None
    if DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()
        except Exception as e:
            print(f"[WARN] Drag-and-drop unavailable ({e}); continuing without it.")
            DND_AVAILABLE = False
    if root is None:
        root = tk.Tk()

    app = LTCSyncApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
