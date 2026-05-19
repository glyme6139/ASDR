"""Standalone eye-diagram viewer fed by decimated VFO audio snapshots."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QComboBox,
    QSpinBox,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class EyeDiagramWindow(QMainWindow):
    """Floating eye-diagram window that stays hidden until explicitly opened."""

    visibility_changed = Signal(bool)
    vfo_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Eye Diagram")
        self.setObjectName("EyeDiagramWindow")
        self.resize(900, 520)

        self._buffers: dict[int, np.ndarray] = {}
        self._max_buffer = 8192
        self._current_vfo = None

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

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

        self._freeze_label = QLabel("Live")
        self._freeze_label.setStyleSheet("color: #7dd3fc; font-weight: 600;")
        header.addWidget(self._freeze_label)
        header.addStretch()

        sps_label = QLabel("Samples/symbol:")
        header.addWidget(sps_label)
        self._sps_spin = QSpinBox()
        self._sps_spin.setRange(8, 256)
        self._sps_spin.setSingleStep(2)
        self._sps_spin.setValue(32)
        self._sps_spin.setFixedWidth(84)
        self._sps_spin.valueChanged.connect(self._on_sps_changed)
        header.addWidget(self._sps_spin)

        phase_label = QLabel("Phase:")
        header.addWidget(phase_label)
        self._phase_slider = QSlider(Qt.Orientation.Horizontal)
        self._phase_slider.setRange(0, max(0, self._sps_spin.value() - 1))
        self._phase_slider.setValue(0)
        self._phase_slider.setFixedWidth(160)
        self._phase_slider.valueChanged.connect(self._on_phase_changed)
        header.addWidget(self._phase_slider)
        self._phase_value_label = QLabel("0")
        self._phase_value_label.setFixedWidth(32)
        header.addWidget(self._phase_value_label)
        layout.addLayout(header)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#111111")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel("left", "Amplitude")
        self._plot.setLabel("bottom", "Normalized Symbol Time")
        self._plot.setYRange(-1.2, 1.2, padding=0)
        self._plot.setXRange(0.0, 2.0, padding=0)
        layout.addWidget(self._plot)

        self._curve = pg.PlotCurveItem(pen=pg.mkPen(color=(0, 255, 255, 35), width=1))
        self._plot.addItem(self._curve)

        self._median_curve = pg.PlotCurveItem(pen=pg.mkPen(color="#ffd166", width=2))
        self._plot.addItem(self._median_curve)

        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_label = QLabel("Waiting for samples…")
        self._stats_label.setStyleSheet("color: #aaaaaa;")
        stats_row.addWidget(self._stats_label)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        self.setCentralWidget(content)

    def set_vfo_choices(self, choices: list[tuple[int, str]], active_vfo_id: int | None = None) -> None:
        """Populate the VFO selector with (id, label) pairs."""
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
        """Update the selected VFO when the main UI changes the active tab."""
        if vfo_id is None:
            return
        for i in range(self._vfo_combo.count()):
            if int(self._vfo_combo.itemData(i)) == int(vfo_id):
                self._vfo_combo.blockSignals(True)
                self._vfo_combo.setCurrentIndex(i)
                self._vfo_combo.blockSignals(False)
                self._set_current_vfo(vfo_id)
                return

    def _set_current_vfo(self, vfo_id: int | None) -> None:
        if vfo_id is None:
            return
        self._current_vfo = int(vfo_id)
        self._vfo_label.setText(f"VFO: {self._current_vfo + 1}")
        self._refresh_plot()
        self.vfo_changed.emit(self._current_vfo)

    def _on_vfo_choice_changed(self, *_args):
        data = self._vfo_combo.currentData()
        if data is None:
            return
        self._set_current_vfo(int(data))

    def _on_sps_changed(self, *_args):
        # Keep phase spin valid when sps changes
        sps = int(self._sps_spin.value())
        max_phase = max(0, sps - 1)
        cur = int(self._phase_slider.value()) if hasattr(self, '_phase_slider') else 0
        self._phase_slider.blockSignals(True)
        self._phase_slider.setRange(0, max_phase)
        if cur > max_phase:
            self._phase_slider.setValue(0)
        self._phase_slider.blockSignals(False)
        # update numeric label
        if hasattr(self, '_phase_value_label'):
            self._phase_value_label.setText(str(self._phase_slider.value()))
        self._refresh_plot()

    def _on_phase_changed(self, value: int):
        if hasattr(self, '_phase_value_label'):
            self._phase_value_label.setText(str(int(value)))
        self._refresh_plot()

    def push_samples(self, vfo_id: int, samples) -> None:
        """Append new samples for rendering; accepts sequence or numpy array."""
        try:
            chunk = np.asarray(samples, dtype=np.float32)
        except Exception:
            return
        if chunk.size == 0:
            return

        vfo_id = int(vfo_id)
        self._buffers[vfo_id] = np.concatenate((self._buffers.get(vfo_id, np.zeros(0, dtype=np.float32)), chunk))
        if self._buffers[vfo_id].size > self._max_buffer:
            self._buffers[vfo_id] = self._buffers[vfo_id][-self._max_buffer :]

        if self._current_vfo is None:
            self._set_current_vfo(vfo_id)
        self._update_stats()

        if self._current_vfo == vfo_id:
            self._refresh_plot()

    def _current_buffer(self) -> np.ndarray:
        if self._current_vfo is None:
            return np.zeros(0, dtype=np.float32)
        return self._buffers.get(self._current_vfo, np.zeros(0, dtype=np.float32))

    def _refresh_plot(self):
        buffer = self._current_buffer()
        if buffer.size == 0:
            self._curve.setData([], [])
            self._median_curve.setData([], [])
            self._plot.setTitle("No eye samples yet")
            return

        sps = int(self._sps_spin.value())
        phase = int(self._phase_slider.value()) if hasattr(self, '_phase_slider') else 0
        if buffer.size < sps * 4 + phase:
            self._curve.setData([], [])
            self._median_curve.setData([], [])
            return

        # Apply integer sample phase offset by trimming the start
        usable = buffer[phase:]
        usable = usable[-(usable.size // sps) * sps :]
        seg = usable.reshape(-1, sps)
        if seg.shape[0] < 3:
            self._curve.setData([], [])
            self._median_curve.setData([], [])
            return

        traces = np.array([np.concatenate((seg[i], seg[i + 1])) for i in range(seg.shape[0] - 1)], dtype=np.float32)

        if traces.shape[0] > 90:
            traces = traces[-90:]

        median_trace = np.median(traces, axis=0)
        x = np.linspace(0.0, 2.0, traces.shape[1], endpoint=False, dtype=np.float32)
        x_plot = np.tile(np.append(x, np.nan), traces.shape[0])
        y_plot = np.concatenate([np.append(t, np.nan) for t in traces])
        self._curve.setData(x_plot, y_plot)
        self._median_curve.setData(x, median_trace)

        eye_span = float(np.percentile(traces, 95) - np.percentile(traces, 5))
        self._plot.setTitle(f"{traces.shape[0]} traces  |  span {eye_span:.2f}  |  {sps} samples/symbol")
        self._update_stats(traces)

    def _update_stats(self, traces: np.ndarray | None = None) -> None:
        if traces is None:
            buffer = self._current_buffer()
            sps = int(self._sps_spin.value())
            if buffer.size < sps * 4:
                self._stats_label.setText("Waiting for more samples…")
                return
            usable = buffer[-(buffer.size // sps) * sps :]
            seg = usable.reshape(-1, sps)
            if seg.shape[0] < 3:
                self._stats_label.setText("Waiting for more samples…")
                return
            traces = np.array([np.concatenate((seg[i], seg[i + 1])) for i in range(seg.shape[0] - 1)], dtype=np.float32)

        peak = float(np.max(traces))
        trough = float(np.min(traces))
        rms = float(np.sqrt(np.mean(np.square(traces))))
        eye_opening = peak - trough
        self._stats_label.setText(
            f"Traces: {traces.shape[0]}   Peak: {peak:.3f}   Trough: {trough:.3f}   RMS: {rms:.3f}   Eye span: {eye_opening:.3f}"
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
