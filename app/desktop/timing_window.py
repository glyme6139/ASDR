"""
Standalone live view for profiling reports.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QColor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QMainWindow, QWidget, QVBoxLayout,
)

import collections
import time
import pyqtgraph as pg


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

    _PLOT_WINDOW_S = 120  # rolling window width in seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Performance Timing")
        self.setObjectName("TimingWindow")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # ── Header ──────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Live timing reports")
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        header.addStretch()

        self.dsp_health_label = QLabel("DSP: waiting for timing data")
        self.dsp_health_label.setStyleSheet("color: #aaaaaa; font-weight: 600;")
        header.addWidget(self.dsp_health_label)
        header.addSpacing(12)

        self.signal_id_label = QLabel("Signal ID: idle")
        self.signal_id_label.setStyleSheet("color: #aaaaaa; font-weight: 600;")
        header.addWidget(self.signal_id_label)
        header.addSpacing(12)

        samples_label = QLabel("Samples:")
        header.addWidget(samples_label)
        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(10, 10_000)
        self.samples_spin.setSingleStep(10)
        self.samples_spin.setValue(10)
        self.samples_spin.setToolTip("Number of samples to average per timing report")
        self.samples_spin.setFixedWidth(80)
        header.addWidget(self.samples_spin)
        header.addSpacing(8)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear all timing rows")
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        # ── Table controls ───────────────────────────────────────────────────
        table_controls = QHBoxLayout()
        table_controls.setContentsMargins(0, 0, 0, 0)
        self._show_all_btn = QPushButton("Show All")
        self._show_all_btn.setFixedWidth(80)
        self._show_all_btn.clicked.connect(self._show_all)
        table_controls.addWidget(self._show_all_btn)
        self._hide_all_btn = QPushButton("Hide All")
        self._hide_all_btn.setFixedWidth(80)
        self._hide_all_btn.clicked.connect(self._hide_all)
        table_controls.addWidget(self._hide_all_btn)
        table_controls.addStretch()
        layout.addLayout(table_controls)

        # ── Table ────────────────────────────────────────────────────────────
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["", "Source", "Stage", "Avg ms", "Min ms", "Max ms", "Samples", "Time"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.setMaximumHeight(200)
        self.table.setColumnWidth(0, 24)
        self.table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self.table)

        # ── Plot ─────────────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground('#111111')
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel('left', 'Latency', units='ms')
        self._plot.setLabel('bottom', 'Elapsed', units='s')
        # Rolling window: pin the right edge at now (x=0), scroll left
        self._plot.setXRange(-self._PLOT_WINDOW_S, 0, padding=0.02)
        self._plot.enableAutoRange(axis='x', enable=False)
        self._plot.enableAutoRange(axis='y', enable=True)

        self._legend = self._plot.addLegend(
            offset=(10, 10),
            labelTextSize='8pt',
            labelTextColor='#dddddd',
        )
        self._legend.setBrush(pg.mkBrush(color=(20, 20, 20, 180)))
        self._legend.setPen(pg.mkPen(color='#444444'))

        self._plot_curves: dict[str, pg.PlotDataItem] = {}
        self._plot_upper: dict[str, pg.PlotDataItem] = {}
        self._plot_lower: dict[str, pg.PlotDataItem] = {}
        self._plot_fills: dict[str, pg.FillBetweenItem] = {}
        self._plot_colors = [
            '#00ffff', '#ffd166', '#7dd3fc', '#ff7b7b', '#9ad3bc', '#c89bff', '#ffb86b', '#88b3ff'
        ]
        layout.addWidget(self._plot, stretch=1)

        # ── Wiring ───────────────────────────────────────────────────────────
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        copy_sc.activated.connect(self._copy_selection)

        self.samples_spin.valueChanged.connect(self.sample_count_changed)
        self.clear_btn.clicked.connect(self.clear_reports)
        self.report_received.connect(self.add_report)

        self._reports: dict[tuple[str, str], dict] = {}
        # histories: (timestamp, mean_ms, min_ms, max_ms)
        self._histories: dict[str, collections.deque] = {}
        self._key_labels: dict[str, tuple[str, str]] = {}
        self._key_colors: dict[str, str] = {}
        self._visible_keys: set[str] = set()
        self._dsp_sample_rate_hz: float | None = None
        self._dsp_block_size: int = 4096

        self.setCentralWidget(content)
        self.resize(1100, 700)

    def clear_reports(self):
        self.table.setRowCount(0)
        self._reports.clear()
        for key in list(self._plot_curves):
            self._remove_plot_key(key)
        self._histories.clear()
        self._key_colors.clear()
        self._key_labels.clear()
        self._visible_keys.clear()
        self._refresh_summary()

    def _remove_plot_key(self, key: str):
        for d in (self._plot_fills, self._plot_upper, self._plot_lower):
            item = d.pop(key, None)
            if item is not None:
                try:
                    self._plot.removeItem(item)
                except Exception:
                    pass
        curve = self._plot_curves.pop(key, None)
        if curve is not None:
            label = key.replace('||', ' — ')
            try:
                self._legend.removeItem(label)
            except Exception:
                pass
            try:
                self._plot.removeItem(curve)
            except Exception:
                pass

    def set_dsp_context(self, sample_rate: float | None = None, block_size: int = 4096):
        self._dsp_sample_rate_hz = sample_rate
        self._dsp_block_size = int(block_size)
        self._refresh_summary()

    def add_report(self, report: object):
        if not isinstance(report, dict):
            return

        source = str(report.get("source", ""))
        stage = str(report.get("stage", ""))

        self.table.setSortingEnabled(False)
        row = self._find_row(source, stage)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)

        mean_ms = float(report.get('mean_ms', 0.0))
        min_ms = float(report.get('min_ms', mean_ms))
        max_ms = float(report.get('max_ms', mean_ms))

        texts = [
            source,
            stage,
            f"{mean_ms:.3f}",
            f"{min_ms:.3f}",
            f"{max_ms:.3f}",
            str(int(report.get("samples", 0))),
            self._format_timestamp(float(report.get("timestamp", 0.0))),
        ]

        self.table.blockSignals(True)
        for i, text in enumerate(texts):
            col_idx = i + 1
            item = _NumericItem(text) if col_idx in (3, 4, 5, 6) else QTableWidgetItem(text)
            if col_idx in (3, 4, 5, 6):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, col_idx, item)
        self.table.blockSignals(False)

        self.table.setSortingEnabled(True)
        self._reports[(source, stage)] = report

        try:
            ts = float(report.get('timestamp', 0.0))
        except Exception:
            ts = 0.0
        if ts <= 0.0:
            ts = time.time()

        key = f"{source}||{stage}"
        if key not in self._histories:
            self._histories[key] = collections.deque(maxlen=400)
            self._key_labels[key] = (source, stage)
            color = self._plot_colors[len(self._key_colors) % len(self._plot_colors)]
            self._key_colors[key] = color
            cb = QTableWidgetItem()
            cb.setFlags(cb.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Checked)
            cb.setBackground(QColor(color))
            cb.setToolTip(f"{source} — {stage}")
            self.table.setItem(row, 0, cb)
            self._visible_keys.add(key)

        self._histories[key].append((ts, mean_ms, min_ms, max_ms))
        if key in self._visible_keys:
            self._update_plot()
        self._refresh_summary()

    def _find_row(self, source: str, stage: str) -> int | None:
        for item in self.table.findItems(source, Qt.MatchExactly):
            if item.column() == 1:
                row = item.row()
                stage_item = self.table.item(row, 2)
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

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        row = item.row()
        src_item = self.table.item(row, 1)
        stage_item = self.table.item(row, 2)
        if not src_item or not stage_item:
            return
        key = f"{src_item.text()}||{stage_item.text()}"
        if item.checkState() == Qt.Checked:
            self._visible_keys.add(key)
        else:
            self._visible_keys.discard(key)
        self._update_plot()

    def _show_all(self):
        for row in range(self.table.rowCount()):
            itm = self.table.item(row, 0)
            if itm:
                itm.setCheckState(Qt.Checked)

    def _hide_all(self):
        for row in range(self.table.rowCount()):
            itm = self.table.item(row, 0)
            if itm:
                itm.setCheckState(Qt.Unchecked)

    def _update_plot(self):
        now = time.time()
        window_start = now - self._PLOT_WINDOW_S

        # Remove curves for hidden keys
        for key in list(self._plot_curves):
            if key not in self._visible_keys:
                self._remove_plot_key(key)

        # Add or update a curve per visible key
        for key in sorted(self._visible_keys):
            hist = list(self._histories.get(key, []))
            if not hist:
                continue

            hist_win = [(t, m, mn, mx) for t, m, mn, mx in hist if t >= window_start]
            if not hist_win:
                hist_win = hist[-1:]  # always show at least the latest point

            x = [t - now for t, _, _, _ in hist_win]
            ys_mean = [m for _, m, _, _ in hist_win]
            ys_min = [mn for _, _, mn, _ in hist_win]
            ys_max = [mx for _, _, _, mx in hist_win]

            color = self._key_colors.get(key, '#ffffff')
            label = key.replace('||', ' — ')
            fill_brush = pg.mkBrush(QColor(color).darker(100))
            fill_color = QColor(color)
            fill_color.setAlpha(35)
            fill_brush = pg.mkBrush(fill_color)

            if key not in self._plot_curves:
                upper = self._plot.plot(x, ys_max, pen=None)
                lower = self._plot.plot(x, ys_min, pen=None)
                fill = pg.FillBetweenItem(upper, lower, brush=fill_brush)
                self._plot.addItem(fill)
                mean_curve = self._plot.plot(
                    x, ys_mean,
                    pen=pg.mkPen(color=color, width=1.8),
                    name=label,
                )
                self._plot_upper[key] = upper
                self._plot_lower[key] = lower
                self._plot_fills[key] = fill
                self._plot_curves[key] = mean_curve
            else:
                self._plot_upper[key].setData(x, ys_max)
                self._plot_lower[key].setData(x, ys_min)
                self._plot_curves[key].setData(x, ys_mean)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _refresh_summary(self):
        dsp_report = self._reports.get(("DSP", "DSP / block total"))
        if dsp_report and self._dsp_sample_rate_hz:
            budget_ms = (self._dsp_block_size / float(self._dsp_sample_rate_hz)) * 1000.0
            mean_ms = float(dsp_report.get("mean_ms", 0.0))
            headroom_ms = budget_ms - mean_ms
            if headroom_ms >= 0:
                self.dsp_health_label.setText(
                    f"DSP: OK {mean_ms:.2f} / {budget_ms:.2f} ms ({headroom_ms:.2f} ms headroom)"
                )
                self.dsp_health_label.setStyleSheet("color: #00cc44; font-weight: 600;")
            else:
                self.dsp_health_label.setText(
                    f"DSP: OVER {mean_ms:.2f} / {budget_ms:.2f} ms ({-headroom_ms:.2f} ms slow)"
                )
                self.dsp_health_label.setStyleSheet("color: #cc2222; font-weight: 600;")
        else:
            self.dsp_health_label.setText("DSP: waiting for timing data")
            self.dsp_health_label.setStyleSheet("color: #aaaaaa; font-weight: 600;")

        sig_report = None
        for key in (
            ("UI / Signal ID", "Signal ID / frequency search"),
            ("UI / Signal ID", "Signal ID / filter search"),
            ("UI / Signal ID", "Signal ID / keyword search"),
            ("UI / Signal ID", "Signal ID / load database"),
        ):
            sig_report = self._reports.get(key)
            if sig_report:
                break
        if sig_report:
            self.signal_id_label.setText(
                f"Signal ID: {float(sig_report.get('mean_ms', 0.0)):.2f} ms avg"
            )
            self.signal_id_label.setStyleSheet("color: #7dd3fc; font-weight: 600;")
        else:
            self.signal_id_label.setText("Signal ID: idle")
            self.signal_id_label.setStyleSheet("color: #aaaaaa; font-weight: 600;")

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        if timestamp <= 0:
            return ""
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
