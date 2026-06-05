"""
WEFAX (Weather Facsimile) decoder.

Subcarrier encoding:
  Black  : 1500 Hz  → pixel  0
  White  : 2300 Hz  → pixel 255
  Phasing: 300 Hz tone (alternating black/white for column sync)
  Stop   : 450 Hz tone

Standard modes:
  IOC 576, 120 LPM  →  1809 px/line, 24 000 samp/line @ 48 kHz
  IOC 576,  60 LPM  →  1809 px/line, 48 000 samp/line @ 48 kHz

Tune the VFO to the WEFAX carrier with USB demodulation; the output audio
contains the 1500-2300 Hz subcarrier that this decoder reads.
"""
from __future__ import annotations

import time
import numpy as np
from typing import Optional
from scipy.signal import butter, sosfilt, hilbert

from .base import BaseAudioDecoder, DecoderResult


class WEFAXDecoder(BaseAudioDecoder):

    BLACK_HZ = 1500.0
    WHITE_HZ = 2300.0
    START_HZ  = 300.0   # phasing / start tone
    STOP_HZ   = 450.0   # stop tone

    def __init__(self, lpm: int = 120, ioc: int = 576, continuous: bool = False,
                 sample_rate: int = 48_000):
        super().__init__('WEFAX', sample_rate)
        self.lpm = lpm
        self.ioc = ioc
        self.continuous = continuous
        self.line_width = int(ioc * np.pi)   # 1809 for IOC 576
        self._build_filters(sample_rate)
        self._reset_state()

    # ------------------------------------------------------------------
    # BaseDecoder interface
    # ------------------------------------------------------------------

    def set_sample_rate(self, rate: int):
        if rate != self.sample_rate:
            self.sample_rate = rate
            self.min_buffer_size = rate // 10
            self._build_filters(rate)

    def configure(self, params: dict):
        """Apply new settings at runtime (lpm, ioc, continuous)."""
        changed = False
        if 'lpm' in params and params['lpm'] != self.lpm:
            self.lpm = int(params['lpm'])
            changed = True
        if 'ioc' in params and params['ioc'] != self.ioc:
            self.ioc = int(params['ioc'])
            self.line_width = int(self.ioc * np.pi)
            changed = True
        if 'continuous' in params:
            self.continuous = bool(params['continuous'])
            changed = True
        if changed:
            self._reset_state()

    def reset(self):
        self._reset_state()

    def format_result(self, result: DecoderResult) -> str:
        d = result.data
        t = d.get('type', '?')
        if t == 'line':
            return f"WEFAX line {d['line_index']} ({d['state']})"
        if t == 'complete':
            return f"WEFAX image complete — {d['line_count']} lines"
        if t == 'start':
            return f"WEFAX start — {d['lpm']} LPM  IOC {d['ioc']}"
        return f"WEFAX {t}"

    # ------------------------------------------------------------------
    # Audio processing
    # ------------------------------------------------------------------

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Accumulate audio until a full scan line is ready, then process it."""
        self._linebuf = np.concatenate([self._linebuf, audio.astype(np.float32)])

        samples_per_line = max(1, int(self.sample_rate * 60.0 / self.lpm))
        last_result = None

        while len(self._linebuf) >= samples_per_line:
            line = self._linebuf[:samples_per_line]
            self._linebuf = self._linebuf[samples_per_line:]
            result = self._process_line(line)
            if result is not None:
                last_result = result

        return last_result

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _process_line(self, line: np.ndarray) -> Optional[DecoderResult]:
        # In continuous mode jump straight to IMAGE on first call
        if self.continuous and self._state == 'IDLE':
            self._state = 'IMAGE'
            self._line_index = 0
            self._start_time = time.time()
            return DecoderResult(
                decoder_name='WEFAX',
                timestamp=time.time(),
                data={
                    'type': 'start',
                    'lpm': self.lpm,
                    'ioc': self.ioc,
                    'line_width': self.line_width,
                    'continuous': True,
                },
            )

        tone_hz, tone_ratio = self._detect_tone(line)
        has_start = (tone_hz is not None
                     and abs(tone_hz - self.START_HZ) < 80
                     and tone_ratio > 0.4)
        has_stop = (tone_hz is not None
                    and abs(tone_hz - self.STOP_HZ) < 80
                    and tone_ratio > 0.4)

        # --- IDLE: wait for start tone ---
        if self._state == 'IDLE':
            if has_start:
                self._tone_count += 1
                if self._tone_count >= 2:
                    self._state = 'PHASING'
                    self._line_index = 0
                    self._start_time = time.time()
                    return DecoderResult(
                        decoder_name='WEFAX',
                        timestamp=time.time(),
                        data={
                            'type': 'start',
                            'lpm': self.lpm,
                            'ioc': self.ioc,
                            'line_width': self.line_width,
                            'continuous': False,
                        },
                    )
            else:
                self._tone_count = 0
            return None

        # Demodulate for PHASING and IMAGE states
        pixels = self._demodulate(line)

        # --- PHASING: emit phasing lines, detect image start ---
        if self._state == 'PHASING':
            if not has_start:
                self._no_tone_count += 1
                if self._no_tone_count >= 2:
                    self._state = 'IMAGE'
                    self._no_tone_count = 0
            else:
                self._no_tone_count = 0
            self._line_index += 1
            return DecoderResult(
                decoder_name='WEFAX',
                timestamp=time.time(),
                data={
                    'type': 'line',
                    'state': 'phasing',
                    'line_index': self._line_index,
                    'pixels': pixels.tolist(),
                    'lpm': self.lpm,
                    'ioc': self.ioc,
                    'line_width': self.line_width,
                },
            )

        # --- IMAGE: emit image lines, detect stop tone (unless continuous) ---
        if self._state == 'IMAGE':
            if not self.continuous and has_stop:
                self._stop_count += 1
                if self._stop_count >= 2:
                    count = self._line_index
                    self._reset_state()
                    return DecoderResult(
                        decoder_name='WEFAX',
                        timestamp=time.time(),
                        data={
                            'type': 'complete',
                            'line_count': count,
                            'lpm': self.lpm,
                            'ioc': self.ioc,
                        },
                    )
            elif not self.continuous:
                self._stop_count = 0
            self._line_index += 1
            return DecoderResult(
                decoder_name='WEFAX',
                timestamp=time.time(),
                data={
                    'type': 'line',
                    'state': 'image',
                    'line_index': self._line_index,
                    'pixels': pixels.tolist(),
                    'lpm': self.lpm,
                    'ioc': self.ioc,
                    'line_width': self.line_width,
                },
            )

        return None

    # ------------------------------------------------------------------
    # Signal processing helpers
    # ------------------------------------------------------------------

    def _build_filters(self, sr: int):
        nyq = sr / 2.0
        # Bandpass around the WEFAX subcarrier (1300–2600 Hz)
        self._bp_sos = butter(
            4, [1300.0 / nyq, 2600.0 / nyq], btype='band', output='sos'
        )

    def _reset_state(self):
        self._state         = 'IDLE'
        self._linebuf       = np.array([], dtype=np.float32)
        self._line_index    = 0
        self._start_time    = None
        self._tone_count    = 0
        self._no_tone_count = 0
        self._stop_count    = 0

    def _detect_tone(self, line: np.ndarray):
        """
        Detect dominant low-frequency tone vs. subcarrier energy.

        Returns (freq_hz, ratio) where ratio is tone energy / (tone + sub),
        or (None, 0.0) when the tone is not prominent.
        """
        n = min(len(line), 8192)
        chunk = line[:n]
        win   = np.hanning(n)
        fft   = np.abs(np.fft.rfft(chunk * win))
        freqs = np.fft.rfftfreq(n, 1.0 / self.sample_rate)

        tone_mask = (freqs >= 200) & (freqs <= 600)
        sub_mask  = (freqs >= 1300) & (freqs <= 2600)
        tone_e    = float(fft[tone_mask].sum()) if tone_mask.any() else 0.0
        sub_e     = float(fft[sub_mask].sum())  if sub_mask.any()  else 0.0
        total     = tone_e + sub_e
        if total < 1e-9:
            return None, 0.0

        ratio = tone_e / total
        if ratio < 0.3:
            return None, ratio

        peak_hz = float(freqs[tone_mask][fft[tone_mask].argmax()])
        return peak_hz, ratio

    def _demodulate(self, line: np.ndarray) -> np.ndarray:
        """
        FM-discriminate the WEFAX subcarrier and return uint8 pixel values.

        Pipeline: bandpass filter → analytic signal → instantaneous frequency
        → linear scale (1500 Hz=0, 2300 Hz=255) → resample to line_width.
        """
        filtered  = sosfilt(self._bp_sos, line)
        analytic  = hilbert(filtered)
        phase     = np.unwrap(np.angle(analytic))
        inst_freq = np.diff(phase) * (self.sample_rate / (2.0 * np.pi))

        px = (inst_freq - self.BLACK_HZ) / (self.WHITE_HZ - self.BLACK_HZ) * 255.0
        px = np.clip(px, 0.0, 255.0)

        # Resample to target line width via linear interpolation
        if len(px) != self.line_width:
            xp = np.arange(len(px), dtype=np.float64)
            xi = np.linspace(0.0, float(len(px) - 1), self.line_width)
            px = np.interp(xi, xp, px)

        return px.astype(np.uint8)
