"""
Standalone live view for profiling reports.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QMainWindow, QWidget, QVBoxLayout,
)


class _NumericItem(QTableWidgetItem):
    """Sort numerically when text is a valid float, otherwise fall back to string."""
    def __lt__(self, other: QTableWidgetItem) -> bool:
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


class TimingWindow(QMainWindow):
    report_received = Signal(object)
    sample_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Performance Timing")
        self.setObjectName("TimingWindow")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Live timing reports")
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        header.addStretch()
        samples_label = QLabel("Samples:")
        header.addWidget(samples_label)
        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 10_000)
        self.samples_spin.setSingleStep(10)
        self.samples_spin.setValue(100)
        self.samples_spin.setToolTip("Number of samples to average per timing report")
        self.samples_spin.setFixedWidth(80)
        header.addWidget(self.samples_spin)
        header.addSpacing(8)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear all timing rows")
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Source", "Stage", "Avg ms", "Min ms", "Max ms", "Samples", "Time"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        copy_sc.activated.connect(self._copy_selection)

        self.samples_spin.valueChanged.connect(self.sample_count_changed)
        self.clear_btn.clicked.connect(self.clear_reports)
        self.report_received.connect(self.add_report)

        self.setCentralWidget(content)
        self.resize(900, 420)

    def clear_reports(self):
        self.table.setRowCount(0)

    def add_report(self, report: object):
        if not isinstance(report, dict):
            return

        source = str(report.get("source", ""))
        stage = str(report.get("stage", ""))

        # Disable sorting while writing so row indices stay stable
        self.table.setSortingEnabled(False)

        row = self._find_row(source, stage)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)

        texts = [
            source,
            stage,
            f"{float(report.get('mean_ms', 0.0)):.3f}",
            f"{float(report.get('min_ms', 0.0)):.3f}",
            f"{float(report.get('max_ms', 0.0)):.3f}",
            str(int(report.get("samples", 0))),
            self._format_timestamp(float(report.get("timestamp", 0.0))),
        ]

        for col, text in enumerate(texts):
            item = _NumericItem(text) if col in (2, 3, 4, 5) else QTableWidgetItem(text)
            if col in (2, 3, 4, 5):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, col, item)

        self.table.setSortingEnabled(True)

    def _find_row(self, source: str, stage: str) -> int | None:
        for item in self.table.findItems(source, Qt.MatchExactly):
            if item.column() == 0:
                row = item.row()
                stage_item = self.table.item(row, 1)
                if stage_item and stage_item.text() == stage:
                    return row
        return None

    def _copy_selection(self):
        selected_rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not selected_rows:
            return
        lines = []
        for row in selected_rows:
            cols = [
                (self.table.item(row, col) or QTableWidgetItem()).text()
                for col in range(self.table.columnCount())
            ]
            lines.append("\t".join(cols))
        QApplication.clipboard().setText("\n".join(lines))

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        if timestamp <= 0:
            return ""
        import time
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
