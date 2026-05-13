"""
TETRA channel visualization window — full protocol stack view.

Tabs:
  Cell Info     — MCC, MNC, CC, LA, network time, frequencies
  Active Calls  — per-slot call state with SSI, encryption, priority
  Neighbours    — up to 32 neighbouring cells from MLE broadcast
  SDS           — short data service messages
  Events        — scrolling burst / control event log
"""

from __future__ import annotations

import json
import time
from collections import deque
from html import escape
from typing import Deque, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from .base import BaseDecoderWindow

# ── Tuning ────────────────────────────────────────────────────────────────────
_FAST_MS      = 500    # status bar + cell info cards
_SLOW_TICKS   = 4      # heavy tables refresh every _SLOW_TICKS fast ticks (2 s)
_MAX_EVENTS   = 200    # rolling event buffer
_LOG_ROWS     = 60     # rows shown in event log table
_CALLS_ROWS   = 8      # max active-call rows (1 per timeslot, 4 slots)
_SDS_ROWS     = 50

_STYLE_DARK  = "background:#0f1012;"
_STYLE_GROUP = (
    "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
    "QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }"
)
_STYLE_TABLE = (
    "QTableWidget { background:#131416; color:#e0e0e0; border:1px solid #444; }"
    "QHeaderView::section { background:#202124; color:#f0f0f0; padding:4px; border:1px solid #444; }"
    "QTableWidget::item:selected { background:#34507a; }"
    "QTableWidget::item:alternate { background:#161820; }"
)
_STYLE_BROWSER = "QTextBrowser { background:#111214; color:#e0e0e0; border:1px solid #444; }"
_MONO = QFont("Consolas", 10)

# Colours
_C_DEFAULT  = "#e0e0e0"
_C_SYNC     = "#4a9eff"
_C_BCCH     = "#8ab4f8"
_C_TCH      = "#c5e1a5"
_C_CONNECT  = "#7ee787"
_C_RELEASE  = "#f28b82"
_C_SDS      = "#fdd663"
_BG_SYNC    = "#142235"
_BG_BCCH    = "#142235"
_BG_TCH     = "#122017"
_BG_ALT     = ""


def _make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    for i in range(len(headers) - 1):
        t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
    t.setStyleSheet(_STYLE_TABLE)
    return t


def _preallocate(table: QTableWidget, n_rows: int, n_cols: int):
    """Fill table with blank items so we can setText() instead of setItem() later."""
    table.setRowCount(n_rows)
    flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
    for r in range(n_rows):
        for c in range(n_cols):
            it = QTableWidgetItem("")
            it.setFlags(flags)
            table.setItem(r, c, it)


def _set_row(table: QTableWidget, row: int, texts: list[str],
             fg: str = _C_DEFAULT, bg: str = ""):
    """Update an existing pre-allocated row in-place (no allocation)."""
    fg_brush = QBrush(QColor(fg))
    bg_brush = QBrush(QColor(bg)) if bg else None
    for c, txt in enumerate(texts):
        it = table.item(row, c)
        if it is None:
            it = QTableWidgetItem(str(txt))
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            table.setItem(row, c, it)
        else:
            it.setText(str(txt))
        it.setForeground(fg_brush)
        if bg_brush is not None:
            it.setBackground(bg_brush)


class TETRAWindow(BaseDecoderWindow):
    """Full-stack TETRA channel inspector."""

    def __init__(self, vfo_id: int):
        super().__init__(f"TETRA — VFO {vfo_id + 1}", vfo_id, "TETRA")
        self._events: Deque[dict]    = deque(maxlen=_MAX_EVENTS)
        self._latest: dict           = {}
        self._calls:  Dict[str,dict] = {}
        self._sds:    List[dict]     = []
        self._neighbours: List[dict] = []
        self._net:    dict           = {}
        self._freeze: bool           = False
        self._tick:   int            = 0

        # dirty flags — set by push_result(), cleared after render
        self._dirty_events:     bool = False
        self._dirty_calls:      bool = False
        self._dirty_neighbours: bool = False
        self._dirty_sds:        bool = False
        self._dirty_status:     bool = False

        # last rendered row count per table (skip redraw if unchanged)
        self._ev_rendered:  int = -1
        self._nb_rendered:  int = -1
        self._sds_rendered: int = -1

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(_FAST_MS)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("TETRA Channel Inspector")
        title.setStyleSheet("font-size:17px; font-weight:700; color:#f5f7fa;")
        self._status_lbl = QLabel("Waiting…")
        self._status_lbl.setStyleSheet("color:#9aa0a6; font-weight:600;")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._status_lbl)
        root.addLayout(hdr)

        # Toolbar
        tb = QHBoxLayout()
        self._freeze_btn = QPushButton("Freeze")
        self._freeze_btn.setCheckable(True)
        self._freeze_btn.toggled.connect(self._on_freeze)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear)
        self._bcch_cb = QCheckBox("BCCH")
        self._bcch_cb.setChecked(True)
        self._bcch_cb.stateChanged.connect(self._invalidate_events)
        self._tch_cb = QCheckBox("TCH/S")
        self._tch_cb.setChecked(True)
        self._tch_cb.stateChanged.connect(self._invalidate_events)
        for w in (self._freeze_btn, self._clear_btn, self._bcch_cb, self._tch_cb):
            tb.addWidget(w)
        tb.addStretch()
        root.addLayout(tb)

        # Tabs
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            "QTabBar::tab { background:#1a1b1e; color:#9aa0a6; padding:6px 14px; }"
            "QTabBar::tab:selected { background:#202124; color:#f1f3f4;"
            " border-bottom:2px solid #4a9eff; }"
        )
        self._tabs.addTab(self._build_cell_tab(),       "Cell Info")
        self._tabs.addTab(self._build_calls_tab(),      "Active Calls")
        self._tabs.addTab(self._build_neighbours_tab(), "Neighbours")
        self._tabs.addTab(self._build_sds_tab(),        "SDS Messages")
        self._tabs.addTab(self._build_events_tab(),     "Event Log")
        root.addWidget(self._tabs, stretch=1)

        self.setCentralWidget(central)
        self.resize(1200, 820)
        self.setStyleSheet(_STYLE_DARK)

    # ── Cell Info tab ─────────────────────────────────────────────────────────

    def _build_cell_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)

        grid = QGroupBox("Current Cell")
        grid.setStyleSheet(_STYLE_GROUP)
        gl = QGridLayout(grid)
        gl.setContentsMargins(10, 16, 10, 10)
        gl.setSpacing(10)

        self._cell_cards: Dict[str, QLabel] = {}
        fields = [
            ("MCC",           "net_mcc"),
            ("MNC",           "net_mnc"),
            ("Colour Code",   "net_cc"),
            ("Location Area", "net_la"),
            ("Cell ID",       "sysinfo_cell_id"),
            ("Freq Band",     "sysinfo_freq_band"),
            ("Power Class",   "sysinfo_power_class"),
            ("TN",            "net_tn"),
            ("FN",            "net_fn"),
            ("MN",            "net_mn"),
            ("Superframe",    "net_sn"),
            ("Synced",        "net_synced"),
            ("Mode",          "mode"),
            ("Burst #",       "burst"),
            ("SW errors",     "sw_errors"),
            ("CRC",           "crc_ok"),
        ]
        for idx, (label, key) in enumerate(fields):
            card = self._make_card(label, key, self._cell_cards)
            gl.addWidget(card, idx // 4, idx % 4)

        root.addWidget(grid)

        nt_grp = QGroupBox("Network Time")
        nt_grp.setStyleSheet(_STYLE_GROUP)
        nt_lay = QHBoxLayout(nt_grp)
        nt_lay.setContentsMargins(10, 16, 10, 10)
        self._nt_lbl = QLabel("—")
        self._nt_lbl.setFont(_MONO)
        self._nt_lbl.setStyleSheet("color:#7ee787; font-size:13px;")
        nt_lay.addWidget(self._nt_lbl)
        root.addWidget(nt_grp)
        root.addStretch()
        return w

    # ── Active Calls tab ──────────────────────────────────────────────────────

    def _build_calls_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self._calls_tbl = _make_table(
            ["Slot", "Event", "SSI", "GSSI", "Enc.", "Priority", "Type"])
        _preallocate(self._calls_tbl, _CALLS_ROWS, 7)
        v.addWidget(self._calls_tbl)
        return w

    # ── Neighbours tab ────────────────────────────────────────────────────────

    def _build_neighbours_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self._nb_tbl = _make_table(["Cell ID", "MCC", "MNC", "LA", "ARFCN"])
        _preallocate(self._nb_tbl, 32, 5)
        v.addWidget(self._nb_tbl)
        return w

    # ── SDS tab ───────────────────────────────────────────────────────────────

    def _build_sds_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self._sds_tbl = _make_table(
            ["Time", "Src SSI", "Dst SSI", "Protocol", "Text / Hex"])
        _preallocate(self._sds_tbl, _SDS_ROWS, 5)
        v.addWidget(self._sds_tbl)
        return w

    # ── Event log tab ─────────────────────────────────────────────────────────

    def _build_events_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._ev_tbl = _make_table(
            ["Time", "#", "Type", "CC", "TS", "Err", "CRC", "Voice", "Ev."])
        _preallocate(self._ev_tbl, _LOG_ROWS, 9)
        self._ev_tbl.itemSelectionChanged.connect(self._on_ev_selected)
        splitter.addWidget(self._ev_tbl)

        self._ev_detail = QTextBrowser()
        self._ev_detail.setStyleSheet(_STYLE_BROWSER)
        self._ev_detail.setFont(_MONO)
        splitter.addWidget(self._ev_detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        v.addWidget(splitter)
        return w

    # ── Card helper ───────────────────────────────────────────────────────────

    def _make_card(self, title: str, key: str, store: dict) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.StyledPanel)
        f.setStyleSheet(
            "QFrame { background:#17181b; border:1px solid #32353b; border-radius:6px; }")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        tl = QLabel(title)
        tl.setStyleSheet("color:#8a9099; font-size:10px;")
        vl = QLabel("—")
        vl.setStyleSheet("color:#f1f3f4; font-size:14px; font-weight:700;")
        vl.setWordWrap(True)
        lay.addWidget(tl)
        lay.addWidget(vl)
        store[key] = vl
        return f

    # ── Data ingress ──────────────────────────────────────────────────────────

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        ev = dict(data)
        ev["_ts"] = time.time()
        self._events.append(ev)
        self._latest = ev
        self._dirty_events  = True
        self._dirty_status  = True

        calls = ev.get("active_calls")
        if isinstance(calls, dict) and calls:
            self._calls.update(calls)
            self._dirty_calls = True

        nb = ev.get("neighbours")
        if isinstance(nb, list) and nb:
            self._neighbours = nb
            self._dirty_neighbours = True

        sds_text = ev.get("sds_text")
        if sds_text:
            self._sds.append({
                "_ts": ev["_ts"],
                "src_ssi": ev.get("ssi", 0),
                "dst_ssi": 0,
                "protocol": 0,
                "text": sds_text,
            })
            if len(self._sds) > _SDS_ROWS:
                self._sds.pop(0)
            self._dirty_sds = True

        net = {k[4:]: v for k, v in ev.items() if k.startswith("net_")}
        if net:
            self._net = net

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _on_tick(self):
        if self._freeze:
            return
        self._tick += 1

        # Fast path: status bar + cell info cards (every tick)
        if self._dirty_status:
            self._refresh_status()
            self._dirty_status = False
        self._refresh_cell_fast()

        # Slow path: heavy tables (every _SLOW_TICKS ticks)
        if self._tick % _SLOW_TICKS == 0:
            tab_idx = self._tabs.currentIndex()
            if tab_idx == 1 and self._dirty_calls:
                self._refresh_calls()
                self._dirty_calls = False
            if tab_idx == 2 and self._dirty_neighbours:
                self._refresh_neighbours()
                self._dirty_neighbours = False
            if tab_idx == 3 and self._dirty_sds:
                self._refresh_sds()
                self._dirty_sds = False
            if tab_idx == 4 and self._dirty_events:
                self._refresh_events()
                self._dirty_events = False

    # ── Renders ───────────────────────────────────────────────────────────────

    def _refresh_cell_fast(self):
        """Update cell-info cards with latest data (cheap label setText calls)."""
        d = self._latest
        sysinfo = d.get("sysinfo") or {}

        for key, lbl in self._cell_cards.items():
            if key.startswith("sysinfo_"):
                raw = sysinfo.get(key[8:])
            else:
                raw = d.get(key)
            if raw is None:
                txt = "—"
            elif key == "net_synced":
                txt = "YES" if raw else "no"
            elif key == "crc_ok":
                txt = "OK" if raw else ("FAIL" if raw is False else "—")
            else:
                txt = str(raw)
            if lbl.text() != txt:
                lbl.setText(txt)

        net = self._net
        if net:
            nt = (f"TN={net.get('tn','?')}  FN={net.get('fn','?')}"
                  f"  MN={net.get('mn','?')}  SN={net.get('sn','?')}"
                  f"   MCC={net.get('mcc','?')}  MNC={net.get('mnc','?')}"
                  f"  CC={net.get('cc','?')}")
            if self._nt_lbl.text() != nt:
                self._nt_lbl.setText(nt)

    def _refresh_calls(self):
        t = self._calls_tbl
        rows = list(self._calls.items())
        n = min(len(rows), _CALLS_ROWS)
        t.blockSignals(True)
        t.setUpdatesEnabled(False)
        try:
            for r in range(n):
                slot, ev = rows[r]
                enc   = "YES" if ev.get("encryption") else "no"
                ctype = ("Circuit" if ev.get("circuit_mode") else "Packet")
                ctype += " Simplex" if ev.get("simplex") else " Duplex"
                fg = (_C_CONNECT if ev.get("event") == "connect" else
                      _C_RELEASE if ev.get("event") == "release" else _C_DEFAULT)
                _set_row(t, r, [str(slot), ev.get("event","?"),
                                str(ev.get("ssi",0)), str(ev.get("gssi",0)),
                                enc, str(ev.get("priority",0)), ctype], fg)
            # Blank unused rows
            for r in range(n, _CALLS_ROWS):
                _set_row(t, r, [""] * 7)
        finally:
            t.setUpdatesEnabled(True)
            t.blockSignals(False)

    def _refresh_neighbours(self):
        if len(self._neighbours) == self._nb_rendered:
            return
        t = self._nb_tbl
        rows = self._neighbours
        n = min(len(rows), 32)
        t.blockSignals(True)
        t.setUpdatesEnabled(False)
        try:
            for r in range(n):
                nb = rows[r]
                _set_row(t, r, [str(nb.get(k,"?"))
                                for k in ("cell_id","mcc","mnc","la","arfcn")])
            for r in range(n, 32):
                _set_row(t, r, [""] * 5)
        finally:
            t.setUpdatesEnabled(True)
            t.blockSignals(False)
        self._nb_rendered = len(rows)

    def _refresh_sds(self):
        if len(self._sds) == self._sds_rendered:
            return
        t = self._sds_tbl
        rows = list(reversed(self._sds[-_SDS_ROWS:]))
        n = min(len(rows), _SDS_ROWS)
        t.blockSignals(True)
        t.setUpdatesEnabled(False)
        try:
            for r in range(n):
                msg = rows[r]
                ts  = time.strftime("%H:%M:%S", time.localtime(msg.get("_ts", 0)))
                fg  = _C_SDS if r == 0 else _C_DEFAULT
                _set_row(t, r, [ts, str(msg.get("src_ssi",0)),
                                str(msg.get("dst_ssi",0)),
                                str(msg.get("protocol",0)),
                                msg.get("text") or msg.get("data_hex","")], fg)
            for r in range(n, _SDS_ROWS):
                _set_row(t, r, [""] * 5)
        finally:
            t.setUpdatesEnabled(True)
            t.blockSignals(False)
        self._sds_rendered = len(self._sds)

    def _refresh_events(self):
        show_bcch = self._bcch_cb.isChecked()
        show_tch  = self._tch_cb.isChecked()
        visible: list[dict] = []
        for ev in reversed(self._events):
            bt = ev.get("burst_type", "")
            if bt == "BCCH" and not show_bcch:
                continue
            if bt == "TCH/S" and not show_tch:
                continue
            visible.append(ev)
            if len(visible) >= _LOG_ROWS:
                break

        n = len(visible)
        if n == self._ev_rendered and not self._dirty_events:
            return

        t = self._ev_tbl
        t.blockSignals(True)
        t.setUpdatesEnabled(False)
        try:
            for r in range(n):
                ev  = visible[r]
                bt  = ev.get("burst_type", "?")
                ts  = time.strftime("%H:%M:%S", time.localtime(ev.get("_ts", 0)))
                cc  = str(ev.get("colour_code", "—"))
                tss = f"TS{ev.get('timeslot','?')}"
                err = str(ev.get("sw_errors", "?"))
                crc = ("OK" if ev.get("crc_ok") else
                       "—" if ev.get("crc_ok") is None else "FAIL")
                voice = "Y" if ev.get("voice_burst") else ""
                ceve  = ev.get("call_event") or ""

                if bt == "SYNC":
                    fg, bg = _C_SYNC, _BG_SYNC
                elif bt == "BCCH":
                    fg, bg = _C_BCCH, _BG_BCCH
                elif ceve == "connect":
                    fg, bg = _C_CONNECT, _BG_TCH
                elif ceve == "release":
                    fg, bg = _C_RELEASE, _BG_TCH
                elif bt == "TCH/S":
                    fg, bg = _C_TCH, _BG_TCH
                else:
                    fg, bg = _C_DEFAULT, ""

                _set_row(t, r,
                         [ts, str(ev.get("burst","?")), bt, cc, tss,
                          err, crc, voice, ceve], fg, bg)

            # Blank unused rows
            for r in range(n, _LOG_ROWS):
                _set_row(t, r, [""] * 9)
        finally:
            t.setUpdatesEnabled(True)
            t.blockSignals(False)

        self._ev_rendered = n

    def _refresh_status(self):
        total = len(self._events)
        if total == 0:
            self._status_lbl.setText("Waiting for TETRA bursts…")
            return
        bcch  = sum(1 for e in self._events if e.get("burst_type") == "BCCH")
        tch   = sum(1 for e in self._events if e.get("burst_type") == "TCH/S")
        sync  = sum(1 for e in self._events if e.get("burst_type") == "SYNC")
        voice = sum(1 for e in self._events if e.get("pcm_samples", 0) > 0)
        mcc    = self._net.get("mcc", "?")
        mnc    = self._net.get("mnc", "?")
        synced = "✓" if self._net.get("synced") else "×"
        self._status_lbl.setText(
            f"[{synced}] MCC={mcc} MNC={mnc}  |  "
            f"SYNC={sync}  BCCH={bcch}  TCH/S={tch}  Voice={voice}  "
            f"Total={total}/{_MAX_EVENTS}"
        )

    # ── Event detail panel ────────────────────────────────────────────────────

    def _on_ev_selected(self):
        row = self._ev_tbl.currentRow()
        if row < 0:
            return
        show_bcch = self._bcch_cb.isChecked()
        show_tch  = self._tch_cb.isChecked()
        visible: list[dict] = []
        for ev in reversed(self._events):
            bt = ev.get("burst_type", "")
            if bt == "BCCH" and not show_bcch:
                continue
            if bt == "TCH/S" and not show_tch:
                continue
            visible.append(ev)
            if len(visible) >= _LOG_ROWS:
                break
        if row < len(visible):
            self._show_ev_detail(visible[row])

    def _show_ev_detail(self, ev: dict):
        safe = {}
        for k, v in ev.items():
            if k.startswith("_"):
                continue
            if k == "pcm" and v is not None:
                safe[k] = f"<{len(v)} samples>"
            elif hasattr(v, "tolist"):
                safe[k] = v.tolist()
            else:
                safe[k] = v
        html = (
            "<div style='font-family:Consolas,monospace;color:#e0e0e0;font-size:11px;'>"
            f"<h3 style='color:#4a9eff;margin-top:0'>"
            f"Burst #{escape(str(ev.get('burst','?')))} — "
            f"{escape(str(ev.get('burst_type','?')))}</h3>"
            f"<pre style='white-space:pre-wrap;'>"
            f"{escape(json.dumps(safe, indent=2, default=str))}</pre>"
            "</div>"
        )
        self._ev_detail.setHtml(html)

    # ── Controls ──────────────────────────────────────────────────────────────

    def _invalidate_events(self):
        self._ev_rendered = -1
        self._dirty_events = True

    def _on_freeze(self, checked: bool):
        self._freeze = checked
        self._freeze_btn.setText("Unfreeze" if checked else "Freeze")

    def _clear(self):
        self._events.clear()
        self._latest     = {}
        self._calls.clear()
        self._sds.clear()
        self._neighbours = []
        self._net        = {}
        self._ev_rendered  = -1
        self._nb_rendered  = -1
        self._sds_rendered = -1
        self._dirty_events = self._dirty_calls = True
        self._dirty_neighbours = self._dirty_sds = True
        self._refresh_events()
        self._refresh_calls()
        self._refresh_neighbours()
        self._refresh_sds()
        self._status_lbl.setText("Cleared.")

    def closeEvent(self, event):
        event.ignore()
        self.hide()
