"""
Cross-reference observed raw SB1 hex strings against all expected SB1 vectors
for MCC=208, MNC=34, CC=43, all (TN, FN, MN) combinations.

Tells us the actual channel BER and whether our MCC/MNC/CC assumption is right.
"""

import sys
import numpy as np
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from app.decoders.tetra.lower_mac import (
    _K, _CONV_NEXT, _G, _parity,
    _BSCH_PUNCT_MASK, BSCH_SEED, _CRC_POLY,
    scramble, deinterleave,
)

# ── re-import encoder helpers ──────────────────────────────────────────────────

def conv_encode(bits):
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
    period = len(mask)
    return np.array([coded[i] for i in range(len(coded)) if mask[i % period]], dtype=np.uint8)

def interleave_fn(bits, N, a):
    out = np.empty(N, dtype=bits.dtype)
    for i in range(1, N + 1):
        out[(a * i) % N] = bits[i - 1]
    return out

def crc16(bits):
    reg = 0xFFFF
    for b in bits:
        lsb = (reg ^ int(b)) & 1
        reg >>= 1
        if lsb: reg ^= _CRC_POLY
    return reg

def make_fcs(payload):
    R = crc16(payload)
    # Complement, LSB-first (proven correct by synthetic test)
    return np.array([(~R >> i) & 1 for i in range(16)], dtype=np.uint8)

def build_payload(mcc, mnc, cc, tn, fn, mn):
    bits = []
    for i in range(9, -1, -1):   bits.append((mcc >> i) & 1)   # MCC 10b
    for i in range(13, -1, -1):  bits.append((mnc >> i) & 1)   # MNC 14b
    for i in range(5, -1, -1):   bits.append((cc >> i) & 1)    # CC  6b
    bits += [0, 0]                                               # reserved
    for i in range(1, -1, -1):   bits.append((tn >> i) & 1)    # TN  2b
    for i in range(4, -1, -1):   bits.append((fn >> i) & 1)    # FN  5b
    for i in range(4, -1, -1):   bits.append((mn >> i) & 1)    # MN  5b
    return np.array(bits[:44], dtype=np.uint8)

def expected_sb1(mcc, mnc, cc, tn, fn, mn, seed=BSCH_SEED):
    payload = build_payload(mcc, mnc, cc, tn, fn, mn)
    fcs = make_fcs(payload)
    info = np.concatenate([payload, fcs])
    coded = conv_encode(info)
    punct = puncture(coded, _BSCH_PUNCT_MASK)
    ileaved = interleave_fn(punct, 120, 11)
    return scramble(ileaved, seed)

# ── Pre-build expected SB1 table for MCC=208, MNC=34, CC=43 ──────────────────

print("Building expected SB1 table for MCC=208, MNC=34, CC=43 ...")
MCC, MNC, CC = 208, 34, 43
table = {}
for tn in range(4):
    for fn in range(18):
        for mn in range(18):
            key = (tn, fn, mn)
            table[key] = expected_sb1(MCC, MNC, CC, tn, fn, mn)
print(f"  {len(table)} entries built.\n")

# ── Helper: parse hex SB1 string to bit array ─────────────────────────────────

def hex_to_bits(s):
    s = s.strip()
    val = int(s, 16)
    bits = []
    for i in range(len(s) * 4 - 1, -1, -1):
        bits.append((val >> i) & 1)
    return np.array(bits[:120], dtype=np.uint8)

# ── Observed raw SB1 hex strings (from log) ──────────────────────────────────

observed = [
    "ABD83D5AE2A7A438BAAE9CAF820D12",
    "0B9A755AE2B5A0AC228D9B9F0E1890",
    "2BBA75DA62C5AA2DB2B59FCFE60C93",
    "ABFE797BF2B5A42DF2879F8FA61091",
    "ABAC75127A95A04C3ABF1DBFA60190",
    "3BF875DA60F5A08DE6859C2FAA0D13",
    "F61D56B8B16A837DAF6618EF421910",
    "C39C79DB72B5A40C368F98AF020452",
    "9BDE7F982AB5A63C36971BBF1A1510",
    "3BBC309BEA12A0ACEAA8189ECA085B",
    "3BFC7151EAA4A2EC6A9C19BF2E0492",
    "1BDC71C30AD4A86C7AA01DFFC61091",
    "ABB8799F62A4A42C7E86998F220912",
]

# ── Find best match for each observed frame ───────────────────────────────────

print(f"{'Raw SB1 (hex)':<32}  {'Best (TN,FN,MN)':<16}  {'MinDist':>7}  {'BER%':>6}")
print("-" * 70)

all_min_dists = []
for hex_str in observed:
    obs_bits = hex_to_bits(hex_str)
    if len(obs_bits) < 120:
        continue
    best_dist = 999
    best_key = None
    for key, exp_bits in table.items():
        d = int(np.sum(obs_bits != exp_bits))
        if d < best_dist:
            best_dist = d
            best_key = key
    ber = 100.0 * best_dist / 120
    all_min_dists.append(best_dist)
    print(f"{hex_str:<32}  {str(best_key):<16}  {best_dist:>7}  {ber:>5.1f}%")

print()
print(f"Min dist range: {min(all_min_dists)} – {max(all_min_dists)}")
print(f"Mean min dist:  {np.mean(all_min_dists):.1f} / 120 bits = {100*np.mean(all_min_dists)/120:.1f}% BER")
print()

# Also try seed=0 (no scrambling) for comparison
print("Trying seed=0 (no scrambling):")
table0 = {}
for tn in range(4):
    for fn in range(18):
        for mn in range(18):
            key = (tn, fn, mn)
            table0[key] = expected_sb1(MCC, MNC, CC, tn, fn, mn, seed=0)

all_min0 = []
for hex_str in observed:
    obs_bits = hex_to_bits(hex_str)
    if len(obs_bits) < 120:
        continue
    best_dist = min(int(np.sum(obs_bits != exp)) for exp in table0.values())
    all_min0.append(best_dist)
print(f"seed=0  mean min dist: {np.mean(all_min0):.1f} = {100*np.mean(all_min0)/120:.1f}% BER")

# Try the actual network seed for comparison
seed_actual = 3 * (4 * (MCC + MNC * 1024) + CC + 1)
print(f"\nTrying actual network seed = 3*(4*(208+34*1024)+43+1) = {seed_actual}:")
tableN = {}
for tn in range(4):
    for fn in range(18):
        for mn in range(18):
            key = (tn, fn, mn)
            tableN[key] = expected_sb1(MCC, MNC, CC, tn, fn, mn, seed=seed_actual)

all_minN = []
for hex_str in observed:
    obs_bits = hex_to_bits(hex_str)
    if len(obs_bits) < 120:
        continue
    best_dist = min(int(np.sum(obs_bits != exp)) for exp in tableN.values())
    all_minN.append(best_dist)
print(f"seed={seed_actual}  mean min dist: {np.mean(all_minN):.1f} = {100*np.mean(all_minN)/120:.1f}% BER")
