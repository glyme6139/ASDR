"""
Verify _Y_BITS (SB sync word) matches the correct TETRA SW.

The TETRA Synchronization Burst SW from ETSI EN 300 392-2 Table 9.31 is 38 bits.
We test by:
1. Comparing _Y_BITS vs the standard SW
2. Building a synthetic SB burst with the standard SW and checking _find_burst detects it
3. Building with _Y_BITS and checking detection
"""
import sys
import numpy as np
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

# ── Import internal constants/functions ───────────────────────────────────────
from app.decoders.tetra.lower_mac import (
    _BSCH_PUNCT_MASK, BSCH_SEED, _GOOD_CRC, _CRC_POLY,
    _K, _CONV_NEXT, _G, _parity,
    scramble, deinterleave, depuncture, viterbi_decode, crc16,
)
from app.decoders.tetra import _Y_BITS, _N_BITS, _P_BITS, _Q_BITS, _X_BITS
from app.decoders.tetra import _find_burst, _SYNC_TRAIN_OFF, _NDB_TRAIN_OFF, _SYNC_MAX_ERRORS as _TRAIN_MAX_ERRORS

# ── Standard TETRA SW from ETSI EN 300 392-2 Table 9.31 (38 bits) ─────────────
# Referenced from gr-tetra/osmo-tetra:
TETRA_SW = np.array([
    1,1,0,1,0,1,1,1, 1,0,1,0,0,0,1,0,
    0,0,1,0,1,0,1,1, 1,0,1,1,0,0,1,1,
    0,0,0,0,0,1
], dtype=np.int8)  # 38 bits

print(f"_Y_BITS length: {len(_Y_BITS)}")
print(f"TETRA_SW length: {len(TETRA_SW)}")
print()
print(f"_Y_BITS: {''.join(str(b) for b in _Y_BITS)}")
print(f"TETRA_SW: {''.join(str(b) for b in TETRA_SW)}")
print()

# Compare first 38 bits
min_len = min(len(_Y_BITS), len(TETRA_SW))
diff = int(np.sum(_Y_BITS[:min_len] != TETRA_SW[:min_len]))
print(f"Hamming distance between _Y_BITS[:38] and TETRA_SW: {diff}/{min_len} ({100*diff/min_len:.0f}%)")
print()

# Also try dibit-swapped SW (in case demodulator swaps bits within each dibit)
SW_swap = TETRA_SW.copy()
for i in range(0, len(SW_swap)-1, 2):
    SW_swap[i], SW_swap[i+1] = SW_swap[i+1], SW_swap[i]
diff_swap = int(np.sum(_Y_BITS[:min_len] != SW_swap[:min_len]))
print(f"Hamming distance between _Y_BITS[:38] and dibit-swapped SW: {diff_swap}/{min_len}")

# Also try inverted SW
SW_inv = 1 - TETRA_SW
diff_inv = int(np.sum(_Y_BITS[:min_len] != SW_inv[:min_len]))
print(f"Hamming distance between _Y_BITS[:38] and inverted SW: {diff_inv}/{min_len}")

# Also try reversed SW
SW_rev = TETRA_SW[::-1]
diff_rev = int(np.sum(_Y_BITS[:min_len] != SW_rev[:min_len]))
print(f"Hamming distance between _Y_BITS[:38] and reversed SW: {diff_rev}/{min_len}")
print()

# ── Build synthetic SB bursts and test detection ───────────────────────────────

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

def interleave(bits, N, a):
    out = np.empty(N, dtype=bits.dtype)
    for i in range(1, N + 1):
        out[(a * i) % N] = bits[i - 1]
    return out

def make_fcs(payload):
    R = crc16(payload)
    return np.array([(~R >> i) & 1 for i in range(16)], dtype=np.uint8)

def make_sb1(mcc=208, mnc=34, cc=43, tn=0, fn=0, mn=0, seed=BSCH_SEED):
    """Encode a valid 120-bit SB1 field."""
    bits = []
    for i in range(9, -1, -1):  bits.append((mcc >> i) & 1)
    for i in range(13, -1, -1): bits.append((mnc >> i) & 1)
    for i in range(5, -1, -1):  bits.append((cc >> i) & 1)
    bits += [0, 0]
    for i in range(1, -1, -1):  bits.append((tn >> i) & 1)
    for i in range(4, -1, -1):  bits.append((fn >> i) & 1)
    for i in range(4, -1, -1):  bits.append((mn >> i) & 1)
    payload = np.array(bits[:44], dtype=np.uint8)
    fcs = make_fcs(payload)
    info = np.concatenate([payload, fcs])
    coded = conv_encode(info)
    punct = puncture(coded, _BSCH_PUNCT_MASK)
    ileaved = interleave(punct, 120, 11)
    return scramble(ileaved, seed)

def make_sb_burst(sw, sb1=None):
    """Build a 510-bit SB burst with the given sync word (38 or 40 bits)."""
    rng = np.random.default_rng(99)
    burst = np.zeros(510, dtype=np.int8)
    # tail(2): zeros
    # SB1(120):
    if sb1 is None:
        sb1 = make_sb1()
    burst[2:122] = sb1
    # SW at offset 122:
    sw_arr = np.array(sw, dtype=np.int8)
    burst[122:122+len(sw_arr)] = sw_arr
    # BBK(30): random
    burst[160:190] = rng.integers(0, 2, 30, dtype=np.int8)
    # SB2(216): random
    burst[190:406] = rng.integers(0, 2, 216, dtype=np.int8)
    # tail(2): zeros
    # FS2(102): random
    burst[408:510] = rng.integers(0, 2, 102, dtype=np.int8)
    return burst

print("=" * 70)
print("Testing burst detection on synthetic SB bursts")
print("=" * 70)

# Test 1: burst with TETRA_SW (correct standard SW)
burst_real = make_sb_burst(TETRA_SW)
hit = _find_burst(burst_real)
print(f"\nBurst with TETRA_SW (38b):     hit={hit}")

# Test 2: burst with _Y_BITS (current code)
burst_y = make_sb_burst(_Y_BITS)
hit2 = _find_burst(burst_y)
print(f"Burst with _Y_BITS (40b):       hit={hit2}")

# Test 3: burst with dibit-swapped TETRA_SW
burst_swap = make_sb_burst(SW_swap)
hit3 = _find_burst(burst_swap)
print(f"Burst with dibit-swapped SW:    hit={hit3}")

# Test 4: burst with inverted SW
burst_inv = make_sb_burst(SW_inv)
hit4 = _find_burst(burst_inv)
print(f"Burst with inverted SW:         hit={hit4}")

# Test 5: burst with reversed SW
burst_rev = make_sb_burst(SW_rev)
hit5 = _find_burst(burst_rev)
print(f"Burst with reversed SW:         hit={hit5}")

print()
print("(Expected: burst with matching SW pattern detected at start=0, kind=SYNC)")
print()

# ── If TETRA_SW is correct, compute what _Y_BITS SHOULD be ────────────────────
print("=" * 70)
print("What _Y_BITS SHOULD look like if TETRA_SW is correct:")
print(f"  38-bit: {list(TETRA_SW)}")
print(f"  Dibit-swapped: {list(SW_swap)}")
print()

# ── Also verify correct BSCH decode on synthetic burst ────────────────────────
from app.decoders.tetra.lower_mac import decode_bsch
sb1_synthetic = make_sb1(mcc=208, mnc=34, cc=43, tn=0, fn=0, mn=0)
decoded, crc_ok = decode_bsch(sb1_synthetic, BSCH_SEED)
print(f"Synthetic SB1 decode CRC: {crc_ok} (should be True)")
