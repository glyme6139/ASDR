"""Standalone eye-diagram viewer fed by decimated VFO audio snapshots."""

from __future__ import annotations

import time
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QComboBox,
    QPushButton,
    QSpinBox,
    QSlider,
    QVBoxLayout,
    QWidget,
)

_EYE_COLORMAP = pg.ColorMap(
    [0.0, 0.06, 0.45, 1.0],
    [
        (11,  11,  11,  255),
        (0,   55,  70,  255),
        (0,   200, 230, 255),
        (240, 255, 255, 255),
    ],
)

_BUFFER_SECONDS = 30
_DEFAULT_SR = 48_000


class EyeDiagramWindow(QMainWindow):
    """Floating eye-diagram window that stays hidden until explicitly opened."""

    visibility_changed = Signal(bool)
    vfo_changed = Signal(object)

    _N_BINS = 300
    _Y_RANGE = 1.35
    _MIN_REFRESH_S = 0.066

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Eye Diagram")
        self.setObjectName("EyeDiagramWindow")
        self.resize(900, 560)

        self._sample_rate = float(_DEFAULT_SR)
        self._buffers: dict[int, np.ndarray] = {}
        self._max_buffer = _BUFFER_SECONDS * _DEFAULT_SR
        self._current_vfo = None
        self._last_refresh = 0.0

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Header ───────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        self._vfo_label = QLabel("VFO: --")
        self._vfo_label.setStyleSheet("font-weight: 600;")
        header.addWidget(self._vfo_label)
        header.addSpacing(10)

        header.addWidget(QLabel("Trace:"))
        self._vfo_combo = QComboBox()
        self._vfo_combo.currentIndexChanged.connect(self._on_vfo_choice_changed)
        header.addWidget(self._vfo_combo, stretch=1)
        header.addStretch()

        header.addWidget(QLabel("Samp/sym:"))
        self._sps_spin = QSpinBox()
        self._sps_spin.setRange(8, 256)
        self._sps_spin.setSingleStep(2)
        self._sps_spin.setValue(32)
        self._sps_spin.setFixedWidth(70)
        self._sps_spin.valueChanged.connect(self._on_sps_changed)
        header.addWidget(self._sps_spin)

        header.addSpacing(8)
        header.addWidget(QLabel("Phase:"))
        self._phase_slider = QSlider(Qt.Orientation.Horizontal)
        self._phase_slider.setRange(0, self._sps_spin.value() - 1)
        self._phase_slider.setValue(0)
        self._phase_slider.setFixedWidth(120)
        self._phase_slider.valueChanged.connect(self._on_phase_changed)
        header.addWidget(self._phase_slider)
        self._phase_value_label = QLabel("0")
        self._phase_value_label.setFixedWidth(28)
        header.addWidget(self._phase_value_label)

        header.addSpacing(6)
        self._auto_phase_btn = QPushButton("Auto")
        self._auto_phase_btn.setFixedWidth(52)
        self._auto_phase_btn.setToolTip("Find the phase offset that maximises eye opening")
        self._auto_phase_btn.clicked.connect(self._auto_phase)
        header.addWidget(self._auto_phase_btn)

        header.addSpacing(8)
        header.addWidget(QLabel("Traces:"))
        self._traces_spin = QSpinBox()
        self._traces_spin.setRange(10, 2000)
        self._traces_spin.setSingleStep(100)
        self._traces_spin.setValue(500)
        self._traces_spin.setFixedWidth(70)
        self._traces_spin.valueChanged.connect(lambda _: self._refresh_plot())
        header.addWidget(self._traces_spin)

        layout.addLayout(header)

        # ── Scrub row ─────────────────────────────────────────────────────────
        scrub_row = QHBoxLayout()
        scrub_row.setContentsMargins(0, 0, 0, 0)
        scrub_row.addWidget(QLabel("Buffer:"))
        self._scrub_slider = QSlider(Qt.Orientation.Horizontal)
        self._scrub_slider.setRange(0, 10000)
        self._scrub_slider.setValue(10000)
        self._scrub_slider.setToolTip(
            f"Scroll through the {_BUFFER_SECONDS}-second history. Right = live."
        )
        self._scrub_slider.valueChanged.connect(self._on_scrub_changed)
        scrub_row.addWidget(self._scrub_slider, stretch=1)
        self._scrub_label = QLabel("● Live")
        self._scrub_label.setStyleSheet("color: #7dd3fc; font-weight: 600;")
        self._scrub_label.setFixedWidth(80)
        self._scrub_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        scrub_row.addWidget(self._scrub_label)
        layout.addLayout(scrub_row)

        # ── Plot ─────────────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground("#111111")
        self._plot.showGrid(x=True, y=True, alpha=0.18)
        self._plot.setLabel("left", "Amplitude (norm.)")
        self._plot.setLabel("bottom", "Normalized Symbol Time")
        self._plot.setYRange(-self._Y_RANGE, self._Y_RANGE, padding=0)
        self._plot.setXRange(0.0, 2.0, padding=0)

        self._image = pg.ImageItem()
        self._image.setColorMap(_EYE_COLORMAP)
        self._image.setRect(QRectF(0.0, -self._Y_RANGE, 2.0, 2.0 * self._Y_RANGE))
        self._image.setLevels((0.0, 1.0))
        self._image.setZValue(-10)
        self._plot.addItem(self._image)

        self._median_curve = pg.PlotCurveItem(pen=pg.mkPen(color="#ffd166", width=2))
        self._plot.addItem(self._median_curve)

        self._sample_line = pg.InfiniteLine(
            pos=0.5, angle=90,
            pen=pg.mkPen(color=(255, 100, 100, 140), width=1, style=Qt.DashLine),
        )
        self._plot.addItem(self._sample_line)

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

    # ── Public API ────────────────────────────────────────────────────────────

    def set_sample_rate(self, sample_rate: float) -> None:
        if sample_rate > 0:
            self._sample_rate = float(sample_rate)
            self._max_buffer = int(_BUFFER_SECONDS * self._sample_rate)

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
        """Append new audio samples. Only triggers a redraw when in live mode."""
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

    # ── Scrub helpers ─────────────────────────────────────────────────────────

    def _is_live(self) -> bool:
        return self._scrub_slider.value() >= self._scrub_slider.maximum()

    def _get_analysis_buffer(self) -> np.ndarray:
        """Return the slice of the current buffer selected by the scrub position."""
        buf = self._current_buffer()
        if self._is_live() or buf.size == 0:
            return buf
        frac = self._scrub_slider.value() / self._scrub_slider.maximum()
        end_idx = max(1, int(round(frac * buf.size)))
        return buf[:end_idx]

    def _on_scrub_changed(self, *_):
        self._update_scrub_label()
        self._refresh_plot()

    def _update_scrub_label(self):
        buf = self._current_buffer()
        if self._is_live() or buf.size == 0:
            self._scrub_label.setText("● Live")
            self._scrub_label.setStyleSheet("color: #7dd3fc; font-weight: 600;")
        else:
            frac = self._scrub_slider.value() / self._scrub_slider.maximum()
            end_idx = int(round(frac * buf.size))
            secs_from_end = (buf.size - end_idx) / self._sample_rate
            self._scrub_label.setText(f"−{secs_from_end:.1f} s")
            self._scrub_label.setStyleSheet("color: #ffb400; font-weight: 600;")

    # ── VFO management ────────────────────────────────────────────────────────

    def _set_current_vfo(self, vfo_id: int | None) -> None:
        if vfo_id is None:
            return
        self._current_vfo = int(vfo_id)
        self._vfo_label.setText(f"VFO: {self._current_vfo + 1}")
        self._refresh_plot()
        self.vfo_changed.emit(self._current_vfo)

    def _on_vfo_choice_changed(self, *_):
        data = self._vfo_combo.currentData()
        if data is not None:
            self._set_current_vfo(int(data))

    # ── Control callbacks ─────────────────────────────────────────────────────

    def _on_sps_changed(self, *_):
        sps = int(self._sps_spin.value())
        max_phase = max(0, sps - 1)
        cur = int(self._phase_slider.value())
        self._phase_slider.blockSignals(True)
        self._phase_slider.setRange(0, max_phase)
        if cur > max_phase:
            self._phase_slider.setValue(0)
        self._phase_slider.blockSignals(False)
        self._phase_value_label.setText(str(self._phase_slider.value()))
        self._refresh_plot()

    def _on_phase_changed(self, value: int):
        self._phase_value_label.setText(str(int(value)))
        self._refresh_plot()

    def _auto_phase(self) -> None:
        """Sweep all phase offsets and jump to the one with the best eye opening."""
        buffer = self._get_analysis_buffer()
        sps = int(self._sps_spin.value())
        if buffer.size < sps * 12:
            return

        best_phase, best_score = 0, -1.0
        center = sps // 2

        for phase in range(sps):
            usable = buffer[phase:]
            n = (usable.size // sps) * sps
            if n < sps * 4:
                continue
            seg = usable[:n].reshape(-1, sps)
            vals = seg[:, center]
            med = np.median(vals)
            upper = vals[vals > med]
            lower = vals[vals <= med]
            if not len(upper) or not len(lower):
                continue
            score = float(
                (np.median(upper) - np.median(lower))
                / (np.std(upper) + np.std(lower) + 1e-9)
            )
            if score > best_score:
                best_score, best_phase = score, phase

        self._phase_slider.setValue(best_phase)

    # ── Sample ingestion helpers ───────────────────────────────────────────────

    def _current_buffer(self) -> np.ndarray:
        if self._current_vfo is None:
            return np.zeros(0, dtype=np.float32)
        return self._buffers.get(self._current_vfo, np.zeros(0, dtype=np.float32))

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _refresh_plot(self):
        buffer = self._get_analysis_buffer()
        sps = int(self._sps_spin.value())
        phase = int(self._phase_slider.value())
        max_traces = int(self._traces_spin.value())

        if buffer.size < sps * 4 + phase:
            self._image.setImage(np.zeros((1, 1), dtype=np.float32), autoLevels=False)
            self._median_curve.setData([], [])
            self._plot.setTitle("Waiting for samples…")
            self._stats_label.setText("Waiting for samples…")
            return

        usable = buffer[phase:]
        usable = usable[-(usable.size // sps) * sps:]
        seg = usable.reshape(-1, sps)
        if seg.shape[0] < 3:
            return

        traces = np.concatenate([seg[:-1], seg[1:]], axis=1).astype(np.float32)
        if traces.shape[0] > max_traces:
            traces = traces[-max_traces:]

        median_trace = np.median(traces, axis=0)
        scale = float(np.max(np.abs(median_trace)))
        if scale > 1e-6:
            traces = traces / scale
            median_trace = median_trace / scale

        np.clip(traces, -self._Y_RANGE, self._Y_RANGE, out=traces)
        np.clip(median_trace, -self._Y_RANGE, self._Y_RANGE, out=median_trace)

        n_x = traces.shape[1]
        n_y = self._N_BINS

        y_bins = np.clip(
            ((traces + self._Y_RANGE) * (n_y / (2.0 * self._Y_RANGE))).astype(np.int32),
            0, n_y - 1,
        )
        x_idx = np.tile(np.arange(n_x, dtype=np.int32), (traces.shape[0], 1))
        H = np.bincount(
            (x_idx * n_y + y_bins).ravel(), minlength=n_x * n_y
        ).reshape(n_x, n_y).astype(np.float32)

        np.sqrt(H, out=H)
        h_max = H.max()
        if h_max > 0:
            H /= h_max

        self._image.setImage(H[:, ::-1], autoLevels=False, levels=(0.0, 1.0))

        x = np.linspace(0.0, 2.0, traces.shape[1], endpoint=False, dtype=np.float32)
        self._median_curve.setData(x, median_trace.astype(np.float32))
        self._sample_line.setValue(0.5)

        self._plot.setTitle(
            f"{traces.shape[0]} traces  |  {sps} samp/sym  |  phase {phase}"
        )
        self._update_stats(traces, sps)

    def _update_stats(self, traces: np.ndarray, sps: int) -> None:
        center = sps // 2
        center_vals = traces[:, center]

        med = float(np.median(center_vals))
        upper = center_vals[center_vals > med]
        lower = center_vals[center_vals <= med]

        if len(upper) > 1 and len(lower) > 1:
            eye_h = float(np.median(upper) - np.median(lower))
            jitter = float((np.std(upper) + np.std(lower)) / 2)
        else:
            eye_h = jitter = 0.0

        rms = float(np.sqrt(np.mean(np.square(traces))))
        self._stats_label.setText(
            f"Traces: {traces.shape[0]}  |  "
            f"Eye height: {eye_h:.3f}  |  "
            f"Level jitter: {jitter:.4f}  |  "
            f"RMS: {rms:.3f}"
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
