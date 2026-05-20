"""Signal Rate window — tracks how often each VFO opens squelch."""

from __future__ import annotations

import time
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_POLL_MS = 250


class SignalRateWindow(QMainWindow):
    """Floating window that tracks how often each VFO opens squelch."""

    visibility_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signal Rate")
        self.setObjectName("SignalRateWindow")
        self.resize(820, 480)

        # Per-VFO squelch state
        self._is_active: dict[int, bool] = {}
        self._went_silent_at: dict[int, float] = {}
        self._events: dict[int, list[float]] = {}   # vfo_id → [unix_timestamp, …]

        self._current_vfo: int | None = None          # None → show All
        self._vfo_names: dict[int, str] = {}

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
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.setCentralWidget(content)

        # --- Header row ---
        header = QHBoxLayout()
        header.setSpacing(6)

        header.addWidget(QLabel("VFO:"))
        self._vfo_combo = QComboBox()
        self._vfo_combo.setMinimumWidth(190)
        self._vfo_combo.currentIndexChanged.connect(self._on_vfo_changed)
        header.addWidget(self._vfo_combo)

        header.addSpacing(12)
        header.addWidget(QLabel("Merge window:"))
        self._merge_spin = QSpinBox()
        self._merge_spin.setRange(0, 60_000)
        self._merge_spin.setSingleStep(50)
        self._merge_spin.setValue(500)
        self._merge_spin.setSuffix(" ms")
        self._merge_spin.setFixedWidth(100)
        self._merge_spin.setToolTip(
            "Silence gaps shorter than this merge the surrounding bursts into one event.\n"
            "Increase for fast repeated bursts; decrease for tightly spaced transmissions."
        )
        header.addWidget(self._merge_spin)

        header.addSpacing(12)
        header.addWidget(QLabel("History:"))
        self._history_spin = QSpinBox()
        self._history_spin.setRange(1, 1440)
        self._history_spin.setValue(60)
        self._history_spin.setSuffix(" min")
        self._history_spin.setFixedWidth(90)
        self._history_spin.setToolTip("How far back the plot shows events.")
        header.addWidget(self._history_spin)

        header.addStretch()
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setFixedWidth(60)
        self._reset_btn.clicked.connect(self._reset)
        header.addWidget(self._reset_btn)

        layout.addLayout(header)

        # --- Scatter plot ---
        self._plot_widget = pg.PlotWidget(background="#111111")
        self._plot = self._plot_widget.getPlotItem()
        self._plot.setLabel("bottom", "Time ago (s)")
        self._plot.setLabel("left", "VFO")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._scatter = pg.ScatterPlotItem(
            size=9, pen=None, brush=pg.mkBrush("#00d4ff")
        )
        self._plot.addItem(self._scatter)
        layout.addWidget(self._plot_widget, stretch=1)

        # --- Stats row ---
        stats = QHBoxLayout()
        stats.setSpacing(24)
        self._lbl_count  = QLabel("Events: 0")
        self._lbl_rate   = QLabel("Rate: —")
        self._lbl_avg    = QLabel("Avg interval: —")
        self._lbl_last   = QLabel("Last event: —")
        for lbl in (self._lbl_count, self._lbl_rate, self._lbl_avg, self._lbl_last):
            stats.addWidget(lbl)
        stats.addStretch()
        layout.addLayout(stats)

    # ------------------------------------------------------------------
    # Public API — called from main_window
    # ------------------------------------------------------------------

    def set_vfo_choices(self, choices: list[tuple[int, str]], active_vfo_id: int | None = None):
        """Update VFO combo. choices = [(vfo_id, display_label), …]"""
        current = self._vfo_combo.currentData()
        self._vfo_combo.blockSignals(True)
        self._vfo_combo.clear()
        self._vfo_combo.addItem("All VFOs", userData=None)
        self._vfo_names.clear()
        for vfo_id, label in choices:
            self._vfo_combo.addItem(label, userData=vfo_id)
            self._vfo_names[vfo_id] = label
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

    def push_squelch_state(self, vfo_id: int, is_active: bool):
        """
        Called once per signal_strength update (≈5 Hz) for every VFO.
        Detects rising edges (squelch opens) and records event timestamps,
        merging closely-spaced bursts according to the merge-window setting.
        """
        was_active = self._is_active.get(vfo_id, False)

        if is_active and not was_active:
            now = time.time()
            merge_s = self._merge_spin.value() / 1000.0
            silent_since = self._went_silent_at.get(vfo_id, 0.0)
            if (now - silent_since) >= merge_s:
                self._events.setdefault(vfo_id, []).append(now)

        if not is_active and was_active:
            self._went_silent_at[vfo_id] = time.time()

        self._is_active[vfo_id] = is_active

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_vfo_changed(self, idx: int):
        self._current_vfo = self._vfo_combo.itemData(idx)

    def _reset(self):
        self._events.clear()
        self._is_active.clear()
        self._went_silent_at.clear()
        self._refresh()

    def _visible_events(self) -> list[tuple[float, int]]:
        """
        Return [(timestamp, vfo_id)] within history window for the selected VFO.
        Prunes stale entries from self._events in-place.
        """
        now = time.time()
        cutoff = now - self._history_spin.value() * 60.0
        result: list[tuple[float, int]] = []
        for vfo_id, ts_list in self._events.items():
            if self._current_vfo is not None and vfo_id != self._current_vfo:
                continue
            pruned = [t for t in ts_list if t >= cutoff]
            self._events[vfo_id] = pruned
            for t in pruned:
                result.append((t, vfo_id))
        result.sort(key=lambda x: x[0])
        return result

    def _refresh(self):
        if not self.isVisible():
            return

        now = time.time()
        events = self._visible_events()

        if not events:
            self._scatter.setData([], [])
            self._lbl_count.setText("Events: 0")
            self._lbl_rate.setText("Rate: 0.00 /min")
            self._lbl_avg.setText("Avg interval: —")
            self._lbl_last.setText("Last event: —")
            self._plot.setXRange(0, self._history_spin.value() * 60.0, padding=0.02)
            self._plot.setYRange(-0.5, 0.5, padding=0.1)
            return

        # Build scatter arrays
        vfo_ids_present = sorted({vid for _, vid in events})
        if self._current_vfo is None:
            vfo_axis = vfo_ids_present
        else:
            vfo_axis = [self._current_vfo]
        vfo_index = {vid: i for i, vid in enumerate(vfo_axis)}

        xs = [now - t for t, _ in events]           # seconds ago → 0 is most recent
        ys = [float(vfo_index.get(vid, 0)) for _, vid in events]
        self._scatter.setData(x=xs, y=ys)

        # X axis: 0 at left (most-recent) is confusing — put 0 at right
        history_s = self._history_spin.value() * 60.0
        self._plot.setXRange(history_s, 0, padding=0.02)

        # Y axis ticks
        if len(vfo_axis) > 1:
            ticks = [(i, self._vfo_names.get(vid, f"VFO {vid+1}")) for i, vid in enumerate(vfo_axis)]
            self._plot.setYRange(-0.5, len(vfo_axis) - 0.5, padding=0.1)
            self._plot.getAxis("left").setTicks([ticks])
            self._plot.setLabel("left", "VFO")
        else:
            single = self._vfo_names.get(vfo_axis[0], f"VFO {vfo_axis[0]+1}") if vfo_axis else "VFO"
            self._plot.setYRange(-0.5, 0.5, padding=0.1)
            self._plot.getAxis("left").setTicks([[(0, "")]])
            self._plot.setLabel("left", single)

        # Stats
        n = len(events)
        hist_min = self._history_spin.value()
        rate = n / hist_min if hist_min > 0 else 0.0

        if n >= 2:
            ts = [t for t, _ in events]
            intervals = [ts[i+1] - ts[i] for i in range(len(ts) - 1)]
            avg_s = sum(intervals) / len(intervals)
            avg_str = f"{avg_s/60:.1f} min" if avg_s >= 60 else f"{avg_s:.1f} s"
        else:
            avg_str = "—"

        since = now - events[-1][0]
        if since < 60:
            last_str = f"{since:.1f} s ago"
        elif since < 3600:
            last_str = f"{since/60:.1f} min ago"
        else:
            last_str = f"{since/3600:.1f} h ago"

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
