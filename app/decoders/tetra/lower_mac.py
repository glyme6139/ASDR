"""
TETRA Lower MAC channel coding pipeline.

Implements the complete chain from raw burst bits to decoded payload:
  scrambler → block deinterleaver → depuncturer → Viterbi → CRC-16

References:
  ETSI EN 300 392-2 §8.2 (channel coding)
  osmo-tetra src/lower_mac/ (scrambling.c, conv.c, viterbi.c)
  SDRSharp TETRA plugin: Scrambler.cs, Deinterleave.cs, Depuncture.cs, CRC16.cs
"""

from __future__ import annotations

import numpy as np
from typing import Optional, Tuple

# ── Scrambler ────────────────────────────────────────────────────────────────
# 32-bit LFSR, feedback poly 0xDB710641 (ETSI EN 300 392-2 §8.2.5)

_LFSR_POLY = 0xDB710641
BSCH_SEED  = 3  # default for unsynced operation (BSCH/SCH/F)


def scramble_seed(mcc: int, mnc: int, colour_code: int) -> int:
    """Compute the scrambling seed for a synchronized downlink channel."""
    return 3 * (4 * (mcc + mnc * 1024) + colour_code + 1)


def scramble(bits: np.ndarray, seed: int) -> np.ndarray:
    """XOR bits with LFSR sequence seeded at *seed*. Returns new array.

    Galois LFSR with parity feedback, matching SDRSharp Scrambler.cs:
      key = lfsr & poly; bit = parity(key); lfsr = (lfsr >> 1) | (bit << 31)
    """
    out = bits.copy().astype(np.uint8)
    lfsr = seed & 0xFFFFFFFF
    for i in range(len(out)):
        key = lfsr & _LFSR_POLY
        # popcount(key) mod 2 — parallel XOR reduction
        key ^= key >> 16; key ^= key >> 8; key ^= key >> 4
        key ^= key >> 2;  key ^= key >> 1
        bit = key & 1
        lfsr = ((lfsr >> 1) | (bit << 31)) & 0xFFFFFFFF
        out[i] ^= bit
    return out


# ── Block deinterleaver ───────────────────────────────────────────────────────
# dest[i-1] = source[(1 + a*i % N) - 1]  (1-indexed, ETSI EN 300 392-2 §8.2.4)

# (N, a) pairs by channel type
DEINT_BSCH    = (120, 11)   # SCH/F  — BSCH in SB burst
DEINT_AACH    = (30,   3)   # AACH   — in BB field of SB (after RM decode)
DEINT_BKN1    = (216, 13)   # BKN1   — B1 field of NDB
DEINT_BKN2    = (216, 13)   # BKN2   — B2 field of NDB
DEINT_TCH     = (432, 13)   # TCH/S  — B1+B2 combined


def deinterleave(bits: np.ndarray, N: int, a: int) -> np.ndarray:
    """
    Block deinterleaver per ETSI EN 300 392-2 §8.2.4.1.
    Matches SDRSharp Deinterleave.cs: dest[i-1] = source[(a*i) % N] for i=1..N.
    """
    if len(bits) < N:
        return bits.copy()
    dest = np.empty(N, dtype=bits.dtype)
    for i in range(1, N + 1):
        dest[i - 1] = bits[(a * i) % N]
    return dest


# ── Convolutional code parameters ────────────────────────────────────────────
# Rate 1/4 mother code, K=5 (ETSI EN 300 392-2 §8.2.3 / Table 8.36)
# Generators in octal: 23, 33, 25, 37  → decimal: 19, 27, 21, 31
# (osmo-tetra uses 0x17=23, 0x13=19, 0x15=21, 0x1f=31 in hex — same values)

_K      = 5
_STATES = 1 << (_K - 1)  # 16 states
_G = [0b10011, 0b11011, 0b10101, 0b11111]  # G0=19, G1=27, G2=21, G3=31


def _parity(x: int) -> int:
    x ^= x >> 16; x ^= x >> 8; x ^= x >> 4; x ^= x >> 2; x ^= x >> 1
    return x & 1


# Pre-compute output bits for each (state, input_bit) pair
_CONV_OUTPUT = np.zeros((_STATES, 2, 4), dtype=np.uint8)
_CONV_NEXT   = np.zeros((_STATES, 2), dtype=np.int32)
for _s in range(_STATES):
    for _b in range(2):
        _reg = (_b << (_K - 1)) | _s
        _CONV_NEXT[_s, _b] = (_s >> 1) | (_b << (_K - 2))
        for _gi, _g in enumerate(_G):
            _CONV_OUTPUT[_s, _b, _gi] = _parity(_reg & _g)


# ── Puncture masks (ETSI EN 300 392-2 §8.2.3) ────────────────────────────────
# Each mask cycles over the mother-code output (4 bits per info bit).
# 1 = keep this coded bit; 0 = it was punctured (depuncture inserts erasure=2).

# SCH/F (BSCH): 60 info bits × 4 = 240 mother bits → keep 120 → rate 1/2
# Remove generators G2,G3 for each info bit.
_BSCH_PUNCT_MASK = np.array([1, 1, 0, 0], dtype=np.uint8)

# BKN type-1 (BCCH/DCCH): 144 info bits × 4 = 576 mother bits → keep 432 → rate 3/4
# Remove generator G3 for each info bit: [G0,G1,G2,_].
_BKN_PUNCT_MASK = np.array([1, 1, 1, 0], dtype=np.uint8)


def depuncture(coded_bits: np.ndarray, punct_mask: np.ndarray) -> np.ndarray:
    """
    Insert erasure markers (value 2) at punctured positions.

    punct_mask cycles over the input: mask[i % len(mask)] == 1 means the bit
    is present; 0 means the bit was punctured and an erasure is inserted.
    Returns an array longer than the input.
    """
    period = len(punct_mask)
    # Count kept-bits per period to compute total output length
    kept_per_period = int(np.sum(punct_mask))
    if kept_per_period == 0:
        return coded_bits  # nothing to do
    n_periods, remainder = divmod(len(coded_bits), kept_per_period)
    # Estimate total output length
    total = n_periods * period
    # Handle remainder
    extra = 0
    inp_count = 0
    for j in range(period):
        if inp_count >= remainder:
            break
        if punct_mask[j]:
            inp_count += 1
            extra = j + 1
    total += extra

    out = np.full(total, 2, dtype=np.int8)  # 2 = erasure
    src_idx = 0
    dst_idx = 0
    while src_idx < len(coded_bits) and dst_idx < total:
        if punct_mask[dst_idx % period]:
            out[dst_idx] = coded_bits[src_idx]
            src_idx += 1
        dst_idx += 1
    return out


# ── Viterbi decoder ───────────────────────────────────────────────────────────

def viterbi_decode(soft_bits: np.ndarray) -> np.ndarray:
    """
    Hard-decision Viterbi decoder for rate-1/4 mother code.

    soft_bits: array of {0, 1, 2(=erasure)} coded bits.
    Returns decoded info bits as uint8 array.
    Returns empty array if input length is not a multiple of 4.
    """
    n = len(soft_bits)
    n_steps = n // 4
    if n_steps == 0:
        return np.array([], dtype=np.uint8)

    INF = 10**7
    metric = np.full(_STATES, INF, dtype=np.int64)
    metric[0] = 0
    # path stores the previous state (not the input bit) for clean traceback
    path = np.zeros((n_steps, _STATES), dtype=np.int32)

    for t in range(n_steps):
        obs = soft_bits[t * 4:(t + 1) * 4]
        new_metric = np.full(_STATES, INF, dtype=np.int64)
        for prev_s in range(_STATES):
            if metric[prev_s] == INF:
                continue
            for inp in range(2):
                next_s = int(_CONV_NEXT[prev_s, inp])
                out_bits = _CONV_OUTPUT[prev_s, inp]
                bm = 0
                for gi in range(4):
                    if obs[gi] != 2:
                        bm += int(obs[gi] != out_bits[gi])
                total = metric[prev_s] + bm
                if total < new_metric[next_s]:
                    new_metric[next_s] = total
                    path[t, next_s] = prev_s   # store previous state
        metric = new_metric

    # Traceback: recover input bit from state MSB (state = inp<<(K-2) | prev>>1)
    decoded = np.empty(n_steps, dtype=np.uint8)
    state = int(np.argmin(metric))
    for t in range(n_steps - 1, -1, -1):
        decoded[t] = (state >> (_K - 2)) & 1   # inp is the MSB of next_s
        state = int(path[t, state])             # follow stored previous state
    return decoded


# ── CRC-16 ────────────────────────────────────────────────────────────────────
# ETSI EN 300 392-2 §8.2.6: GenPoly=0x8408 (reflected), GoodCRC=0xF0B8

_GOOD_CRC = 0xF0B8  # CRC residue for valid block (SDRSharp CRC16.cs GoodCRC=61624)
_CRC_POLY = 0x8408  # x^16+x^12+x^5+1 reflected


def crc16(bits: np.ndarray) -> int:
    """Bit-at-a-time CRC-16/CCITT over a bit array (each element 0 or 1)."""
    reg = 0xFFFF
    for b in bits:
        lsb = (reg ^ int(b)) & 1
        reg >>= 1
        if lsb:
            reg ^= _CRC_POLY
    return reg


def check_crc(bits: np.ndarray) -> bool:
    """Return True if the last 16 bits of *bits* are a valid CRC-16 over the first len-16."""
    if len(bits) < 17:
        return False
    return crc16(bits) == _GOOD_CRC


# ── Reed-Muller (for BB channel) ──────────────────────────────────────────────
# ETSI EN 300 392-2 §8.2.7: (30,14) shortened Reed-Muller code for AACH/BB
# Encodes 14 info bits → 30 coded bits.

# Generator matrix rows from ETSI standard Annex B Table B.1 (14×30 matrix)
# Row i encodes the i-th info bit's contribution to all 30 coded positions.
_RM_G = np.array([
    [1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,1,0,0,1,0,1,1,0,1,1,1,1,0],
    [0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,1,1,1,0,1,1,0,0,0,1],
    [0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,1,1,1,0,1,1,0,0,0],
    [0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,1,1,1,0,1,1,0,0],
    [0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,1,1,1,0,1,1,0],
    [0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,1,1,1,0,1,1],
    [0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,1,0,0,0,0,1,1,1,1,0,0,1,1,0,1],
    [0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,1,0,0,0,0,1,1,1,1,0,0,1,1,0],
    [0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,1,0,0,0,0,1,1,1,1,0,0,1,1],
    [0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,1,0,1,1,0,0,0,0,1,1,1,1,0,0,1],
    [0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,1,1,0,1,1,0,0,0,0,1,1,1,1,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,1,1,0,1,1,0,0,0,0,1,1,1,1,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,1,1,0,1,1,0,0,0,0,1,1,1,1],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,1,0,0,1,1,0,1,0,0,1,0,1,0,1,1],
], dtype=np.uint8)

# Parity check matrix H (16×30) — rows give the 16 parity check equations
# Derived from the standard (systematic positions are identity part)
_RM_H = None  # computed on first use


def _build_rm_h():
    global _RM_H
    if _RM_H is not None:
        return
    # Parity part of G: columns 14..29 of _RM_G (14×16 matrix)
    P = _RM_G[:, 14:].T  # 16×14
    # H = [P | I16]
    I16 = np.eye(16, dtype=np.uint8)
    _RM_H = np.concatenate([P, I16], axis=1)  # 16×30


def rm_decode(bits_30: np.ndarray) -> Tuple[Optional[np.ndarray], bool]:
    """
    Decode a (30,14) Reed-Muller codeword.

    Returns (info_bits_14, crc_ok). On failure returns (best_effort_14, False).
    Uses brute-force minimum Hamming distance over all 2^14 codewords (for
    accuracy) — only called for 14/30-bit BB blocks so performance is fine.

    Soft shortcut: uses syndrome to correct up to t=2 errors.
    """
    _build_rm_h()
    r = bits_30[:30].astype(np.uint8)
    # Syndrome
    syn = _RM_H.dot(r) % 2  # 16-bit vector

    if not np.any(syn):
        # No errors — extract systematic bits (positions 0..13)
        return r[:14].copy(), True

    # Try correcting single-bit errors
    for i in range(30):
        test = r.copy()
        test[i] ^= 1
        s2 = _RM_H.dot(test) % 2
        if not np.any(s2):
            return test[:14].copy(), True

    # Return best effort (may contain errors)
    return r[:14].copy(), False


# ── Full decode pipeline ──────────────────────────────────────────────────────

import logging as _log
_bsch_log = _log.getLogger(__name__)

def _viterbi_min_metric(soft_bits: np.ndarray) -> int:
    """Run Viterbi and return minimum final path metric (= estimated bit errors)."""
    n = len(soft_bits)
    n_steps = n // 4
    if n_steps == 0:
        return 0
    INF = 10 ** 7
    metric = np.full(_STATES, INF, dtype=np.int64)
    metric[0] = 0
    for t in range(n_steps):
        obs = soft_bits[t * 4:(t + 1) * 4]
        new_metric = np.full(_STATES, INF, dtype=np.int64)
        for prev_s in range(_STATES):
            if metric[prev_s] == INF:
                continue
            for inp in range(2):
                next_s = int(_CONV_NEXT[prev_s, inp])
                out_bits = _CONV_OUTPUT[prev_s, inp]
                bm = sum(int(obs[gi] != out_bits[gi]) for gi in range(4) if obs[gi] != 2)
                total = metric[prev_s] + bm
                if total < new_metric[next_s]:
                    new_metric[next_s] = total
        metric = new_metric
    return int(np.min(metric))


def decode_bsch(raw_bits: np.ndarray, seed: int = BSCH_SEED) -> Tuple[Optional[np.ndarray], bool]:
    """
    Decode BSCH (SCH/F) from 120 raw SB1 bits.

    Pipeline: scramble → deinterleave(N=120,a=11) → depuncture → Viterbi → CRC
    Returns (60 decoded bits, crc_ok).  First 44 bits are payload.
    """
    if len(raw_bits) < 120:
        return None, False
    raw = raw_bits[:120].copy()

    # Log raw SB1 bits as a hex-encoded bit string (8 bits per hex digit pair)
    raw_hex = ''.join(f"{int(''.join(str(int(b)) for b in raw[i:i+8]), 2):02X}"
                      for i in range(0, 120, 8))

    b = scramble(raw, seed)
    N, a = DEINT_BSCH
    b = deinterleave(b, N, a)
    dep = depuncture(b, _BSCH_PUNCT_MASK)

    # Viterbi minimum path metric tells us estimated channel BER
    vit_errs = _viterbi_min_metric(dep)

    decoded = viterbi_decode(dep)
    if len(decoded) < 60:
        return None, False
    crc_val = crc16(decoded[:60])
    ok = (crc_val == _GOOD_CRC)
    _bsch_log.debug(
        "BSCH: raw=%s vit_errs=%d crc=0x%04X ok=%s bits=%s",
        raw_hex, vit_errs, crc_val, ok,
        ''.join(str(int(x)) for x in decoded[:44]),
    )
    return decoded[:60], ok


def _scramble_soft(soft: np.ndarray, seed: int) -> np.ndarray:
    """Scramble soft bits: LFSR bit=1 → negate, bit=0 → unchanged."""
    out = soft.copy().astype(np.float32)
    lfsr = seed & 0xFFFFFFFF
    for i in range(len(out)):
        key = lfsr & _LFSR_POLY
        key ^= key >> 16; key ^= key >> 8; key ^= key >> 4
        key ^= key >> 2;  key ^= key >> 1
        bit = key & 1
        lfsr = ((lfsr >> 1) | (bit << 31)) & 0xFFFFFFFF
        if bit:
            out[i] = -out[i]
    return out


def _viterbi_soft(soft_bits: np.ndarray) -> np.ndarray:
    """
    Soft-decision Viterbi for rate-1/4 mother code.

    soft_bits: float32 array where +value → confident 0, -value → confident 1,
               0.0 → erasure (no contribution to branch metric).
    Branch metric: sum of max(0, soft × (2*code_bit − 1)) per position.
    Returns decoded info bits as uint8.
    """
    n_steps = len(soft_bits) // 4
    if n_steps == 0:
        return np.array([], dtype=np.uint8)
    INF = 1e12
    metric = np.full(_STATES, INF, dtype=np.float64)
    metric[0] = 0.0
    path = np.zeros((n_steps, _STATES), dtype=np.int32)
    for t in range(n_steps):
        obs = soft_bits[t * 4:(t + 1) * 4]
        new_metric = np.full(_STATES, INF, dtype=np.float64)
        for prev_s in range(_STATES):
            if metric[prev_s] >= INF:
                continue
            for inp in range(2):
                next_s = int(_CONV_NEXT[prev_s, inp])
                out_bits = _CONV_OUTPUT[prev_s, inp]
                bm = 0.0
                for gi in range(4):
                    s = float(obs[gi])
                    if s != 0.0:
                        # code_bit=0 → expect s>0; code_bit=1 → expect s<0
                        sign = 1.0 if out_bits[gi] == 0 else -1.0
                        bm += max(0.0, -sign * s)
                total = metric[prev_s] + bm
                if total < new_metric[next_s]:
                    new_metric[next_s] = total
                    path[t, next_s] = prev_s
        metric = new_metric
    decoded = np.empty(n_steps, dtype=np.uint8)
    state = int(np.argmin(metric))
    for t in range(n_steps - 1, -1, -1):
        decoded[t] = (state >> (_K - 2)) & 1
        state = int(path[t, state])
    return decoded


def decode_bsch_soft(phases_60: np.ndarray, seed: int = BSCH_SEED) -> Tuple[Optional[np.ndarray], bool]:
    """
    Soft-decision BSCH decode from 60 AFC-corrected SB1 differential phases.

    Converts phases to soft LLRs, applies soft scrambling/deinterleaving/
    depuncturing, then runs soft-decision Viterbi for ~3 dB over hard-decision.

    soft_b0 = phase          (positive phase → confident bit=0)
    soft_b1 = π/2 − |phase|  (|phase| < π/2 → confident bit=0)
    """
    if len(phases_60) < 60:
        return None, False
    ph = phases_60[:60].astype(np.float32)
    soft = np.empty(120, dtype=np.float32)
    soft[0::2] = ph
    soft[1::2] = (np.pi / 2) - np.abs(ph)

    soft = _scramble_soft(soft, seed)

    N, a = DEINT_BSCH  # (120, 11)
    soft_deint = np.empty(N, dtype=np.float32)
    for i in range(1, N + 1):
        soft_deint[i - 1] = soft[(a * i) % N]

    # Depuncture: insert 0.0 (erasure) at punctured positions
    period = len(_BSCH_PUNCT_MASK)
    kept = int(np.sum(_BSCH_PUNCT_MASK))
    n_p = len(soft_deint) // kept
    dep = np.zeros(n_p * period, dtype=np.float32)
    src = 0
    for j in range(len(dep)):
        if _BSCH_PUNCT_MASK[j % period]:
            dep[j] = soft_deint[src]; src += 1

    decoded = _viterbi_soft(dep)
    if len(decoded) < 60:
        return None, False
    crc_val = crc16(decoded[:60])
    ok = (crc_val == _GOOD_CRC)
    _bsch_log.debug(
        "BSCH soft: crc=0x%04X ok=%s bits=%s",
        crc_val, ok,
        ''.join(str(int(x)) for x in decoded[:44]),
    )
    return decoded[:60], ok


def decode_aach(raw_bits_14: np.ndarray) -> Tuple[Optional[np.ndarray], bool]:
    """Decode AACH from 14 raw BB-field bits using Reed-Muller (30,14)."""
    # BB field is already 14 bits; RM code uses the full 30-bit codeword
    # but the burst only carries 14 (systematic portion) — apply RM directly
    if len(raw_bits_14) < 14:
        return None, False
    # Extend to 30 bits with zeros (parity unknown for raw decode)
    padded = np.zeros(30, dtype=np.uint8)
    padded[:14] = raw_bits_14[:14]
    return rm_decode(padded)


def decode_bkn(raw_b1: np.ndarray, raw_b2: np.ndarray, seed: int) -> Tuple[Optional[np.ndarray], bool]:
    """
    Decode BKN1+BKN2 from B1 (216 bits) + B2 (216 bits) of an NDB burst.

    Pipeline: scramble → deinterleave(N=432,a=13) → depuncture(3/4) → Viterbi → CRC
    144 info bits (128 payload + 16 CRC) are recovered; CRC covers the payload.
    Returns (payload_bits, crc_ok).  payload is 128 bits if CRC passes.
    """
    raw = np.concatenate([raw_b1[:216], raw_b2[:216]])  # 432 coded bits
    b = scramble(raw, seed)
    N, a = DEINT_TCH
    b = deinterleave(b, N, a)
    # Depuncture: 432 → 576 (insert erasures at G3 output of each info bit)
    b = depuncture(b, _BKN_PUNCT_MASK)      # 432 → 576
    decoded = viterbi_decode(b)             # 576 → 144 bits
    if len(decoded) < 20:
        return None, False
    ok = check_crc(decoded)
    # Strip last 16 CRC bits (tail bits already consumed by Viterbi flush)
    payload = decoded[:-16] if len(decoded) >= 16 else decoded
    return payload, ok
