"""
BSCH pipeline self-test.
Encodes known bits through every transmitter stage, then decodes with our
decoder and verifies bit-for-bit correctness at each step.
Run from the ASDR root: python test_bsch.py
"""

import sys
import numpy as np
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from app.decoders.tetra.lower_mac import (
    _K, _STATES, _G, _CONV_OUTPUT, _CONV_NEXT, _parity,
    _BSCH_PUNCT_MASK, BSCH_SEED, _LFSR_POLY, _GOOD_CRC, _CRC_POLY,
    scramble, deinterleave, depuncture, viterbi_decode, crc16, decode_bsch,
)

PASS = "PASS"
FAIL = "*** FAIL ***"

def ok(cond): return PASS if cond else FAIL


# ── Encoder helpers ────────────────────────────────────────────────────────────

def conv_encode(bits):
    """Rate-1/4 K=5 convolutional encoder."""
    out = []
    state = 0
    for b in bits:
        b = int(b)
        reg = (b << (_K - 1)) | state
        for g in _G:
            out.append(_parity(reg & g))
        state = _CONV_NEXT[state, b]
    return np.array(out, dtype=np.uint8)


def puncture(coded, mask):
    """Keep only bits where mask[i%period]==1."""
    period = len(mask)
    return np.array([coded[i] for i in range(len(coded)) if mask[i % period]], dtype=np.uint8)


def interleave(bits, N, a):
    """Transmitter interleaver — exact inverse of our deinterleaver.
    Deinterleaver: dest[i-1] = src[(a*i)%N] for i=1..N
    Interleaver:   out[(a*i)%N] = src[i-1]  for i=1..N
    """
    out = np.empty(N, dtype=bits.dtype)
    for i in range(1, N + 1):
        out[(a * i) % N] = bits[i - 1]
    return out


# ── CRC helpers ────────────────────────────────────────────────────────────────

def find_fcs(payload_bits):
    """
    Find 16-bit FCS bits (appended after payload) so that
    crc16(payload | fcs_bits) == _GOOD_CRC.
    Tries four common variants.
    """
    results = {}
    R = crc16(payload_bits)

    # Variant A: direct register value, LSB-first
    fcs = np.array([(R >> i) & 1 for i in range(16)], dtype=np.uint8)
    results['direct_lsb'] = (fcs, crc16(np.concatenate([payload_bits, fcs])))

    # Variant B: direct register value, MSB-first
    fcs = np.array([(R >> (15 - i)) & 1 for i in range(16)], dtype=np.uint8)
    results['direct_msb'] = (fcs, crc16(np.concatenate([payload_bits, fcs])))

    # Variant C: one's-complement, LSB-first
    fcs = np.array([(~R >> i) & 1 for i in range(16)], dtype=np.uint8) & 1
    results['compl_lsb'] = (fcs, crc16(np.concatenate([payload_bits, fcs])))

    # Variant D: one's-complement, MSB-first
    fcs = np.array([(~R >> (15 - i)) & 1 for i in range(16)], dtype=np.uint8) & 1
    results['compl_msb'] = (fcs, crc16(np.concatenate([payload_bits, fcs])))

    print(f"  CRC of payload alone: 0x{R:04X}")
    for name, (fcs, residue) in results.items():
        flag = " <-- MATCH" if residue == _GOOD_CRC else ""
        print(f"  {name:14s}: residue=0x{residue:04X}{flag}")

    for name, (fcs, residue) in results.items():
        if residue == _GOOD_CRC:
            return fcs
    return None


# ── Stage-by-stage test (60 arbitrary bits, no CRC needed) ────────────────────

print("=" * 64)
print("Stage 1: round-trip with 60 arbitrary (random seed=42) bits")
print("=" * 64)

rng = np.random.default_rng(42)
info = rng.integers(0, 2, size=60, dtype=np.uint8)
print(f"Original 60b: {''.join(map(str, info))}")

# Encode
coded   = conv_encode(info)
punct   = puncture(coded, _BSCH_PUNCT_MASK)
N, a    = 120, 11
ileaved = interleave(punct, N, a)
scr     = scramble(ileaved, BSCH_SEED)

print(f"Encoded:  len(coded)={len(coded)} len(punct)={len(punct)} len(scr)={len(scr)}")

# Decode
ds  = scramble(scr, BSCH_SEED)
print(f"Descramble  == interleaved: {ok(np.array_equal(ds, ileaved))}")

dei = deinterleave(ds, N, a)
print(f"Deinterleave == punctured:  {ok(np.array_equal(dei, punct))}")

dep = depuncture(dei, _BSCH_PUNCT_MASK)
print(f"Depuncture len={len(dep)} (want 240)")

dec = viterbi_decode(dep)
print(f"Viterbi len={len(dec)} (want 60)")

errs = int(np.sum(dec != info))
print(f"Bit errors vs original:     {errs}  {ok(errs == 0)}")
if errs > 0:
    diff = np.where(dec != info)[0]
    print(f"  Error positions: {diff.tolist()}")
    print(f"  Decoded: {''.join(map(str, dec))}")
    print(f"  Original:{''.join(map(str, info))}")

print()
print("=" * 64)
print("Stage 2: find correct CRC FCS variant")
print("=" * 64)

# Use all-zero payload so CRC is deterministic
payload = np.zeros(44, dtype=np.uint8)
print("Finding FCS for 44-zero payload:")
fcs = find_fcs(payload)
if fcs is not None:
    info60 = np.concatenate([payload, fcs])
    check = crc16(info60)
    print(f"Valid 60-bit frame CRC check: 0x{check:04X}  {ok(check == _GOOD_CRC)}")

    # Now full pipeline test with valid CRC frame
    print()
    print("=" * 64)
    print("Stage 3: full pipeline with valid CRC frame")
    print("=" * 64)

    coded2   = conv_encode(info60)
    punct2   = puncture(coded2, _BSCH_PUNCT_MASK)
    ileaved2 = interleave(punct2, N, a)
    scr2     = scramble(ileaved2, BSCH_SEED)

    result, crc_ok = decode_bsch(scr2, BSCH_SEED)
    print(f"decode_bsch CRC ok: {crc_ok}  {ok(crc_ok)}")
    if result is not None:
        errs2 = int(np.sum(result[:60] != info60))
        print(f"Bit errors in payload: {errs2}  {ok(errs2 == 0)}")
else:
    print("ERROR: no FCS variant gives _GOOD_CRC residue. CRC algorithm mismatch.")
    print("Testing unreflected CRC (poly=0x1021, init=0xFFFF):")

    def crc16_msb(bits):
        reg = 0xFFFF
        for b in bits:
            msb = ((reg >> 15) ^ int(b)) & 1
            reg = (reg << 1) & 0xFFFF
            if msb: reg ^= 0x1021
        return reg

    R2 = crc16_msb(payload)
    print(f"  CRC16-MSB of 44 zeros: 0x{R2:04X}")
    for order, label in [
        (range(15, -1, -1), 'direct_msb'),
        (range(16),         'direct_lsb'),
    ]:
        fcs = np.array([(R2 >> i) & 1 for i in order], dtype=np.uint8)
        res_msb = crc16_msb(np.concatenate([payload, fcs]))
        res_lsb = crc16(np.concatenate([payload, fcs]))
        print(f"  {label}: crc16_msb residue=0x{res_msb:04X}  crc16_lsb residue=0x{res_lsb:04X}")
