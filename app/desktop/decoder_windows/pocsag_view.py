"""POCSAG paging decoder window — message log, statistics, and PDU visualizer."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from PySide6.QtCore import Qt, QRect, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .base import BaseDecoderWindow

_MAX_ROWS = 500
_MAX_CAPS = 100
_MONO     = QFont('Consolas', 10)
_MONO_SM  = QFont('Consolas', 9)

_STYLE_TABLE = (
    'QTableWidget { background:#131416; color:#e0e0e0; border:1px solid #444; }'
    'QHeaderView::section { background:#202124; color:#f0f0f0; padding:4px; border:1px solid #444; }'
    'QTableWidget::item:selected { background:#34507a; }'
    'QTableWidget::item:alternate { background:#161820; }'
)
_STYLE_GROUP = (
    'QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }'
    'QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }'
)
_STYLE_TABS = (
    'QTabBar::tab { background:#1a1d21; color:#aaa; padding:4px 12px;'
    '  border:1px solid #444; border-bottom:none; border-radius:3px 3px 0 0; }'
    'QTabBar::tab:selected { background:#202124; color:#e0e0e0; }'
    'QTabWidget::pane { border:1px solid #444; }'
)
_STYLE_BTN = (
    'QPushButton { background:#2a2d31; color:#e0e0e0; border:1px solid #555;'
    '  border-radius:3px; padding:3px 8px; }'
    'QPushButton:hover { background:#3a3d41; }'
    'QPushButton:checked { background:#5a3d1a; border-color:#c87941; }'
)

_C_ALPHA   = QColor('#14203a')
_C_NUMERIC = QColor('#12231a')
_C_TONE    = QColor('#2a2a14')

_MSG_COLS = ['Time', 'RIC', 'Func', 'Type', 'Baud', 'Message', 'Raw Hex']
_COL_TIME = 0
_COL_RIC  = 1
_COL_FUNC = 2
_COL_TYPE = 3
_COL_BAUD = 4
_COL_MSG  = 5
_COL_RAW  = 6

_CAP_COLS     = ['RIC', 'Count', 'Last Message']
_COL_CAP_RIC  = 0
_COL_CAP_CNT  = 1
_COL_CAP_LAST = 2

_CENTER = Qt.AlignCenter | Qt.AlignVCenter


def _item(text: str, align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    return it


# ---------------------------------------------------------------------------
# PDU block visual constants
# ---------------------------------------------------------------------------

_PDU_BLOCK_H = 46   # height of each codeword block
_PDU_FRAME_H = 13   # height of frame label row below blocks
_PDU_MARGIN  = 6
_PDU_SYNC_W  = 44
_PDU_GAP     = 4    # gap between sync and first codeword

_CW_BG: Dict[str, QColor] = {
    'addr':    QColor('#3d2000'),
    'data':    QColor('#0c280c'),
    'idle':    QColor('#141416'),
    'bad':     QColor('#280000'),
    'unknown': QColor('#181818'),
}
_CW_FG: Dict[str, QColor] = {
    'addr':    QColor('#ffa040'),
    'data':    QColor('#7ee787'),
    'idle':    QColor('#3a3a3a'),
    'bad':     QColor('#f87171'),
    'unknown': QColor('#2e2e2e'),
}
_CW_LABEL: Dict[str, str] = {
    'addr':    'ADR',
    'data':    'DAT',
    'idle':    'IDL',
    'bad':     'ERR',
    'unknown': '···',
}

_SYNC_BG  = QColor('#0d1e3a')
_SYNC_FG  = QColor('#4a9eff')
_SEL_BG   = QColor('#ffffff')   # selected block text
_SEL_BORDER = QColor('#ffffff')

_FONT_BLOCK = QFont('Consolas', 7)
_FONT_FRAME = QFont('Consolas', 6)

_FUNC_NAMES = ['Tone', 'Numeric', 'Numeric2', 'Alpha']


def _cw_detail(cw: dict, frame_slot: int, capcode: int, func: int) -> str:
    """Return a multi-line bit-field description for one codeword."""
    c    = cw['corrected']
    idx  = cw['idx']
    err  = cw['errors']
    etype = f'  ← ECC corrected {err} bit{"s" if err != 1 else ""}' if err > 0 else ''
    t    = cw['type']

    if t == 'idle':
        return f'CW {idx:2d}  IDLE  0x{c:08X}  — slot filler, no message data'

    if t == 'bad':
        return (f'CW {idx:2d}  UNCORRECTABLE  raw=0x{cw["raw"]:08X}\n'
                f'        BCH ECC found more than 2 bit errors — codeword discarded')

    if t == 'addr':
        addr_hi = (c >> 13) & 0x3FFFF
        fn      = (c >> 11) & 0x3
        ecc     = (c >> 1)  & 0x3FF
        par     = c & 1
        fn_name = _FUNC_NAMES[fn]
        return (f'CW {idx:2d}  ADDRESS  0x{c:08X}{etype}\n'
                f'  [31]     = 0          Address codeword flag\n'
                f'  [30-13]  = 0x{addr_hi:05X} ({addr_hi:,})   18-bit address field\n'
                f'  [12-11]  = {fn:02b}b ({fn})      Function — {fn_name}\n'
                f'  [10-1]   = 0x{ecc:03X}        BCH(31,21) ECC (10 bits)\n'
                f'  [0]      = {par}          Even parity\n'
                f'  RIC = (0x{addr_hi:05X} << 3) | frame {frame_slot} = {capcode}')

    if t == 'data':
        data = (c >> 11) & 0xFFFFF
        ecc  = (c >> 1)  & 0x3FF
        par  = c & 1
        return (f'CW {idx:2d}  DATA  0x{c:08X}{etype}\n'
                f'  [31]     = 1          Data codeword flag\n'
                f'  [30-11]  = 0x{data:05X} ({data:,})   20-bit message payload\n'
                f'  [10-1]   = 0x{ecc:03X}        BCH(31,21) ECC (10 bits)\n'
                f'  [0]      = {par}          Even parity')

    return f'CW {idx:2d}  (unknown type)'


# ---------------------------------------------------------------------------
# PDU canvas widget
# ---------------------------------------------------------------------------

class POCSAGPDUWidget(QWidget):
    """Draws a POCSAG batch structure as coloured blocks; emits detail text on click."""

    block_clicked = Signal(str)   # detail text for the clicked block

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._codewords:  list = []
        self._frame_slot: int  = 0
        self._capcode:    int  = 0
        self._func:       int  = 0
        self._sel:        int  = -2   # -2=nothing, -1=sync, 0-15=cw idx
        self.setMinimumHeight(_PDU_BLOCK_H + _PDU_FRAME_H + _PDU_MARGIN * 2 + 6)
        self.setMaximumHeight(_PDU_BLOCK_H + _PDU_FRAME_H + _PDU_MARGIN * 2 + 6)
        self.setCursor(Qt.PointingHandCursor)

    def load(self, data: dict) -> None:
        self._codewords  = data.get('codewords') or []
        self._frame_slot = data.get('frame_slot', 0)
        self._capcode    = data.get('capcode', 0)
        self._func       = data.get('func', 0)
        self._sel        = -2
        self.update()

    def clear(self) -> None:
        self._codewords = []
        self._sel = -2
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p  = QPainter(self)
        W  = self.width()
        x0 = _PDU_MARGIN
        y0 = _PDU_MARGIN

        # Sync block
        sr = QRect(x0, y0, _PDU_SYNC_W, _PDU_BLOCK_H)
        self._draw_block(p, sr, _SYNC_BG, _SYNC_FG, 'SYNC\n32b', self._sel == -1)

        # 16 codeword blocks
        cw_map    = {c['idx']: c for c in self._codewords}
        avail_w   = W - x0 - _PDU_SYNC_W - _PDU_GAP - _PDU_MARGIN
        cw_w      = max(18, avail_w // 16)
        cw_x0     = x0 + _PDU_SYNC_W + _PDU_GAP

        for i in range(16):
            cw    = cw_map.get(i)
            ct    = cw['type'] if cw else 'unknown'
            bg    = _CW_BG.get(ct, _CW_BG['unknown'])
            fg    = _CW_FG.get(ct, _CW_FG['unknown'])
            label = _CW_LABEL.get(ct, '···')
            rx    = cw_x0 + i * cw_w
            rr    = QRect(rx, y0, cw_w - 1, _PDU_BLOCK_H)
            self._draw_block(p, rr, bg, fg, f'{i}\n{label}', self._sel == i)

        # Frame labels F0-F7 centred over each pair
        p.setFont(_FONT_FRAME)
        addr_frame = self._frame_slot if self._codewords else -1
        for f in range(8):
            fx  = cw_x0 + f * 2 * cw_w
            fw  = cw_w * 2 - 1
            fr  = QRect(fx, y0 + _PDU_BLOCK_H + 2, fw, _PDU_FRAME_H)
            fc  = QColor('#ffa040') if f == addr_frame else QColor('#424242')
            p.setPen(fc)
            p.drawText(fr, Qt.AlignCenter, f'F{f}')

        p.end()

    def _draw_block(self, p: QPainter, r: QRect,
                    bg: QColor, fg: QColor, text: str, selected: bool) -> None:
        fill = bg.lighter(180) if selected else bg
        p.fillRect(r, fill)
        pen_color = _SEL_BORDER if selected else fg.darker(130)
        p.setPen(QPen(pen_color, 2 if selected else 1))
        p.drawRect(r)
        p.setPen(QColor('#ffffff') if selected else fg)
        p.setFont(_FONT_BLOCK)
        p.drawText(r, Qt.AlignCenter, text)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        pos = event.position() if hasattr(event, 'position') else None
        x   = int(pos.x()) if pos else event.x()
        y   = int(pos.y()) if pos else event.y()

        x0, y0 = _PDU_MARGIN, _PDU_MARGIN
        if not (y0 <= y <= y0 + _PDU_BLOCK_H):
            return

        if x0 <= x <= x0 + _PDU_SYNC_W:
            self._sel = -1
            self.block_clicked.emit(
                'SYNC WORD  0x7CD215D8  — marks the start of a POCSAG batch (32 bits)\n'
                '  Alternating-bit preamble of 576 bits precedes each batch so\n'
                '  receivers can lock their bit clock before the sync word arrives.')
            self.update()
            return

        cw_x0  = x0 + _PDU_SYNC_W + _PDU_GAP
        avail_w = self.width() - x0 - _PDU_SYNC_W - _PDU_GAP - _PDU_MARGIN
        cw_w   = max(18, avail_w // 16)
        idx    = int((x - cw_x0) // cw_w)
        if 0 <= idx < 16:
            self._sel = idx
            cw_map = {c['idx']: c for c in self._codewords}
            cw = cw_map.get(idx)
            if cw:
                detail = _cw_detail(cw, self._frame_slot, self._capcode, self._func)
            else:
                detail = f'CW {idx}  — not part of this message (idle or out of range)'
            self.block_clicked.emit(detail)
            self.update()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class POCSAGWindow(BaseDecoderWindow):
    """Floating POCSAG decoder window: message log, statistics, and PDU view."""

    def __init__(self, vfo_id: int) -> None:
        super().__init__(f'POCSAG — VFO {vfo_id}', vfo_id, 'POCSAG')
        self._total         = 0
        self._alpha_count   = 0
        self._numeric_count = 0
        self._tone_count    = 0
        self._baud_1200     = 0
        self._baud_512      = 0
        self._capcode_counts: Dict[int, int] = defaultdict(int)
        self._capcode_last:   Dict[int, str] = {}
        self._paused = False
        self._pending: List[dict] = []
        self._build_ui()
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(2000)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet('background:#0f1012; color:#e0e0e0;')
        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)
        vlay.addLayout(self._make_toolbar())

        tabs = QTabWidget()
        tabs.setStyleSheet(_STYLE_TABS)
        tabs.addTab(self._make_messages_tab(), 'Messages')
        tabs.addTab(self._make_stats_tab(),    'Statistics')
        vlay.addWidget(tabs)

        self.resize(1020, 680)

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._count_lbl = QLabel('Messages: 0')
        self._count_lbl.setStyleSheet('color:#aaa; font-size:11px;')
        bar.addWidget(self._count_lbl)

        self._baud_lbl = QLabel('1200: 0   512: 0')
        self._baud_lbl.setStyleSheet('color:#888; font-size:11px; margin-left:12px;')
        bar.addWidget(self._baud_lbl)

        bar.addStretch()

        hint = QLabel('Tune to a POCSAG frequency (e.g. 152.0 / 157.45 MHz) in NBFM')
        hint.setStyleSheet('color:#666; font-size:10px; font-style:italic;')
        bar.addWidget(hint)

        bar.addStretch()

        self._show_raw_cb = QCheckBox('Show Raw Hex')
        self._show_raw_cb.setStyleSheet('color:#aaa; font-size:11px;')
        self._show_raw_cb.toggled.connect(self._on_show_raw_toggled)
        bar.addWidget(self._show_raw_cb)

        self._pause_btn = QPushButton('Pause')
        self._pause_btn.setFixedWidth(64)
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        self._pause_btn.setStyleSheet(_STYLE_BTN)
        bar.addWidget(self._pause_btn)

        clear_btn = QPushButton('Clear')
        clear_btn.setFixedWidth(60)
        clear_btn.setStyleSheet(_STYLE_BTN)
        clear_btn.clicked.connect(self._clear)
        bar.addWidget(clear_btn)

        return bar

    def _make_messages_tab(self) -> QWidget:
        container = QWidget()
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet('QSplitter::handle { background:#333; height:3px; }')

        # --- Message table ---
        self._table = QTableWidget(0, len(_MSG_COLS))
        self._table.setHorizontalHeaderLabels(_MSG_COLS)
        self._table.setStyleSheet(_STYLE_TABLE)
        self._table.setFont(_MONO)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_RIC,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_FUNC, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_TYPE, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_BAUD, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_MSG,  QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_RAW,  QHeaderView.ResizeToContents)
        self._table.setColumnHidden(_COL_RAW, True)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._table)

        # --- PDU panel ---
        pdu_panel = QWidget()
        pdu_panel.setStyleSheet('background:#0c0e10;')
        pdu_vlay = QVBoxLayout(pdu_panel)
        pdu_vlay.setContentsMargins(6, 4, 6, 4)
        pdu_vlay.setSpacing(3)

        pdu_hdr = QHBoxLayout()
        pdu_title = QLabel('Batch PDU  —  click a block to inspect its bit fields')
        pdu_title.setStyleSheet('color:#666; font-size:10px;')
        pdu_hdr.addWidget(pdu_title)
        pdu_hdr.addStretch()
        ric_note = QLabel('RIC = (addr[30-13] << 3) | frame_slot')
        ric_note.setStyleSheet('color:#555; font-size:10px; font-style:italic;')
        pdu_hdr.addWidget(ric_note)
        pdu_vlay.addLayout(pdu_hdr)

        self._pdu = POCSAGPDUWidget()
        self._pdu.block_clicked.connect(self._on_block_clicked)
        pdu_vlay.addWidget(self._pdu)

        self._pdu_detail = QLabel('Select a message row, then click a codeword block.')
        self._pdu_detail.setFont(_MONO_SM)
        self._pdu_detail.setStyleSheet(
            'color:#c0c0c0; background:#111214; border:1px solid #2a2a2a;'
            ' padding:4px 6px; border-radius:2px;')
        self._pdu_detail.setWordWrap(True)
        self._pdu_detail.setTextFormat(Qt.PlainText)
        pdu_vlay.addWidget(self._pdu_detail)

        splitter.addWidget(pdu_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 0)

        vlay.addWidget(splitter)
        return container

    def _make_stats_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet('background:#0f1012;')
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(8)

        row = QHBoxLayout()
        self._stat_total,   box = self._counter_box('Total',     '#4a9eff')
        row.addWidget(box)
        self._stat_alpha,   box = self._counter_box('Alpha',     '#8ab4f8')
        row.addWidget(box)
        self._stat_numeric, box = self._counter_box('Numeric',   '#7ee787')
        row.addWidget(box)
        self._stat_tone,    box = self._counter_box('Tone',      '#fdd663')
        row.addWidget(box)
        self._stat_1200,    box = self._counter_box('1200 baud', '#e0e0e0')
        row.addWidget(box)
        self._stat_512,     box = self._counter_box('512 baud',  '#e0e0e0')
        row.addWidget(box)
        vlay.addLayout(row)

        cap_lbl = QLabel('Active RICs (Radio Identity Codes)')
        cap_lbl.setStyleSheet('color:#e0e0e0; font-weight:bold; font-size:12px;')
        vlay.addWidget(cap_lbl)

        self._cap_table = QTableWidget(0, len(_CAP_COLS))
        self._cap_table.setHorizontalHeaderLabels(_CAP_COLS)
        self._cap_table.setStyleSheet(_STYLE_TABLE)
        self._cap_table.setFont(_MONO)
        self._cap_table.setAlternatingRowColors(True)
        self._cap_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._cap_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._cap_table.verticalHeader().setVisible(False)
        self._cap_table.verticalHeader().setDefaultSectionSize(22)
        cap_hdr = self._cap_table.horizontalHeader()
        cap_hdr.setSectionResizeMode(_COL_CAP_RIC,  QHeaderView.ResizeToContents)
        cap_hdr.setSectionResizeMode(_COL_CAP_CNT,  QHeaderView.ResizeToContents)
        cap_hdr.setSectionResizeMode(_COL_CAP_LAST, QHeaderView.Stretch)
        vlay.addWidget(self._cap_table)

        return w

    @staticmethod
    def _counter_box(title: str, color: str):
        box = QGroupBox(title)
        box.setStyleSheet(_STYLE_GROUP)
        inner = QVBoxLayout(box)
        lbl = QLabel('0')
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setFont(QFont('Consolas', 18, QFont.Bold))
        lbl.setStyleSheet(f'color:{color};')
        inner.addWidget(lbl)
        return lbl, box

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        if self._paused:
            self._pending.append(data)
            return
        self._insert_row(data)

    def _insert_row(self, data: dict) -> None:
        ts       = datetime.now().strftime('%H:%M:%S')
        ric      = str(data.get('capcode', '?'))
        func     = str(data.get('func', '?'))
        msg_type = str(data.get('type', '?'))
        baud     = str(data.get('baud', '?'))
        text     = str(data.get('text', '')).strip()
        raw_hex  = str(data.get('raw_hex', '')).strip()

        self._total += 1
        if msg_type == 'alpha':
            self._alpha_count += 1
        elif msg_type == 'numeric':
            self._numeric_count += 1
        elif msg_type == 'tone':
            self._tone_count += 1
        if baud == '1200':
            self._baud_1200 += 1
        elif baud == '512':
            self._baud_512 += 1

        try:
            ric_int = int(ric)
            self._capcode_counts[ric_int] += 1
            self._capcode_last[ric_int] = text or f'[{msg_type}]'
        except (ValueError, TypeError):
            pass

        row = self._table.rowCount()
        self._table.insertRow(row)

        it_time = _item(ts, _CENTER)
        it_time.setData(Qt.UserRole, data)   # store full dict for PDU view
        self._table.setItem(row, _COL_TIME, it_time)
        self._table.setItem(row, _COL_RIC,  _item(ric,      _CENTER))
        self._table.setItem(row, _COL_FUNC, _item(func,     _CENTER))
        self._table.setItem(row, _COL_TYPE, _item(msg_type, _CENTER))
        self._table.setItem(row, _COL_BAUD, _item(baud,     _CENTER))
        self._table.setItem(row, _COL_MSG,  _item(text))
        self._table.setItem(row, _COL_RAW,  _item(raw_hex))

        color = {'alpha': _C_ALPHA, 'numeric': _C_NUMERIC, 'tone': _C_TONE}.get(msg_type)
        if color:
            for col in range(len(_MSG_COLS)):
                it = self._table.item(row, col)
                if it:
                    it.setBackground(color)

        if self._table.rowCount() > _MAX_ROWS:
            self._table.removeRow(0)

        self._table.scrollToBottom()
        self._count_lbl.setText(f'Messages: {self._total}')
        self._baud_lbl.setText(f'1200: {self._baud_1200}   512: {self._baud_512}')

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def _refresh_stats(self) -> None:
        self._stat_total.setText(str(self._total))
        self._stat_alpha.setText(str(self._alpha_count))
        self._stat_numeric.setText(str(self._numeric_count))
        self._stat_tone.setText(str(self._tone_count))
        self._stat_1200.setText(str(self._baud_1200))
        self._stat_512.setText(str(self._baud_512))

        sorted_caps = sorted(self._capcode_counts.items(), key=lambda x: -x[1])[:_MAX_CAPS]
        self._cap_table.setRowCount(0)
        for cap, cnt in sorted_caps:
            r = self._cap_table.rowCount()
            self._cap_table.insertRow(r)
            self._cap_table.setItem(r, _COL_CAP_RIC,  _item(str(cap), _CENTER))
            self._cap_table.setItem(r, _COL_CAP_CNT,  _item(str(cnt), _CENTER))
            self._cap_table.setItem(r, _COL_CAP_LAST, _item(self._capcode_last.get(cap, '')))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        it = self._table.item(row, _COL_TIME)
        if it is None:
            return
        data = it.data(Qt.UserRole)
        if isinstance(data, dict):
            self._pdu.load(data)
            self._pdu_detail.setText('Click a block above to see its bit-field breakdown.')

    def _on_block_clicked(self, detail: str) -> None:
        self._pdu_detail.setText(detail)

    def _on_show_raw_toggled(self, checked: bool) -> None:
        self._table.setColumnHidden(_COL_RAW, not checked)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self._pause_btn.setText('Resume' if checked else 'Pause')
        if not checked:
            for d in self._pending:
                self._insert_row(d)
            self._pending.clear()

    def _clear(self) -> None:
        self._table.setRowCount(0)
        self._total = self._alpha_count = self._numeric_count = self._tone_count = 0
        self._baud_1200 = self._baud_512 = 0
        self._capcode_counts.clear()
        self._capcode_last.clear()
        self._pending.clear()
        self._pdu.clear()
        self._pdu_detail.setText('Select a message row, then click a codeword block.')
        self._count_lbl.setText('Messages: 0')
        self._baud_lbl.setText('1200: 0   512: 0')
        self._refresh_stats()
