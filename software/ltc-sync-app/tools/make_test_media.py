#!/usr/bin/env python3
"""Generate synthetic multi-camera test media with LTC for trying the LTC Sync App.

Creates three "camera" clips (video from FFmpeg test sources, audio: a tone on channel 1 and
LTC on channel 2) and one "recorder" WAV (tone on channel 1, LTC on channel 2). The clips
overlap in time the way a real multi-camera shoot would. The LTC carries the date and a
two-digit camera ID in its user bits, in the layout the TimecodeMaster firmware writes.

Usage:  python make_test_media.py OUTPUT_DIR [--fps 25]
Needs numpy and ffmpeg on the path. No real footage is involved.
"""
import argparse
import os
import subprocess
import wave

import numpy as np

RATE = 48000


def nibbles(v, n):
    return [(v >> i) & 1 for i in range(n)]


def frame_bits(h, m, s, f, drop, ub):
    """80 LTC bits (bit 0 first). ub = 8 nibbles [UB1..UB8]."""
    b = [0] * 80
    def put(pos, val, n):
        for i, x in enumerate(nibbles(val, n)):
            b[pos + i] = x
    put(0, f % 10, 4); put(8, f // 10, 2); b[10] = 1 if drop else 0
    put(16, s % 10, 4); put(24, s // 10, 3)
    put(32, m % 10, 4); put(40, m // 10, 3)
    put(48, h % 10, 4); put(56, h // 10, 2)
    for i, pos in enumerate((4, 12, 20, 28, 36, 44, 52, 60)):
        put(pos, ub[i], 4)
    sync = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
    b[64:80] = sync
    return b


def user_bits(month, day, year, cam):
    # units first, tens second; camera: second digit first (as the firmware writes them)
    return [month % 10, month // 10, day % 10, day // 10, year % 10, year // 10, cam % 10, cam // 10]


def ltc_audio(start, seconds, fps, ub, amp=0.5):
    """Biphase-mark LTC at 48 kHz. Returns float32 samples. fps must divide 48000/80 evenly (24, 25, 30)."""
    spb = RATE // (fps * 80)
    h, m, s, f = start
    nframes = int(seconds * fps)
    out = np.empty(nframes * 80 * spb, dtype=np.float32)
    level = 1.0
    pos = 0
    for _ in range(nframes):
        for bit in frame_bits(h, m, s, f, False, ub):
            level = -level
            half = spb // 2
            out[pos:pos + half] = level * amp
            if bit:
                level = -level
            out[pos + half:pos + spb] = level * amp
            pos += spb
        f += 1
        if f >= fps:
            f = 0; s += 1
            if s >= 60:
                s = 0; m += 1
                if m >= 60:
                    m = 0; h = (h + 1) % 24
    return out


def write_wav(path, ch1, ch2):
    n = min(len(ch1), len(ch2))
    pcm = np.stack([ch1[:n], ch2[:n]], axis=1)
    pcm = (np.clip(pcm, -1, 1) * 32767).astype('<i2')
    with wave.open(path, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def tone(n, freq, amp=0.2):
    t = np.arange(n) / RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=25, choices=(24, 25, 30))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    fps = a.fps
    date = (11, 20, 26)  # month, day, year

    # name, camera ID, start timecode, length in s, FFmpeg video source, tone Hz
    clips = [
        ("A001C001_CAM01.mp4", 1, (10, 0, 0, 0), 40, "testsrc2", 440),
        ("B001C001_CAM02.mp4", 2, (10, 0, 12, 10), 40, "smptebars", 523),
        ("C001C001_CAM03.mp4", 3, (10, 0, 25, 0), 40, "mandelbrot", 659),
    ]
    for name, cam, start, secs, src, hz in clips:
        wav = os.path.join(a.out, name + ".tmp.wav")
        ltc = ltc_audio(start, secs, fps, user_bits(*date, cam))
        write_wav(wav, tone(len(ltc), hz), ltc)
        vid = os.path.join(a.out, name)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"{src}=size=640x360:rate={fps}",
                        "-i", wav, "-t", str(secs),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", vid], check=True)
        os.remove(wav)
        print("wrote", vid)

    # sound recorder: starts before the cameras and runs through all of them
    start, secs = (9, 59, 55, 0), 80
    ltc = ltc_audio(start, secs, fps, user_bits(*date, 0))
    rec = os.path.join(a.out, "ZOOM0001_LR.wav")
    write_wav(rec, tone(len(ltc), 220, 0.15), ltc)
    print("wrote", rec)


if __name__ == "__main__":
    main()
