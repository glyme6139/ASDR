"""
TETRA network time tracking: TN / FN / MN / SN.

ETSI EN 300 392-2 §21.4.1:
  - Timeslot number (TN):    1..4  (4 timeslots per TDMA frame)
  - Frame number (FN):       1..18 (18 frames per multiframe)
  - Multiframe number (MN):  1..60 (60 multiframes per hyperframe)
  - Superframe number (SN):  implicit (hyperframe counter)

BSCH appears at: TN == (4 - (MN + 1) % 4) % 4 + 1,  FN == 18
BNCH appears at: TN == (4 - (MN + 3) % 4) % 4 + 1,  FN == 18

From the SDRSharp TETRA plugin (NetworkTime.cs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NetworkTime:
    tn: int = 1   # timeslot 1-4
    fn: int = 1   # frame 1-18
    mn: int = 1   # multiframe 1-60
    sn: int = 0   # superframe (hyperframe count)
    wall_ts: float = field(default_factory=time.time)

    mcc: int = 0
    mnc: int = 0
    cc: int  = 0   # colour code
    la:  int = 0   # location area

    synced: bool = False

    # Derived cell identity
    @property
    def cell_id(self) -> str:
        return f"MCC={self.mcc} MNC={self.mnc} CC={self.cc} LA={self.la}"

    def update_from_bsch(self, bsch_bits) -> None:
        """Parse 60-bit SYNC PDU from decoded BSCH and update state.

        ETSI EN 300 392-2 §21.5.2 / osmo-tetra testpdu.c SYNC PDU layout:
          [0:4]   System Code (4 bits)
          [4:10]  Colour Code (6 bits)
          [10:12] TN (2 bits, 0-based → add 1)
          [12:17] FN (5 bits, 0-based → add 1)
          [17:23] MN (6 bits, 0-based → add 1)
          [23:31] sharing/DTX/reserved (8 bits, ignored)
          [31:41] MCC (10 bits)
          [41:55] MNC (14 bits)
          [55:60] other (5 bits, ignored)
        """
        if bsch_bits is None or len(bsch_bits) < 55:
            return
        b = bsch_bits
        self.cc  = _bits_to_int(b[4:10])
        self.tn  = _bits_to_int(b[10:12]) + 1
        self.fn  = _bits_to_int(b[12:17]) + 1
        self.mn  = _bits_to_int(b[17:23]) + 1
        if len(b) >= 55:
            self.mcc = _bits_to_int(b[31:41])
            self.mnc = _bits_to_int(b[41:55])
        self.wall_ts = time.time()
        self.synced = True

    def is_bsch_slot(self) -> bool:
        expected_tn = (4 - (self.mn + 1) % 4) % 4 + 1
        return self.fn == 18 and self.tn == expected_tn

    def is_bnch_slot(self) -> bool:
        expected_tn = (4 - (self.mn + 3) % 4) % 4 + 1
        return self.fn == 18 and self.tn == expected_tn

    def tick(self) -> None:
        """Advance by one timeslot."""
        self.tn += 1
        if self.tn > 4:
            self.tn = 1
            self.fn += 1
            if self.fn > 18:
                self.fn = 1
                self.mn += 1
                if self.mn > 60:
                    self.mn = 1
                    self.sn += 1

    def to_dict(self) -> dict:
        return {
            "tn": self.tn, "fn": self.fn, "mn": self.mn, "sn": self.sn,
            "mcc": self.mcc, "mnc": self.mnc, "cc": self.cc, "la": self.la,
            "synced": self.synced,
            "cell_id": self.cell_id,
            "wall_ts": self.wall_ts,
        }


def _bits_to_int(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v
