"""DMR burst viewer — scrolling table of decoded bursts and call events."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .base import BaseDecoderWindow

_MAX_ROWS = 300
_MONO     = QFont('Consolas', 10)
_STYLE    = (
    'QTableWidget { background:#0e1012; color:#e0e0e0; border:1px solid #444; }'
    'QHeaderView::section { background:#1a1d21; color:#f0f0f0; padding:4px; border:1px solid #444; }'
    'QTableWidget::item:selected { background:#2d4a6e; }'
    'QTableWidget::item:alternate { background:#111316; }'
)

_HEADERS = ['Time', 'Type', 'TS', 'Src', 'Dst', 'Call', 'Emergency', 'Errors']
_C_TIME  = 0
_C_TYPE  = 1
_C_TS    = 2
_C_SRC   = 3
_C_DST   = 4
_C_CALL  = 5
_C_EMERG = 6
_C_ERR   = 7

_COLOR_VOICE  = QColor('#0d2010')   # dark green  — voice
_COLOR_DATA   = QColor('#0d1520')   # dark blue   — data / LC
_COLOR_EMERG  = QColor('#3a1010')   # dark red    — emergency
_COLOR_DIRECT = QColor('#20180d')   # dark amber  — direct mode


def _btn(label: str) -> QPushButton:
    b = QPushButton(label)
    b.setStyleSheet(
        'QPushButton{background:#2a2d31;color:#e0e0e0;border:1px solid #555;'
        'border-radius:3px;padding:3px 8px;}'
        'QPushButton:hover{background:#3a3d41;}'
    )
    return b


class DMRWindow(BaseDecoderWindow):
    """Floating window showing a live table of decoded DMR bursts."""

    def __init__(self, vfo_id: int):
        super().__init__(f'DMR — VFO {vfo_id}', vfo_id, 'DMR')
        self._total = 0
        self._voice = 0
        self._data  = 0
        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet('background:#0a0c0e; color:#e0e0e0;')
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(12)

        self._total_lbl = QLabel('Bursts: 0')
        self._voice_lbl = QLabel('Voice: 0')
        self._data_lbl  = QLabel('Data: 0')
        for lbl in (self._total_lbl, self._voice_lbl, self._data_lbl):
            lbl.setStyleSheet('color:#aaa; font-size:11px;')
            bar.addWidget(lbl)

        bar.addStretch()
        hint = QLabel('NFM mode · 12.5 kHz BW · 4800 baud 4FSK')
        hint.setStyleSheet('color:#666; font-size:10px; font-style:italic;')
        bar.addWidget(hint)
        bar.addStretch()

        clear_btn = _btn('Clear')
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._clear)
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setStyleSheet(_STYLE)
        self._table.setFont(_MONO)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)

        hdr = self._table.horizontalHeader()
        for col in (_C_TIME, _C_TYPE, _C_TS, _C_CALL, _C_EMERG, _C_ERR):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_C_SRC, QHeaderView.Stretch)
        hdr.setSectionResizeMode(_C_DST, QHeaderView.Stretch)

        layout.addWidget(self._table)
        self.resize(960, 560)

    # ── BaseDecoderWindow interface ────────────────────────────────────────────

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return

        category = data.get('category', '')
        sw       = data.get('sync_word', '')
        ts       = str(data.get('timeslot', '?'))
        errs     = data.get('sync_errors', 0)
        lc       = data.get('lc') or {}

        src      = str(lc.get('src', '')) if lc else ''
        dst      = str(lc.get('dst', '')) if lc else ''
        call_t   = lc.get('call_type', '') if lc else ''
        emerg    = bool(lc.get('emergency', False)) if lc else False
        call_str = {'group': 'Group', 'individual': 'Individual'}.get(call_t, '')
        ts_str   = datetime.now().strftime('%H:%M:%S')

        row = self._table.rowCount()
        self._table.insertRow(row)

        center = Qt.AlignCenter | Qt.AlignVCenter

        def _cell(text: str, align=Qt.AlignLeft | Qt.AlignVCenter) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setTextAlignment(align)
            return it

        self._table.setItem(row, _C_TIME,  _cell(ts_str, center))
        self._table.setItem(row, _C_TYPE,  _cell(sw,     center))
        self._table.setItem(row, _C_TS,    _cell(ts,     center))
        self._table.setItem(row, _C_SRC,   _cell(src))
        self._table.setItem(row, _C_DST,   _cell(dst))
        self._table.setItem(row, _C_CALL,  _cell(call_str, center))
        self._table.setItem(row, _C_EMERG, _cell('YES' if emerg else '', center))
        self._table.setItem(row, _C_ERR,   _cell(str(errs) if errs else '', center))

        if emerg:
            bg = _COLOR_EMERG
        elif category == 'voice':
            bg = _COLOR_VOICE
        elif category == 'data':
            bg = _COLOR_DATA
        else:
            bg = _COLOR_DIRECT

        for col in range(len(_HEADERS)):
            item = self._table.item(row, col)
            if item:
                item.setBackground(bg)

        if self._table.rowCount() > _MAX_ROWS:
            self._table.removeRow(0)

        self._table.scrollToBottom()

        self._total += 1
        if category == 'voice':
            self._voice += 1
        elif category == 'data':
            self._data += 1

        self._total_lbl.setText(f'Bursts: {self._total}')
        self._voice_lbl.setText(f'Voice: {self._voice}')
        self._data_lbl.setText(f'Data: {self._data}')

    def _clear(self):
        self._table.setRowCount(0)
        self._total = self._voice = self._data = 0
        self._total_lbl.setText('Bursts: 0')
        self._voice_lbl.setText('Voice: 0')
        self._data_lbl.setText('Data: 0')
