"""
TETRA (Terrestrial Trunked Radio) decoder — ETSI EN 300 392-2.

Full protocol stack:
  PHY   : π/4-DQPSK demodulation, burst detection (NDB + SB)
  Lower MAC: scrambler, block deinterleaver, depuncturer, Viterbi, CRC-16, RM
  MAC   : SyncPDU, SysInfoPDU, ResourcePDU, CMCE, MLE
  Voice : TCH/S ACELP frames → PCM (via tetraVoiceDec.dll)
"""

from __future__ import annotations

import time
import logging
import numpy as np
from typing import Optional, List
from collections import deque

from ..base import BaseDecoder, DecoderResult
from .network_time import NetworkTime
from .lower_mac import (
    BSCH_SEED, scramble_seed, scramble,
    deinterleave, DEINT_BSCH, DEINT_TCH,
    decode_bsch, decode_bsch_soft, decode_bkn, decode_bkn_block,
    rm_decode, check_crc,
)
from .mac import (
    parse_mac_block, parse_sysinfo,
    CellInfo, CallEvent, SdsMessage, NeighbourCell,
)

logger = logging.getLogger(__name__)

# ── Physical constants ────────────────────────────────────────────────────────

_SYMBOL_RATE     = 18_000
_MIN_SAMPLE_RATE = 36_000
_TETRA_BW        = 25_000   # TETRA channel bandwidth in Hz
_TAIL_LEN        = 24    # IQ samples kept between chunks (~2 symbol periods)

# NDB burst field layout (chips = dibits = 2 bits each after DQPSK)
# ETSI EN 300 392-2 Table 9.33 (0-indexed, 2 bits per symbol)
# [0:2] tail | [2:218] B1 | [218:240] training(22) | [240:256] BB | [256:472] B2 | [472:474] tail | [474:510] guard
_NDB_CHIPS  = 510
_NDB_B1_S   = 2;   _NDB_B1_E   = 218   # 216 bits
_NDB_TRAIN_OFF = 218                    # training sequence starts at bit 218
_NDB_BB_OFF = 240; _NDB_BB_LEN = 16    # stolen channel (BB/AACH) = 16 bits
_NDB_B2_S   = 256; _NDB_B2_E   = 472   # 216 bits

# SB burst field offsets (from actual burst start — ETSI EN 300 392-2 Table 9.34)
# tail(2)+SB1(120)+SW(38)+BBK(30)+SB2(216)+tail(2)+FS2(102)=510
_SB_BLK1_S  = 2;   _SB_BLK1_E  = 122   # 120 bits — BSCH content
_SB_BBK_OFF = 160; _SB_BBK_LEN = 30    # 30 bits — AACH broadcast block
_SB_BLK2_S  = 190; _SB_BLK2_E  = 406  # 216 bits — BNCH/SYSINFO

_SYNC_MAX_ERRORS = 3   # Y-word is 38 bits — at good SNR expect ≤2 errors
_NDB_MAX_ERRORS  = 2   # NDB training is 22 bits — 2 errors = 9% BER ceiling

# Training sequences (22 chips each)
_N_BITS = np.array([1,1,0,1, 0,0,0,0, 1,1,1,0, 1,0,0,1, 1,1,0,1, 0,0], dtype=np.int8)
_P_BITS = np.array([0,1,1,1, 1,0,1,0, 0,1,0,0, 0,0,1,1, 0,1,1,1, 1,0], dtype=np.int8)
_Q_BITS = np.array([1,0,1,1, 0,1,1,1, 0,0,0,0, 0,1,1,0, 1,0,1,1, 0,1], dtype=np.int8)
_X_BITS = np.array([1,0,0,1, 1,1,0,1, 0,0,0,0, 1,1,1,0, 1,0,0,1, 1,1,
                    0,1,0,0, 0,0,1,1], dtype=np.int8)
# ETSI EN 300 392-2 Table 9.31 — 38-chip synchronisation word (SW)
_Y_BITS = np.array([1,1,0,1,0,1,1,1, 1,0,1,0,0,0,1,0,
                    0,0,1,0,1,0,1,1, 1,0,1,1,0,0,1,1,
                    0,0,0,0,0,1], dtype=np.int8)
_SYNC_TRAIN_OFF = 122   # SW at offset 122 from actual SB burst start (symbol 61)

# Expected differential phase for each of the 19 SW symbols.
# Derived from the 19 dibits of _Y_BITS using TETRA π/4-DQPSK mapping:
#   (0,0)→+π/4  (0,1)→+3π/4  (1,0)→-π/4  (1,1)→-3π/4
_Y_EXPECTED_PHASES = np.array([
    -3*np.pi/4, 3*np.pi/4, 3*np.pi/4, -3*np.pi/4, -np.pi/4, -np.pi/4,
    np.pi/4, -np.pi/4, np.pi/4, -np.pi/4, -np.pi/4, -3*np.pi/4,
    -np.pi/4, -3*np.pi/4, np.pi/4, -3*np.pi/4, np.pi/4, np.pi/4, 3*np.pi/4,
], dtype=np.float32)


# ACELP bit reordering tables (osmo-tetra tch_reordering.c, 0-indexed)
_CLASS0 = np.array([
    35,36,37,38,39,40,41,42,43,33,47,48,
    56,61,62,63,65,66,67,68,69,70,74,75,
    83,88,89,90,91,92,93,94,95,96,97,
    101,102,110,115,116,117,118,119,120,121,
    122,123,124,128,129,137,
], dtype=np.int32) - 1
_CLASS1 = np.array([
    58,85,112,54,81,108,135,50,77,104,131,
    45,72,99,126,55,82,109,136,5,13,34,
    8,16,17,22,23,24,25,26,6,14,7,15,
    60,87,114,46,73,100,127,44,71,98,125,
    33,49,76,103,130,59,86,113,57,84,111,
], dtype=np.int32) - 1
_CLASS2 = np.array([
    18,19,20,21,31,32,53,80,107,134,
    1,2,3,4,9,10,11,12,27,28,29,30,
    52,79,106,133,51,78,105,132,
], dtype=np.int32) - 1

_N0, _N1, _N2 = len(_CLASS0), len(_CLASS1), len(_CLASS2)  # 51, 56, 30
_ACELP_BITS   = _N0 + _N1 + _N2                            # 137


def _afc_estimate(y_phases: np.ndarray) -> float:
    """Circular mean of (measured − expected) for the 19 Y sync symbols → per-symbol phase bias."""
    diffs = np.angle(np.exp(1j * (y_phases.astype(np.float64) - _Y_EXPECTED_PHASES.astype(np.float64))))
    return float(np.angle(np.mean(np.exp(1j * diffs))))


class TETRADecoder(BaseDecoder):
    """
    Full-stack TETRA π/4-DQPSK decoder.

    Per NDB/SB burst:
      • Lower MAC: scramble → deinterleave → Viterbi → CRC
      • MAC: SyncPDU / SysInfo / ResourcePDU / CMCE / MLE / SDS
      • Network time: maintained from BSCH, updated every superframe slot
      • Voice: TCH/S B1+B2 → 2 × 137-bit ACELP frames → 480 PCM @ 8 kHz
    """

    def __init__(self):
        super().__init__('TETRA', sample_rate=_MIN_SAMPLE_RATE)
        self._bits:     np.ndarray = np.zeros(0, dtype=np.int8)
        self._phases:   np.ndarray = np.zeros(0, dtype=np.float32)
        self._tail:     Optional[np.ndarray] = None
        self._tail_sps: int = 4
        self._burst_n:  int = 0
        self._pending:  deque = deque()

        self._net: NetworkTime = NetworkTime()
        self._cell: Optional[CellInfo] = None
        self._calls: dict  = {}      # slot → CallEvent
        self._sds:   list  = []      # SdsMessage list (last 50)
        self._neighbours: list = []  # NeighbourCell list

        self._scramble_seed: int = BSCH_SEED
        self._net_synced:    bool = False
        self._soft_last_log: float = 0.0
        self._last_sr:       float = 0.0

    # ── BaseDecoder ──────────────────────────────────────────────────────────

    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        if not self.is_enabled or data is None or len(data) < 4:
            return None
        if not np.iscomplexobj(data):
            return None

        sr = float(self.sample_rate)
        if sr < 12_000:
            logger.warning("TETRA: sample rate %.0f Hz too low (need ≥12 kHz)", sr)
            return None

        if abs(sr - self._last_sr) > 100 and self._last_sr > 0:
            self._bits   = np.zeros(0, dtype=np.int8)
            self._phases = np.zeros(0, dtype=np.float32)
            self._tail   = None
            logger.info("TETRA: sample rate changed %.0f→%.0f Hz, clearing buffers", self._last_sr, sr)
        self._last_sr = sr

        # Remove DC (HackRF LO leakthrough) before filtering
        data = data - np.mean(data)

        # Narrow to TETRA channel bandwidth: 200 kHz → 25 kHz cuts 9 dB of excess noise
        data, sr = _narrowband_filter(data, sr)

        if self._tail is not None:
            iq = np.concatenate([self._tail, data])
        else:
            iq = data
        self._tail = iq[-_TAIL_LEN:].copy()

        result = _demodulate(iq, sr)
        if result is None:
            return None
        new_bits, new_phases = result
        if len(new_bits) == 0:
            return None

        self._bits   = np.concatenate([self._bits,   new_bits])
        self._phases = np.concatenate([self._phases, new_phases])
        self._drain()

        return self._pending.popleft() if self._pending else None

    def set_iq_sample_rate(self, rate: float) -> None:
        """Called by the VFO channeliser with the actual narrowband IQ rate."""
        rate = float(rate)
        if rate > 0 and abs(rate - self.sample_rate) > 100:
            self.sample_rate = int(rate)

    def reset(self):
        self._bits   = np.zeros(0, dtype=np.int8)
        self._phases = np.zeros(0, dtype=np.float32)
        self._tail   = None
        self._burst_n = 0
        self._pending.clear()
        self._net   = NetworkTime()
        self._calls = {}
        self._sds   = []
        self._neighbours = []
        self._scramble_seed = BSCH_SEED
        self._net_synced    = False

    def format_result(self, result: DecoderResult) -> str:
        d = result.data if isinstance(result.data, dict) else {}
        parts = [f"#{d.get('burst','?')}"]
        bt = d.get('burst_type', '')
        if bt:
            parts.append(bt)
        cc = d.get('colour_code')
        if cc is not None:
            parts.append(f"CC={cc}")
        ts = d.get('timeslot')
        if ts is not None:
            parts.append(f"TS={ts}")
        ev = d.get('call_event')
        if ev:
            parts.append(f"[{ev}]")
        sds = d.get('sds_text')
        if sds:
            parts.append(f"SDS:{sds[:30]}")
        err = d.get('sw_errors')
        if err is not None:
            parts.append(f"err={err}")
        if d.get('crc_ok') is False:
            parts.append('CRC!')
        if d.get('pcm_samples'):
            parts.append('[voice]')
        return ' '.join(parts)

    # ── Burst search ──────────────────────────────────────────────────────────

    def _drain(self):
        buf = self._bits
        pha = self._phases
        min_lookahead = max(len(_Y_BITS) + _SYNC_TRAIN_OFF,
                            len(_N_BITS) + _NDB_TRAIN_OFF)
        while len(buf) >= _NDB_CHIPS:

            # Cap buffer to keep soft-correlator matrix multiply O(1) in time.
            if len(pha) > _MAX_PHASE_BUF:
                trim = len(pha) - _MAX_PHASE_BUF
                pha = pha[trim:]
                buf = buf[trim * 2:]

            # ── Primary: soft phase-domain SYNC correlator ──────────────────
            # Scans ±18 kHz residual carrier grid — covers the full range of
            # 4th-power estimation error (±sr/8 ≈ ±9.3 kHz after 3× upsample).
            soft_result = _find_burst_soft(pha)
            if soft_result is not None:
                start_bit, residual_df, soft_score = soft_result
            else:
                soft_score = 0.0
            above_thresh = soft_result is not None and soft_score >= _SOFT_THRESHOLD

            if above_thresh:
                soft = soft_result
            else:
                # Rate-limited diagnostic: show best score every 2 s
                now = time.time()
                if now - self._soft_last_log > 2.0:
                    logger.info(
                        "TETRA soft miss: best score=%.1f/19 df=%.0f Hz buf=%d sym",
                        soft_score,
                        residual_df if soft_result else 0.0,
                        len(pha),
                    )
                    self._soft_last_log = now
                soft = None

            if soft is not None:
                start_bit, residual_df, soft_score = soft
                end = start_bit + _NDB_CHIPS
                if end > len(buf):
                    buf = buf[start_bit:]
                    pha = pha[start_bit // 2:]
                    break

                # Re-derive bits from phases corrected for the found df
                ps = start_bit // 2
                pe = ps + _NDB_CHIPS // 2
                if pe <= len(pha):
                    # Apply residual-df ramp correction then re-decide bits
                    sym_idx  = np.arange(pe - ps, dtype=np.float64)
                    ramp     = sym_idx * (2.0 * np.pi * residual_df / _SYMBOL_RATE)
                    corr_pha = np.angle(
                        np.exp(1j * (pha[ps:pe].astype(np.float64) - ramp))
                    ).astype(np.float32)
                    b0 = (corr_pha < 0).astype(np.int8)
                    b1 = (np.abs(corr_pha) > (np.pi / 2)).astype(np.int8)
                    burst = np.empty(_NDB_CHIPS, dtype=np.int8)
                    burst[0::2] = b0
                    burst[1::2] = b1

                    # Verify sync word quality (hard check on corrected bits)
                    sw_s = _SYNC_TRAIN_OFF
                    sw_e = sw_s + len(_Y_BITS)
                    errs = int(np.sum(burst[sw_s:sw_e] != _Y_BITS))
                    # logger.info(
                    #     "TETRA soft-SYNC: score=%.1f df=%.0f Hz sw_errs=%d",
                    #     soft_score, residual_df, errs,
                    # )

                    r = self._decode_burst(burst, 'SYNC', 'SYNC', errs, corr_pha)
                    if r is not None:
                        self._pending.append(r)
                buf = buf[end:]
                pha = pha[end // 2:]
                continue

            # ── Fallback: hard-decision burst finder (NDB + weak SYNC) ──────
            best_hit = None
            best_k   = 0
            best_buf = buf
            for k in range(4):
                test_buf = buf if k == 0 else _bits_from_phases(pha, k)
                h = _find_burst(test_buf)
                if h is not None:
                    if best_hit is None or h[3] < best_hit[3]:
                        best_hit = h
                        best_k   = k
                        best_buf = test_buf

            hit = best_hit
            if hit is None:
                keep = min_lookahead
                buf = buf[-keep:]      if len(buf) > keep      else buf
                pha = pha[-(keep//2):] if len(pha) > keep // 2 else pha
                break

            start, kind, train, errs = hit
            end = start + _NDB_CHIPS
            if end > len(best_buf):
                buf = buf[start:]
                pha = pha[start // 2:]
                break

            burst = best_buf[start:end].copy()

            ps = start // 2
            pe = (start + _NDB_CHIPS) // 2
            burst_pha = None
            if kind == 'SYNC' and pe <= len(pha):
                raw_slice = pha[ps:pe]
                if best_k == 0:
                    burst_pha = raw_slice.copy()
                else:
                    burst_pha = ((raw_slice - best_k * np.pi / 2 + np.pi)
                                 % (2 * np.pi) - np.pi).astype(np.float32)

            r = self._decode_burst(burst, kind, train, errs, burst_pha)
            if r is not None:
                self._pending.append(r)
            buf = buf[end:]
            pha = pha[end // 2:]
        self._bits   = buf
        self._phases = pha

    def _decode_burst(self, burst, kind, train, errs, phases=None) -> Optional[DecoderResult]:
        max_errs = _SYNC_MAX_ERRORS if kind == 'SYNC' else _NDB_MAX_ERRORS
        if errs > max_errs:
            return None
        now = time.time()
        if hasattr(self, '_last_burst_time'):
            interval_ms = (now - self._last_burst_time) * 1000
            # logger.info("TETRA burst interval: %.0f ms (real SB≈453ms, false-positive=random)", interval_ms)
        self._last_burst_time = now
        self._burst_n += 1
        is_sync = (kind == 'SYNC')
        bb_info: dict = {}
        crc_ok: Optional[bool] = None
        pcm: Optional[np.ndarray] = None
        call_event: Optional[str] = None
        sds_text: Optional[str] = None
        neighbours: Optional[list] = None
        sysinfo_dict: Optional[dict] = None

        if is_sync:
            burst_type = 'SYNC'
            sb1 = burst[_SB_BLK1_S:_SB_BLK1_E]  # 120 bits

            # AFC: estimate per-symbol phase bias from Y sync word, re-decode SB1
            bsch, crc_ok = None, False
            if phases is not None:
                y_s = _SYNC_TRAIN_OFF // 2          # symbol 61
                y_e = y_s + len(_Y_EXPECTED_PHASES) # symbol 80
                sb1_s = _SB_BLK1_S // 2             # symbol 1
                sb1_e = _SB_BLK1_E // 2             # symbol 61
                if y_e <= len(phases) and (sb1_e - sb1_s) == 60:
                    theta = _afc_estimate(phases[y_s:y_e])
                    corrected = np.angle(
                        np.exp(1j * (phases[sb1_s:sb1_e].astype(np.float64) - theta))
                    ).astype(np.float32)

                    b0_tmp = (corrected < 0).astype(np.int8)
                    b1_tmp = (np.abs(corrected) > (np.pi / 2)).astype(np.int8)
                    expected_phase = ((1 - 2 * b0_tmp.astype(np.float32)) *
                                      (np.pi / 4 + b1_tmp.astype(np.float32) * np.pi / 2))
                    phase_err = np.angle(
                        np.exp(1j * (corrected.astype(np.float64) -
                                     expected_phase.astype(np.float64)))
                    )
                    rms_phase_err = float(np.sqrt(np.mean(phase_err ** 2)))
                    logger.info(
                        "TETRA AFC: theta=%.4f rad (%.0f Hz)  "
                        "SB1 phase-rms=%.3f rad (0=perfect, >0.4=low-SNR, "
                        "0.56rad→14%%BER)",
                        theta, theta * _SYMBOL_RATE / (2 * np.pi), rms_phase_err,
                    )

                    b0 = (corrected < 0).astype(np.int8)
                    b1 = (np.abs(corrected) > (np.pi / 2)).astype(np.int8)
                    sb1 = np.empty(120, dtype=np.int8)
                    sb1[0::2] = b0
                    sb1[1::2] = b1

                    # Soft-decision decode using AFC-corrected phases (~3 dB gain over hard)
                    bsch, crc_ok = decode_bsch_soft(corrected, self._scramble_seed)

            # Fall back to hard-decision if soft unavailable or failed
            if not crc_ok:
                bsch, crc_ok = decode_bsch(sb1, self._scramble_seed)
            if crc_ok and bsch is not None:
                self._net.update_from_bsch(bsch)
                self._scramble_seed = scramble_seed(
                    self._net.mcc, self._net.mnc, self._net.cc)
                self._net_synced = True
                logger.info("TETRA SYNC: %s", self._net.cell_id)
            # BBK (30 bits) → AACH decode
            bbk = burst[_SB_BBK_OFF:_SB_BBK_OFF + _SB_BBK_LEN]
            if len(bbk) >= 14:
                aach, _ = rm_decode(bbk[:30] if len(bbk) >= 30 else
                                    np.pad(bbk, (0, 30 - len(bbk))))
                if aach is not None:
                    bb_info['aach'] = int(
                        sum(int(b) << (13 - i) for i, b in enumerate(aach[:14]))
                    )
        else:
            # NDB burst — decode BB field
            bb = burst[_NDB_BB_OFF:_NDB_BB_OFF + _NDB_BB_LEN]
            bb_info = _parse_bb(bb)
            is_bcch  = bb_info.get('system_code') == 1
            burst_type = 'BCCH' if is_bcch else 'TCH/S'

            b1 = burst[_NDB_B1_S:_NDB_B1_E]
            b2 = burst[_NDB_B2_S:_NDB_B2_E]

            if is_bcch:
                # Decode B1 and B2 independently (each is a separate BKN block).
                # B1 is the primary bearer; B2 may carry a second MAC PDU.
                p1, ok1 = decode_bkn_block(b1, self._scramble_seed)
                p2, ok2 = decode_bkn_block(b2, self._scramble_seed)

                if not ok1 and not ok2:
                    return None   # suppress false BCCH noise

                # Use whichever block passed CRC (prefer B1)
                payload = p1 if ok1 else p2
                crc_ok  = ok1 or ok2

                if payload is not None:
                    frame = parse_mac_block(payload, tn=self._net.tn)
                    if frame is not None:
                        bb_info.update(frame.to_dict())
                        p = frame.payload
                        if isinstance(p, CallEvent):
                            call_event = p.event
                            self._calls[frame.slot] = p
                        elif isinstance(p, SdsMessage):
                            call_event = 'sds'
                            sds_text = p.text or p.data_hex[:40]
                            self._sds.append(p)
                            if len(self._sds) > 50:
                                self._sds.pop(0)
                        elif isinstance(p, list):  # NeighbourCell list
                            neighbours = [c.to_dict() for c in p]
                            self._neighbours = p

                # If B2 also passed CRC and carries something different, queue it
                if ok1 and ok2 and p2 is not None:
                    frame2 = parse_mac_block(p2, tn=self._net.tn)
                    if frame2 is not None and frame2.payload is not None:
                        p2obj = frame2.payload
                        if isinstance(p2obj, CallEvent):
                            self._calls[frame2.slot] = p2obj
                        elif isinstance(p2obj, SdsMessage):
                            self._sds.append(p2obj)
                            if len(self._sds) > 50:
                                self._sds.pop(0)
                        elif isinstance(p2obj, list):
                            self._neighbours = p2obj

                if self._net.is_bnch_slot() and self._cell is None:
                    si = parse_sysinfo(payload)
                    if si:
                        si.mcc = self._net.mcc
                        si.mnc = self._net.mnc
                        si.colour_code = self._net.cc
                        self._cell = si
                        sysinfo_dict = si.to_dict()
            else:
                # Traffic channel — only decode voice when synced to a cell
                if not self._net_synced:
                    return None
                pcm = _decode_tch(np.concatenate([b1, b2]))

        net_dict = self._net.to_dict()

        return DecoderResult(
            decoder_name='TETRA',
            timestamp=time.time(),
            data={
                'burst':      self._burst_n,
                'burst_type': burst_type,
                'burst_kind': kind,
                'train_seq':  train,
                'sw_errors':  errs,
                'crc_ok':     crc_ok,
                'inverted':   False,
                'voice_burst': (not is_sync) and (burst_type == 'TCH/S'),
                'pcm_samples': int(len(pcm)) if pcm is not None else 0,
                'pcm':        pcm,
                'call_event': call_event,
                'sds_text':   sds_text,
                'neighbours': neighbours,
                'sysinfo':    sysinfo_dict,
                'active_calls': {str(k): v.to_dict() for k, v in self._calls.items()},
                **bb_info,
                **{f"net_{k}": v for k, v in net_dict.items()},
            },
            confidence=max(0.3, 1.0 - errs * 0.12),
            metadata={'type': 'tetra_burst'},
        )


# ── Voice decoding ────────────────────────────────────────────────────────────

def _decode_tch(bits_432: np.ndarray) -> Optional[np.ndarray]:
    """B1+B2 (432 bits) → 480 PCM int16 @ 8 kHz via ACELP codec."""
    from ..tetra_codec import get_codec, FRAME_BITS
    codec = get_codec()
    if not codec.available:
        return None
    decoded = bits_432[:274].astype(np.int8)
    f0, f1 = _acelp_reorder(decoded)
    pcm0 = codec.decode(f0)
    pcm1 = codec.decode(f1)
    return np.concatenate([pcm0, pcm1])


def _acelp_reorder(bits_274: np.ndarray):
    """Port of osmo-tetra tetra_acelp_type2_to_codec() → two 137-bit frames."""
    out = np.zeros(2 * _ACELP_BITS, dtype=np.int8)
    cur = 0
    for bit in range(_N0):
        for frm in range(2):
            out[frm * _ACELP_BITS + _CLASS0[bit]] = bits_274[cur]; cur += 1
    for bit in range(_N1):
        for frm in range(2):
            out[frm * _ACELP_BITS + _CLASS1[bit]] = bits_274[cur]; cur += 1
    for bit in range(_N2):
        for frm in range(2):
            out[frm * _ACELP_BITS + _CLASS2[bit]] = bits_274[cur]; cur += 1
    return out[:_ACELP_BITS], out[_ACELP_BITS:]


# ── Demodulation ──────────────────────────────────────────────────────────────

_MIN_PERIOD = 3.0   # min samples/symbol before upsampling (Nyquist margin)


def _fft_upsample(iq: np.ndarray, factor: int) -> np.ndarray:
    """Upsample complex IQ by integer factor via FFT zero-padding.

    Equivalent to ideal (sinc) interpolation — correct for any band-limited
    signal.  Much better than linear interpolation when the input sample rate
    is only 1-2× the symbol rate, which aliases the high-frequency spectral
    tails of the RRC pulse into the decision region.
    """
    n  = len(iq)
    F  = np.fft.fft(iq)
    N2 = n * factor
    F2 = np.zeros(N2, dtype=np.complex128)
    h  = n // 2
    F2[:h + 1]          = F[:h + 1]
    F2[N2 - (n-h-1):]   = F[h + 1:]
    return np.fft.ifft(F2) * factor


def _narrowband_filter(iq: np.ndarray, sr: float) -> tuple:
    """FFT brick-wall LPF to TETRA channel bandwidth, keeping sample rate unchanged.

    Zero out FFT bins outside ±(TETRA_BW/2) Hz.  No decimation — _demodulate
    continues to run at the original sr with its existing oversampling ratio.
    Typical gain: 75 kHz VFO → 25 kHz pass-band removes 4.8 dB of OOB noise.
    """
    if sr <= _TETRA_BW * 1.5:
        return iq, sr
    n = len(iq)
    if n < 16:
        return iq, sr
    keep = max(2, round(n * _TETRA_BW / (2.0 * sr)))  # bins to keep each side of DC
    F = np.fft.fft(iq)
    F[keep + 1 : n - keep] = 0
    return np.fft.ifft(F).astype(np.complex64), sr


_N_TIMING = 8   # symbol timing hypotheses tried per chunk (evenly spaced over 1 period)


def _demodulate(iq: np.ndarray, sr: float) -> Optional[tuple]:
    """
    π/4-DQPSK differential demodulator with symbol timing recovery.

    Returns (bits, phases) for the timing hypothesis with maximum mean
    squared amplitude at the symbol sample points (eye-opening criterion —
    carrier-agnostic, robust even when the 4th-power carrier estimate is
    noise-dominated).

    FFT upsamples to ≥ _MIN_PERIOD samples/symbol first; the timing search
    then runs cheaply on the upsampled signal using linear interpolation.
    """
    period = sr / _SYMBOL_RATE          # samples/symbol
    n_syms = int((len(iq) - 1) / period)
    if n_syms < 2:
        return None

    if period < _MIN_PERIOD:
        up     = int(np.ceil(_MIN_PERIOD / period))
        iq     = _fft_upsample(iq, up)
        sr     = sr * up
        period = sr / _SYMBOL_RATE
        n_syms = int((len(iq) - 1) / period)
        if n_syms < 2:
            return None

    # ── Carrier frequency offset correction ──────────────────────────────
    diffs         = iq[1:] * np.conj(iq[:-1])
    bias_per_samp = float(np.angle(np.mean(diffs ** 4))) / 4.0  # rad/sample
    freq_corr     = float(bias_per_samp * period)               # rad/symbol

    # ── Symbol timing search ──────────────────────────────────────────────
    # For RRC-filtered DQPSK, amplitude |iq| is maximised at the centre of
    # each symbol eye.  Try _N_TIMING evenly-spaced offsets over one period
    # and keep the one with the highest mean squared sample power — this
    # requires no knowledge of carrier phase or data symbols.
    k   = np.arange(1, n_syms + 1, dtype=np.float64)
    lim = len(iq) - 2

    best_bits   = None
    best_phases = None
    best_power  = -1.0

    for i in range(_N_TIMING):
        tau   = i * period / _N_TIMING       # fractional timing offset (samples)
        c_pos = k * period + tau
        p_pos = c_pos - period

        ci = c_pos.astype(np.int32);  cf = (c_pos - ci).astype(np.float32)
        pi = p_pos.astype(np.int32);  pf = (p_pos - pi).astype(np.float32)
        ci = np.clip(ci, 0, lim);     pi = np.clip(pi, 0, lim)

        curr = iq[ci] + cf * (iq[ci + 1] - iq[ci])
        prev = iq[pi] + pf * (iq[pi + 1] - iq[pi])

        pwr = float(np.mean(np.abs(curr) ** 2))
        if pwr <= best_power:
            continue

        best_power = pwr
        raw        = np.angle(curr * np.conj(prev)) - freq_corr
        phase      = ((raw + np.pi) % (2 * np.pi) - np.pi).astype(np.float32)
        b0         = (phase < 0).astype(np.int8)
        b1         = (np.abs(phase) > (np.pi / 2)).astype(np.int8)
        bits       = np.empty(n_syms * 2, dtype=np.int8)
        bits[0::2] = b0
        bits[1::2] = b1
        best_bits   = bits
        best_phases = phase

    return best_bits, best_phases


# ── Frequency-hypothesis bit derivation ──────────────────────────────────────

def _bits_from_phases(phases: np.ndarray, k: int) -> np.ndarray:
    """Re-derive bits from hypothesis-0 corrected phases shifted by k × π/2.

    The 4th-power carrier estimator has a 4-fold ambiguity: adding k×π/2
    (rad/symbol) to the frequency correction gives an equally valid estimate.
    Trying k ∈ {1,2,3} extends the correctable carrier range from ±sr/8 to ±sr/2.
    """
    phase = ((phases - k * np.pi / 2 + np.pi) % (2 * np.pi) - np.pi).astype(np.float32)
    b0 = (phase < 0).astype(np.int8)
    b1 = (np.abs(phase) > (np.pi / 2)).astype(np.int8)
    bits = np.empty(len(phase) * 2, dtype=np.int8)
    bits[0::2] = b0
    bits[1::2] = b1
    return bits


# ── Soft sync-word correlator ─────────────────────────────────────────────────

_SOFT_THRESHOLD  = 12.5   # minimum |Σ exp(j·Δφ)| over 19 Y symbols (max=19)
                          # noise peak ≈13–14 (2500 tests) / 14.8 (102K tests); real signal ≈17
                          # Lowered from 15.5 — CRC is the real quality gate; threshold is just
                          # a pre-filter to avoid wasted Viterbi calls on obvious noise.
_SOFT_DF_STEP    = 300    # Hz — residual carrier offset search step
_SOFT_DF_MAX     = 18000  # Hz — ±range; covers full 4th-power error (±sr/8 ≈ ±9.3 kHz)
_MAX_PHASE_BUF   = 1500   # symbols — hard cap to bound soft-correlator cost

# Pre-compute the symbol index ramp for the Y sync word (19 elements)
_Y_SYM_IDX = np.arange(len(_Y_EXPECTED_PHASES), dtype=np.float64)


def _find_burst_soft(phases: np.ndarray) -> Optional[tuple]:
    """Phase-domain sync word search over a carrier-offset grid.

    For each candidate burst start, tries ±_SOFT_DF_MAX Hz of residual carrier
    offset in _SOFT_DF_STEP Hz steps.  The 2-D correlation (position × df) is
    computed as a single matrix multiply, making it fast enough for real-time use.

    Returns (start_bit, residual_df_hz, score) or None.
    """
    y_off = _SYNC_TRAIN_OFF // 2          # 61 — sync word symbol offset
    y_len = len(_Y_EXPECTED_PHASES)        # 19

    n_sym = len(phases)
    limit = n_sym - y_off - y_len + 1
    if limit <= 0:
        return None

    # Sliding windows of 19 phases starting at the Y-word offset
    seg = phases[y_off : y_off + limit + y_len - 1]
    if len(seg) < y_len:
        return None
    windows = np.lib.stride_tricks.sliding_window_view(
        seg.astype(np.float64), y_len)      # (limit, 19)

    # Unit-complex IQ from measured phases: (limit, 19)
    iq_win = np.exp(1j * windows)

    # Template bank: for each df candidate, exp(-j × (Y_expected + ramp))
    # Shape: (19, n_df) so we can do a single matmul
    df_values = np.arange(-_SOFT_DF_MAX, _SOFT_DF_MAX + 1,
                           _SOFT_DF_STEP, dtype=np.float64)   # (n_df,)
    # ramp[m, df] = m × 2π × df / symbol_rate
    ramp_mat  = _Y_SYM_IDX[:, None] * (2.0 * np.pi / _SYMBOL_RATE) * df_values  # (19, n_df)
    templates = np.exp(-1j * (_Y_EXPECTED_PHASES[:, None].astype(np.float64)
                               + ramp_mat))                    # (19, n_df)

    # (limit, 19) @ (19, n_df) → (limit, n_df)  — one BLAS call
    corr = np.abs(iq_win @ templates)                          # (limit, n_df)

    flat_idx   = int(np.argmax(corr))
    best_pos, best_df_idx = np.unravel_index(flat_idx, corr.shape)
    best_score = float(corr[best_pos, best_df_idx])

    # Always return best result — caller applies threshold and logs misses.
    return (int(best_pos) * 2, float(df_values[best_df_idx]), best_score)


# ── Burst detection ───────────────────────────────────────────────────────────

_NDB_SEQS = (('NDB1', _N_BITS), ('NDB2', _P_BITS), ('NDB3', _Q_BITS), ('EXT', _X_BITS))


def _find_burst(buf: np.ndarray):
    """
    Vectorised burst scan — O(N) numpy instead of O(N) Python loop.

    Uses sliding_window_view to compute Hamming distances between every
    candidate window and each training sequence in one numpy call, then
    picks the earliest position below the error threshold.
    SYNC is preferred over NDB at the same position.
    """
    limit = len(buf) - _NDB_CHIPS + 1
    if limit <= 0:
        return None

    y_len = len(_Y_BITS)

    # ── SYNC: Y-word at offset _SYNC_TRAIN_OFF from burst start ──────────
    # Windows: buf[SYNC_TRAIN_OFF + start : SYNC_TRAIN_OFF + start + y_len]
    # for start in [0, limit)
    sw = np.lib.stride_tricks.sliding_window_view(
        buf[_SYNC_TRAIN_OFF : _SYNC_TRAIN_OFF + limit + y_len - 1], y_len)
    sync_errs = np.sum(sw != _Y_BITS, axis=1, dtype=np.int32)   # shape (limit,)

    # ── NDB: best-of-4 training sequences at offset _NDB_TRAIN_OFF ───────
    ndb_errs  = np.full(limit, 999, dtype=np.int32)
    ndb_which = np.full(limit, -1,  dtype=np.int8)
    for ti, (tname, tbits) in enumerate(_NDB_SEQS):
        t_len = len(tbits)
        seg   = buf[_NDB_TRAIN_OFF : _NDB_TRAIN_OFF + limit + t_len - 1]
        if len(seg) < t_len:
            continue
        nw   = np.lib.stride_tricks.sliding_window_view(seg, t_len)
        errs = np.sum(nw != tbits, axis=1, dtype=np.int32)
        better = errs < ndb_errs[:len(errs)]
        ndb_errs[:len(errs)]  = np.where(better, errs, ndb_errs[:len(errs)])
        ndb_which[:len(errs)] = np.where(better, ti,   ndb_which[:len(errs)])

    # ── Pick earliest start that satisfies either threshold ───────────────
    sync_ok = sync_errs <= _SYNC_MAX_ERRORS
    ndb_ok  = ndb_errs  <= _NDB_MAX_ERRORS
    either  = sync_ok | ndb_ok
    if not np.any(either):
        return None

    first = int(np.argmax(either))   # argmax returns first True index
    if sync_ok[first]:
        return (first, 'SYNC', 'SYNC', int(sync_errs[first]))
    ti = int(ndb_which[first])
    return (first, 'NDB', _NDB_SEQS[ti][0], int(ndb_errs[first]))


# ── BB field parser ───────────────────────────────────────────────────────────

def _parse_bb(bb: np.ndarray) -> dict:
    if len(bb) < 14:
        return {}
    sys_code = _bits_to_int(bb[:4])
    out = {
        'system_code': sys_code,
        'bb_hex': f"0x{_bits_to_int(bb[:14]):04X}",
    }
    if sys_code == 1:
        out['colour_code'] = _bits_to_int(bb[4:10])
        out['timeslot']    = _bits_to_int(bb[10:12])
    return out


def _bits_to_int(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v
