"""Signal constellation diagram — plots I vs Q of the analytic signal."""

from __future__ import annotations

import time
import numpy as np
import pyqtgraph as pg
from scipy.signal import hilbert
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
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

_N_BINS = 300
_AXIS_RANGE = 1.5


class ConstellationWindow(QMainWindow):
    """Floating constellation diagram fed by VFO audio samples."""

    visibility_changed = Signal(bool)
    vfo_changed = Signal(object)

    _MIN_REFRESH_S = 0.066
    _DEFAULT_SR = 48_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Constellation")
        self.setObjectName("ConstellationWindow")
        self.resize(640, 700)

        self._buffers: dict[int, np.ndarray] = {}
        self._iq_buffers: dict[int, np.ndarray] = {}
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

        header.addWidget(QLabel("Points:"))
        self._points_spin = QSpinBox()
        self._points_spin.setRange(256, 65536)
        self._points_spin.setSingleStep(256)
        self._points_spin.setValue(4096)
        self._points_spin.setFixedWidth(80)
        self._points_spin.setToolTip("Number of most-recent samples to display")
        self._points_spin.valueChanged.connect(self._refresh_plot)
        header.addWidget(self._points_spin)

        header.addSpacing(8)
        header.addWidget(QLabel("Bins:"))
        self._bins_spin = QSpinBox()
        self._bins_spin.setRange(64, 512)
        self._bins_spin.setSingleStep(32)
        self._bins_spin.setValue(_N_BINS)
        self._bins_spin.setFixedWidth(68)
        self._bins_spin.setToolTip("Grid resolution for density (persist) mode")
        self._bins_spin.valueChanged.connect(self._refresh_plot)
        header.addWidget(self._bins_spin)

        header.addSpacing(8)
        self._persist_btn = QPushButton("Density Off")
        self._persist_btn.setFixedWidth(88)
        self._persist_btn.setCheckable(True)
        self._persist_btn.setToolTip("Toggle density (heatmap) mode vs scatter dots")
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
        self._plot = pg.PlotWidget(aspectLocked=True)
        self._plot.setBackground("#111111")
        self._plot.showGrid(x=True, y=True, alpha=0.18)
        self._plot.setLabel("left", "Q")
        self._plot.setLabel("bottom", "I")
        self._plot.setXRange(-_AXIS_RANGE, _AXIS_RANGE, padding=0)
        self._plot.setYRange(-_AXIS_RANGE, _AXIS_RANGE, padding=0)

        # Unit circle reference
        theta = np.linspace(0, 2 * np.pi, 256)
        self._unit_circle = pg.PlotCurveItem(
            np.cos(theta), np.sin(theta),
            pen=pg.mkPen(color=(80, 80, 80, 160), width=1),
        )
        self._plot.addItem(self._unit_circle)

        # Density heatmap layer
        self._image = pg.ImageItem()
        self._image.setColorMap(_PERSIST_COLORMAP)
        self._image.setLevels((0.0, 1.0))
        self._image.setZValue(-10)
        self._image.setVisible(False)
        self._plot.addItem(self._image)

        # Scatter layer
        self._scatter = pg.ScatterPlotItem(
            size=2, pen=None,
            brush=pg.mkBrush(0, 255, 200, 80),
        )
        self._plot.addItem(self._scatter)

        layout.addWidget(self._plot, stretch=1)

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
            self._iq_buffers.clear()
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
            buf = buf[-self._max_buffer:].copy()
        self._buffers[vfo_id] = buf

        if self._current_vfo is None:
            self._set_current_vfo(vfo_id)

        if self._current_vfo == vfo_id and self._is_live():
            now = time.monotonic()
            if now - self._last_refresh >= self._MIN_REFRESH_S:
                self._last_refresh = now
                self._refresh_plot()

    def push_iq_samples(self, vfo_id: int, samples) -> None:
        try:
            chunk = np.asarray(samples, dtype=np.complex64)
        except Exception:
            return
        if chunk.size == 0:
            return

        vfo_id = int(vfo_id)
        buf = self._iq_buffers.get(vfo_id, np.zeros(0, dtype=np.complex64))
        buf = np.concatenate((buf, chunk))
        if buf.size > self._max_buffer:
            buf = buf[-self._max_buffer:].copy()
        self._iq_buffers[vfo_id] = buf

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

    def _on_persist_toggled(self, checked: bool):
        self._persist_mode = checked
        self._persist_btn.setText("Density On" if checked else "Density Off")
        self._image.setVisible(checked)
        self._scatter.setVisible(not checked)
        self._refresh_plot()

    def _is_live(self) -> bool:
        return self._scrub_slider.value() >= self._scrub_slider.maximum()

    def _current_buffer(self) -> np.ndarray:
        if self._current_vfo is None:
            return np.zeros(0, dtype=np.float32)
        return self._buffers.get(self._current_vfo, np.zeros(0, dtype=np.float32))

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

    def _analytic_iq(self, buf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (I, Q) via Hilbert transform, then RMS-normalize."""
        n = len(buf)
        if n < 8:
            return buf, np.zeros_like(buf)

        # Analytic signal: real part is original, imaginary part is Hilbert transform
        analytic = hilbert(buf).astype(np.complex64)
        I = np.real(analytic)
        Q = np.imag(analytic)

        # RMS-normalize so the cloud fits in ±1
        rms = float(np.sqrt(np.mean(I ** 2 + Q ** 2)))
        if rms > 1e-6:
            scale = 1.0 / (rms * np.sqrt(2))
            I = I * scale
            Q = Q * scale

        return I.astype(np.float32), Q.astype(np.float32)

    def _refresh_plot(self):
        n_points = int(self._points_spin.value())

        # Raw IQ path: use complex buffer directly, no Hilbert needed
        iq_buf = self._iq_buffers.get(self._current_vfo) if self._current_vfo is not None else None
        if iq_buf is not None and iq_buf.size > 0:
            if not self._is_live() and iq_buf.size > 0:
                frac = self._scrub_slider.value() / self._scrub_slider.maximum()
                end_idx = max(1, int(round(frac * iq_buf.size)))
                iq_buf = iq_buf[:end_idx]
            if iq_buf.size < 16:
                self._scatter.setData([], [])
                self._plot.setTitle("Waiting for IQ samples…")
                self._stats_label.setText("Waiting for IQ samples…")
                return
            window = iq_buf[-n_points:] if iq_buf.size > n_points else iq_buf
            I = np.real(window).astype(np.float32)
            Q = np.imag(window).astype(np.float32)
            rms = float(np.sqrt(np.mean(I ** 2 + Q ** 2)))
            if rms > 1e-6:
                scale = 1.0 / (rms * np.sqrt(2))
                I, Q = I * scale, Q * scale
            if self._persist_mode:
                self._render_density(I, Q)
            else:
                self._scatter.setData(x=I, y=Q)
            self._update_stats(I, Q)
            return

        # Hilbert path: derive I/Q from real-valued audio
        buf = self._get_analysis_buffer()
        if buf.size < 16:
            self._scatter.setData([], [])
            self._plot.setTitle("Waiting for samples…")
            self._stats_label.setText("Waiting for samples…")
            return

        window = buf[-n_points:] if buf.size > n_points else buf
        I, Q = self._analytic_iq(window)

        if self._persist_mode:
            self._render_density(I, Q)
        else:
            self._scatter.setData(x=I, y=Q)

        self._update_stats(I, Q)

    def _render_density(self, I: np.ndarray, Q: np.ndarray) -> None:
        n_bins = int(self._bins_spin.value())
        lo, hi = -_AXIS_RANGE, _AXIS_RANGE

        I_bins = np.clip(((I - lo) / (hi - lo) * n_bins).astype(np.int32), 0, n_bins - 1)
        Q_bins = np.clip(((Q - lo) / (hi - lo) * n_bins).astype(np.int32), 0, n_bins - 1)

        H = np.bincount(I_bins * n_bins + Q_bins, minlength=n_bins * n_bins)
        H = H.reshape(n_bins, n_bins).astype(np.float32)

        np.sqrt(H, out=H)
        h_max = H.max()
        if h_max > 0:
            H /= h_max

        # x→I (cols), y→Q (rows); ImageItem uses col-major so transpose
        self._image.setImage(H.T[:, ::-1], autoLevels=False, levels=(0.0, 1.0))
        self._image.setRect(pg.QtCore.QRectF(lo, lo, hi - lo, hi - lo))

    def _update_stats(self, I: np.ndarray, Q: np.ndarray) -> None:
        amplitude = np.sqrt(I ** 2 + Q ** 2)
        phase_deg = np.degrees(np.arctan2(Q, I))
        mean_amp = float(np.mean(amplitude))
        std_amp = float(np.std(amplitude))
        mean_phase = float(np.mean(phase_deg))

        self._plot.setTitle(f"{len(I)} points")
        self._stats_label.setText(
            f"Amplitude  mean: {mean_amp:.3f}  std: {std_amp:.3f}  |  "
            f"Phase mean: {mean_phase:.1f}°"
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
