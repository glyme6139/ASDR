"""Signal Rate window — signal strength history with a draggable event threshold."""

from __future__ import annotations

import time
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_POLL_MS     = 100                         # 10 Hz refresh — smooth enough for 50 Hz data
_HISTORY_HZ  = 50                         # signal_strength_fast rate
_MAX_SAMPLES = 120 * 60 * _HISTORY_HZ     # ring-buffer cap per VFO (~120 min at 50 Hz)

_VFO_COLORS = [
    '#00ffff', '#ffff00', '#00ff00', '#ff00ff',
    '#ff8800', '#ff4444', '#8888ff', '#00ff88',
]


class SignalRateWindow(QMainWindow):
    """Floating window showing signal strength history with a draggable threshold."""

    visibility_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signal Rate")
        self.setObjectName("SignalRateWindow")
        self.resize(920, 520)

        # {vfo_id: deque([(unix_t, db), ...])}
        self._db_history: dict[int, deque] = {}
        self._current_vfo: int | None = None
        self._vfo_names:   dict[int, str] = {}
        self._vfo_colors:  dict[int, str] = {}

        self._threshold_db: float = -60.0

        self._curves:         dict[int, pg.PlotDataItem]    = {}
        self._event_scatters: dict[int, pg.ScatterPlotItem] = {}

        self._initUI()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        content = QWidget(self)
        layout  = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.setCentralWidget(content)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(6)

        header.addWidget(QLabel("VFO:"))
        self._vfo_combo = QComboBox()
        self._vfo_combo.setMinimumWidth(190)
        self._vfo_combo.currentIndexChanged.connect(self._on_vfo_changed)
        header.addWidget(self._vfo_combo)

        header.addSpacing(12)
        header.addWidget(QLabel("Merge:"))
        self._merge_spin = QSpinBox()
        self._merge_spin.setRange(0, 60_000)
        self._merge_spin.setSingleStep(50)
        self._merge_spin.setValue(500)
        self._merge_spin.setSuffix(" ms")
        self._merge_spin.setFixedWidth(100)
        self._merge_spin.setToolTip(
            "Silence gaps shorter than this merge surrounding bursts into one event."
        )
        header.addWidget(self._merge_spin)

        header.addSpacing(12)
        header.addWidget(QLabel("History:"))
        self._history_spin = QSpinBox()
        self._history_spin.setRange(1, 1440)
        self._history_spin.setValue(60)
        self._history_spin.setSuffix(" min")
        self._history_spin.setFixedWidth(90)
        self._history_spin.setToolTip("How far back the plot shows.")
        header.addWidget(self._history_spin)

        header.addSpacing(12)
        header.addWidget(QLabel("Threshold:"))
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(-140.0, 0.0)
        self._threshold_spin.setValue(self._threshold_db)
        self._threshold_spin.setSuffix(" dB")
        self._threshold_spin.setSingleStep(1.0)
        self._threshold_spin.setDecimals(1)
        self._threshold_spin.setFixedWidth(100)
        self._threshold_spin.setToolTip("Drag the red dashed line on the plot, or type here.")
        self._threshold_spin.valueChanged.connect(self._on_threshold_spin_changed)
        header.addWidget(self._threshold_spin)

        header.addStretch()
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setFixedWidth(60)
        self._reset_btn.clicked.connect(self._reset)
        header.addWidget(self._reset_btn)

        layout.addLayout(header)

        # Signal strength plot
        self._plot_widget = pg.PlotWidget(background="#111111")
        self._plot = self._plot_widget.getPlotItem()
        self._plot.setLabel("bottom", "Time ago (s)")
        self._plot.setLabel("left", "Signal (dB)")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setMenuEnabled(False)
        self._plot.getViewBox().setMouseEnabled(x=True, y=False)

        self._threshold_line = pg.InfiniteLine(
            pos=self._threshold_db,
            angle=0,
            movable=True,
            pen=pg.mkPen(color='#ff4444', width=2, style=Qt.DashLine),
            label='Threshold: {value:.1f} dB',
            labelOpts={
                'position': 0.02,
                'color': '#ff6666',
                'fill': pg.mkBrush('#1a1a1acc'),
            },
        )
        self._threshold_line.sigPositionChanged.connect(self._on_threshold_line_moved)
        self._plot.addItem(self._threshold_line)

        layout.addWidget(self._plot_widget, stretch=1)

        # Stats row
        stats = QHBoxLayout()
        stats.setSpacing(24)
        self._lbl_count = QLabel("Events: 0")
        self._lbl_rate  = QLabel("Rate: —")
        self._lbl_avg   = QLabel("Avg interval: —")
        self._lbl_last  = QLabel("Last event: —")
        for lbl in (self._lbl_count, self._lbl_rate, self._lbl_avg, self._lbl_last):
            stats.addWidget(lbl)
        stats.addStretch()
        layout.addLayout(stats)

    # ------------------------------------------------------------------
    # Public API — called from main_window
    # ------------------------------------------------------------------

    def set_vfo_choices(self, choices: list[tuple[int, str]], active_vfo_id: int | None = None):
        current = self._vfo_combo.currentData()
        self._vfo_combo.blockSignals(True)
        self._vfo_combo.clear()
        self._vfo_combo.addItem("All VFOs", userData=None)
        self._vfo_names.clear()
        for i, (vfo_id, label) in enumerate(choices):
            self._vfo_combo.addItem(label, userData=vfo_id)
            self._vfo_names[vfo_id]  = label
            self._vfo_colors[vfo_id] = _VFO_COLORS[i % len(_VFO_COLORS)]
        restored = False
        if current is not None:
            for i in range(self._vfo_combo.count()):
                if self._vfo_combo.itemData(i) == current:
                    self._vfo_combo.setCurrentIndex(i)
                    restored = True
                    break
        if not restored:
            self._vfo_combo.setCurrentIndex(0)
        self._vfo_combo.blockSignals(False)
        self._on_vfo_changed(self._vfo_combo.currentIndex())

    def push_signal_strength(self, vfo_id: int, db: float):
        """Record a signal strength sample (~5 Hz)."""
        hist = self._db_history.setdefault(vfo_id, deque(maxlen=_MAX_SAMPLES))
        hist.append((time.time(), float(db)))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _visible_vfo_ids(self) -> list[int]:
        if self._current_vfo is not None:
            return [self._current_vfo] if self._current_vfo in self._db_history else []
        return sorted(self._db_history.keys())

    def _rebuild_curves(self):
        for c in self._curves.values():
            self._plot.removeItem(c)
        for s in self._event_scatters.values():
            self._plot.removeItem(s)
        self._curves.clear()
        self._event_scatters.clear()

        for vfo_id in self._visible_vfo_ids():
            color = self._vfo_colors.get(vfo_id, '#00ffff')
            self._curves[vfo_id] = self._plot.plot(
                pen=pg.mkPen(color=color, width=1),
                name=self._vfo_names.get(vfo_id, f"VFO {vfo_id + 1}"),
            )
            scatter = pg.ScatterPlotItem(size=8, pen=pg.mkPen(None))
            self._plot.addItem(scatter)
            self._event_scatters[vfo_id] = scatter

    def _compute_events(self, vfo_id: int, cutoff: float) -> list[float]:
        """Rising-edge timestamps where dB crosses above the threshold."""
        history = self._db_history.get(vfo_id)
        if not history:
            return []
        threshold  = self._threshold_db
        merge_s    = self._merge_spin.value() / 1000.0
        events: list[float] = []
        was_above  = False
        last_close = 0.0
        for t, db in history:
            if t < cutoff:
                continue
            above = db >= threshold
            if above and not was_above:
                if (t - last_close) >= merge_s:
                    events.append(t)
            elif not above and was_above:
                last_close = t
            was_above = above
        return events

    def _on_vfo_changed(self, idx: int):
        self._current_vfo = self._vfo_combo.itemData(idx)
        self._rebuild_curves()

    def _on_threshold_line_moved(self, line: pg.InfiniteLine):
        val = float(line.value())
        self._threshold_db = val
        self._threshold_spin.blockSignals(True)
        self._threshold_spin.setValue(val)
        self._threshold_spin.blockSignals(False)

    def _on_threshold_spin_changed(self, val: float):
        self._threshold_db = val
        self._threshold_line.blockSignals(True)
        self._threshold_line.setValue(val)
        self._threshold_line.blockSignals(False)

    def _reset(self):
        self._db_history.clear()
        self._rebuild_curves()
        self._refresh()

    def _refresh(self):
        if not self.isVisible():
            return

        now       = time.time()
        history_s = self._history_spin.value() * 60.0
        cutoff    = now - history_s

        vfo_ids = self._visible_vfo_ids()

        if set(vfo_ids) != set(self._curves.keys()):
            self._rebuild_curves()
            vfo_ids = self._visible_vfo_ids()

        all_db:     list[float] = []
        all_events: list[float] = []

        for vfo_id in vfo_ids:
            hist  = self._db_history.get(vfo_id, deque())
            pts   = [(t, db) for t, db in hist if t >= cutoff]
            color = self._vfo_colors.get(vfo_id, '#00ffff')

            if pts:
                xs = [now - t for t, _ in pts]
                ys = [db for _, db in pts]
                all_db.extend(ys)
                self._curves[vfo_id].setData(x=xs, y=ys)
            else:
                self._curves[vfo_id].setData([], [])

            events = self._compute_events(vfo_id, cutoff)
            all_events.extend(events)
            scatter = self._event_scatters.get(vfo_id)
            if scatter:
                if events:
                    scatter.setData(
                        x=[now - t for t in events],
                        y=[self._threshold_db] * len(events),
                        brush=pg.mkBrush(color),
                    )
                else:
                    scatter.setData([], [])

        # X: oldest on left, newest (0 s ago) on right
        self._plot.setXRange(history_s, 0, padding=0.02)

        # Y: auto-fit data, always keep threshold line in view
        if all_db:
            lo = min(min(all_db), self._threshold_db) - 5
            hi = max(max(all_db), self._threshold_db) + 5
            self._plot.setYRange(lo, hi, padding=0)

        # Stats
        all_events.sort()
        n        = len(all_events)
        hist_min = self._history_spin.value()
        rate     = n / hist_min if hist_min > 0 else 0.0

        if n >= 2:
            intervals = [all_events[i + 1] - all_events[i] for i in range(n - 1)]
            avg_s     = sum(intervals) / len(intervals)
            avg_str   = f"{avg_s / 60:.1f} min" if avg_s >= 60 else f"{avg_s:.1f} s"
        else:
            avg_str = "—"

        if all_events:
            since = now - all_events[-1]
            if since < 60:
                last_str = f"{since:.1f} s ago"
            elif since < 3600:
                last_str = f"{since / 60:.1f} min ago"
            else:
                last_str = f"{since / 3600:.1f} h ago"
        else:
            last_str = "—"

        self._lbl_count.setText(f"Events: {n}")
        self._lbl_rate.setText(f"Rate: {rate:.2f} /min")
        self._lbl_avg.setText(f"Avg interval: {avg_str}")
        self._lbl_last.setText(f"Last event: {last_str}")

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit(False)
