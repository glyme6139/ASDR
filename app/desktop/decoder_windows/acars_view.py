"""ACARS message viewer — scrolling table of decoded aircraft messages."""

from __future__ import annotations

import time
from datetime import datetime
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
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

_MAX_ROWS   = 200
_MONO       = QFont('Consolas', 10)
_STYLE      = (
    'QTableWidget { background:#131416; color:#e0e0e0; border:1px solid #444; }'
    'QHeaderView::section { background:#202124; color:#f0f0f0; padding:4px; border:1px solid #444; }'
    'QTableWidget::item:selected { background:#34507a; }'
    'QTableWidget::item:alternate { background:#161820; }'
)

_HEADERS = ['Time', 'Registration', 'Label', 'Blk', 'Flight', 'Text']
_COL_TIME = 0
_COL_REG  = 1
_COL_LBL  = 2
_COL_BLK  = 3
_COL_FLT  = 4
_COL_TXT  = 5


class ACARSWindow(BaseDecoderWindow):
    """Floating window that shows a table of decoded ACARS messages."""

    def __init__(self, vfo_id: int):
        super().__init__(f'ACARS — VFO {vfo_id}', vfo_id, 'ACARS')
        self._count = 0
        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet('background:#0f1012; color:#e0e0e0;')
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Toolbar
        bar = QHBoxLayout()
        self._count_lbl = QLabel('Messages: 0')
        self._count_lbl.setStyleSheet('color:#aaa; font-size:11px;')
        bar.addWidget(self._count_lbl)
        bar.addStretch()

        info = QLabel('Tune to an ACARS VHF frequency (e.g. 129.125 / 130.025 / 136.900 MHz) in AM mode')
        info.setStyleSheet('color:#888; font-size:10px; font-style:italic;')
        bar.addWidget(info)
        bar.addStretch()

        clear_btn = QPushButton('Clear')
        clear_btn.setFixedWidth(60)
        clear_btn.setStyleSheet(
            'QPushButton{background:#2a2d31;color:#e0e0e0;border:1px solid #555;border-radius:3px;padding:3px 8px;}'
            'QPushButton:hover{background:#3a3d41;}'
        )
        clear_btn.clicked.connect(self._clear)
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

        # Table
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
        hdr.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_REG,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_LBL,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_BLK,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_FLT,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_TXT,  QHeaderView.Stretch)

        layout.addWidget(self._table)
        self.resize(920, 520)

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return

        ts   = datetime.now().strftime('%H:%M:%S')
        reg  = data.get('registration', '').strip()
        lbl  = data.get('label', '').strip()
        blk  = data.get('block_id', '').strip()
        flt  = data.get('flight', '').strip()
        text = data.get('text', '').strip().replace('\n', ' ')

        row = self._table.rowCount()
        self._table.insertRow(row)

        def _item(s: str, align=Qt.AlignLeft | Qt.AlignVCenter) -> QTableWidgetItem:
            it = QTableWidgetItem(s)
            it.setTextAlignment(align)
            return it

        self._table.setItem(row, _COL_TIME, _item(ts, Qt.AlignCenter | Qt.AlignVCenter))
        self._table.setItem(row, _COL_REG,  _item(reg))
        self._table.setItem(row, _COL_LBL,  _item(lbl, Qt.AlignCenter | Qt.AlignVCenter))
        self._table.setItem(row, _COL_BLK,  _item(blk, Qt.AlignCenter | Qt.AlignVCenter))
        self._table.setItem(row, _COL_FLT,  _item(flt))
        self._table.setItem(row, _COL_TXT,  _item(text))

        # Colour-code by label category
        bg = _label_color(lbl)
        if bg:
            for col in range(len(_HEADERS)):
                item = self._table.item(row, col)
                if item:
                    item.setBackground(bg)

        # Trim if over limit
        if self._table.rowCount() > _MAX_ROWS:
            self._table.removeRow(0)

        self._table.scrollToBottom()
        self._count += 1
        self._count_lbl.setText(f'Messages: {self._count}')

    def _clear(self):
        self._table.setRowCount(0)
        self._count = 0
        self._count_lbl.setText('Messages: 0')


# Map ARINC label prefixes to background colours for visual grouping
from PySide6.QtGui import QColor

_LABEL_COLORS = {
    'H1': QColor('#1a2a1a'),   # free text / OOOI
    '5U': QColor('#1a1a2a'),   # position report
    '_d': QColor('#2a1a1a'),   # departure
    'CF': QColor('#2a2a10'),   # fuel
    'QS': QColor('#2a1a2a'),   # ATIS
    'SA': QColor('#1a2a2a'),   # weather
}


def _label_color(label: str) -> 'QColor | None':
    return _LABEL_COLORS.get(label[:2] if len(label) >= 2 else label)
