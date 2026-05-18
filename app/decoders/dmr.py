"""
DMR (Digital Mobile Radio) decoder — ETSI TS 102 361-1.

Modulation:  4FSK, ±1.944 kHz / ±5.832 kHz deviation (±1 / ±3 symbol levels)
Symbol rate: 4800 sym/s → 9600 bps
Channel BW:  12.5 kHz
Frame:       60 ms (2 × 30 ms time slots, TDMA)

Burst layout (scan for 48-bit SYNC word in the bit stream):
  [...guard...] | B1 (98 bits) | SYNC (48 bits) | B2 (98 bits) | [...guard...]

For data bursts: B1 + B2 = 196 channel bits → BPTC(196,96) → 96 info bits
  → 72-bit Link Control word (source/dest/call-type) + 24-bit RS check

Sync words (ETSI TS 102 361-1 §9.1.7):
  BS Voice: 0x755FD7DF75F7    MS Voice: 0x7F7D5DD57DFD
  BS Data:  0xDFF57D75DF5D    MS Data:  0xD5D7F77FD757

France context: DMR Tier II commercial (MOTOTRBO / Hytera);
  tune VFO to NFM mode, bandwidth 12.5 kHz.
"""

import time
import logging
import numpy as np
from typing import Optional, Tuple
from collections import deque

from .base import BaseDecoder, DecoderResult

logger = logging.getLogger(__name__)

# ── Physical constants ─────────────────────────────────────────────────────────

_SYMBOL_RATE     = 4800       # symbols / second
_MIN_SAMPLE_RATE = 24_000     # 5× oversampling floor
_DMR_BW          = 6_250      # one-sided BW in Hz (→ 12.5 kHz channel)
_BLOCK_LEN       = 98         # bits per payload block (B1 or B2)
_SYNC_LEN        = 48         # sync word length in bits
_MAX_SYNC_ERRORS = 3          # tolerated bit errors in sync match
_TAIL_LEN        = 30         # IQ samples kept as overlap between chunks
_MIN_PERIOD      = 3.0        # samples/symbol floor before FFT upsample
_N_TIMING        = 8          # symbol timing hypotheses tried per chunk

_BURST_SPAN = _BLOCK_LEN + _SYNC_LEN + _BLOCK_LEN   # 244 bits
_MAX_BITS   = _BURST_SPAN * 60                        # buffer cap


# ── Sync word definitions ──────────────────────────────────────────────────────

def _bits48(v: int) -> np.ndarray:
    return np.array([(v >> (47 - i)) & 1 for i in range(48)], dtype=np.uint8)

_SW_BS_VOICE = _bits48(0x755FD7DF75F7)
_SW_BS_DATA  = _bits48(0xDFF57D75DF5D)
_SW_MS_VOICE = _bits48(0x7F7D5DD57DFD)
_SW_MS_DATA  = _bits48(0xD5D7F77FD757)
_SW_DIRECT1  = _bits48(0x5D577F7757FF)
_SW_DIRECT2  = _bits48(0xF7FDD5DDFD55)

# (label, bit-array, category)
_SYNC_TABLE = [
    ('BS_VOICE', _SW_BS_VOICE, 'voice'),
    ('BS_DATA',  _SW_BS_DATA,  'data'),
    ('MS_VOICE', _SW_MS_VOICE, 'voice'),
    ('MS_DATA',  _SW_MS_DATA,  'data'),
    ('DIRECT1',  _SW_DIRECT1,  'direct'),
    ('DIRECT2',  _SW_DIRECT2,  'direct'),
]


# ── BPTC(196,96) decoder ───────────────────────────────────────────────────────
# B1[98] + B2[98] = 196 channel bits → 96 info bits
# De-interleave: deint[k] = raw[(k × 181) mod 196]
# Matrix: 13 rows × 15 cols  (195 useful + 1 pad)
# Row FEC: Hamming(15,11,3) corrects single-bit errors per row
# Data: first 8 columns of first 12 rows → 96 info bits

# Hamming(15,11,3) parity equations used by DMR BPTC rows:
#   d[0..10] = data bits, c[11..14] = check bits
#   s0 = d[0]^d[1]^d[3]^d[4]^d[6]^d[8]^d[9] ^ c[11]
#   s1 = d[0]^d[2]^d[3]^d[5]^d[6]^d[9]^d[10] ^ c[12]
#   s2 = d[1]^d[2]^d[3]^d[7]^d[8]^d[9]^d[10] ^ c[13]
#   s3 = d[4]^d[5]^d[6]^d[7]^d[8]^d[9]^d[10] ^ c[14]
# Syndrome index (s3 s2 s1 s0) → error bit position (-1 = no error)
_H1511_SYNDROME = [
    -1, 11, 12,  0, 13,  1,  2,  3,
    14,  4,  5,  6,  7,  8, 10,  9,
]


def _hamming15_correct(row: np.ndarray) -> np.ndarray:
    """Correct up to 1-bit error in a 15-bit Hamming(15,11,3) row (in-place safe)."""
    d = row
    s0 = int(d[0])^int(d[1])^int(d[3])^int(d[4])^int(d[6])^int(d[8])^int(d[9]) ^int(d[11])
    s1 = int(d[0])^int(d[2])^int(d[3])^int(d[5])^int(d[6])^int(d[9])^int(d[10])^int(d[12])
    s2 = int(d[1])^int(d[2])^int(d[3])^int(d[7])^int(d[8])^int(d[9])^int(d[10])^int(d[13])
    s3 = int(d[4])^int(d[5])^int(d[6])^int(d[7])^int(d[8])^int(d[9])^int(d[10])^int(d[14])
    syn = (s3 << 3) | (s2 << 2) | (s1 << 1) | s0
    if syn == 0:
        return row
    pos = _H1511_SYNDROME[syn]
    if pos >= 0:
        row = row.copy()
        row[pos] ^= 1
    return row


def _bptc_decode(b1: np.ndarray, b2: np.ndarray) -> Optional[np.ndarray]:
    """BPTC(196,96): concatenate B1+B2, de-interleave, row-correct, extract 96 bits."""
    raw = np.concatenate([b1[:98], b2[:98]]).astype(np.uint8)
    if len(raw) < 196:
        return None

    # De-interleave: deint[k] = raw[(k * 181) % 196]
    idx   = (np.arange(196, dtype=np.int32) * 181) % 196
    deint = raw[idx]

    # Arrange in 13 × 15 matrix (element 195 is padding, ignored)
    mat = deint[:195].reshape(13, 15).copy()

    # Hamming(15,11) row correction
    for r in range(13):
        mat[r] = _hamming15_correct(mat[r])

    # Extract 96 info bits: cols 0-7 of rows 0-11
    return mat[:12, :8].flatten()


# ── Link Control parser ────────────────────────────────────────────────────────

_FLCO_NAMES = {
    0x00: 'GROUP_VOICE',
    0x03: 'UNIT_VOICE',
    0x08: 'TELE_VOICE',
}


def _u(bits: np.ndarray, start: int, length: int) -> int:
    """Extract unsigned int from bit array, MSB first."""
    return int(sum(int(bits[start + i]) << (length - 1 - i) for i in range(length)))


def _parse_lc(info96: np.ndarray) -> Optional[dict]:
    """Parse 96-bit BPTC payload as a DMR Link Control word."""
    if len(info96) < 72:
        return None
    lc = info96[:72]    # first 72 bits = LC word; bits 72-95 = RS check

    flco     = _u(lc, 0, 6)
    pf       = int(lc[6])
    result   = {'flco': flco, 'flco_name': _FLCO_NAMES.get(flco, f'0x{flco:02X}'), 'pf': pf}

    if flco in (0x00, 0x03, 0x08):   # voice call types with src/dst
        svc_opts = _u(lc, 16, 8)
        dst      = _u(lc, 24, 24)
        src      = _u(lc, 48, 24)
        result.update({
            'fid':       _u(lc, 8, 8),
            'svc_opts':  svc_opts,
            'dst':       dst,
            'src':       src,
            'call_type': 'group' if flco == 0x00 else 'individual',
            'emergency': bool(svc_opts & 0x80),
        })

    return result


# ── DSP helpers ────────────────────────────────────────────────────────────────

def _fft_upsample(iq: np.ndarray, factor: int) -> np.ndarray:
    n  = len(iq)
    F  = np.fft.fft(iq)
    N2 = n * factor
    F2 = np.zeros(N2, dtype=np.complex128)
    h  = n // 2
    F2[:h + 1]        = F[:h + 1]
    F2[N2 - (n-h-1):] = F[h + 1:]
    return np.fft.ifft(F2) * factor


def _narrowband_filter(iq: np.ndarray, sr: float) -> Tuple[np.ndarray, float]:
    """Brick-wall LPF: zero FFT bins outside ±_DMR_BW Hz."""
    if sr <= _DMR_BW * 2.5 or len(iq) < 16:
        return iq, sr
    n    = len(iq)
    keep = max(2, round(n * _DMR_BW / sr))
    F    = np.fft.fft(iq)
    F[keep + 1 : n - keep] = 0
    return np.fft.ifft(F).astype(np.complex64), sr


def _demodulate_4fsk(iq: np.ndarray, sr: float) -> Optional[np.ndarray]:
    """
    FM-demodulate and recover 4FSK symbols from narrow-band IQ.

    Tries _N_TIMING evenly-spaced clock offsets; picks the one with the best
    4-level clustering (minimum mean-squared quantisation error).

    Returns uint8 bit array (MSB-first dibits: +3→01, +1→00, -1→10, -3→11)
    or None if not enough samples.
    """
    period = sr / _SYMBOL_RATE
    n_syms = int((len(iq) - 1) / period)
    if n_syms < 4:
        return None

    if period < _MIN_PERIOD:
        up     = int(np.ceil(_MIN_PERIOD / period))
        iq     = _fft_upsample(iq, up)
        sr     = sr * up
        period = sr / _SYMBOL_RATE
        n_syms = int((len(iq) - 1) / period)
        if n_syms < 4:
            return None

    # FM demodulation: instantaneous phase difference = proportional to frequency
    fm  = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
    lim = len(fm) - 1
    k   = np.arange(1, n_syms + 1, dtype=np.float64)

    # Threshold between ±1 and ±3 levels for a unit-variance 4-level signal:
    #   std({±3,±1}) = sqrt(5) ≈ 2.236  →  boundary = 2/std ≈ 0.894
    TH  = 2.0 / np.sqrt(5.0)

    best_bits  = None
    best_score = -1.0

    for i in range(_N_TIMING):
        tau = i * period / _N_TIMING
        pos = k * period + tau
        pi_ = np.clip(pos.astype(np.int32), 0, lim)
        pf_ = (pos - pi_).astype(np.float32)
        samps = fm[pi_] + pf_ * (fm[np.minimum(pi_ + 1, lim)] - fm[pi_])

        std = float(np.std(samps))
        if std < 1e-8:
            continue
        norm = samps / std

        # Quantise to 4 levels
        q = np.where(norm > TH,  3.0,
            np.where(norm > 0.0, 1.0,
            np.where(norm > -TH,-1.0,-3.0)))

        score = 1.0 / (float(np.mean((norm - q) ** 2)) + 1e-6)
        if score <= best_score:
            continue

        best_score = score
        # +3→01, +1→00, -1→10, -3→11
        msb  = (q < 0).astype(np.uint8)       # 1 for negative levels
        lsb  = (np.abs(q) > 1).astype(np.uint8)  # 1 for ±3 levels
        bits = np.empty(n_syms * 2, dtype=np.uint8)
        bits[0::2] = msb
        bits[1::2] = lsb
        best_bits  = bits

    return best_bits


# ── Sync search ────────────────────────────────────────────────────────────────

def _find_sync(bits: np.ndarray) -> Optional[Tuple[int, str, str, int]]:
    """
    Vectorised scan for the earliest DMR sync word in *bits*.

    Returns (sync_start, sync_name, category, bit_errors) or None.
    """
    limit = len(bits) - _SYNC_LEN + 1
    if limit <= 0:
        return None

    # Sliding windows of 48 bits
    windows = np.lib.stride_tricks.sliding_window_view(bits, _SYNC_LEN)   # (limit, 48)

    best: Optional[Tuple[int, str, str, int]] = None
    for name, word, category in _SYNC_TABLE:
        errs = np.sum(windows != word, axis=1, dtype=np.int32)   # (limit,)
        min_e = int(errs.min())
        if min_e > _MAX_SYNC_ERRORS:
            continue
        pos = int(errs.argmin())
        if best is None or pos < best[0] or (pos == best[0] and min_e < best[3]):
            best = (pos, name, category, min_e)

    return best


# ── Main decoder class ─────────────────────────────────────────────────────────

class DMRDecoder(BaseDecoder):
    """
    DMR 4FSK burst decoder.

    Receives pre-channelised narrow-band IQ from the VFO channeliser (~12.5 kHz).
    FM-demodulates → 4800-baud 4FSK symbol recovery → sync-word search →
    BPTC(196,96) FEC for data bursts → Link Control parsing.
    """

    def __init__(self):
        super().__init__('DMR', sample_rate=_MIN_SAMPLE_RATE)
        self._bits:    np.ndarray             = np.zeros(0, dtype=np.uint8)
        self._tail:    Optional[np.ndarray]   = None
        self._burst_n: int                    = 0
        self._pending: deque                  = deque()

    # ── BaseDecoder interface ──────────────────────────────────────────────────

    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        if not self.is_enabled or data is None or len(data) < 4:
            return None
        if not np.iscomplexobj(data):
            return None

        sr   = float(max(self.sample_rate, _MIN_SAMPLE_RATE))
        data, sr = _narrowband_filter(data, sr)

        iq = np.concatenate([self._tail, data]) if self._tail is not None else data
        self._tail = iq[-_TAIL_LEN:].copy()

        new_bits = _demodulate_4fsk(iq, sr)
        if new_bits is None or len(new_bits) == 0:
            return None

        self._bits = np.concatenate([self._bits, new_bits])
        if len(self._bits) > _MAX_BITS:
            self._bits = self._bits[-_MAX_BITS // 2:]

        self._drain()
        return self._pending.popleft() if self._pending else None

    def reset(self):
        self._bits    = np.zeros(0, dtype=np.uint8)
        self._tail    = None
        self._burst_n = 0
        self._pending.clear()

    def format_result(self, result: DecoderResult) -> str:
        d = result.data if isinstance(result.data, dict) else {}
        parts = [f"#{d.get('burst', '?')}", d.get('sync_word', '')]
        lc = d.get('lc')
        if lc:
            src = lc.get('src')
            dst = lc.get('dst')
            ct  = 'G' if lc.get('call_type') == 'group' else 'U'
            if src is not None and dst is not None:
                parts.append(f"{src}→{dst}({ct})")
            if lc.get('emergency'):
                parts.append('EMERG')
        errs = d.get('sync_errors', 0)
        if errs:
            parts.append(f"err={errs}")
        return ' '.join(p for p in parts if p)

    # ── Burst processing ───────────────────────────────────────────────────────

    def _drain(self):
        buf = self._bits

        while len(buf) >= _BURST_SPAN:
            hit = _find_sync(buf)
            if hit is None:
                keep = _BLOCK_LEN + _SYNC_LEN - 1
                buf  = buf[-keep:] if len(buf) > keep else buf
                break

            sync_start, sw_name, category, errs = hit
            sync_end = sync_start + _SYNC_LEN
            b1_start = sync_start - _BLOCK_LEN
            b2_end   = sync_end   + _BLOCK_LEN

            if b1_start < 0:
                # Not enough leading bits for B1 — skip past this sync
                buf = buf[sync_end:]
                continue

            if b2_end > len(buf):
                # B2 not yet received — keep from b1_start and wait
                buf = buf[b1_start:]
                break

            b1 = buf[b1_start:sync_start]
            b2 = buf[sync_end:b2_end]

            r = self._decode_burst(b1, b2, sw_name, category, errs)
            if r is not None:
                self._pending.append(r)

            buf = buf[b2_end:]

        self._bits = buf

    def _decode_burst(
        self,
        b1: np.ndarray,
        b2: np.ndarray,
        sw_name: str,
        category: str,
        errs: int,
    ) -> Optional[DecoderResult]:
        self._burst_n += 1
        lc_info: Optional[dict] = None

        if category == 'data':
            info = _bptc_decode(b1, b2)
            if info is not None:
                lc_info = _parse_lc(info)

        data: dict = {
            'burst':       self._burst_n,
            'sync_word':   sw_name,
            'category':    category,
            'sync_errors': errs,
            # Timeslot estimation: bursts alternate TS1/TS2 in a TDMA frame.
            # Without CACH decoding this is approximate.
            'timeslot':    1 + (self._burst_n % 2),
        }
        if lc_info:
            data['lc'] = lc_info

        return DecoderResult(
            decoder_name='DMR',
            timestamp=time.time(),
            data=data,
            confidence=max(0.3, 1.0 - errs * 0.15),
            metadata={'type': 'dmr_burst'},
        )
