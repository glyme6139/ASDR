"""Signal decoding window — line-coding identification and bit-stream extraction."""

from __future__ import annotations

import time
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

_BUFFER_SECONDS = 30
_DEFAULT_SR = 48_000
_MAX_SYM_MARKERS = 128   # max symbol-boundary lines drawn at once

# ── Encoding helpers ──────────────────────────────────────────────────────────

ENCODINGS = [
    "NRZ-L",
    "NRZ-I",
    "Manchester (IEEE 802.3)",
    "Manchester (Thomas)",
    "Differential Manchester",
    "Biphase Mark (BMC)",
    "Miller",
    "AMI",
    "RZ",
    "4B5B",
]

_4B5B_REVERSE = {
    0b11110: 0x0, 0b01001: 0x1, 0b10100: 0x2, 0b10101: 0x3,
    0b01010: 0x4, 0b01011: 0x5, 0b01110: 0x6, 0b01111: 0x7,
    0b10010: 0x8, 0b10011: 0x9, 0b10110: 0xA, 0b10111: 0xB,
    0b11010: 0xC, 0b11011: 0xD, 0b11100: 0xE, 0b11101: 0xF,
}


def _slice(signal: np.ndarray, threshold: float) -> np.ndarray:
    return (signal >= threshold).astype(np.int8)


def _centers(binary: np.ndarray, sps: int, phase: int) -> np.ndarray:
    n = max(0, (len(binary) - phase - sps // 2) // sps)
    if n == 0:
        return np.array([], dtype=np.int8)
    idx = phase + sps // 2 + np.arange(n, dtype=np.int64) * sps
    return binary[np.clip(idx, 0, len(binary) - 1)]


def _quarters(binary: np.ndarray, sps: int, phase: int):
    n = max(0, (len(binary) - phase - sps) // sps)
    if n == 0:
        return np.array([], dtype=np.int8), np.array([], dtype=np.int8)
    q1 = np.clip(phase + sps // 4 + np.arange(n, dtype=np.int64) * sps, 0, len(binary) - 1)
    q3 = np.clip(phase + 3 * sps // 4 + np.arange(n, dtype=np.int64) * sps, 0, len(binary) - 1)
    return binary[q1], binary[q3]


def decode_bits(binary: np.ndarray, sps: int, phase: int, encoding: str) -> list[int]:
    """Return list of 0/1 decoded bits (-1 = symbol violation)."""
    if len(binary) < sps + phase or sps < 2:
        return []

    if encoding == "NRZ-L":
        return _centers(binary, sps, phase).tolist()

    if encoding == "NRZ-I":
        raw = _centers(binary, sps, phase)
        if len(raw) < 2:
            return []
        return [int(raw[i] != raw[i - 1]) for i in range(1, len(raw))]

    if encoding in ("Manchester (IEEE 802.3)", "Manchester (Thomas)"):
        f, s = _quarters(binary, sps, phase)
        ieee = "IEEE" in encoding
        out = []
        for a, b in zip(f, s):
            if a == 1 and b == 0:
                out.append(0 if ieee else 1)
            elif a == 0 and b == 1:
                out.append(1 if ieee else 0)
            else:
                out.append(-1)
        return out

    if encoding == "Differential Manchester":
        n = max(0, (len(binary) - phase - sps) // sps)
        if n < 2:
            return []
        out = []
        for i in range(1, n):
            ep = phase + i * sps - 1
            sc = phase + i * sps
            if ep < 0 or sc >= len(binary):
                break
            out.append(0 if int(binary[ep]) != int(binary[sc]) else 1)
        return out

    if encoding == "Biphase Mark (BMC)":
        f, s = _quarters(binary, sps, phase)
        return [1 if a != b else 0 for a, b in zip(f, s)]

    if encoding == "Miller":
        # 1 = mid-bit transition; 0 = no mid-bit (boundary trans if prev was 0)
        f, s = _quarters(binary, sps, phase)
        return [1 if a != b else 0 for a, b in zip(f, s)]

    if encoding == "AMI":
        raw = _centers(binary, sps, phase)
        return [0 if b == 0 else 1 for b in raw]

    if encoding == "RZ":
        n = max(0, (len(binary) - phase - sps // 4) // sps)
        if n == 0:
            return []
        idx = np.clip(phase + sps // 4 + np.arange(n, dtype=np.int64) * sps, 0, len(binary) - 1)
        return binary[idx].tolist()

    if encoding == "4B5B":
        raw = _centers(binary, sps, phase)
        out = []
        for i in range(0, len(raw) - 4, 5):
            code = 0
            for j in range(5):
                code = (code << 1) | int(raw[i + j])
            nibble = _4B5B_REVERSE.get(code, -1)
            if nibble < 0:
                out.extend([-1] * 4)
            else:
                out.extend([(nibble >> (3 - k)) & 1 for k in range(4)])
        return out

    return []


def bits_to_text(bits: list[int], fmt: str, group: int) -> str:
    if not bits:
        return ""
    clean: list[int | None] = [b if b in (0, 1) else None for b in bits]

    if fmt == "Binary":
        chars = [str(b) if b is not None else "?" for b in clean]
        if group > 1:
            return " ".join("".join(chars[i:i + group]) for i in range(0, len(chars), group))
        return "".join(chars)

    if fmt == "Hex":
        parts = []
        for i in range(0, len(clean) - 7, 8):
            byte = clean[i:i + 8]
            if None in byte:
                parts.append("??")
            else:
                parts.append(f"{sum(b << (7 - j) for j, b in enumerate(byte)):02X}")
        return " ".join(parts)

    if fmt == "ASCII":
        parts = []
        for i in range(0, len(clean) - 7, 8):
            byte = clean[i:i + 8]
            if None in byte:
                parts.append("·")
            else:
                val = sum(b << (7 - j) for j, b in enumerate(byte))
                parts.append(chr(val) if 32 <= val < 127 else f"[{val:02X}]")
        return "".join(parts)

    return ""


# ── Main window class ─────────────────────────────────────────────────────────

class SignalDecoderWindow(QMainWindow):
    """Floating signal decoder. Shares the same sample buffer as oscilloscope/eye diagram."""

    visibility_changed = Signal(bool)
    vfo_changed = Signal(object)

    _MIN_REFRESH_S = 0.066

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signal Decoder")
        self.setObjectName("SignalDecoderWindow")
        self.resize(1100, 720)

        self._buffers: dict[int, np.ndarray] = {}
        self._sample_rate = float(_DEFAULT_SR)
        self._max_buffer = int(_BUFFER_SECONDS * self._sample_rate)
        self._current_vfo: int | None = None
        self._last_refresh = 0.0
        self._view_initialized = False  # set X range only on first render
        # Track last buffer length per VFO for incremental bit-stream output
        self._stream_pos: dict[int, int] = {}

        # pyqtgraph items pooled to avoid repeated add/remove
        self._sym_lines: list[pg.InfiniteLine] = []
        self._bit_labels: list[pg.TextItem] = []

        content = QWidget(self)
        root = QVBoxLayout(content)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── Row 1: VFO + encoding controls ───────────────────────────────────
        r1 = QHBoxLayout()
        r1.setContentsMargins(0, 0, 0, 0)

        self._vfo_label = QLabel("VFO: --")
        self._vfo_label.setStyleSheet("font-weight: 600;")
        r1.addWidget(self._vfo_label)
        r1.addSpacing(8)

        r1.addWidget(QLabel("VFO:"))
        self._vfo_combo = QComboBox()
        self._vfo_combo.currentIndexChanged.connect(self._on_vfo_combo_changed)
        r1.addWidget(self._vfo_combo, stretch=1)
        r1.addStretch()

        r1.addWidget(QLabel("Encoding:"))
        self._enc_combo = QComboBox()
        self._enc_combo.addItems(ENCODINGS)
        self._enc_combo.setFixedWidth(190)
        self._enc_combo.setToolTip("Line-coding scheme to decode")
        self._enc_combo.currentIndexChanged.connect(self._on_settings_changed)
        r1.addWidget(self._enc_combo)

        r1.addSpacing(8)
        r1.addWidget(QLabel("Polarity:"))
        self._pol_combo = QComboBox()
        self._pol_combo.addItems(["Normal", "Inverted"])
        self._pol_combo.setFixedWidth(80)
        self._pol_combo.setToolTip("Invert signal before slicing")
        self._pol_combo.currentIndexChanged.connect(self._on_settings_changed)
        r1.addWidget(self._pol_combo)

        r1.addSpacing(8)
        r1.addWidget(QLabel("Threshold:"))
        self._thresh_spin = QDoubleSpinBox()
        self._thresh_spin.setRange(-2.0, 2.0)
        self._thresh_spin.setSingleStep(0.05)
        self._thresh_spin.setValue(0.0)
        self._thresh_spin.setDecimals(2)
        self._thresh_spin.setFixedWidth(68)
        self._thresh_spin.setToolTip("Decision threshold (normalized amplitude)")
        self._thresh_spin.valueChanged.connect(self._on_threshold_spin_changed)
        r1.addWidget(self._thresh_spin)

        root.addLayout(r1)

        # ── Row 2: clock + display options ───────────────────────────────────
        r2 = QHBoxLayout()
        r2.setContentsMargins(0, 0, 0, 0)

        r2.addWidget(QLabel("Samp/sym:"))
        self._sps_spin = QSpinBox()
        self._sps_spin.setRange(2, 65536)
        self._sps_spin.setValue(32)
        self._sps_spin.setFixedWidth(80)
        self._sps_spin.setToolTip("Clock period in samples (samples per symbol)")
        self._sps_spin.valueChanged.connect(self._on_sps_changed)
        r2.addWidget(self._sps_spin)

        self._auto_clock_btn = QPushButton("Auto Clock")
        self._auto_clock_btn.setFixedWidth(84)
        self._auto_clock_btn.setToolTip(
            "Estimate samples/symbol from sign-transition autocorrelation.\n"
            "Works best on signals with regular transitions (preambles, BPSK, FM)."
        )
        self._auto_clock_btn.clicked.connect(self._auto_clock)
        r2.addWidget(self._auto_clock_btn)

        r2.addSpacing(8)
        r2.addWidget(QLabel("Phase:"))
        self._phase_spin = QSpinBox()
        self._phase_spin.setRange(0, 65535)
        self._phase_spin.setValue(0)
        self._phase_spin.setFixedWidth(60)
        self._phase_spin.setToolTip("Symbol alignment offset (samples)")
        self._phase_spin.valueChanged.connect(self._on_settings_changed)
        r2.addWidget(self._phase_spin)

        self._auto_phase_btn = QPushButton("Auto")
        self._auto_phase_btn.setFixedWidth(48)
        self._auto_phase_btn.setToolTip("Find phase that minimises symbol violations")
        self._auto_phase_btn.clicked.connect(self._auto_phase)
        r2.addWidget(self._auto_phase_btn)

        r2.addSpacing(8)
        r2.addWidget(QLabel("Window:"))
        self._window_spin = QSpinBox()
        self._window_spin.setRange(64, 65536)
        self._window_spin.setSingleStep(64)
        self._window_spin.setValue(1024)
        self._window_spin.setFixedWidth(80)
        self._window_spin.setToolTip("Number of samples displayed in the waveform view")
        self._window_spin.valueChanged.connect(self._on_settings_changed)
        r2.addWidget(self._window_spin)

        r2.addSpacing(8)
        r2.addWidget(QLabel("Group:"))
        self._group_combo = QComboBox()
        self._group_combo.addItems(["4", "8", "16", "32"])
        self._group_combo.setCurrentIndex(1)
        self._group_combo.setFixedWidth(48)
        self._group_combo.setToolTip("Bit grouping in text display")
        self._group_combo.currentIndexChanged.connect(self._on_settings_changed)
        r2.addWidget(self._group_combo)

        r2.addSpacing(8)
        r2.addWidget(QLabel("Format:"))
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(["Binary", "Hex", "ASCII"])
        self._fmt_combo.setFixedWidth(72)
        self._fmt_combo.currentIndexChanged.connect(self._on_settings_changed)
        r2.addWidget(self._fmt_combo)

        r2.addSpacing(8)
        self._labels_cb = QCheckBox("Bit labels")
        self._labels_cb.setChecked(True)
        self._labels_cb.setToolTip("Annotate each symbol with its decoded bit value")
        self._labels_cb.stateChanged.connect(self._on_settings_changed)
        r2.addWidget(self._labels_cb)

        r2.addSpacing(8)
        self._digital_cb = QCheckBox("Digital overlay")
        self._digital_cb.setChecked(True)
        self._digital_cb.setToolTip("Show the reconstructed square-wave signal")
        self._digital_cb.stateChanged.connect(self._on_settings_changed)
        r2.addWidget(self._digital_cb)

        r2.addSpacing(8)
        self._markers_btn = QPushButton("Markers")
        self._markers_btn.setFixedWidth(68)
        self._markers_btn.setCheckable(True)
        self._markers_btn.setToolTip("Toggle A/B measurement markers (drag to measure)")
        self._markers_btn.clicked.connect(self._on_markers_toggled)
        r2.addWidget(self._markers_btn)

        r2.addSpacing(8)
        self._reset_view_btn = QPushButton("Reset View")
        self._reset_view_btn.setFixedWidth(80)
        self._reset_view_btn.setToolTip("Restore default zoom and pan")
        self._reset_view_btn.clicked.connect(self._reset_view)
        r2.addWidget(self._reset_view_btn)

        r2.addStretch()
        root.addLayout(r2)

        # ── Scrub row ─────────────────────────────────────────────────────────
        scrub = QHBoxLayout()
        scrub.setContentsMargins(0, 0, 0, 0)
        self._scrub_slider = QSlider(Qt.Orientation.Horizontal)
        self._scrub_slider.setRange(0, 10000)
        self._scrub_slider.setValue(10000)
        self._scrub_slider.setToolTip("Scroll through the 30-second buffer (rightmost = live)")
        self._scrub_slider.valueChanged.connect(self._on_scrub_changed)
        scrub.addWidget(self._scrub_slider, stretch=1)
        self._scrub_label = QLabel("● Live")
        self._scrub_label.setStyleSheet("color: #7dd3fc; font-weight: 600; min-width: 70px;")
        self._scrub_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scrub.addWidget(self._scrub_label)
        root.addLayout(scrub)

        # ── Splitter: waveform plot (top) + bit stream (bottom) ───────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        # ── Waveform plot ─────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground("#111111")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("left", "Amplitude (norm.)")
        self._plot.setLabel("bottom", "Samples")
        self._plot.setYRange(-1.6, 1.6, padding=0)

        self._curve = pg.PlotCurveItem(pen=pg.mkPen(color="#00ffcc", width=1.5))
        self._plot.addItem(self._curve)

        self._digital_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color="#ff8800", width=2, style=Qt.PenStyle.DashLine)
        )
        self._plot.addItem(self._digital_curve)

        # Threshold: draggable horizontal line
        self._thresh_line = pg.InfiniteLine(
            pos=0.0, angle=0,
            pen=pg.mkPen(color=(255, 180, 0, 200), width=1, style=Qt.PenStyle.DashLine),
            movable=True,
            label="{value:.2f}",
            labelOpts={"color": "#ffb400", "position": 0.05, "fill": (11, 11, 11, 160)},
        )
        self._thresh_line.sigPositionChangeFinished.connect(self._on_thresh_line_dragged)
        self._plot.addItem(self._thresh_line)

        # Measurement markers A / B
        self._marker_a = pg.InfiniteLine(
            pos=-200.0, angle=90, movable=True,
            pen=pg.mkPen(color="#ff44aa", width=1),
            label="A  {value:.0f}",
            labelOpts={"color": "#ff44aa", "position": 0.90, "fill": (11, 11, 11, 160)},
        )
        self._marker_a.sigPositionChanged.connect(self._on_marker_moved)
        self._marker_a.setVisible(False)
        self._plot.addItem(self._marker_a)

        self._marker_b = pg.InfiniteLine(
            pos=200.0, angle=90, movable=True,
            pen=pg.mkPen(color="#44ffaa", width=1),
            label="B  {value:.0f}",
            labelOpts={"color": "#44ffaa", "position": 0.83, "fill": (11, 11, 11, 160)},
        )
        self._marker_b.sigPositionChanged.connect(self._on_marker_moved)
        self._marker_b.setVisible(False)
        self._plot.addItem(self._marker_b)

        splitter.addWidget(self._plot)

        # ── Bit stream panel ──────────────────────────────────────────────────
        bit_widget = QWidget()
        bit_root = QVBoxLayout(bit_widget)
        bit_root.setContentsMargins(0, 2, 0, 0)
        bit_root.setSpacing(2)

        bit_hdr = QHBoxLayout()
        bit_hdr.setContentsMargins(0, 0, 0, 0)
        bit_hdr.addWidget(QLabel("Decoded bit stream:"))
        bit_hdr.addStretch()

        self._decode_btn = QPushButton("Decode")
        self._decode_btn.setFixedWidth(56)
        self._decode_btn.setToolTip("Decode the current waveform window and append to output")
        self._decode_btn.clicked.connect(self._decode_current)
        bit_hdr.addWidget(self._decode_btn)

        self._freeze_btn = QPushButton("Freeze")
        self._freeze_btn.setFixedWidth(56)
        self._freeze_btn.setCheckable(True)
        self._freeze_btn.setToolTip("Pause live bit-stream output (waveform still updates)")
        bit_hdr.addWidget(self._freeze_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedWidth(48)
        self._clear_btn.clicked.connect(self._clear_stream)
        bit_hdr.addWidget(self._clear_btn)

        bit_root.addLayout(bit_hdr)

        self._bit_display = QPlainTextEdit()
        self._bit_display.setReadOnly(True)
        self._bit_display.setMaximumBlockCount(500)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._bit_display.setFont(mono)
        self._bit_display.setStyleSheet(
            "QPlainTextEdit { background:#0d1117; color:#39ff14; "
            "border:1px solid #303030; }"
        )
        bit_root.addWidget(self._bit_display)

        splitter.addWidget(bit_widget)
        splitter.setSizes([460, 180])
        root.addWidget(splitter, stretch=1)

        # ── Stats row ─────────────────────────────────────────────────────────
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_label = QLabel("Waiting for samples…")
        self._stats_label.setStyleSheet("color: #aaaaaa;")
        stats_row.addWidget(self._stats_label)
        stats_row.addStretch()
        self._marker_label = QLabel("")
        self._marker_label.setStyleSheet("color: #dddddd; font-weight: 600;")
        self._marker_label.setVisible(False)
        stats_row.addWidget(self._marker_label)
        root.addLayout(stats_row)

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

    # ── Internal helpers ──────────────────────────────────────────────────────

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

    def _normalize(self, buf: np.ndarray) -> np.ndarray:
        rms = float(np.sqrt(np.mean(np.square(buf))))
        return buf / (rms * np.sqrt(2)) if rms > 1e-6 else buf.copy()

    def _set_current_vfo(self, vfo_id: int | None) -> None:
        if vfo_id is None:
            return
        self._current_vfo = int(vfo_id)
        self._vfo_label.setText(f"VFO: {self._current_vfo + 1}")
        self._stream_pos.pop(self._current_vfo, None)
        self._view_initialized = False
        self._refresh_plot()
        self.vfo_changed.emit(self._current_vfo)

    # ── Control callbacks ─────────────────────────────────────────────────────

    def _on_vfo_combo_changed(self, *_):
        data = self._vfo_combo.currentData()
        if data is not None:
            self._set_current_vfo(int(data))

    def _on_settings_changed(self, *_):
        self._refresh_plot()

    def _on_sps_changed(self, value: int):
        self._phase_spin.setMaximum(max(1, value - 1))
        self._refresh_plot()

    def _on_threshold_spin_changed(self, value: float):
        self._thresh_line.blockSignals(True)
        self._thresh_line.setValue(value)
        self._thresh_line.blockSignals(False)
        self._refresh_plot()

    def _on_thresh_line_dragged(self):
        val = round(float(self._thresh_line.value()), 2)
        self._thresh_spin.blockSignals(True)
        self._thresh_spin.setValue(val)
        self._thresh_spin.blockSignals(False)
        self._refresh_plot()

    def _on_scrub_changed(self, *_):
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

    def _on_markers_toggled(self, checked: bool):
        win = int(self._window_spin.value())
        self._marker_a.setPos(win * 0.25)
        self._marker_b.setPos(win * 0.75)
        self._marker_a.setVisible(checked)
        self._marker_b.setVisible(checked)
        self._marker_label.setVisible(checked)
        self._on_marker_moved()

    def _on_marker_moved(self):
        if not self._markers_btn.isChecked():
            return
        a = float(self._marker_a.value())
        b = float(self._marker_b.value())
        sps = max(1, int(self._sps_spin.value()))
        delta = abs(b - a)
        ms = delta / self._sample_rate * 1000.0
        syms = delta / sps
        self._marker_label.setText(
            f"A: {a:.0f}  B: {b:.0f}  ΔT: {ms:.3f} ms  ≈ {syms:.1f} sym"
        )

    def _clear_stream(self):
        self._bit_display.clear()
        if self._current_vfo is not None:
            buf = self._current_buffer()
            self._stream_pos[self._current_vfo] = buf.size

    def _decode_current(self):
        """Decode the currently displayed window and append it to the bit stream."""
        buf_full = self._get_analysis_buffer()
        sps = max(2, int(self._sps_spin.value()))
        if buf_full.size < sps * 2:
            return
        window = min(int(self._window_spin.value()), buf_full.size)
        buf_norm = self._normalize(buf_full)
        view = buf_norm[-window:]
        phase = int(self._phase_spin.value()) % sps
        threshold = float(self._thresh_spin.value())
        encoding = self._enc_combo.currentText()
        inverted = self._pol_combo.currentText() == "Inverted"
        signal = -view if inverted else view
        binary = _slice(signal, threshold)
        bits = decode_bits(binary, sps, phase, encoding)
        if not bits:
            return
        group = int(self._group_combo.currentText())
        fmt = self._fmt_combo.currentText()
        line = bits_to_text(bits, fmt, group)
        if line:
            self._bit_display.appendPlainText(line)

    # ── Auto-clock & auto-phase ───────────────────────────────────────────────

    def _auto_clock(self) -> None:
        buffer = self._get_analysis_buffer()
        if buffer.size < 500:
            return
        chunk = buffer[-min(buffer.size, 16384):].astype(np.float64)
        chunk -= chunk.mean()
        transitions = np.abs(np.diff(np.sign(chunk)))
        fft_len = 1 << (2 * len(transitions) - 1).bit_length()
        F = np.fft.rfft(transitions, fft_len)
        acf = np.fft.irfft(F * np.conj(F))
        if acf[0] < 1e-12:
            return
        acf = acf / acf[0]
        # Search for dominant period in [4, 1024] samples
        search = acf[4:1025]
        peak = int(np.argmax(search))
        if search[peak] < 0.03:
            return
        self._sps_spin.setValue(peak + 4)

    def _auto_phase(self) -> None:
        """Sweep phase offsets and pick the one with the fewest Manchester violations
        (or best NRZ eye opening for non-biphase encodings)."""
        buffer = self._get_analysis_buffer()
        sps = int(self._sps_spin.value())
        if buffer.size < sps * 12:
            return
        buf_norm = self._normalize(buffer)
        threshold = float(self._thresh_spin.value())
        inverted = self._pol_combo.currentText() == "Inverted"
        signal = -buf_norm if inverted else buf_norm
        binary = _slice(signal, threshold)
        encoding = self._enc_combo.currentText()

        best_phase, best_score = 0, -1.0
        for phase in range(sps):
            bits = decode_bits(binary, sps, phase, encoding)
            if not bits:
                continue
            valid = [b for b in bits if b >= 0]
            errors = len(bits) - len(valid)
            if not valid:
                continue
            # For biphase: minimise violations; for NRZ: maximise eye opening
            if encoding in ("Manchester (IEEE 802.3)", "Manchester (Thomas)",
                            "Differential Manchester", "Biphase Mark (BMC)"):
                score = 1.0 - errors / len(bits)
            else:
                raw = _centers(binary, sps, phase)
                if len(raw) < 4:
                    continue
                med = float(np.median(raw.astype(np.float32)))
                hi = raw[raw > med].astype(np.float32)
                lo = raw[raw <= med].astype(np.float32)
                if not len(hi) or not len(lo):
                    continue
                score = float(np.median(hi) - np.median(lo))
            if score > best_score:
                best_score, best_phase = score, phase

        self._phase_spin.setValue(best_phase)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _reset_view(self):
        window = min(int(self._window_spin.value()), max(1, self._current_buffer().size))
        self._plot.setXRange(0.0, float(window), padding=0.01)
        self._plot.setYRange(-1.6, 1.6, padding=0)

    def _clear_overlays(self):
        for item in self._sym_lines:
            self._plot.removeItem(item)
        self._sym_lines.clear()
        for item in self._bit_labels:
            self._plot.removeItem(item)
        self._bit_labels.clear()

    def _refresh_plot(self):
        buf_full = self._get_analysis_buffer()

        sps = max(2, int(self._sps_spin.value()))
        window = min(int(self._window_spin.value()), buf_full.size)

        if buf_full.size < sps * 2:
            self._curve.setData([], [])
            self._digital_curve.setData([], [])
            self._clear_overlays()
            self._plot.setTitle("Waiting for samples…")
            self._stats_label.setText("Waiting for samples…")
            return

        buf_norm = self._normalize(buf_full)
        view = buf_norm[-window:]

        phase = int(self._phase_spin.value()) % sps
        threshold = float(self._thresh_spin.value())
        encoding = self._enc_combo.currentText()
        inverted = self._pol_combo.currentText() == "Inverted"
        signal = -view if inverted else view

        x = np.arange(len(signal), dtype=np.float32)
        self._curve.setData(x, signal.astype(np.float32))
        if not self._view_initialized:
            self._plot.setXRange(0.0, float(len(x)), padding=0.01)
            self._view_initialized = True

        # Binary slice for current view
        binary = _slice(signal, threshold)
        bits = decode_bits(binary, sps, phase, encoding)

        # ── Symbol boundary lines + bit labels ────────────────────────────────
        self._clear_overlays()

        sym_xs = np.arange(phase, len(signal), sps, dtype=np.int64)
        n_draw = min(len(sym_xs), _MAX_SYM_MARKERS)
        boundary_pen = pg.mkPen(color=(90, 90, 200, 110), width=1, style=Qt.PenStyle.DotLine)
        show_labels = self._labels_cb.isChecked() and n_draw <= 80

        for i in range(n_draw):
            xs = float(sym_xs[i])
            line = pg.InfiniteLine(pos=xs, angle=90, pen=boundary_pen, movable=False)
            self._plot.addItem(line)
            self._sym_lines.append(line)

            if show_labels and i < len(bits):
                bv = bits[i]
                txt = "?" if bv == -1 else str(bv)
                col = "#ff4040" if bv == -1 else ("#00ff99" if bv == 1 else "#ffbb00")
                lbl = pg.TextItem(txt, color=col, anchor=(0.5, 1.0))
                lbl.setFont(QFont("Consolas", 8))
                lbl.setPos(xs + sps * 0.5, 1.4)
                self._plot.addItem(lbl)
                self._bit_labels.append(lbl)

        # ── Digital overlay (reconstructed square wave) ───────────────────────
        if self._digital_cb.isChecked() and len(sym_xs) > 0:
            sx, sy = [], []
            for i, xs in enumerate(sym_xs[:_MAX_SYM_MARKERS]):
                x0 = int(xs)
                x1 = int(min(xs + sps, len(signal)))
                level = float(binary[min(x0 + sps // 2, len(binary) - 1)]) * 2.0 - 1.0
                sx += [x0, x1]
                sy += [level, level]
            self._digital_curve.setData(np.array(sx, np.float32), np.array(sy, np.float32))
        else:
            self._digital_curve.setData([], [])

        # ── Incremental bit-stream output (live only) ─────────────────────────
        if self._is_live() and not self._freeze_btn.isChecked():
            vfo_id = self._current_vfo
            last = self._stream_pos.get(vfo_id, 0)
            # Only append when buffer has grown by ≥ 1 symbol since last output
            if vfo_id is not None and buf_full.size >= last + sps:
                new_portion = buf_norm[last:]
                new_sig = -new_portion if inverted else new_portion
                new_bin = _slice(new_sig, threshold)
                new_bits = decode_bits(new_bin, sps, phase, encoding)
                if new_bits:
                    group = int(self._group_combo.currentText())
                    fmt = self._fmt_combo.currentText()
                    line = bits_to_text(new_bits, fmt, group)
                    if line:
                        self._bit_display.appendPlainText(line)
                self._stream_pos[vfo_id] = buf_full.size

        # ── Stats ─────────────────────────────────────────────────────────────
        n_valid = sum(1 for b in bits if b >= 0)
        n_err = sum(1 for b in bits if b == -1)
        bit_rate = self._sample_rate / sps
        br_str = f"{bit_rate/1000:.2f} kbps" if bit_rate >= 1000 else f"{bit_rate:.1f} bps"
        err_str = f"  |  Errors: {n_err}" if n_err > 0 else ""
        self._plot.setTitle(f"{encoding}  |  {sps} samp/sym  |  {br_str}")
        self._stats_label.setText(
            f"Symbols: {n_valid + n_err}{err_str}  |  "
            f"Threshold: {threshold:.2f}  |  Phase: {phase}  |  "
            f"Bit rate: {br_str}"
        )

    # ── Window lifecycle ──────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
