"""
RFI Logger window — displays detected RFI events in a live table.

Receives pre-classified event dicts from RFIClassifier (via main_window).
Works in both normal VFO mode and sweep mode because it is driven by the
spectrum-level SignalAnalyzerProcess, not per-VFO decoders.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QLabel, QSpinBox, QFileDialog,
    QAbstractItemView, QCheckBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

_COLUMNS = [
    ("Time",          80),
    ("Freq (MHz)",    90),
    ("BW (kHz)",      70),
    ("SNR (dB)",      65),
    ("Peak (dBFS)",   80),
    ("~dBm",          60),
    ("~mW",           65),
    ("~mV rms",       70),
    ("~mA rms",       65),
    ("Modulation",    110),
    ("Source / Band", 180),
    ("Risk",          60),
    ("Likely sources",260),
]

_RISK_COLORS = {
    "low":     QColor("#2d6a2d"),   # dark green
    "medium":  QColor("#7a6a1a"),   # dark amber
    "high":    QColor("#7a2020"),   # dark red
    "unknown": QColor("#3a3a3a"),   # grey
}
_RISK_TEXT_COLORS = {
    "low":     QColor("#88ff88"),
    "medium":  QColor("#ffdd66"),
    "high":    QColor("#ff8888"),
    "unknown": QColor("#aaaaaa"),
}

_DEFAULT_MAX_ROWS = 500


class RFILogWindow(QMainWindow):
    """Live RFI event log table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RFI Logger")
        self.resize(1300, 520)
        self._max_rows = _DEFAULT_MAX_ROWS

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)

        self._count_label = QLabel("0 events")
        self._count_label.setStyleSheet("color:#9aa0a6;")
        toolbar.addWidget(self._count_label)
        toolbar.addStretch()

        toolbar.addWidget(QLabel("Max rows:"))
        self._max_rows_spin = QSpinBox()
        self._max_rows_spin.setRange(50, 10000)
        self._max_rows_spin.setValue(_DEFAULT_MAX_ROWS)
        self._max_rows_spin.setToolTip("Maximum number of rows kept in the table")
        self._max_rows_spin.valueChanged.connect(self._on_max_rows_changed)
        toolbar.addWidget(self._max_rows_spin)

        self._autoscroll_check = QCheckBox("Auto-scroll")
        self._autoscroll_check.setChecked(True)
        toolbar.addWidget(self._autoscroll_check)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self.clear)
        toolbar.addWidget(self._clear_btn)

        self._export_btn = QPushButton("Export CSV…")
        self._export_btn.clicked.connect(self._export_csv)
        toolbar.addWidget(self._export_btn)

        layout.addLayout(toolbar)

        # --- Note about estimates ---
        note = QLabel(
            "Power estimates (dBm / mW / mV / mA) are rough. "
            "Set <b>calibration.offset_db</b> in <i>app/rfi/rfi_sources.json</i> to calibrate."
        )
        note.setStyleSheet("color:#888; font-size:11px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        # --- Table ---
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([c[0] for c in _COLUMNS])
        self._table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)

        mono = QFont("Courier New", 9)
        self._table.setFont(mono)

        for i, (_, width) in enumerate(_COLUMNS):
            self._table.setColumnWidth(i, width)

        layout.addWidget(self._table)

        self._apply_style()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def push_event(self, event: dict) -> None:
        """Add one classified RFI event to the table. Call from main thread."""
        self._trim_if_needed()

        row = self._table.rowCount()
        self._table.insertRow(row)

        ts = time.strftime("%H:%M:%S", time.localtime(event.get("timestamp", time.time())))
        freq_mhz = event.get("center_hz", 0) / 1e6
        bw_khz = event.get("bandwidth_hz", 0) / 1e3
        snr = event.get("snr_db", 0.0)
        peak = event.get("peak_db", -120.0)
        dbm = event.get("dbm_est", peak)
        mw = event.get("power_mw_est", 0.0)
        mv = event.get("voltage_mv_est", 0.0)
        ma = event.get("current_ma_est", 0.0)
        mod = event.get("modulation_hint", "?")
        source = event.get("source_name", "Unknown")
        risk = event.get("rfi_risk", "unknown")
        likely = ", ".join(event.get("likely_sources", []))

        values = [
            ts,
            f"{freq_mhz:.4f}",
            f"{bw_khz:.1f}",
            f"{snr:.1f}",
            f"{peak:.1f}",
            f"{dbm:.1f}",
            f"{mw:.4f}" if mw < 1000 else f"{mw:.2f}",
            f"{mv:.3f}",
            f"{ma:.4f}",
            mod,
            source,
            risk.upper(),
            likely,
        ]

        risk_bg = _RISK_COLORS.get(risk, _RISK_COLORS["unknown"])
        risk_fg = _RISK_TEXT_COLORS.get(risk, _RISK_TEXT_COLORS["unknown"])

        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole, event)
            if col == 11:   # Risk column — colour coded
                item.setBackground(risk_bg)
                item.setForeground(risk_fg)
            self._table.setItem(row, col, item)

        self._table.setRowHeight(row, 22)

        count = self._table.rowCount()
        self._count_label.setText(f"{count} event{'s' if count != 1 else ''}")

        if self._autoscroll_check.isChecked():
            self._table.scrollToBottom()

    def clear(self) -> None:
        self._table.setRowCount(0)
        self._count_label.setText("0 events")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _trim_if_needed(self) -> None:
        while self._table.rowCount() >= self._max_rows:
            self._table.removeRow(0)

    def _on_max_rows_changed(self, value: int) -> None:
        self._max_rows = value
        while self._table.rowCount() > self._max_rows:
            self._table.removeRow(0)

    def _export_csv(self) -> None:
        fname, _ = QFileDialog.getSaveFileName(
            self, "Export RFI log as CSV",
            f"rfi_export_{time.strftime('%Y-%m-%d_%H-%M-%S')}.csv",
            "CSV files (*.csv);;All files (*)"
        )
        if not fname:
            return
        try:
            with open(fname, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([c[0] for c in _COLUMNS])
                for row in range(self._table.rowCount()):
                    writer.writerow([
                        self._table.item(row, col).text() if self._table.item(row, col) else ""
                        for col in range(len(_COLUMNS))
                    ])
        except Exception as exc:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export failed", str(exc))

    def _apply_style(self) -> None:
        self.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #1a1a1a;
            color: #ffffff;
        }
        QTableWidget {
            background-color: #1e1e1e;
            alternate-background-color: #252525;
            color: #ffffff;
            gridline-color: #333333;
            border: 1px solid #404040;
            selection-background-color: #0066cc;
            selection-color: #ffffff;
        }
        QHeaderView::section {
            background-color: #252525;
            color: #00ffff;
            border: 1px solid #404040;
            padding: 3px 6px;
        }
        QPushButton {
            background-color: #0066cc;
            color: white;
            border: none;
            padding: 4px 10px;
            border-radius: 3px;
        }
        QPushButton:hover { background-color: #0052a3; }
        QPushButton:pressed { background-color: #003d7a; }
        QSpinBox, QCheckBox, QLabel {
            color: #ffffff;
            background-color: transparent;
        }
        QSpinBox {
            background-color: #252525;
            border: 1px solid #404040;
            padding: 2px 4px;
        }
        QScrollBar:vertical {
            background-color: #252525;
            width: 10px;
        }
        QScrollBar::handle:vertical {
            background-color: #555;
            border-radius: 4px;
        }
        """)
