"""
Manchester (biphase-M / IEEE 802.3) decoder.

Convention:
  1 = low→high transition at the bit centre  (rising)
  0 = high→low transition at the bit centre  (falling)

Clock ticks where both quarter-point samples have the same sign are silent
bit-boundary transitions and carry no data — they are silently skipped.
"""
import time
import numpy as np
from typing import Callable, Optional

from .base import BaseAudioDecoder, DecoderResult

import logging
logger = logging.getLogger(__name__)


class _ManchesterSampler:
    """
    Sample-by-sample clock recovery and bit slicer.

    The bit period is divided into quarters:
      - T/4  sample → first-half polarity
      - 3T/4 sample → second-half polarity

    A mid-bit transition is decoded:
      first < 0, second ≥ 0  →  bit 1
      first ≥ 0, second < 0  →  bit 0
      both same sign          →  bit-boundary artifact, discarded
    """

    def __init__(self, audio_rate: int, baud_rate: int, on_bit: Callable[[int], None]):
        self.spb = float(audio_rate) / float(baud_rate)
        self.on_bit = on_bit
        self._dc = 0.0
        self._dc_alpha = 0.005
        self._phase = 0.0
        self._last_s = 0.0
        self._q1 = 0.0
        self._q3 = 0.0
        self._q1_ready = False
        self._q3_ready = False

    def reset(self):
        self._dc = 0.0
        self._dc_alpha = 0.005
        self._phase = 0.0
        self._last_s = 0.0
        self._q1_ready = False
        self._q3_ready = False

    def process(self, samples: np.ndarray) -> None:
        spb = self.spb
        spb_half = spb * 0.5

        for raw in samples:
            self._dc += self._dc_alpha * (float(raw) - self._dc)
            s = float(raw) - self._dc

            # Clock recovery: nudge phase toward the mid-bit on any zero crossing
            if (self._last_s < 0) != (s < 0):
                err = self._phase - spb_half
                if err > spb_half:
                    err -= spb
                elif err < -spb_half:
                    err += spb
                if abs(err) < spb * 0.45:
                    self._phase -= err * 0.15

            self._last_s = s
            self._phase += 1.0

            if not self._q1_ready and self._phase >= spb * 0.25:
                self._q1 = s
                self._q1_ready = True

            if not self._q3_ready and self._phase >= spb * 0.75:
                self._q3 = s
                self._q3_ready = True

            if self._phase >= spb:
                self._phase -= spb
                if self._q1_ready and self._q3_ready:
                    if self._q1 < 0 and self._q3 >= 0:
                        self.on_bit(1)
                    elif self._q1 >= 0 and self._q3 < 0:
                        self.on_bit(0)
                self._q1_ready = False
                self._q3_ready = False


class ManchesterDecoder(BaseAudioDecoder):
    """
    Manchester decoder that accumulates decoded bits and emits them as
    hex-formatted byte strings.  Bytes are packed MSB-first; byte alignment
    depends on where the stream starts (no framing sync word).
    """

    def __init__(self, sample_rate: int = 48000, baud_rate: int = 1200):
        super().__init__("Manchester", sample_rate)
        self.baud_rate = baud_rate
        self._sampler = _ManchesterSampler(sample_rate, baud_rate, self._on_bit)
        self._bits: list = []

    def set_sample_rate(self, rate: int):
        self.sample_rate = rate
        self._sampler = _ManchesterSampler(rate, self.baud_rate, self._on_bit)
        self._bits = []

    def _on_bit(self, bit: int):
        self._bits.append(bit)

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        if audio.size == 0:
            return None

        self._sampler.process(audio)

        n_bytes = len(self._bits) // 8
        if n_bytes == 0:
            return None

        consumed = n_bytes * 8
        bit_slice = self._bits[:consumed]
        self._bits = self._bits[consumed:]

        bytes_out = []
        for i in range(n_bytes):
            val = 0
            for j in range(8):
                val = (val << 1) | bit_slice[i * 8 + j]
            bytes_out.append(val)

        hex_str = ' '.join(f'{b:02X}' for b in bytes_out)
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in bytes_out)

        logger.debug("Manchester@%d decoded %d bytes: %s", self.baud_rate, n_bytes, hex_str)

        return DecoderResult(
            decoder_name='Manchester',
            timestamp=time.time(),
            data={
                'hex': hex_str,
                'ascii': ascii_str,
                'bytes': bytes_out,
                'baud': self.baud_rate,
            },
            confidence=0.7,
            metadata={'type': 'manchester'},
        )

    def reset(self):
        self._sampler.reset()
        self._bits = []

    def format_result(self, result: DecoderResult) -> str:
        data = result.data if isinstance(result.data, dict) else {}
        baud = data.get('baud', '?')
        hex_str = data.get('hex', '')
        ascii_str = data.get('ascii', '')
        return f"[Manchester@{baud}] {hex_str}  |  {ascii_str}"
