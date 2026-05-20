"""Triggered oscilloscope window fed by VFO audio samples."""

from __future__ import annotations

import time
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_BUFFER_SECONDS = 30

_PERSIST_COLORMAP = pg.ColorMap(
    [0.0, 0.06, 0.45, 1.0],
    [
        (11,  11,  11,  255),
        (0,   55,  70,  255),
        (0,   200, 230, 255),
        (240, 255, 255, 255),
    ],
)

_Y_MIN = -1.6
_Y_MAX = 1.6
_N_BINS_Y = 300


class OscilloscopeWindow(QMainWindow):
    """Floating triggered oscilloscope. Mirrors the eye-diagram window interface."""

    visibility_changed = Signal(bool)
    vfo_changed = Signal(object)

    _MIN_REFRESH_S = 0.066   # ~15 fps
    _DEFAULT_SR = 48_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Oscilloscope")
        self.setObjectName("OscilloscopeWindow")
        self.resize(960, 560)

        self._buffers: dict[int, np.ndarray] = {}
        self._sample_rate = float(self._DEFAULT_SR)
        self._max_buffer = int(_BUFFER_SECONDS * self._sample_rate)
        self._current_vfo: int | None = None
        self._last_refresh = 0.0
        self._persist_mode = False

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # ── Header ───────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        self._vfo_label = QLabel("VFO: --")
        self._vfo_label.setStyleSheet("font-weight: 600;")
        header.addWidget(self._vfo_label)
        header.addSpacing(10)

        header.addWidget(QLabel("Trace:"))
        self._vfo_combo = QComboBox()
        self._vfo_combo.currentIndexChanged.connect(self._on_vfo_combo_changed)
        header.addWidget(self._vfo_combo, stretch=1)
        header.addStretch()

        header.addWidget(QLabel("Window:"))
        self._window_spin = QSpinBox()
        self._window_spin.setRange(64, 8192)
        self._window_spin.setSingleStep(64)
        self._window_spin.setValue(512)
        self._window_spin.setFixedWidth(72)
        self._window_spin.setToolTip("Sweep length in samples")
        self._window_spin.valueChanged.connect(self._on_settings_changed)
        header.addWidget(self._window_spin)

        header.addSpacing(8)
        header.addWidget(QLabel("Pre-trig %:"))
        self._pretrig_spin = QSpinBox()
        self._pretrig_spin.setRange(5, 90)
        self._pretrig_spin.setSingleStep(5)
        self._pretrig_spin.setValue(20)
        self._pretrig_spin.setFixedWidth(50)
        self._pretrig_spin.setToolTip("Percentage of window shown before the trigger event")
        self._pretrig_spin.valueChanged.connect(self._on_settings_changed)
        header.addWidget(self._pretrig_spin)

        header.addSpacing(8)
        header.addWidget(QLabel("Level:"))
        self._level_spin = QDoubleSpinBox()
        self._level_spin.setRange(-2.0, 2.0)
        self._level_spin.setSingleStep(0.05)
        self._level_spin.setValue(0.0)
        self._level_spin.setDecimals(2)
        self._level_spin.setFixedWidth(68)
        self._level_spin.setToolTip("Trigger threshold (normalized amplitude)")
        self._level_spin.valueChanged.connect(self._on_level_spin_changed)
        header.addWidget(self._level_spin)

        header.addSpacing(8)
        header.addWidget(QLabel("Edge:"))
        self._edge_combo = QComboBox()
        self._edge_combo.addItems(["Rising", "Falling", "Either"])
        self._edge_combo.setFixedWidth(72)
        self._edge_combo.currentIndexChanged.connect(self._on_settings_changed)
        header.addWidget(self._edge_combo)

        header.addSpacing(8)
        header.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Auto", "Normal"])
        self._mode_combo.setFixedWidth(68)
        self._mode_combo.setToolTip(
            "Auto: free-run when no trigger found. Normal: wait for trigger."
        )
        self._mode_combo.currentIndexChanged.connect(self._on_settings_changed)
        header.addWidget(self._mode_combo)

        header.addSpacing(8)
        self._persist_btn = QPushButton("Persist Off")
        self._persist_btn.setFixedWidth(84)
        self._persist_btn.setCheckable(True)
        self._persist_btn.setToolTip("Toggle persistence (phosphor) mode")
        self._persist_btn.clicked.connect(self._on_persist_toggled)
        header.addWidget(self._persist_btn)

        layout.addLayout(header)

        # ── Scrub row ────────────────────────────────────────────────────────
        scrub_row = QHBoxLayout()
        scrub_row.setContentsMargins(0, 0, 0, 0)
        scrub_row.setSpacing(6)

        self._scrub_slider = QSlider(Qt.Orientation.Horizontal)
        self._scrub_slider.setRange(0, 10000)
        self._scrub_slider.setValue(10000)
        self._scrub_slider.setToolTip("Scroll through 30-second buffer (rightmost = live)")
        self._scrub_slider.valueChanged.connect(self._on_scrub_changed)
        scrub_row.addWidget(self._scrub_slider, stretch=1)

        self._scrub_label = QLabel("● Live")
        self._scrub_label.setStyleSheet("color: #7dd3fc; font-weight: 600; min-width: 70px;")
        self._scrub_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scrub_row.addWidget(self._scrub_label)

        layout.addLayout(scrub_row)

        # ── Plot ─────────────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground("#111111")
        self._plot.showGrid(x=True, y=True, alpha=0.18)
        self._plot.setLabel("left", "Amplitude (norm.)")
        self._plot.setLabel("bottom", "Time", units="ms")
        self._plot.setYRange(_Y_MIN, _Y_MAX, padding=0)

        # Persistence heatmap layer (only visible in persist mode)
        self._image = pg.ImageItem()
        self._image.setColorMap(_PERSIST_COLORMAP)
        self._image.setLevels((0.0, 1.0))
        self._image.setZValue(-10)
        self._image.setVisible(False)
        self._plot.addItem(self._image)

        # Single-sweep curve
        self._curve = pg.PlotCurveItem(pen=pg.mkPen(color="#00ffcc", width=1.5))
        self._plot.addItem(self._curve)

        # Trigger level: horizontal dashed line — draggable
        self._trig_line = pg.InfiniteLine(
            pos=0.0, angle=0,
            pen=pg.mkPen(color=(255, 180, 0, 180), width=1, style=Qt.DashLine),
            movable=True,
            label="{value:.2f}",
            labelOpts={"color": "#ffb400", "position": 0.05, "fill": (11, 11, 11, 160)},
        )
        self._trig_line.sigPositionChangeFinished.connect(self._on_trig_line_dragged)
        self._plot.addItem(self._trig_line)

        # Pre-trigger marker: vertical dashed line at x=0
        self._pretrig_line = pg.InfiniteLine(
            pos=0.0, angle=90,
            pen=pg.mkPen(color=(120, 120, 255, 120), width=1, style=Qt.DashLine),
        )
        self._plot.addItem(self._pretrig_line)

        layout.addWidget(self._plot)

        # ── Stats row ────────────────────────────────────────────────────────
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_label = QLabel("Waiting for samples…")
        self._stats_label.setStyleSheet("color: #aaaaaa;")
        stats_row.addWidget(self._stats_label)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        self.setCentralWidget(content)
        self._update_x_range()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_sample_rate(self, sample_rate: float) -> None:
        if sample_rate > 0:
            self._sample_rate = float(sample_rate)
            self._max_buffer = int(_BUFFER_SECONDS * self._sample_rate)
            self._update_x_range()

    def set_vfo_choices(self, choices: list[tuple[int, str]], active_vfo_id: int | None = None) -> None:
        current_id = self._current_vfo if self._current_vfo is not None else active_vfo_id
        self._vfo_combo.blockSignals(True)
        self._vfo_combo.clear()
        for vfo_id, label in choices:
            self._vfo_combo.addItem(label, int(vfo_id))
        self._vfo_combo.blockSignals(False)

        if choices:
            target_id = current_id if current_id is not None else active_vfo_id
            idx = 0
            if target_id is not None:
                for i in range(self._vfo_combo.count()):
                    if int(self._vfo_combo.itemData(i)) == int(target_id):
                        idx = i
                        break
            self._vfo_combo.setCurrentIndex(idx)
            self._set_current_vfo(int(self._vfo_combo.currentData()))
        else:
            self._current_vfo = None
            self._vfo_label.setText("VFO: --")
            self._buffers.clear()
            self._refresh_plot()

    def set_active_vfo(self, vfo_id: int | None) -> None:
        if vfo_id is None:
            return
        for i in range(self._vfo_combo.count()):
            if int(self._vfo_combo.itemData(i)) == int(vfo_id):
                self._vfo_combo.blockSignals(True)
                self._vfo_combo.setCurrentIndex(i)
                self._vfo_combo.blockSignals(False)
                self._set_current_vfo(vfo_id)
                return

    def push_samples(self, vfo_id: int, samples) -> None:
        """Append new audio samples. Refresh is rate-limited to ~15 fps."""
        try:
            chunk = np.asarray(samples, dtype=np.float32)
        except Exception:
            return
        if chunk.size == 0:
            return

        vfo_id = int(vfo_id)
        buf = self._buffers.get(vfo_id, np.zeros(0, dtype=np.float32))
        buf = np.concatenate((buf, chunk))
        if buf.size > self._max_buffer:
            buf = buf[-self._max_buffer:]
        self._buffers[vfo_id] = buf

        if self._current_vfo is None:
            self._set_current_vfo(vfo_id)

        if self._current_vfo == vfo_id and self._is_live():
            now = time.monotonic()
            if now - self._last_refresh >= self._MIN_REFRESH_S:
                self._last_refresh = now
                self._refresh_plot()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _set_current_vfo(self, vfo_id: int | None) -> None:
        if vfo_id is None:
            return
        self._current_vfo = int(vfo_id)
        self._vfo_label.setText(f"VFO: {self._current_vfo + 1}")
        self._refresh_plot()
        self.vfo_changed.emit(self._current_vfo)

    def _on_vfo_combo_changed(self, *_args):
        data = self._vfo_combo.currentData()
        if data is not None:
            self._set_current_vfo(int(data))

    def _on_settings_changed(self, *_args):
        self._update_x_range()
        self._refresh_plot()

    def _on_level_spin_changed(self, value: float):
        self._trig_line.blockSignals(True)
        self._trig_line.setValue(value)
        self._trig_line.blockSignals(False)
        self._refresh_plot()

    def _on_trig_line_dragged(self):
        """Sync the dragged trigger line back to the level spinbox."""
        val = round(float(self._trig_line.value()), 2)
        self._level_spin.blockSignals(True)
        self._level_spin.setValue(val)
        self._level_spin.blockSignals(False)
        self._refresh_plot()

    def _on_persist_toggled(self, checked: bool):
        self._persist_mode = checked
        self._persist_btn.setText("Persist On" if checked else "Persist Off")
        self._image.setVisible(checked)
        self._curve.setVisible(not checked)
        self._refresh_plot()

    def _update_x_range(self):
        """Recompute the time-axis range and update plot + image rect."""
        window = int(self._window_spin.value())
        pretrig_frac = int(self._pretrig_spin.value()) / 100.0
        ms_per_sample = 1000.0 / self._sample_rate
        x_start = -pretrig_frac * window * ms_per_sample
        x_end = (1.0 - pretrig_frac) * window * ms_per_sample
        self._plot.setXRange(x_start, x_end, padding=0.02)
        self._image.setRect(QRectF(x_start, _Y_MIN, x_end - x_start, _Y_MAX - _Y_MIN))

    def _current_buffer(self) -> np.ndarray:
        if self._current_vfo is None:
            return np.zeros(0, dtype=np.float32)
        return self._buffers.get(self._current_vfo, np.zeros(0, dtype=np.float32))

    def _is_live(self) -> bool:
        return self._scrub_slider.value() >= self._scrub_slider.maximum()

    def _get_analysis_buffer(self) -> np.ndarray:
        buf = self._current_buffer()
        if self._is_live() or buf.size == 0:
            return buf
        frac = self._scrub_slider.value() / self._scrub_slider.maximum()
        end_idx = max(1, int(round(frac * buf.size)))
        return buf[:end_idx]

    def _on_scrub_changed(self, *_) -> None:
        self._update_scrub_label()
        self._refresh_plot()

    def _update_scrub_label(self) -> None:
        if self._is_live():
            self._scrub_label.setText("● Live")
            self._scrub_label.setStyleSheet("color: #7dd3fc; font-weight: 600; min-width: 70px;")
        else:
            buf = self._current_buffer()
            frac = self._scrub_slider.value() / self._scrub_slider.maximum()
            offset_s = (frac - 1.0) * (buf.size / self._sample_rate if buf.size > 0 else _BUFFER_SECONDS)
            self._scrub_label.setText(f"{offset_s:.1f} s")
            self._scrub_label.setStyleSheet("color: #f0a500; font-weight: 600; min-width: 70px;")

    def _find_triggers(
        self, buf: np.ndarray, level: float, edge: str, window: int, pretrig: int
    ) -> np.ndarray:
        """Return buffer indices where edge crossings occur, filtered by valid margin."""
        b0, b1 = buf[:-1], buf[1:]
        if edge == "Rising":
            mask = (b0 < level) & (b1 >= level)
        elif edge == "Falling":
            mask = (b0 > level) & (b1 <= level)
        else:
            mask = ((b0 < level) & (b1 >= level)) | ((b0 > level) & (b1 <= level))

        indices = np.where(mask)[0] + 1
        post = window - pretrig
        valid = indices[(indices >= pretrig) & (indices <= len(buf) - post)]

        # Enforce half-window minimum separation between triggers
        if len(valid) < 2:
            return valid
        min_sep = max(window // 2, 1)
        kept = [int(valid[0])]
        for idx in valid[1:]:
            if int(idx) - kept[-1] >= min_sep:
                kept.append(int(idx))
        return np.array(kept, dtype=np.int64)

    def _normalize(self, buf: np.ndarray) -> np.ndarray:
        """RMS-normalize so a typical signal sits comfortably in ±1."""
        rms = float(np.sqrt(np.mean(np.square(buf))))
        return buf / (rms * np.sqrt(2)) if rms > 1e-6 else buf.copy()

    def _refresh_plot(self):
        buffer = self._get_analysis_buffer()
        window = int(self._window_spin.value())
        pretrig_pct = int(self._pretrig_spin.value())
        pretrig = max(1, window * pretrig_pct // 100)
        post = window - pretrig
        level = float(self._level_spin.value())
        edge = self._edge_combo.currentText()
        auto_mode = self._mode_combo.currentText() == "Auto"
        ms_per_sample = 1000.0 / self._sample_rate

        if buffer.size < window + 2:
            self._curve.setData([], [])
            self._plot.setTitle("Waiting for samples…")
            self._stats_label.setText("Waiting for samples…")
            return

        buf_norm = self._normalize(buffer)
        triggers = self._find_triggers(buf_norm, level, edge, window, pretrig)

        if len(triggers) == 0:
            if auto_mode:
                # Free-run: snap to the most recent full window
                t = len(buf_norm) - post
                triggers = np.array([t], dtype=np.int64)
            else:
                self._curve.setData([], [])
                self._plot.setTitle("Waiting for trigger…")
                self._stats_label.setText(
                    f"No trigger  |  level {level:.2f}  |  edge: {edge}"
                )
                return

        # Time axis: x=0 at the trigger event
        x_ms = (np.arange(window, dtype=np.float32) - pretrig) * ms_per_sample

        if self._persist_mode:
            self._render_persist(buf_norm, triggers, window, pretrig, x_ms)
        else:
            t = int(triggers[-1])
            sweep = buf_norm[t - pretrig: t + post]
            self._curve.setData(x_ms, sweep)

        self._update_stats(buf_norm, triggers, window, pretrig, level, edge, ms_per_sample)

    def _render_persist(
        self,
        buf_norm: np.ndarray,
        triggers: np.ndarray,
        window: int,
        pretrig: int,
        x_ms: np.ndarray,
    ) -> None:
        """Build a 2-D density heatmap from all triggered sweeps and push to ImageItem."""
        post = window - pretrig

        # Vectorised sweep extraction via advanced indexing
        offsets = np.arange(-pretrig, post, dtype=np.int64)
        trig_idx = triggers[:, np.newaxis] + offsets[np.newaxis, :]  # (n, window)
        trig_idx = np.clip(trig_idx, 0, len(buf_norm) - 1)
        sweeps = buf_norm[trig_idx]  # (n_triggers, window)

        np.clip(sweeps, _Y_MIN, _Y_MAX, out=sweeps)

        # Map y values to bins via bincount on linearised indices
        n_x = window
        n_y = _N_BINS_Y
        y_bins = np.clip(
            ((sweeps - _Y_MIN) * (n_y / (_Y_MAX - _Y_MIN))).astype(np.int32),
            0, n_y - 1,
        )
        x_idx = np.tile(np.arange(n_x, dtype=np.int32), (sweeps.shape[0], 1))
        H = np.bincount(
            (x_idx * n_y + y_bins).ravel(), minlength=n_x * n_y
        ).reshape(n_x, n_y).astype(np.float32)

        np.sqrt(H, out=H)
        h_max = H.max()
        if h_max > 0:
            H /= h_max

        # Flip y: pyqtgraph col-major puts image[:,0] at the top of the screen (y_max).
        self._image.setImage(H[:, ::-1], autoLevels=False, levels=(0.0, 1.0))

    def _update_stats(
        self,
        buf_norm: np.ndarray,
        triggers: np.ndarray,
        window: int,
        pretrig: int,
        level: float,
        edge: str,
        ms_per_sample: float,
    ) -> None:
        n = len(triggers)
        if n >= 2:
            spacings = np.diff(triggers.astype(np.float64))
            mean_period_ms = float(np.median(spacings)) * ms_per_sample
            freq_hz = 1000.0 / mean_period_ms if mean_period_ms > 0 else 0.0
            freq_str = f"{freq_hz / 1000:.3f} kHz" if freq_hz >= 1000 else f"{freq_hz:.1f} Hz"
            period_str = f"{mean_period_ms:.3f} ms"
        else:
            freq_str = period_str = "—"

        post = window - pretrig
        t = int(triggers[-1])
        sweep = buf_norm[t - pretrig: t + post]
        peak = float(np.max(np.abs(sweep)))

        self._plot.setTitle(
            f"{n} trigger{'s' if n != 1 else ''}  |  {edge}  |  level {level:.2f}"
        )
        self._stats_label.setText(
            f"Triggers: {n}  |  Freq: {freq_str}  |  Period: {period_str}  |  Peak: {peak:.3f}"
        )

    # ── Window events ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
