"""
TETRA MAC/MLE/CMCE PDU parser.

Decodes MAC-SYNC, MAC-SYSINFO, D-SETUP, D-RELEASE, D-CONNECT,
D-SDS-DATA, D-STATUS and MLE NEIGHBOUR-CELL broadcast PDUs.

Reference: ETSI EN 300 392-2 §21 (MAC), §23 (CMCE), §19 (MLE/MM)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)

_BITS = lambda bits, start, n: _to_int(bits[start:start + n])


def _to_int(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


# ── PDU type constants ────────────────────────────────────────────────────────

# MAC PDU type (bits [0:2] of decoded BKN block)
MAC_RESOURCE      = 0  # MAC-RESOURCE
MAC_FRAG          = 1  # MAC-FRAG
MAC_RESERVE       = 2  # MAC-RESERVE
MAC_END           = 3  # MAC-END (downlink)

# Logical channel type (embedded in MAC-RESOURCE)
LCHAN_BSCH  = 0  # Broadcast Synchronization
LCHAN_BCCH  = 1  # Broadcast Control
LCHAN_MCCH  = 3  # Mobility Control
LCHAN_DCCH  = 4  # Dedicated Control

# CMCE PDU types (Protocol Discriminator = 0)
CMCE_D_SETUP     = 0x00
CMCE_D_CONNECT   = 0x01
CMCE_D_RELEASE   = 0x02
CMCE_D_DISC      = 0x03
CMCE_D_INFO      = 0x08
CMCE_D_STATUS    = 0x09
CMCE_D_SDS_DATA  = 0x0C

# MLE PDU types (Protocol Discriminator = 5)
MLE_D_NWRK_BCAST = 0x18  # D-NWRK-BROADCAST (neighbour cells)


@dataclass
class MacFrame:
    """Parsed MAC layer frame."""
    pdu_type: int           = -1
    lchan:    int           = -1
    ssi:      int           = 0    # Short Subscriber Identity (24 bits)
    usage:    int           = 0    # channel usage marker
    slot:     int           = 1    # timeslot 1-4
    fill:     bool          = False
    payload:  Optional[object] = None
    raw:      Optional[object] = None

    def to_dict(self) -> dict:
        d = {"pdu_type": self.pdu_type, "lchan": self.lchan,
             "ssi": self.ssi, "slot": self.slot, "fill": self.fill}
        if self.payload is not None and hasattr(self.payload, 'to_dict'):
            d.update(self.payload.to_dict())
        return d


@dataclass
class CellInfo:
    """Cell broadcast information from SYSINFO / SYNC."""
    mcc: int = 0
    mnc: int = 0
    colour_code: int = 0
    la: int = 0           # Location Area
    cell_id: int = 0
    freq_band: int = 0
    freq_offset: int = 0
    duplex_spacing: int = 0
    reverse_operation: bool = False
    num_of_ctrl_blk_bits: int = 0
    ms_txpwr_max_cell: int = 0
    rxlev_access_min: int = 0
    access_parameter: int = 0
    radio_downlink_timeout: int = 0
    hyperframe_cipher_key_flag: bool = False
    power_class: int = 0
    cell_load_ca: int = 0
    cck_id: int = 0

    def to_dict(self) -> dict:
        return {
            "mcc": self.mcc, "mnc": self.mnc,
            "colour_code": self.colour_code, "la": self.la,
            "cell_id": self.cell_id,
            "freq_band": self.freq_band, "freq_offset": self.freq_offset,
            "power_class": self.power_class,
        }


@dataclass
class CallEvent:
    """A CMCE call state event."""
    event: str          = ""   # "setup", "connect", "release", "info"
    ssi:   int          = 0    # calling party SSI
    gssi:  int          = 0    # group SSI / called party
    slot:  int          = 0
    encryption: bool    = False
    priority: int       = 0
    simplex: bool       = True
    circuit_mode: bool  = True
    cause:  int         = 0    # release cause

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items()}


@dataclass
class SdsMessage:
    """Short Data Service message."""
    src_ssi: int    = 0
    dst_ssi: int    = 0
    protocol: int   = 0
    data_hex: str   = ""
    text: str       = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items()}


@dataclass
class NeighbourCell:
    """One neighbour cell entry from MLE D-NWRK-BROADCAST."""
    cell_id: int = 0
    mcc: int = 0
    mnc: int = 0
    la: int = 0
    arfcn: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items()}


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_mac_block(bits, tn: int = 1) -> Optional[MacFrame]:
    """Parse a decoded BKN block (variable length, ≥18 bits)."""
    if bits is None or len(bits) < 18:
        return None
    try:
        pdu_type = _BITS(bits, 0, 2)
        frame = MacFrame(pdu_type=pdu_type, slot=tn, raw=bits)

        if pdu_type == MAC_RESOURCE:
            return _parse_mac_resource(bits, frame)
        elif pdu_type == MAC_END:
            return _parse_mac_end(bits, frame)
        else:
            return frame
    except Exception as e:
        logger.debug("MAC parse error: %s", e)
        return None


def _parse_mac_resource(bits, frame: MacFrame) -> MacFrame:
    """MAC-RESOURCE PDU (downlink). ETSI EN 300 392-2 §21.4.3."""
    if len(bits) < 50:
        return frame
    frame.lchan  = _BITS(bits,  2, 4)
    frame.fill   = bool(_BITS(bits,  6, 1))
    frame.ssi    = _BITS(bits, 14, 24)
    frame.usage  = _BITS(bits,  8, 2)

    # If there is a TMAC-PDU appended (fill == 0), parse it
    if not frame.fill and len(bits) > 50:
        tmac_bits = bits[50:]
        frame.payload = _parse_upper(tmac_bits)
    return frame


def _parse_mac_end(bits, frame: MacFrame) -> MacFrame:
    if len(bits) > 8:
        frame.payload = _parse_upper(bits[8:])
    return frame


def _parse_upper(bits) -> Optional[object]:
    """Parse LLC/MLE/CMCE PDU from payload bits."""
    if len(bits) < 8:
        return None
    # Protocol Discriminator (PD): bits [0:4]
    pd = _BITS(bits, 0, 4)
    if pd == 0:       # CMCE
        return _parse_cmce(bits)
    elif pd == 5:     # MLE
        return _parse_mle(bits)
    return None


# ── CMCE ─────────────────────────────────────────────────────────────────────

def _parse_cmce(bits) -> Optional[CallEvent]:
    if len(bits) < 8:
        return None
    pdu_type = _BITS(bits, 4, 6)
    ev = CallEvent()
    try:
        if pdu_type == CMCE_D_SETUP:
            ev.event = "setup"
            if len(bits) >= 60:
                ev.simplex  = bool(_BITS(bits, 10, 1))
                ev.circuit_mode = bool(_BITS(bits, 11, 1))
                ev.priority = _BITS(bits, 12, 4)
                ev.ssi      = _BITS(bits, 16, 24)
                ev.encryption = bool(_BITS(bits, 40, 1))
        elif pdu_type == CMCE_D_CONNECT:
            ev.event = "connect"
            if len(bits) >= 40:
                ev.ssi = _BITS(bits, 10, 24)
        elif pdu_type in (CMCE_D_RELEASE, CMCE_D_DISC):
            ev.event = "release"
            if len(bits) >= 16:
                ev.cause = _BITS(bits, 10, 6)
        elif pdu_type == CMCE_D_SDS_DATA:
            ev.event = "sds"
            return _parse_sds(bits)
        elif pdu_type == CMCE_D_STATUS:
            ev.event = "status"
            if len(bits) >= 40:
                ev.ssi = _BITS(bits, 10, 24)
        else:
            ev.event = f"cmce_{pdu_type:#04x}"
    except Exception as e:
        logger.debug("CMCE parse error: %s", e)
    return ev


def _parse_sds(bits) -> SdsMessage:
    msg = SdsMessage()
    try:
        if len(bits) >= 48:
            msg.protocol = _BITS(bits, 10,  8)
            msg.src_ssi  = _BITS(bits, 18, 24)
            if len(bits) >= 72:
                msg.dst_ssi = _BITS(bits, 42, 24)
            if len(bits) > 80:
                data_bits = bits[80:]
                data_bytes = bytearray()
                for i in range(0, len(data_bits) - 7, 8):
                    data_bytes.append(_to_int(data_bits[i:i+8]))
                msg.data_hex = data_bytes.hex()
                if msg.protocol == 0:  # text SDS
                    try:
                        msg.text = data_bytes.decode('utf-8', errors='replace')
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("SDS parse error: %s", e)
    return msg


# ── MLE ──────────────────────────────────────────────────────────────────────

def _parse_mle(bits) -> Optional[List[NeighbourCell]]:
    if len(bits) < 8:
        return None
    pdu_type = _BITS(bits, 4, 6)
    if pdu_type != MLE_D_NWRK_BCAST:
        return None
    cells = []
    pos = 10
    try:
        n_cells = _BITS(bits, pos, 3); pos += 3
        for _ in range(min(n_cells, 32)):
            if pos + 30 > len(bits):
                break
            cell = NeighbourCell()
            cell.cell_id = _BITS(bits, pos, 16); pos += 16
            cell.mcc     = _BITS(bits, pos, 10); pos += 10
            cell.mnc     = _BITS(bits, pos, 14); pos += 14
            cell.la      = _BITS(bits, pos, 14); pos += 14
            cell.arfcn   = _BITS(bits, pos, 12); pos += 12
            cells.append(cell)
    except Exception as e:
        logger.debug("MLE parse error: %s", e)
    return cells if cells else None


# ── SYSINFO / BSCH field parser ───────────────────────────────────────────────

def parse_sysinfo(bits) -> Optional[CellInfo]:
    """Parse D-SYSINFO / BNCH payload (≈ 60 bits after BSCH decode)."""
    if bits is None or len(bits) < 42:
        return None
    ci = CellInfo()
    try:
        ci.la               = _BITS(bits,  0, 14)
        ci.cell_id          = _BITS(bits, 14, 16)
        ci.freq_band        = _BITS(bits, 30,  4)
        ci.freq_offset      = _BITS(bits, 34,  2)
        ci.duplex_spacing   = _BITS(bits, 36,  3)
        ci.reverse_operation = bool(_BITS(bits, 39, 1))
        ci.num_of_ctrl_blk_bits = _BITS(bits, 40, 2)
        if len(bits) >= 54:
            ci.ms_txpwr_max_cell = _BITS(bits, 42, 3)
            ci.rxlev_access_min  = _BITS(bits, 45, 4)
            ci.access_parameter  = _BITS(bits, 49, 4)
            ci.radio_downlink_timeout = _BITS(bits, 53, 4)
    except Exception as e:
        logger.debug("SYSINFO parse error: %s", e)
    return ci
