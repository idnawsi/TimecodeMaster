# Timing

## LTC bit rate

An LTC frame has 80 bits and one frame is sent per video frame, so the bit rate is `fps x 80`. The signal is biphase-mark coded: the level always changes at the start of a bit cell, and a one also changes level in the middle of the cell. A bit cell must therefore contain an even number of samples if the mid-cell edge is to land on a sample boundary, and it is simplest when the number is a whole integer.

## The non-integer rates

NTSC-derived rates are the integer rate divided by 1.001:

| Name | Exact rate | Bit rate |
|---|---|---|
| 23.976 | 24000 / 1001 = 23.976024 fps | 1918.0819 bit/s |
| 29.97 | 30000 / 1001 = 29.970030 fps | 2397.6024 bit/s |

A fixed 48000 Hz sample clock gives 48000 / 1918.0819 = 25.02 samples per bit, which is not an integer. The usual workarounds are to alternate between 25 and 26 samples per bit (adds jitter to the edges) or to run the integer rate (24.000 fps) and accept the error. A 0.1 percent error is 3.6 s per hour of recording, which is far outside what a post workflow tolerates.

This design changes the sample clock instead. Dividing the clock by 1001 as well moves the entire timebase by the same factor as the frame rate:

```
sample clock  = 48 000 000 / 1001 = 47 952.048 Hz
samples / bit = 47 952.048 / 1918.0819 = 25   (23.976 fps)
samples / bit = 47 952.048 / 2397.6024 = 20   (29.97 fps)
```

Both are exact integers. The edges of the output waveform are spaced by exact multiples of one sample period, and the frame period is exactly 1001/24000 s or 1001/30000 s.

| Rate | Sample clock | Samples per bit | Bit cell | Frame period |
|---|---|---|---|---|
| 23.976 | 47952.048 Hz | 25 | 521.4 us | 41.708 ms |
| 24 | 48000 Hz | 25 | 520.8 us | 41.667 ms |
| 25 | 48000 Hz | 24 | 500.0 us | 40.000 ms |
| 29.97 | 47952.048 Hz | 20 | 417.1 us | 33.367 ms |
| 30 | 48000 Hz | 20 | 416.7 us | 33.333 ms |

### Si5351 settings

```
VCO (PLL A)      = 25 MHz x (30 + 18/25)       = 768.000000 MHz
Multisynth 0     = 1000 or 1001                 (integer mode, P1 = 128 x M - 512, P2 = 0, P3 = 1)
Output divider R = 16
CLK0 = 768 MHz / (M x 16)     = 48000.000 Hz   (M = 1000)
                              = 47952.048 Hz   (M = 1001)
```

Integer multisynth mode is used for both. The output frequency is therefore bit-exact relative to the TCXO, and the only error is the TCXO's own. The PLL is set once at start-up. A frame-rate change rewrites only the multisynth.

The frame rate, drop-frame flag and sample rate are sent together from the master to the clients. The firmware accepts only 47952 and 48000 as sample rates.

## Drop frame

29.97 fps timecode with non-drop numbering counts 30 frames per second but the real rate is 29.97003, so the displayed time falls behind the wall clock by 3.6 s per hour. Drop-frame numbering skips frame numbers 0 and 1 at the start of every minute except minutes divisible by ten, which removes 108 numbers per hour (3.6 s at 30 numbers per second). No picture frames are removed.

The firmware applies this when the frame rate is 29.97 and the drop-frame flag is set (frame-flag bit 10 in the LTC word). At other rates the flag is ignored. Residual error with drop frame is 3.6 ms per hour (86.4 ms per day), which is inherent to the scheme.

A short press of the FPS button cycles through 23.976, 24, 25, 29.97 non-drop, 29.97 drop, 30.

## Drift budget

Each unit derives its sample clock from a 0.5 ppm TCXO through exact integer division, so a unit's frequency error equals its TCXO error. For two units that were synchronised at the same instant:

```
worst relative error = 0.5 ppm + 0.5 ppm = 1.0 ppm
per hour             = 1e-6 x 3600 s = 3.6 ms
per 12 hours         = 43 ms
```

| Frame rate | Frame period | Worst-case drift after 1 hour | After 12 hours |
|---|---|---|---|
| 24 | 41.7 ms | 0.09 frame | 1.0 frame |
| 25 | 40.0 ms | 0.09 frame | 1.1 frames |
| 30 | 33.3 ms | 0.11 frame | 1.3 frames |

This ignores TCXO ageing and warm-up. The 0.5 ppm figure is the manufacturer's stability over temperature. Real units are usually better than the worst case, and units in the same bag see the same temperature. For a long day, resync during a break, or use the automatic sync interval on the master's page.

The DS3231M RTC is not part of the LTC timebase. It only provides the calendar date for the user bits and a time source when the master starts.

### Clock integrity check

The sample interrupt can in principle miss a pulse if another interrupt blocks it for more than one sample period (20.8 us). To make this visible, the LTC device compares the number of pulses it has counted with the number expected from the CPU cycle counter in windows of 2048 pulses (about 43 ms). A missed pulse shows as a positive count, an extra pulse (for example ringing on the clock line) as a negative one. The master's firmware page shows the totals for every unit, the longest gap between two interrupts, and when errors happened. The interrupt runs at level 3 so that the SPI, I2C and tick interrupts at level 1 cannot hold it off.

## Implementation notes

These describe what the current firmware does and does not do, so that the output can be matched against a receiver's expectations.

- Supported rates are 23.976, 24, 25, 29.97 (drop or non-drop) and 30 fps. 50, 59.94 and 60 fps are not generated.
- Bit 10 is the drop-frame flag. The colour-frame flag (bit 11) is left at zero.
- The biphase polarity-correction bit (bit 27 for 24 and 30 fps, bit 59 for 25 fps) is left at zero. Decoders recover data from the intervals between transitions, so this does not affect decoding, but it means the waveform polarity at the start of a frame is not fixed from frame to frame.
- The binary group flag bits are zero. The user-bit field carries the date (month, day, year as BCD) and a two-character camera ID.
- The DAC output is a full-scale square wave (0 and 255), buffered by a Schmitt inverter and attenuated by the output stage. Edge timing is set by the sample clock; edge shape is set by the analog stage and the cable.
- LTC output offset: a signed number of frames, limited to +/-30, that is added to the timecode when the frame is built (drop-frame aware, wraps at 24 hours). The unit's displayed time and its jam-sync and wireless sync behaviour are unaffected.
- Jam sync accepts any edge interval between 170 and 310 us for half bits and 360 to 580 us for full bits, which covers 23.976 to 30 fps with some tolerance.
