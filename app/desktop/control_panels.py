"""
Control panels for VFO tabs, decoders, and device settings.
"""

from typing import Dict, Optional

from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QComboBox, QGroupBox,
    QListWidget, QListWidgetItem, QTextEdit, QTabWidget, QSizePolicy,
    QCheckBox,
)
from .widgets import AcceptCommaDoubleSpinBox
QDoubleSpinBox = AcceptCommaDoubleSpinBox
from PySide6.QtCore import Qt, Signal
import logging

logger = logging.getLogger(__name__)

from PySide6.QtGui import QValidator


# Subclass QDoubleSpinBox to accept both comma and dot as decimal separators.
class AcceptCommaDoubleSpinBox(QDoubleSpinBox):
    def valueFromText(self, text: str) -> float:
        # Convert comma to dot before parsing
        t = text.replace(',', '.')
        try:
            return float(t)
        except Exception:
            return super().valueFromText(t)

    def validate(self, text: str, pos: int):
        # Accept empty or a lone '-' as intermediate
        if text == '' or text == '-' or text == ',':
            return QValidator.Intermediate, text, pos
        t = text.replace(',', '.')
        try:
            # allow numbers with trailing decimal separator
            if t.endswith('.'):
                float(t[:-1])
                return QValidator.Intermediate, text, pos
            val = float(t)
        except Exception:
            return QValidator.Invalid, text, pos
        if self.minimum() <= val <= self.maximum():
            return QValidator.Acceptable, text, pos
        return QValidator.Intermediate, text, pos


# Use our subclass throughout this module wherever QDoubleSpinBox is used.
QDoubleSpinBox = AcceptCommaDoubleSpinBox

DECODER_NAMES = ['POCSAG', 'RDS', 'AIS', 'ADSB']


# ---------------------------------------------------------------------------
# Single VFO tab widget
# ---------------------------------------------------------------------------

class SingleVFOTab(QWidget):
    """Settings widget for one VFO (shown inside a tab)."""

    frequency_changed = Signal(int, float)   # vfo_id, Hz
    demod_changed = Signal(int, str)         # vfo_id, mode
    bandwidth_changed = Signal(int, float)   # vfo_id, Hz
    volume_changed = Signal(int, float)      # vfo_id, 0–1
    squelch_changed = Signal(int, float)     # vfo_id, dBFS
    mute_changed = Signal(int, bool)         # vfo_id, muted
    decoder_toggled = Signal(int, str, bool) # vfo_id, decoder_name, enabled

    def __init__(self, vfo_id: int, parent=None):
        super().__init__(parent)
        self.vfo_id = vfo_id
        self._muted = False
        self._suppress_signals = False
        self._initUI()

    def _initUI(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # --- Frequency ---
        freq_group = QGroupBox("Frequency")
        freq_layout = QHBoxLayout()
        freq_layout.addWidget(QLabel("MHz:"))
        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setRange(1.0, 6000.0)
        self.freq_spin.setValue(100.0)
        self.freq_spin.setDecimals(6)
        self.freq_spin.setSingleStep(0.001)
        self.freq_spin.valueChanged.connect(self._on_freq_changed)
        freq_layout.addWidget(self.freq_spin)
        freq_group.setLayout(freq_layout)
        layout.addWidget(freq_group)

        # --- Demod + Bandwidth ---
        demod_group = QGroupBox("Demodulation")
        demod_layout = QVBoxLayout()

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.demod_combo = QComboBox()
        self.demod_combo.addItems(['NFM', 'WFM', 'AM', 'USB', 'LSB', 'DSB', 'CW', 'IQ'])
        self.demod_combo.currentTextChanged.connect(
            lambda mode: self.demod_changed.emit(self.vfo_id, mode)
        )
        mode_row.addWidget(self.demod_combo)
        demod_layout.addLayout(mode_row)

        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel("BW (kHz):"))
        self.bw_spin = QDoubleSpinBox()
        self.bw_spin.setRange(0.5, 20_000.0)
        self.bw_spin.setValue(12.5)
        self.bw_spin.setDecimals(1)
        self.bw_spin.valueChanged.connect(
            lambda v: self.bandwidth_changed.emit(self.vfo_id, v * 1e3)
        )
        bw_row.addWidget(self.bw_spin)
        demod_layout.addLayout(bw_row)

        demod_group.setLayout(demod_layout)
        layout.addWidget(demod_group)

        # --- Audio ---
        audio_group = QGroupBox("Audio")
        audio_layout = QVBoxLayout()

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Vol:"))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        vol_row.addWidget(self.vol_slider)
        self.vol_label = QLabel("80%")
        self.vol_label.setFixedWidth(36)
        vol_row.addWidget(self.vol_label)
        audio_layout.addLayout(vol_row)

        mute_row = QHBoxLayout()
        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setCheckable(True)
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        mute_row.addWidget(self.mute_btn)
        mute_row.addStretch()
        audio_layout.addLayout(mute_row)

        sq_row = QHBoxLayout()
        sq_row.addWidget(QLabel("Squelch:"))
        self.squelch_slider = QSlider(Qt.Horizontal)
        self.squelch_slider.setRange(-120, 0)
        self.squelch_slider.setValue(-100)
        self.squelch_slider.valueChanged.connect(self._on_squelch_changed)
        sq_row.addWidget(self.squelch_slider)
        self.squelch_label = QLabel("-100")
        self.squelch_label.setFixedWidth(36)
        sq_row.addWidget(self.squelch_label)
        audio_layout.addLayout(sq_row)

        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)

        # --- Decoders ---
        dec_group = QGroupBox("Decoders")
        dec_layout = QVBoxLayout()
        self.decoder_list = QListWidget()
        self.decoder_list.setMaximumHeight(100)
        for name in DECODER_NAMES:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.decoder_list.addItem(item)
        self.decoder_list.itemChanged.connect(self._on_decoder_toggled)
        dec_layout.addWidget(self.decoder_list)

        dec_layout.addWidget(QLabel("Output:"))
        self.decoder_output = QTextEdit()
        self.decoder_output.setReadOnly(True)
        self.decoder_output.setMaximumHeight(80)
        self.decoder_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dec_layout.addWidget(self.decoder_output)

        dec_group.setLayout(dec_layout)
        layout.addWidget(dec_group)

        layout.addStretch()
        self.setLayout(layout)

    # ------------------------------------------------------------------
    # Public setters (called from main window without triggering signals)
    # ------------------------------------------------------------------

    def set_frequency_silent(self, freq_hz: float):
        """Update frequency spinbox without emitting frequency_changed."""
        self._suppress_signals = True
        self.freq_spin.setValue(freq_hz / 1e6)
        self._suppress_signals = False

    def get_frequency_hz(self) -> float:
        return self.freq_spin.value() * 1e6

    def get_bandwidth_hz(self) -> float:
        return self.bw_spin.value() * 1e3

    def append_decoder_output(self, decoder_name: str, text: str):
        self.decoder_output.append(f"[{decoder_name}] {text}")
        # Keep at most 200 lines
        doc = self.decoder_output.document()
        while doc.blockCount() > 200:
            cursor = self.decoder_output.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    # ------------------------------------------------------------------
    # Internal signal handlers
    # ------------------------------------------------------------------

    def _on_freq_changed(self, value_mhz: float):
        if not self._suppress_signals:
            self.frequency_changed.emit(self.vfo_id, value_mhz * 1e6)

    def _on_volume_changed(self, value: int):
        self.vol_label.setText(f"{value}%")
        self.volume_changed.emit(self.vfo_id, value / 100.0)

    def _on_mute_toggled(self, checked: bool):
        self._muted = checked
        self.mute_btn.setText("Unmute" if checked else "Mute")
        self.mute_changed.emit(self.vfo_id, checked)

    def _on_squelch_changed(self, value: int):
        self.squelch_label.setText(str(value))
        self.squelch_changed.emit(self.vfo_id, float(value))

    def _on_decoder_toggled(self, item: QListWidgetItem):
        name = item.text()
        enabled = item.checkState() == Qt.CheckState.Checked
        self.decoder_toggled.emit(self.vfo_id, name, enabled)


# ---------------------------------------------------------------------------
# VFO tab panel
# ---------------------------------------------------------------------------

class VFOTabPanel(QWidget):
    """
    Tab widget managing multiple VFOs. Each tab is a SingleVFOTab.
    A permanent '+' tab at the right adds a new VFO; each tab has an 'x' close button.
    All per-VFO signals are re-emitted here so main_window only needs to connect once.
    """

    vfo_added = Signal(int)          # new vfo_id
    vfo_removed = Signal(int)        # removed vfo_id
    active_vfo_changed = Signal(int) # vfo_id of newly selected tab

    # Re-emitted per-VFO signals
    frequency_changed = Signal(int, float)
    demod_changed = Signal(int, str)
    bandwidth_changed = Signal(int, float)
    volume_changed = Signal(int, float)
    squelch_changed = Signal(int, float)
    mute_changed = Signal(int, bool)
    decoder_toggled = Signal(int, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tabs: Dict[int, SingleVFOTab] = {}   # vfo_id → widget
        self._vfo_colors: Dict[int, str] = {}

        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_widget.currentChanged.connect(self._on_current_changed)

        # "+" button in the corner
        add_btn = QPushButton("+")
        add_btn.setFixedSize(24, 24)
        add_btn.setToolTip("Add VFO")
        add_btn.clicked.connect(self._add_vfo)
        self._tab_widget.setCornerWidget(add_btn, Qt.TopRightCorner)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tab_widget)
        self.setLayout(layout)

        # Create first VFO on startup
        self._add_vfo()

    # ------------------------------------------------------------------
    # VFO lifecycle
    # ------------------------------------------------------------------

    def _add_vfo(self) -> int:
        """Create a new VFO tab and return its vfo_id."""
        # Determine next vfo_id (never reuse deleted IDs within this session)
        existing = list(self._tabs.keys())
        vfo_id = (max(existing) + 1) if existing else 0

        tab = SingleVFOTab(vfo_id)
        self._tabs[vfo_id] = tab

        # Wire all signals up
        tab.frequency_changed.connect(self.frequency_changed)
        tab.demod_changed.connect(self.demod_changed)
        tab.bandwidth_changed.connect(self.bandwidth_changed)
        tab.volume_changed.connect(self.volume_changed)
        tab.squelch_changed.connect(self.squelch_changed)
        tab.mute_changed.connect(self.mute_changed)
        tab.decoder_toggled.connect(self.decoder_toggled)

        label = f"VFO {vfo_id + 1}"
        self._tab_widget.addTab(tab, label)
        self._tab_widget.setCurrentWidget(tab)

        self.vfo_added.emit(vfo_id)
        logger.info(f"VFOTabPanel: added VFO {vfo_id}")
        return vfo_id

    def _on_tab_close_requested(self, tab_index: int):
        if self._tab_widget.count() <= 1:
            return  # always keep at least one VFO
        widget = self._tab_widget.widget(tab_index)
        vfo_id = self._widget_to_vfo_id(widget)
        if vfo_id is None:
            return
        self._tab_widget.removeTab(tab_index)
        del self._tabs[vfo_id]
        self.vfo_removed.emit(vfo_id)
        logger.info(f"VFOTabPanel: removed VFO {vfo_id}")

    def _on_current_changed(self, index: int):
        widget = self._tab_widget.widget(index)
        vfo_id = self._widget_to_vfo_id(widget)
        if vfo_id is not None:
            self.active_vfo_changed.emit(vfo_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def active_vfo_id(self) -> Optional[int]:
        widget = self._tab_widget.currentWidget()
        return self._widget_to_vfo_id(widget)

    def set_frequency(self, vfo_id: int, freq_hz: float):
        """Update the spinbox for a VFO without re-emitting frequency_changed."""
        tab = self._tabs.get(vfo_id)
        if tab:
            tab.set_frequency_silent(freq_hz)

    def add_decoder_output(self, vfo_id: int, decoder_name: str, text: str):
        tab = self._tabs.get(vfo_id)
        if tab:
            tab.append_decoder_output(decoder_name, text)

    def set_vfo_color(self, vfo_id: int, color: str):
        """Set a small colored dot next to the VFO tab label."""
        self._vfo_colors[vfo_id] = color
        idx = self._vfo_id_to_tab_index(vfo_id)
        if idx < 0:
            return

        icon = self._make_color_icon(color)
        self._tab_widget.setTabIcon(idx, icon)

    def _make_color_icon(self, color: str) -> QIcon:
        pixmap = QPixmap(10, 10)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(1, 1, 8, 8)
        painter.end()

        return QIcon(pixmap)

    def set_vfo_out_of_range(self, vfo_id: int, out_of_range: bool):
        """Gray the tab label and disable only decoding when the VFO is outside the SDR bandwidth."""
        tab = self._tabs.get(vfo_id)
        if tab is None:
            return
        tab.decoder_list.setEnabled(not out_of_range)
        idx = self._vfo_id_to_tab_index(vfo_id)
        if idx >= 0:
            color = QColor('#888888') if out_of_range else QColor('#ffffff')
            self._tab_widget.tabBar().setTabTextColor(idx, color)
            if out_of_range:
                self._tab_widget.setTabIcon(idx, QIcon())
            else:
                vfo_color = self._vfo_colors.get(vfo_id)
                if vfo_color:
                    self._tab_widget.setTabIcon(idx, self._make_color_icon(vfo_color))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _vfo_id_to_tab_index(self, vfo_id: int) -> int:
        for i in range(self._tab_widget.count()):
            if self._widget_to_vfo_id(self._tab_widget.widget(i)) == vfo_id:
                return i
        return -1

    def _widget_to_vfo_id(self, widget: Optional[QWidget]) -> Optional[int]:
        for vfo_id, tab in self._tabs.items():
            if tab is widget:
                return vfo_id
        return None


# ---------------------------------------------------------------------------
# Device panel (unchanged from original, lightly cleaned up)
# ---------------------------------------------------------------------------

class DevicePanel(QWidget):
    """Device settings and status panel."""

    center_freq_changed = Signal(float)  # Hz
    sample_rate_changed = Signal(float)  # Hz
    lna_gain_changed = Signal(int)
    vga_gain_changed = Signal(int)
    amp_enabled_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initUI()

    def _initUI(self):
        layout = QVBoxLayout()

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status:"))
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: red;")
        status_row.addWidget(self.status_label)
        layout.addLayout(status_row)

        # Center frequency
        cf_row = QHBoxLayout()
        cf_row.addWidget(QLabel("Center Freq (MHz):"))
        self.cf_spin = QDoubleSpinBox()
        self.cf_spin.setRange(1.0, 6000.0)
        self.cf_spin.setValue(100.0)
        self.cf_spin.setDecimals(3)
        self.cf_spin.setSingleStep(1.0)
        self.cf_spin.valueChanged.connect(
            lambda v: self.center_freq_changed.emit(v * 1e6)
        )
        cf_row.addWidget(self.cf_spin)
        layout.addLayout(cf_row)

        # Sample rate
        sr_row = QHBoxLayout()
        sr_row.addWidget(QLabel("Sample Rate (MHz):"))
        self.sr_combo = QComboBox()
        self.sr_combo.addItems(['1', '2', '4', '8', '16', '20'])
        self.sr_combo.setCurrentText('20')
        self.sr_combo.currentTextChanged.connect(
            lambda v: self.sample_rate_changed.emit(int(float(v) * 1e6))
        )
        sr_row.addWidget(self.sr_combo)
        layout.addLayout(sr_row)

        # LNA
        lna_row = QHBoxLayout()
        lna_row.addWidget(QLabel("LNA (dB):"))
        self.lna_slider = QSlider(Qt.Horizontal)
        self.lna_slider.setRange(0, 40)
        self.lna_slider.setValue(24)
        self.lna_slider.setTickPosition(QSlider.TicksBelow)
        self.lna_slider.setTickInterval(8)
        self.lna_slider.valueChanged.connect(self.lna_gain_changed.emit)
        lna_row.addWidget(self.lna_slider)
        self.lna_label = QLabel("24")
        self.lna_label.setFixedWidth(28)
        lna_row.addWidget(self.lna_label)
        self.lna_slider.valueChanged.connect(lambda v: self.lna_label.setText(str(v)))
        layout.addLayout(lna_row)

        # VGA
        vga_row = QHBoxLayout()
        vga_row.addWidget(QLabel("VGA (dB):"))
        self.vga_slider = QSlider(Qt.Horizontal)
        self.vga_slider.setRange(0, 62)
        self.vga_slider.setValue(20)
        self.vga_slider.setTickPosition(QSlider.TicksBelow)
        self.vga_slider.setTickInterval(10)
        self.vga_slider.valueChanged.connect(self.vga_gain_changed.emit)
        vga_row.addWidget(self.vga_slider)
        self.vga_label = QLabel("20")
        self.vga_label.setFixedWidth(28)
        vga_row.addWidget(self.vga_label)
        self.vga_slider.valueChanged.connect(lambda v: self.vga_label.setText(str(v)))
        layout.addLayout(vga_row)

        # RF Amp
        amp_row = QHBoxLayout()
        self.amp_check = QCheckBox("RF Amp (~11 dB)")
        self.amp_check.setChecked(False)
        self.amp_check.toggled.connect(self.amp_enabled_changed.emit)
        amp_row.addWidget(self.amp_check)
        amp_row.addStretch()
        layout.addLayout(amp_row)

        self.setLayout(layout)

    def set_connected(self, connected: bool):
        if connected:
            self.status_label.setText("Connected")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: red;")


# ---------------------------------------------------------------------------
# Combined control panel
# ---------------------------------------------------------------------------

class ControlPanel(QWidget):
    """Right-side panel: VFO tabs on top, device settings below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initUI()

    def _initUI(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)

        # VFO tabs occupy most of the space
        self.vfo_tab = VFOTabPanel()
        layout.addWidget(self.vfo_tab, stretch=3)

        # Device group at the bottom
        device_group = QGroupBox("Device")
        device_layout = QVBoxLayout()
        self.device_panel = DevicePanel()
        device_layout.addWidget(self.device_panel)
        device_group.setLayout(device_layout)
        layout.addWidget(device_group, stretch=1)

        self.setLayout(layout)
