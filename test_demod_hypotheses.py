"""
Comprehensive BSCH diagnostic:
1. Test dibit-swap hypothesis (is b0/b1 ordering wrong?)
2. Decode Viterbi output even when CRC fails — what MCC/MNC/CC do the bits suggest?
3. Try phase-inversion hypothesis (all bits flipped)
4. Try both dibit orderings × both polarities = 4 variants

Run from ASDR root: python test_demod_hypotheses.py
"""
import sys
import numpy as np
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from app.decoders.tetra.lower_mac import (
    _K, _CONV_NEXT, _G, _parity,
    _BSCH_PUNCT_MASK, BSCH_SEED, _CRC_POLY, _GOOD_CRC,
    scramble, deinterleave, depuncture, viterbi_decode, crc16,
)

# ── Raw SB1 hex strings observed from logs ─────────────────────────────────────
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

def hex_to_bits(s):
    s = s.strip()
    val = int(s, 16)
    n = len(s) * 4
    bits = [(val >> (n - 1 - i)) & 1 for i in range(n)]
    return np.array(bits[:120], dtype=np.uint8)

def decode_pipeline(bits120, seed=BSCH_SEED):
    """Full decode pipeline. Returns (decoded_60_bits, crc_ok, vit_errs)."""
    b = scramble(bits120[:120].copy(), seed)
    b = deinterleave(b, 120, 11)
    dep = depuncture(b, _BSCH_PUNCT_MASK)
    # Min metric (estimated errors)
    INF = 10**7
    metric = np.full(16, INF, dtype=np.int64)
    metric[0] = 0
    n_steps = len(dep) // 4
    for t in range(n_steps):
        obs = dep[t*4:(t+1)*4]
        new_m = np.full(16, INF, dtype=np.int64)
        for ps in range(16):
            if metric[ps] == INF: continue
            for inp in range(2):
                ns = int(_CONV_NEXT[ps, inp])
                reg = (inp << (_K-1)) | ps
                out = [_parity(reg & g) for g in _G]
                bm = sum(int(obs[gi] != out[gi]) for gi in range(4) if obs[gi] != 2)
                total = metric[ps] + bm
                if total < new_m[ns]:
                    new_m[ns] = total
        metric = new_m
    vit_errs = int(np.min(metric))
    decoded = viterbi_decode(dep)
    if len(decoded) < 60:
        return None, False, vit_errs
    crc_val = crc16(decoded[:60])
    return decoded[:60], crc_val == _GOOD_CRC, vit_errs

def parse_payload(bits44):
    """Parse BSCH payload from 44 bits: MCC(10)+MNC(14)+CC(6)+res(2)+TN(2)+FN(5)+MN(5)."""
    mcc = int(''.join(str(b) for b in bits44[0:10]), 2)
    mnc = int(''.join(str(b) for b in bits44[10:24]), 2)
    cc  = int(''.join(str(b) for b in bits44[24:30]), 2)
    tn  = int(''.join(str(b) for b in bits44[32:34]), 2)
    fn  = int(''.join(str(b) for b in bits44[34:39]), 2)
    mn  = int(''.join(str(b) for b in bits44[39:44]), 2)
    return mcc, mnc, cc, tn, fn, mn

def swap_dibits(bits):
    """Swap bits within each dibit pair: (b0,b1,b2,b3,...) -> (b1,b0,b3,b2,...)"""
    out = bits.copy()
    for i in range(0, len(out) - 1, 2):
        out[i], out[i+1] = out[i+1], out[i]
    return out

print("=" * 80)
print("Testing 4 demodulation hypotheses on 13 observed SB1 frames")
print("  A = original bits, seed=3")
print("  B = dibit-swapped bits, seed=3")
print("  C = inverted bits, seed=3")
print("  D = dibit-swapped + inverted, seed=3")
print("=" * 80)
print()

results = {k: {'vit_errs': [], 'crc_pass': 0, 'decoded': []} for k in 'ABCD'}

for hex_str in observed:
    orig = hex_to_bits(hex_str)
    variants = {
        'A': orig,
        'B': swap_dibits(orig),
        'C': 1 - orig,
        'D': swap_dibits(1 - orig),
    }
    for vname, bits in variants.items():
        decoded, crc_ok, vit_errs = decode_pipeline(bits)
        results[vname]['vit_errs'].append(vit_errs)
        if crc_ok:
            results[vname]['crc_pass'] += 1
        if decoded is not None:
            results[vname]['decoded'].append(decoded[:44])

print("Summary table: mean_vit_errs / crc_passes (out of 13)")
print("-" * 50)
for vname in 'ABCD':
    ve = results[vname]['vit_errs']
    mean_ve = np.mean(ve)
    crc = results[vname]['crc_pass']
    print(f"  Variant {vname}: mean vit_errs={mean_ve:.1f}/120  CRC passes={crc}/13")
print()

# Show Viterbi-decoded MCC/MNC/CC for the best variant
best = min('ABCD', key=lambda v: np.mean(results[v]['vit_errs']))
print(f"Best variant by vit_errs: {best}")
print(f"Decoded MCC/MNC/CC for each frame (variant {best}):")
for hex_str, decoded in zip(observed, results[best]['decoded']):
    mcc, mnc, cc, tn, fn, mn = parse_payload(decoded)
    print(f"  {hex_str[:20]}...  MCC={mcc} MNC={mnc} CC={cc} TN={tn} FN={fn} MN={mn}")

print()
print("=" * 80)
print("Detailed variant-A vit_errs per frame:")
for hex_str, ve in zip(observed, results['A']['vit_errs']):
    print(f"  {hex_str[:20]}...  vit_errs={ve}")

print()
print("=" * 80)
print("Checking if any decoded payload has self-consistent TN/FN/MN sequence...")
print("(consecutive SYNC bursts should have incrementing MN, same TN)")
print()
for vname in 'ABCD':
    decoded_list = results[vname]['decoded']
    if len(decoded_list) < 2:
        continue
    parsed = [parse_payload(d) for d in decoded_list]
    # Check for monotonic or incrementing MN
    mns = [p[5] for p in parsed]  # mn values
    mcc_vals = [p[0] for p in parsed]
    mnc_vals = [p[1] for p in parsed]
    cc_vals  = [p[2] for p in parsed]
    uniq_mcc = set(mcc_vals)
    uniq_mnc = set(mnc_vals)
    uniq_cc  = set(cc_vals)
    consistent_mcc = len(uniq_mcc) <= 3
    consistent_mnc = len(uniq_mnc) <= 3
    consistent_cc  = len(uniq_cc)  <= 3
    print(f"Variant {vname}: MCC={sorted(uniq_mcc)} MNC={sorted(uniq_mnc)[:5]} CC={sorted(uniq_cc)}")
    print(f"  MN sequence: {mns}")
    if consistent_mcc and consistent_mnc and consistent_cc:
        print(f"  *** Appears consistent! ***")
