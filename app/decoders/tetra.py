"""
TETRA (Terrestrial Trunked Radio) burst detector — ETSI EN 300 392-2.

Demodulates π/4-DQPSK IQ and detects Normal Downlink Bursts (NDB) via
sync-word correlation, then extracts the Broadcast Block (BB) for colour
code and timeslot information.

Recommended VFO settings
─────────────────────────
  • Mode      : IQ  (raw IQ is passed to decoders regardless of mode)
  • Bandwidth : ≥ 75 kHz  →  ≥ 4 samples/symbol at 18 000 Bd
  • Frequency : TETRA downlink channel (e.g. 380–400 MHz public safety)

Limitations (v1)
─────────────────
  • No matched filter / root-raised-cosine  — SNR penalty on noisy signals
  • No timing recovery  — relies on stable HackRF sample clock
  • No Viterbi / RCPC decoding  — Block 1 / Block 2 content is raw bits
  • BB parsing assumes BCCH timeslot; traffic-channel BB will show garbage
  • Carrier frequency should be within ≈ 2 kHz of the TETRA channel centre
"""

import time
import logging
import numpy as np
from typing import Optional, Tuple
from collections import deque

from .base import BaseDecoder, DecoderResult

logger = logging.getLogger(__name__)

# ── Physical-layer constants (ETSI EN 300 392-2) ────────────────────────────

_SYMBOL_RATE     = 18_000       # symbols / second
_MIN_SAMPLE_RATE = 36_000       # 2 sps — absolute minimum for demodulation

# Normal Downlink Burst: 510 chips (255 π/4-DQPSK symbols) per timeslot
# ETSI EN 300 392-2, Table 9.33
#   tail(2) | B1(216) | SW(38) | BB(14) | B2(216) | tail(2) | FS2(22) = 510
_NDB_CHIPS  = 510
_NDB_SW_OFF = 218    # chip index of first SW chip  (2 + 216)
_NDB_BB_OFF = 256    # chip index of first BB chip  (218 + 38)
_NDB_B2_OFF = 270    # chip index of first B2 chip  (256 + 14)
_SW_LEN     = 38

# Sync word (SW), ETSI EN 300 392-2 Table 9.33  — 38 chips, MSB-first
_SW_BITS = np.array([
    1, 1, 0, 0, 1, 0, 0, 1,
    0, 1, 0, 1, 1, 1, 1, 0,
    1, 0, 0, 1, 0, 0, 0, 1,
    0, 0, 0, 0, 0, 1, 0, 0,
    1, 1, 1, 1, 1, 1,
], dtype=np.float32)
_SW_BIPOLAR = 2.0 * _SW_BITS - 1.0     # ±1 for cross-correlation

# π/4-DQPSK: differential phase → (I-bit, Q-bit), ETSI EN 300 392-2 Table 9.24
#   transmitted dibit (b₂ₙ, b₂ₙ₊₁) maps to phase change Δφ
_PI4_MAP: Tuple = (
    ( np.pi / 4,     0, 0),   # 00
    ( 3 * np.pi / 4, 0, 1),   # 01
    (-3 * np.pi / 4, 1, 1),   # 11
    (-np.pi / 4,     1, 0),   # 10
)

# These constants mirror what the SDR worker uses to size IQ chunks.
# They let us estimate the effective sample rate from len(data).
_AUDIO_RATE = 48_000
_CHUNK      = 2_048


# ── Decoder class ────────────────────────────────────────────────────────────

class TETRADecoder(BaseDecoder):
    """
    TETRA π/4-DQPSK Normal Downlink Burst detector.

    Processes narrowband IQ (the first argument of process()); audio is
    ignored.  Returns one DecoderResult per detected burst.
    """

    def __init__(self):
        super().__init__('TETRA', sample_rate=_MIN_SAMPLE_RATE)
        self._bits:    np.ndarray          = np.zeros(0, dtype=np.int8)
        self._burst_n: int                 = 0
        self._pending: deque               = deque()
        # Last sps_i samples from the previous chunk, used to compute the
        # differential phase for the first symbol of each new chunk so we
        # don't lose one symbol at every chunk boundary.
        self._tail: Optional[np.ndarray]   = None
        self._tail_sps_i: int              = 4

    # ── BaseDecoder interface ────────────────────────────────────────────

    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        if not self.is_enabled or data is None or len(data) < 4:
            return None
        if not np.iscomplexobj(data):
            return None

        # Estimate effective IQ sample rate from chunk length.
        # Identical to ADSBDecoder's approach: iq_needed = nb_sr*CHUNK/AUDIO_RATE
        # → nb_sr = len(data) * AUDIO_RATE / CHUNK
        nb_sr  = len(data) * _AUDIO_RATE / _CHUNK
        sps_i  = max(1, int(nb_sr / _SYMBOL_RATE))
        if nb_sr < _MIN_SAMPLE_RATE:
            return None

        # Prepend the tail from the previous chunk so the differential for
        # the first symbol of this chunk can be computed correctly.
        if self._tail is not None and self._tail_sps_i == sps_i:
            iq_in = np.concatenate([self._tail, data])
        else:
            iq_in = data

        self._tail       = data[-sps_i:].copy()
        self._tail_sps_i = sps_i

        new_bits = _demodulate(iq_in, sps_i)
        if new_bits is None or len(new_bits) == 0:
            return None

        self._bits = np.concatenate([self._bits, new_bits])
        self._drain()

        return self._pending.popleft() if self._pending else None

    def reset(self):
        self._bits       = np.zeros(0, dtype=np.int8)
        self._burst_n    = 0
        self._pending.clear()
        self._tail       = None
        self._tail_sps_i = 4

    def format_result(self, result: DecoderResult) -> str:
        d = result.data if isinstance(result.data, dict) else {}
        parts = [f"#{d.get('burst', '?')}"]
        if d.get('inverted'):
            parts.append("~inv")
        parts.append(f"err={d.get('sw_errors', '?')}")
        cc = d.get('colour_code')
        if cc is not None:
            parts.append(f"CC={cc}")
        ts = d.get('timeslot')
        if ts is not None:
            parts.append(f"TS={ts}")
        if d.get('system_code') == 1:
            parts.append("[BCCH]")
        return ' '.join(parts)

    # ── Burst search ──────────────────────────────────────────────────────

    def _drain(self):
        buf = self._bits
        while len(buf) >= _NDB_CHIPS:
            pos, inv = _find_sw(buf)
            if pos is None:
                # Discard all except a trailing window to avoid split-sync misses
                keep = _SW_LEN - 1
                buf  = buf[-keep:] if len(buf) > keep else buf
                break

            start = pos - _NDB_SW_OFF
            if start < 0:
                # SW found but burst header is before the buffer — advance past SW
                buf = buf[pos + _SW_LEN:]
                continue

            end = start + _NDB_CHIPS
            if end > len(buf):
                # Incomplete burst; preserve from burst start and wait for more
                buf = buf[start:]
                break

            burst = buf[start:end].copy()
            if inv:
                burst = 1 - burst

            r = self._decode_ndb(burst, inv)
            if r is not None:
                self._pending.append(r)
            buf = buf[end:]

        self._bits = buf

    def _decode_ndb(self, burst: np.ndarray, inverted: bool) -> Optional[DecoderResult]:
        """Validate sync word and extract BB metadata from one 510-chip burst."""
        sw_slice  = burst[_NDB_SW_OFF : _NDB_SW_OFF + _SW_LEN]
        sw_errors = int(np.sum(sw_slice != _SW_BITS.astype(np.int8)))
        if sw_errors > 6:
            return None

        self._burst_n += 1
        bb   = burst[_NDB_BB_OFF : _NDB_BB_OFF + 14]
        info = _parse_bb(bb)

        logger.debug(
            "TETRA NDB #%d  SW_err=%d  inv=%s  %s",
            self._burst_n, sw_errors, inverted, info,
        )

        return DecoderResult(
            decoder_name='TETRA',
            timestamp=time.time(),
            data={
                'burst':    self._burst_n,
                'sw_errors': sw_errors,
                'inverted': inverted,
                **info,
            },
            confidence=max(0.3, 1.0 - sw_errors * 0.12),
            metadata={'type': 'tetra_ndb'},
        )


# ── Pure functions ────────────────────────────────────────────────────────────

def _demodulate(iq: np.ndarray, sps_i: int) -> Optional[np.ndarray]:
    """
    π/4-DQPSK differential demodulator.

    For each symbol k, computes iq[k·sps_i] × conj(iq[(k-1)·sps_i]) and
    maps the resulting angle to the nearest constellation point.  Multiplying
    samples sps_i apart (not consecutive samples) cancels constant carrier
    frequency offset, making the demodulator tolerant to ≈ ±(symbol_rate/4)
    Hz of residual tuning error.

    When the caller prepends the last sps_i samples from the previous chunk,
    the differential for the very first symbol of the new data is computed
    correctly with no cross-chunk symbol loss.
    """
    if sps_i < 1 or len(iq) <= sps_i:
        return None

    # Symbol-aligned sample indices.  When a tail from the previous chunk is
    # prepended, indices[0] = sps_i falls inside the new data so the diff
    # iq[sps_i] × conj(iq[0]) crosses the chunk boundary correctly.
    indices = np.arange(sps_i, len(iq), sps_i)
    if len(indices) == 0:
        return None

    curr  = iq[indices].astype(np.complex64)
    prev  = iq[indices - sps_i].astype(np.complex64)
    phase = np.angle(curr * np.conj(prev)).astype(np.float32)

    n_syms = len(phase)
    bits   = np.empty(n_syms * 2, dtype=np.int8)
    for i, p in enumerate(phase):
        b0, b1 = _pi4_decode(float(p))
        bits[2 * i]     = b0
        bits[2 * i + 1] = b1

    return bits


def _pi4_decode(phase: float) -> Tuple[int, int]:
    """Nearest-neighbour π/4-DQPSK decision: angle → (I-bit, Q-bit)."""
    best_d, best_b0, best_b1 = 999.0, 0, 0
    for ref, b0, b1 in _PI4_MAP:
        d = abs(phase - ref)
        if d > np.pi:
            d = 2.0 * np.pi - d
        if d < best_d:
            best_d, best_b0, best_b1 = d, b0, b1
    return best_b0, best_b1


def _find_sw(buf: np.ndarray, max_errors: int = 6) -> Tuple[Optional[int], bool]:
    """
    Locate the first NDB sync word in *buf* using sliding cross-correlation.

    Both polarities are checked (π/4-DQPSK can produce inverted chips if
    the receiver's initial phase reference is wrong).

    Returns (chip_index, inverted) or (None, False).
    """
    if len(buf) < _SW_LEN:
        return None, False

    bipolar = (2 * buf.astype(np.float32) - 1)
    corr    = np.correlate(bipolar, _SW_BIPOLAR, mode='valid')
    thr     = float(_SW_LEN - 2 * max_errors)

    pos_hits = np.where(corr >= thr)[0]
    neg_hits = np.where(corr <= -thr)[0]

    has_pos = len(pos_hits) > 0
    has_neg = len(neg_hits) > 0

    if not has_pos and not has_neg:
        return None, False
    if has_pos and has_neg:
        fp, fn = int(pos_hits[0]), int(neg_hits[0])
        return (fp, False) if fp <= fn else (fn, True)
    if has_pos:
        return int(pos_hits[0]), False
    return int(neg_hits[0]), True


def _parse_bb(bb: np.ndarray) -> dict:
    """
    Parse the 14-chip Broadcast Block (ETSI EN 300 392-2 §21.5.3).

    On BCCH timeslots the BB carries the first 14 bits of the SYNC PDU:
      chips [0:4]   → system code  (0b0001 = TETRA Release 1)
      chips [4:10]  → colour code  (0–63, identifies the cell)
      chips [10:12] → timeslot number (0–3)
      chips [12:14] → frame number MSBs (bits [4:3] of the 5-bit field)

    On traffic-channel timeslots the BB content is unrelated; the system_code
    field will not equal 1 and the other fields are not decoded.
    """
    if len(bb) < 14:
        return {}
    sys_code = _bits_to_int(bb[:4])
    out: dict = {'system_code': sys_code}
    if sys_code == 1:   # TETRA Release 1 marker
        out['colour_code'] = _bits_to_int(bb[4:10])
        out['timeslot']    = _bits_to_int(bb[10:12])
    return out


def _bits_to_int(bits: np.ndarray) -> int:
    """Big-endian (MSB-first) bit array → integer."""
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v
