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
    decode_bsch, decode_aach, decode_bkn,
    rm_decode, check_crc,
)
from .mac import (
    parse_mac_block, parse_sysinfo,
    CellInfo, CallEvent, SdsMessage, NeighbourCell,
)

logger = logging.getLogger(__name__)

# ── Physical constants ────────────────────────────────────────────────────────

_SYMBOL_RATE      = 18_000
_MIN_SAMPLE_RATE  = 36_000
_AUDIO_RATE       = 48_000
_CHUNK            = 2_048

# NDB burst field layout (chips = dibits = 2 bits each after DQPSK)
# ETSI EN 300 392-2 Table 9.33
_NDB_CHIPS  = 510
_NDB_B1_S   = 2;   _NDB_B1_E   = 218   # 216 bits
_NDB_SW_OFF = 218; _NDB_SW_LEN = 38
_NDB_BB_OFF = 256; _NDB_BB_LEN = 14
_NDB_B2_S   = 270; _NDB_B2_E   = 486   # 216 bits

# SB burst block offsets
_SB_BLK1_S  = 14;  _SB_BLK1_E  = 134   # 120 bits
_SB_BBK_OFF = 252; _SB_BBK_LEN = 30
_SB_BLK2_S  = 282; _SB_BLK2_E  = 498   # 216 bits

_TRAIN_MAX_ERRORS = 5

# Synchronisation word (38 chips)
_SW_BITS = np.array([
    1,1,0,0,1,0,0,1, 0,1,0,1,1,1,1,0,
    1,0,0,1,0,0,0,1, 0,0,0,0,0,1,0,0,
    1,1,1,1,1,1,
], dtype=np.float32)
_SW_BIPOLAR = 2.0 * _SW_BITS - 1.0

# Training sequences (22 chips each)
_N_BITS = np.array([1,1,0,1, 0,0,0,0, 1,1,1,0, 1,0,0,1, 1,1,0,1, 0,0], dtype=np.int8)
_P_BITS = np.array([0,1,1,1, 1,0,1,0, 0,1,0,0, 0,0,1,1, 0,1,1,1, 1,0], dtype=np.int8)
_Q_BITS = np.array([1,0,1,1, 0,1,1,1, 0,0,0,0, 0,1,1,0, 1,0,1,1, 0,1], dtype=np.int8)
_X_BITS = np.array([1,0,0,1, 1,1,0,1, 0,0,0,0, 1,1,1,0, 1,0,0,1, 1,1,
                    0,1,0,0, 0,0,1,1], dtype=np.int8)
_Y_BITS = np.array([1,1,0,0, 0,0,0,1, 1,0,0,1, 1,1,0,0, 1,1,1,0,
                    1,0,0,1, 1,1,0,0, 0,0,0,1, 1,0,0,1, 1,1,0,0], dtype=np.int8)
_SYNC_TRAIN_OFF = 214
_NDB_TRAIN_OFF  = 244

_PI4_MAP = (
    ( np.pi / 4,     0, 0),
    ( 3*np.pi / 4,   0, 1),
    (-3*np.pi / 4,   1, 1),
    (-np.pi / 4,     1, 0),
)

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

    # ── BaseDecoder ──────────────────────────────────────────────────────────

    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        if not self.is_enabled or data is None or len(data) < 4:
            return None
        if not np.iscomplexobj(data):
            return None

        nb_sr = len(data) * _AUDIO_RATE / _CHUNK
        sps   = max(1, int(nb_sr / _SYMBOL_RATE))
        if nb_sr < _MIN_SAMPLE_RATE:
            return None

        if self._tail is not None and self._tail_sps == sps:
            iq = np.concatenate([self._tail, data])
        else:
            iq = data
        self._tail     = data[-sps:].copy()
        self._tail_sps = sps

        new_bits = _demodulate(iq, sps)
        if new_bits is None or len(new_bits) == 0:
            return None

        self._bits = np.concatenate([self._bits, new_bits])
        self._drain()

        return self._pending.popleft() if self._pending else None

    def reset(self):
        self._bits  = np.zeros(0, dtype=np.int8)
        self._tail  = None
        self._burst_n = 0
        self._pending.clear()
        self._net   = NetworkTime()
        self._calls = {}
        self._sds   = []
        self._neighbours = []
        self._scramble_seed = BSCH_SEED

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
        min_lookahead = max(len(_Y_BITS) + _SYNC_TRAIN_OFF,
                            len(_N_BITS) + _NDB_TRAIN_OFF)
        while len(buf) >= _NDB_CHIPS:
            hit = _find_burst(buf)
            if hit is None:
                keep = min_lookahead
                buf = buf[-keep:] if len(buf) > keep else buf
                break
            start, kind, train, errs = hit
            end = start + _NDB_CHIPS
            if end > len(buf):
                buf = buf[start:]
                break
            burst = buf[start:end].copy()
            r = self._decode_burst(burst, kind, train, errs)
            if r is not None:
                self._pending.append(r)
            buf = buf[end:]
        self._bits = buf

    def _decode_burst(self, burst, kind, train, errs) -> Optional[DecoderResult]:
        if errs > _TRAIN_MAX_ERRORS:
            return None
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
            # Decode BSCH from SB1 (120 bits)
            sb1 = burst[_SB_BLK1_S:_SB_BLK1_E]
            bsch, crc_ok = decode_bsch(sb1, self._scramble_seed)
            if crc_ok and bsch is not None:
                self._net.update_from_bsch(bsch)
                self._scramble_seed = scramble_seed(
                    self._net.mcc, self._net.mnc, self._net.cc)
                logger.info(
                    "TETRA SYNC: %s", self._net.cell_id
                )
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
                # Decode control channel payload
                payload, crc_ok = decode_bkn(b1, b2, self._scramble_seed)
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
                if self._net.is_bnch_slot() and self._cell is None:
                    si = parse_sysinfo(payload)
                    if si:
                        si.mcc = self._net.mcc
                        si.mnc = self._net.mnc
                        si.colour_code = self._net.cc
                        self._cell = si
                        sysinfo_dict = si.to_dict()
            else:
                # Traffic channel — decode voice
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

def _demodulate(iq: np.ndarray, sps: int) -> Optional[np.ndarray]:
    if sps < 1 or len(iq) <= sps:
        return None
    idx   = np.arange(sps, len(iq), sps)
    if len(idx) == 0:
        return None
    curr  = iq[idx].astype(np.complex64)
    prev  = iq[idx - sps].astype(np.complex64)
    phase = np.angle(curr * np.conj(prev)).astype(np.float32)
    bits  = np.empty(len(phase) * 2, dtype=np.int8)
    for i, p in enumerate(phase):
        b0, b1 = _pi4_decode(float(p))
        bits[2*i] = b0; bits[2*i+1] = b1
    return bits


def _pi4_decode(phase: float):
    best_d, best_b0, best_b1 = 999.0, 0, 0
    for ref, b0, b1 in _PI4_MAP:
        d = abs(phase - ref)
        if d > np.pi:
            d = 2.0*np.pi - d
        if d < best_d:
            best_d, best_b0, best_b1 = d, b0, b1
    return best_b0, best_b1


# ── Burst detection ───────────────────────────────────────────────────────────

def _find_burst(buf: np.ndarray):
    """Scan buf for the best sync-word hit. Returns (start, kind, train, errs) or None."""
    best = None
    limit = len(buf) - _NDB_CHIPS + 1
    for start in range(0, limit):
        # Try SYNC training at offset 214
        sync_off = start + _SYNC_TRAIN_OFF
        if sync_off + len(_Y_BITS) <= len(buf):
            e = int(np.sum(buf[sync_off:sync_off+len(_Y_BITS)] != _Y_BITS))
            if e <= _TRAIN_MAX_ERRORS:
                if best is None or e < best[3]:
                    best = (start, 'SYNC', 'SYNC', e)

        # Try NDB training at offset 244
        ndb_off = start + _NDB_TRAIN_OFF
        if ndb_off + max(len(_N_BITS), len(_X_BITS)) <= len(buf):
            for tname, tbits in (('NDB1',_N_BITS),('NDB2',_P_BITS),
                                  ('NDB3',_Q_BITS),('EXT',_X_BITS)):
                e = int(np.sum(buf[ndb_off:ndb_off+len(tbits)] != tbits))
                if e <= _TRAIN_MAX_ERRORS:
                    if best is None or e < best[3]:
                        best = (start, 'NDB', tname, e)
        if best is not None and best[3] == 0:
            break  # perfect match, stop scanning
    return best


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
