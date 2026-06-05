"""
Control panels for VFO tabs, decoders, and device settings.
"""

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Callable, Dict, Optional

from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QComboBox, QGroupBox,
    QListWidget, QListWidgetItem, QTextEdit, QTabWidget, QSizePolicy,
    QCheckBox, QFileDialog, QInputDialog, QToolButton, QFrame,
)
from PySide6.QtWidgets import QScrollArea
from .widgets import AcceptCommaDoubleSpinBox
from app.decoders.modulation import MODULATION_DECODER_NAMES
QDoubleSpinBox = AcceptCommaDoubleSpinBox
from PySide6.QtCore import Qt, Signal, QTimer
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

DECODER_NAMES = ['POCSAG', 'ADSB', 'TETRA', 'ACARS', 'DMR', 'FSK', 'Manchester', 'DVB-T', 'WEFAX'] + [n for n in MODULATION_DECODER_NAMES if n != 'FSK']

_BOOKMARK_FILE = 'bookmarks.json'


class CollapsibleSection(QWidget):
    def __init__(self, title: str, content_widget: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)

        self._content_widget = content_widget
        self._content_container = QWidget()
        self._content_container.setObjectName("collapsible_content_container")
        container_layout = QVBoxLayout(self._content_container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        container_layout.setSpacing(0)
        container_layout.addWidget(content_widget)
        self._content_container.setStyleSheet(
            "QWidget#collapsible_content_container {"
            "  border: 1px solid rgba(255, 255, 255, 0.14);"
            "  border-radius: 6px;"
            "}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.toggle_button = QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setAutoRaise(True)
        self.toggle_button.setStyleSheet(
            "QToolButton {"
            "  border: none;"
            "  padding: 4px 2px;"
            "  font-weight: 600;"
            "  text-align: left;"
            "}"
            "QToolButton:hover {"
            "  background: rgba(255, 255, 255, 0.04);"
            "  border-radius: 4px;"
            "}"
        )
        self.toggle_button.toggled.connect(self._on_toggled)

        outer.addWidget(self.toggle_button)
        outer.addWidget(self._content_container)

        self._content_container.setVisible(expanded)

    def _on_toggled(self, checked: bool):
        self._content_container.setVisible(checked)
        self.toggle_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)


@dataclass
class VFOBookmark:
    name: str
    frequency: float        # Hz
    demod_mode: str  = 'NFM'
    bandwidth: float = 12_500.0  # Hz
    squelch_level: float = -100.0
    volume: float    = 0.8
    decoders: list = field(default_factory=list)

    def display_label(self) -> str:
        mhz = self.frequency / 1e6
        bw  = self.bandwidth / 1e3
        if self.decoders:
            dec_text = ', '.join(self.decoders[:4])
            if len(self.decoders) > 4:
                dec_text += ', ...'
            return f"{self.name}  —  {mhz:.4f} MHz  [{self.demod_mode}, {bw:.1f} kHz, {dec_text}]"
        return f"{self.name}  —  {mhz:.4f} MHz  [{self.demod_mode}, {bw:.1f} kHz]"


# ---------------------------------------------------------------------------
# Single VFO tab widget
# ---------------------------------------------------------------------------

class SingleVFOTab(QWidget):
    """Settings widget for one VFO (shown inside a tab)."""

    frequency_changed = Signal(int, float)    # vfo_id, Hz
    demod_changed = Signal(int, str)          # vfo_id, mode
    bandwidth_changed = Signal(int, float)    # vfo_id, Hz
    volume_changed = Signal(int, float)       # vfo_id, 0–1
    squelch_changed = Signal(int, float)      # vfo_id, dBFS
    squelch_enabled_changed = Signal(int, bool) # vfo_id, enabled
    mute_changed = Signal(int, bool)          # vfo_id, muted
    paused_changed = Signal(int, bool)        # vfo_id, paused
    decoder_toggled = Signal(int, str, bool)  # vfo_id, decoder_name, enabled
    open_window_requested = Signal(int, str)  # vfo_id, decoder_name

    def __init__(self, vfo_id: int, parent=None):
        super().__init__(parent)
        self.vfo_id = vfo_id
        self._muted = False
        self._paused = False
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
        self.demod_combo.addItems(['NFM', 'WFM', 'AM', 'USB', 'LSB', 'DSB', 'CW', 'IQ', 'Raw IQ'])
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
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Freeze signal capture (buffers stop updating)")
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        mute_row.addWidget(self.pause_btn)
        mute_row.addStretch()
        audio_layout.addLayout(mute_row)

        sq_row = QHBoxLayout()
        self.squelch_check = QCheckBox("SQL")
        self.squelch_check.setToolTip("Enable squelch")
        self.squelch_check.setChecked(True)
        self.squelch_check.toggled.connect(self._on_squelch_enabled_changed)
        sq_row.addWidget(self.squelch_check)
        self.squelch_slider = QSlider(Qt.Horizontal)
        self.squelch_slider.setRange(-120, 120)
        self.squelch_slider.setValue(-100)
        self.squelch_slider.valueChanged.connect(self._on_squelch_changed)
        sq_row.addWidget(self.squelch_slider)
        self.squelch_label = QLabel("-100")
        self.squelch_label.setFixedWidth(36)
        sq_row.addWidget(self.squelch_label)
        audio_layout.addLayout(sq_row)

        sig_row = QHBoxLayout()
        sig_row.addWidget(QLabel("Signal:"))
        self.signal_strength_label = QLabel("--- dB")
        self.signal_strength_label.setFixedWidth(80)
        self.signal_strength_label.setStyleSheet("color: #888888;")
        sig_row.addWidget(self.signal_strength_label)
        sig_row.addStretch()
        audio_layout.addLayout(sig_row)

        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)

        # --- Decoders ---
        dec_group = QGroupBox("Decoders")
        dec_layout = QVBoxLayout()
        self.decoder_list = QListWidget()
        self.decoder_list.setMaximumHeight(150)
        self.decoder_list.setMinimumHeight(100)
        for name in DECODER_NAMES:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.decoder_list.addItem(item)
        self.decoder_list.itemChanged.connect(self._on_decoder_toggled)
        self.decoder_list.itemSelectionChanged.connect(self._update_view_btn)
        self.decoder_list.itemChanged.connect(self._update_view_btn)
        dec_layout.addWidget(self.decoder_list)

        view_row = QHBoxLayout()
        self.view_btn = QPushButton("Open View…")
        self.view_btn.setEnabled(False)
        self.view_btn.setToolTip("Open a visualization window for the selected decoder")
        self.view_btn.clicked.connect(self._on_view_clicked)
        view_row.addWidget(self.view_btn)
        view_row.addStretch()
        dec_layout.addLayout(view_row)

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

    def update_signal_strength(self, db: float, sq_open: bool):
        """Update signal strength display. Called from a polling timer in main_window."""
        self.signal_strength_label.setText(f"{db:.1f} dB")
        if sq_open:
            self.signal_strength_label.setStyleSheet("color: #00cc44; font-weight: bold;")
        else:
            self.signal_strength_label.setStyleSheet("color: #cc2222;")

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

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")
        self.pause_btn.setStyleSheet(
            "color: #f0a500; font-weight: 600;" if checked else ""
        )
        self.paused_changed.emit(self.vfo_id, checked)

    def is_paused(self) -> bool:
        return self._paused

    def _on_squelch_changed(self, value: int):
        self.squelch_label.setText(str(value))
        self.squelch_changed.emit(self.vfo_id, float(value))

    def _on_squelch_enabled_changed(self, enabled: bool):
        self.squelch_enabled_changed.emit(self.vfo_id, enabled)

    def _on_decoder_toggled(self, item: QListWidgetItem):
        name = item.text()
        enabled = item.checkState() == Qt.CheckState.Checked
        self.decoder_toggled.emit(self.vfo_id, name, enabled)

    def _update_view_btn(self):
        from app.desktop.decoder_windows import has_window
        item = self.decoder_list.currentItem()
        if item is None:
            self.view_btn.setEnabled(False)
            return
        checked = item.checkState() == Qt.CheckState.Checked
        self.view_btn.setEnabled(checked and has_window(item.text()))

    def _on_view_clicked(self):
        item = self.decoder_list.currentItem()
        if item is not None:
            self.open_window_requested.emit(self.vfo_id, item.text())


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
    vfo_renamed = Signal(int, str)   # vfo_id, new name

    # Re-emitted per-VFO signals
    frequency_changed = Signal(int, float)
    demod_changed = Signal(int, str)
    bandwidth_changed = Signal(int, float)
    volume_changed = Signal(int, float)
    squelch_changed = Signal(int, float)
    squelch_enabled_changed = Signal(int, bool)
    mute_changed = Signal(int, bool)
    paused_changed = Signal(int, bool)
    decoder_toggled = Signal(int, str, bool)
    open_window_requested = Signal(int, str)  # vfo_id, decoder_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tabs: Dict[int, SingleVFOTab] = {}   # vfo_id → widget
        self._vfo_colors: Dict[int, str] = {}

        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_widget.currentChanged.connect(self._on_current_changed)

        # Context menu on tab bar for rename
        try:
            tabbar = self._tab_widget.tabBar()
            tabbar.setContextMenuPolicy(Qt.CustomContextMenu)
            tabbar.customContextMenuRequested.connect(self._on_tab_context_menu)
        except Exception:
            pass

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
        tab.squelch_enabled_changed.connect(self.squelch_enabled_changed)
        tab.mute_changed.connect(self.mute_changed)
        tab.paused_changed.connect(self.paused_changed)
        tab.decoder_toggled.connect(self.decoder_toggled)
        tab.open_window_requested.connect(self.open_window_requested)

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

    def _on_tab_context_menu(self, pos):
        """Handle right-click on a tab to offer rename option."""
        try:
            tabbar = self._tab_widget.tabBar()
            idx = tabbar.tabAt(pos)
            if idx < 0:
                return
            widget = self._tab_widget.widget(idx)
            vfo_id = self._widget_to_vfo_id(widget)
            if vfo_id is None:
                return
            current = self._tab_widget.tabText(idx)
            name, ok = QInputDialog.getText(self, "Rename VFO", "Name:", text=current)
            if not ok:
                return
            name = name.strip()
            if not name:
                name = current
            self._tab_widget.setTabText(idx, name)
            try:
                self.vfo_renamed.emit(vfo_id, name)
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def active_vfo_id(self) -> Optional[int]:
        widget = self._tab_widget.currentWidget()
        return self._widget_to_vfo_id(widget)

    def set_active_vfo(self, vfo_id: int):
        tab = self._tabs.get(vfo_id)
        if tab is not None:
            self._tab_widget.setCurrentWidget(tab)

    def set_frequency(self, vfo_id: int, freq_hz: float):
        """Update the spinbox for a VFO without re-emitting frequency_changed."""
        tab = self._tabs.get(vfo_id)
        if tab:
            tab.set_frequency_silent(freq_hz)

    def add_decoder_output(self, vfo_id: int, decoder_name: str, text: str):
        tab = self._tabs.get(vfo_id)
        if tab:
            tab.append_decoder_output(decoder_name, text)

    def update_signal_strength(self, vfo_id: int, db: float, sq_open: bool):
        tab = self._tabs.get(vfo_id)
        if tab:
            tab.update_signal_strength(db, sq_open)

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

    def add_vfo(self) -> int:
        """Public wrapper — add a VFO tab and return its vfo_id."""
        return self._add_vfo()

    def get_active_vfo_snapshot(self) -> Optional[dict]:
        """Return current active VFO settings as a plain dict (for bookmarking)."""
        vfo_id = self.active_vfo_id()
        tab    = self._tabs.get(vfo_id)
        if tab is None:
            return None
        decoders = []
        for i in range(tab.decoder_list.count()):
            item = tab.decoder_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                decoders.append(item.text())
        return {
            'frequency':     tab.freq_spin.value() * 1e6,
            'demod_mode':    tab.demod_combo.currentText(),
            'bandwidth':     tab.bw_spin.value() * 1e3,
            'squelch_level': float(tab.squelch_slider.value()),
            'volume':        tab.vol_slider.value() / 100.0,
            'decoders':      decoders,
        }

    def get_all_vfo_settings(self) -> dict:
        """Return a dict of all VFO settings and per-vfo enabled decoders."""
        out = {}
        for vfo_id, tab in self._tabs.items():
            # tab fields
            decoders = []
            for i in range(tab.decoder_list.count()):
                item = tab.decoder_list.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    decoders.append(item.text())

            name_idx = self._vfo_id_to_tab_index(vfo_id)
            name = self._tab_widget.tabText(name_idx) if name_idx >= 0 else f"VFO {vfo_id + 1}"

            out[str(vfo_id)] = {
                'name': name,
                'frequency': tab.freq_spin.value() * 1e6,
                'demod_mode': tab.demod_combo.currentText(),
                'bandwidth': tab.bw_spin.value() * 1e3,
                'volume': tab.vol_slider.value() / 100.0,
                'squelch_level': float(tab.squelch_slider.value()),
                'squelch_enabled': tab.squelch_check.isChecked(),
                'enabled': True,
                'decoders': decoders,
            }
        return out

    def apply_settings_to_vfo(self, vfo_id: int, settings: dict):
        """Apply a settings dict to a VFO tab, propagating all signals to the backend."""
        tab = self._tabs.get(vfo_id)
        if tab is None:
            return
        tab.freq_spin.setValue(settings.get('frequency', 100e6) / 1e6)
        tab.demod_combo.setCurrentText(settings.get('demod_mode', 'NFM'))
        tab.bw_spin.setValue(settings.get('bandwidth', 12_500.0) / 1e3)
        tab.squelch_slider.setValue(int(settings.get('squelch_level', -100.0)))
        tab.vol_slider.setValue(int(settings.get('volume', 0.8) * 100))
        # Rename tab to bookmark name if provided (truncated to 12 chars to fit tab bar)
        name = settings.get('name', '').strip()
        if name:
            idx = self._vfo_id_to_tab_index(vfo_id)
            if idx >= 0:
                self._tab_widget.setTabText(idx, name)
            try:
                self.vfo_renamed.emit(vfo_id, name)
            except Exception:
                pass

        decoder_names = set(settings.get('decoders', []) or [])
        for i in range(tab.decoder_list.count()):
            item = tab.decoder_list.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.text() in decoder_names else Qt.CheckState.Unchecked)

    def set_vfo_bandwidth(self, vfo_id: int, bandwidth_hz: float):
        """Update the bandwidth spinbox without re-emitting bandwidth_changed."""
        tab = self._tabs.get(vfo_id)
        if tab is None:
            return
        tab.bw_spin.blockSignals(True)
        tab.bw_spin.setValue(bandwidth_hz / 1e3)
        tab.bw_spin.blockSignals(False)

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

    def replace_all_vfos(self, settings_list: list):
        """Remove all existing VFO tabs and create new ones from settings_list.

        Each entry in settings_list should be a dict suitable for
        `apply_settings_to_vfo`. This method emits `vfo_removed` for each
        removed VFO and `vfo_added` for each newly created VFO.
        """
        # Remove all existing tabs (emit vfo_removed for each)
        try:
            # collect indices by vfo_id so removals are safe
            existing = list(self._tabs.keys())
            # remove tabs in reverse tab index order to avoid shifting
            indices = []
            for vfo_id in existing:
                idx = self._vfo_id_to_tab_index(vfo_id)
                if idx >= 0:
                    indices.append((idx, vfo_id))
            indices.sort(reverse=True)
            for idx, vfo_id in indices:
                self._tab_widget.removeTab(idx)
                if vfo_id in self._tabs:
                    del self._tabs[vfo_id]
                try:
                    self.vfo_removed.emit(vfo_id)
                except Exception:
                    pass
        except Exception:
            pass

        # Create new VFOs from settings_list
        created = []
        try:
            first = True
            for s in settings_list:
                vid = self._add_vfo()
                self.apply_settings_to_vfo(vid, s)
                created.append(vid)
                first = False
        except Exception:
            pass
        # Ensure at least one VFO exists
        if not self._tabs:
            vid = self._add_vfo()
            created.append(vid)
        return created


# ---------------------------------------------------------------------------
# Bookmark panel
# ---------------------------------------------------------------------------

class BookmarkPanel(QWidget):
    """
    Persistent bookmark panel — always visible in the control sidebar.

    Bookmarks store a full VFO settings snapshot (frequency, mode, bandwidth,
    squelch, volume).  The file format is plain JSON so bookmarks can be shared
    or hand-edited.  An auto-save copy is kept at bookmarks.json alongside the
    running process.

    Signals
    -------
    bookmark_add_requested(dict)
        Emitted when the user wants to create a VFO from a bookmark.
        The dict matches the VFOBookmark field names.
    """

    bookmark_add_requested = Signal(dict)

    def __init__(self, get_vfo_snapshot: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self._bookmarks: list = []           # List[VFOBookmark]
        self._get_vfo_snapshot = get_vfo_snapshot
        self._current_file: Optional[str] = None
        self._initUI()
        self._autoload()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # ── toolbar row ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(3)
        self.import_btn   = QPushButton("Import")
        self.export_btn   = QPushButton("Export")
        self.save_vfo_btn = QPushButton("+ Save VFO")
        self.import_btn.setToolTip("Load bookmarks from a JSON file (merges with current list)")
        self.export_btn.setToolTip("Save all bookmarks to a JSON file")
        self.save_vfo_btn.setToolTip("Save the active VFO's settings as a new bookmark")
        for btn in (self.import_btn, self.export_btn, self.save_vfo_btn):
            toolbar.addWidget(btn)
        outer.addLayout(toolbar)

        # ── bookmark list ──
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(140)
        self.list_widget.setMinimumHeight(140)
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setToolTip("Double-click a bookmark to add it as a new VFO")
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._on_add_to_vfo())
        outer.addWidget(self.list_widget)

        # ── action row ──
        action_row = QHBoxLayout()
        action_row.setSpacing(3)
        self.add_btn    = QPushButton("Add to VFOs")
        self.remove_btn = QPushButton("Remove")
        self.add_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.add_btn.setToolTip("Create a new VFO tab with this bookmark's settings")
        self.remove_btn.setToolTip("Delete selected bookmark from the list")
        action_row.addWidget(self.add_btn)
        action_row.addWidget(self.remove_btn)
        outer.addLayout(action_row)
        self.setLayout(outer)

        self.import_btn.clicked.connect(self._on_import)
        self.export_btn.clicked.connect(self._on_export)
        self.save_vfo_btn.clicked.connect(self._on_save_vfo)
        self.add_btn.clicked.connect(self._on_add_to_vfo)
        self.remove_btn.clicked.connect(self._on_remove)

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    def _refresh_list(self):
        self.list_widget.clear()
        for bm in self._bookmarks:
            item = QListWidgetItem(bm.display_label())
            item.setToolTip(bm.display_label())
            self.list_widget.addItem(item)

    def _on_selection_changed(self):
        has = bool(self.list_widget.selectedItems())
        self.add_btn.setEnabled(has)
        self.remove_btn.setEnabled(has)

    def _selected_bookmark(self) -> Optional[VFOBookmark]:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._bookmarks):
            return self._bookmarks[row]
        return None

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def _on_add_to_vfo(self):
        bm = self._selected_bookmark()
        if bm:
            self.bookmark_add_requested.emit(asdict(bm))

    def _on_remove(self):
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._bookmarks):
            self._bookmarks.pop(row)
            self._refresh_list()
            self._autosave()

    def _on_save_vfo(self):
        if self._get_vfo_snapshot is None:
            return
        snapshot = self._get_vfo_snapshot()
        if snapshot is None:
            return
        default_name = f"{snapshot.get('frequency', 100e6) / 1e6:.4f} MHz"
        name, ok = QInputDialog.getText(
            self, "Save Bookmark", "Bookmark name:", text=default_name
        )
        if not ok or not name.strip():
            return
        bm = VFOBookmark(
            name=name.strip(),
            frequency=snapshot.get('frequency', 100e6),
            demod_mode=snapshot.get('demod_mode', 'NFM'),
            bandwidth=snapshot.get('bandwidth', 12_500.0),
            squelch_level=snapshot.get('squelch_level', -100.0),
            volume=snapshot.get('volume', 0.8),
            decoders=list(snapshot.get('decoders', []) or []),
        )
        self._bookmarks.append(bm)
        self._refresh_list()
        self._autosave()

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Bookmarks", "",
            "Bookmark files (*.json);;All files (*)"
        )
        if path:
            added = self._load_file(path)
            if added is not None:
                self._current_file = path

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Bookmarks",
            self._current_file or _BOOKMARK_FILE,
            "Bookmark files (*.json);;All files (*)"
        )
        if path:
            self._save_file(path)
            self._current_file = path

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _autoload(self):
        if os.path.exists(_BOOKMARK_FILE):
            self._load_file(_BOOKMARK_FILE)
            self._current_file = _BOOKMARK_FILE

    def _autosave(self):
        self._save_file(self._current_file or _BOOKMARK_FILE)

    def _load_file(self, path: str) -> Optional[int]:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            loaded = []
            for item in data.get('bookmarks', []):
                loaded.append(VFOBookmark(
                    name=item.get('name', 'Unnamed'),
                    frequency=float(item.get('frequency', 100e6)),
                    demod_mode=item.get('demod_mode', 'NFM'),
                    bandwidth=float(item.get('bandwidth', 12_500.0)),
                    squelch_level=float(item.get('squelch_level', -100.0)),
                    volume=float(item.get('volume', 0.8)),
                    decoders=list(item.get('decoders', []) or []),
                ))
            # Merge: skip entries already present (same name + frequency)
            existing_keys = {(b.name, b.frequency) for b in self._bookmarks}
            new = [b for b in loaded if (b.name, b.frequency) not in existing_keys]
            self._bookmarks.extend(new)
            self._refresh_list()
            logger.info("Loaded %d bookmark(s) from %s", len(new), path)
            return len(new)
        except Exception as e:
            logger.error("Failed to load bookmarks from %s: %s", path, e)
            return None

    def _save_file(self, path: str):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(
                    {'version': 1, 'bookmarks': [asdict(b) for b in self._bookmarks]},
                    f, indent=2,
                )
            logger.info("Saved %d bookmark(s) to %s", len(self._bookmarks), path)
        except Exception as e:
            logger.error("Failed to save bookmarks to %s: %s", path, e)


# ---------------------------------------------------------------------------
# Source panel  (replaces the old DevicePanel; supports HackRF + IQ file)
# ---------------------------------------------------------------------------

class SourcePanel(QWidget):
    """Source selection panel: HackRF live capture, HackRF sweep, or IQ file playback.

    Keeps the same external signal/method names as the old DevicePanel so
    the rest of the codebase needs only minimal changes.
    """

    # ---- Existing DevicePanel signals (backward-compat) ----
    center_freq_changed  = Signal(float)   # Hz
    sample_rate_changed  = Signal(float)   # Hz
    lna_gain_changed     = Signal(int)
    vga_gain_changed     = Signal(int)
    amp_enabled_changed  = Signal(bool)

    # ---- New signals ----
    connect_requested        = Signal()
    disconnect_requested     = Signal()
    file_source_opened       = Signal(str, float, float)   # path, center_freq, sample_rate
    record_start_requested   = Signal(str, str, float)     # file_path, fmt, max_duration_s
    record_stop_requested    = Signal()
    playback_play_requested  = Signal()
    playback_pause_requested = Signal()
    playback_stop_requested  = Signal()
    playback_speed_changed   = Signal(float)
    playback_seek_requested  = Signal(int)     # sample position
    playback_loop_changed    = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Playback scrubber state
        self._file_total_samples: int = 0
        self._file_sample_rate: float = 20e6
        self._scrubber_seeking: bool = False
        self._recording_path: str = ''
        self._is_connected: bool = False

        # Debounce timer — fires connect_requested 500 ms after last sweep param change
        self._sweep_reconnect_timer = QTimer(self)
        self._sweep_reconnect_timer.setSingleShot(True)
        self._sweep_reconnect_timer.setInterval(500)
        self._sweep_reconnect_timer.timeout.connect(self._apply_sweep_params)

        self._initUI()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Source type selector
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["HackRF", "IQ File"])
        src_row.addWidget(self.source_combo)
        src_row.addStretch()
        layout.addLayout(src_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #404040;")
        layout.addWidget(sep)

        # HackRF panel (includes sweep mode toggle)
        self._hackrf_panel = self._make_hackrf_panel()
        layout.addWidget(self._hackrf_panel)

        # IQ File panel (hidden by default)
        self._file_panel = self._make_file_panel()
        layout.addWidget(self._file_panel)
        self._file_panel.setVisible(False)

        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        self.setLayout(layout)

    def _make_hackrf_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Status + connect/disconnect buttons
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status:"))
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: red;")
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setToolTip("Connect / restart with current settings")
        self.connect_btn.clicked.connect(self.connect_requested.emit)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setToolTip("Disconnect HackRF (switches to demo mode)")
        self.disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        status_row.addWidget(self.connect_btn)
        status_row.addWidget(self.disconnect_btn)
        lay.addLayout(status_row)

        # Sweep mode toggle
        sweep_toggle_row = QHBoxLayout()
        self.sweep_mode_check = QCheckBox("Sweep Mode")
        self.sweep_mode_check.setToolTip(
            "Sweep across a frequency range instead of fixed capture"
        )
        self.sweep_mode_check.toggled.connect(self._on_sweep_mode_toggled)
        sweep_toggle_row.addWidget(self.sweep_mode_check)
        sweep_toggle_row.addStretch()
        lay.addLayout(sweep_toggle_row)

        # ---- Normal-mode controls (hidden in sweep mode) ----
        self._hackrf_normal_controls = QWidget()
        nc = QVBoxLayout(self._hackrf_normal_controls)
        nc.setContentsMargins(0, 0, 0, 0)
        nc.setSpacing(4)

        cf_row = QHBoxLayout()
        cf_row.addWidget(QLabel("Center Freq (MHz):"))
        self.cf_spin = QDoubleSpinBox()
        self.cf_spin.setRange(1.0, 6000.0)
        self.cf_spin.setValue(100.0)
        self.cf_spin.setDecimals(3)
        self.cf_spin.setSingleStep(1.0)
        self.cf_spin.valueChanged.connect(lambda v: self.center_freq_changed.emit(v * 1e6))
        cf_row.addWidget(self.cf_spin)
        nc.addLayout(cf_row)

        sr_row = QHBoxLayout()
        sr_row.addWidget(QLabel("Sample Rate (MHz):"))
        self.sr_combo = QComboBox()
        self.sr_combo.addItems(['1', '2', '4', '8', '16', '20'])
        self.sr_combo.setCurrentText('20')
        self.sr_combo.currentTextChanged.connect(
            lambda v: self.sample_rate_changed.emit(int(float(v) * 1e6))
        )
        sr_row.addWidget(self.sr_combo)
        nc.addLayout(sr_row)

        lay.addWidget(self._hackrf_normal_controls)

        # ---- Sweep-mode controls (hidden by default) ----
        self._hackrf_sweep_controls = QWidget()
        sc = QVBoxLayout(self._hackrf_sweep_controls)
        sc.setContentsMargins(0, 0, 0, 0)
        sc.setSpacing(4)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Start (MHz):"))
        self.sweep_start_spin = QDoubleSpinBox()
        self.sweep_start_spin.setRange(1.0, 7250.0)
        self.sweep_start_spin.setValue(80.0)
        self.sweep_start_spin.setDecimals(1)
        self.sweep_start_spin.setSingleStep(10.0)
        freq_row.addWidget(self.sweep_start_spin)
        freq_row.addWidget(QLabel("Stop (MHz):"))
        self.sweep_stop_spin = QDoubleSpinBox()
        self.sweep_stop_spin.setRange(1.0, 7250.0)
        self.sweep_stop_spin.setValue(1000.0)
        self.sweep_stop_spin.setDecimals(1)
        self.sweep_stop_spin.setSingleStep(10.0)
        freq_row.addWidget(self.sweep_stop_spin)
        sc.addLayout(freq_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step (MHz):"))
        self.sweep_step_combo = QComboBox()
        self.sweep_step_combo.addItems(['2', '4', '8', '10', '12', '14', '16', '18', '20'])
        self.sweep_step_combo.setCurrentText('20')
        self.sweep_step_combo.setToolTip("Bandwidth captured per step (= HackRF sample rate)")
        step_row.addWidget(self.sweep_step_combo)
        step_row.addWidget(QLabel("Bin (kHz):"))
        self.sweep_bin_combo = QComboBox()
        self.sweep_bin_combo.addItems(['25', '50', '100', '200', '500'])
        self.sweep_bin_combo.setCurrentText('100')
        self.sweep_bin_combo.setToolTip("FFT bin width — narrower = better frequency resolution")
        step_row.addWidget(self.sweep_bin_combo)
        step_row.addStretch()
        sc.addLayout(step_row)

        self._hackrf_sweep_controls.setVisible(False)
        lay.addWidget(self._hackrf_sweep_controls)

        # Auto-restart sweep when any sweep-specific parameter changes
        for widget in (self.sweep_start_spin, self.sweep_stop_spin):
            widget.valueChanged.connect(self._schedule_sweep_reconnect)
        for widget in (self.sweep_step_combo, self.sweep_bin_combo):
            widget.currentTextChanged.connect(self._schedule_sweep_reconnect)

        # ---- Gain controls (always visible) ----
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
        lay.addLayout(lna_row)

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
        lay.addLayout(vga_row)

        amp_row = QHBoxLayout()
        self.amp_check = QCheckBox("RF Amp (~11 dB)")
        self.amp_check.setChecked(False)
        self.amp_check.toggled.connect(self.amp_enabled_changed.emit)
        amp_row.addWidget(self.amp_check)
        amp_row.addStretch()
        lay.addLayout(amp_row)

        # ---- Recording group (hidden in sweep mode) ----
        self._rec_group = QGroupBox("Recording")
        rec_lay = QVBoxLayout(self._rec_group)
        rec_lay.setSpacing(4)

        rec_ctrl = QHBoxLayout()
        self.record_btn = QPushButton("● Record")
        self.record_btn.setCheckable(True)
        self.record_btn.setToolTip("Start/stop IQ recording")
        self.record_btn.clicked.connect(self._on_record_clicked)
        rec_ctrl.addWidget(self.record_btn)

        rec_ctrl.addWidget(QLabel("Fmt:"))
        self.record_fmt_combo = QComboBox()
        self.record_fmt_combo.addItems(["IQ", "RAW", "WAV"])
        self.record_fmt_combo.setToolTip("IQ=complex64, RAW=int8, WAV=16-bit stereo")
        rec_ctrl.addWidget(self.record_fmt_combo)

        rec_ctrl.addWidget(QLabel("Max:"))
        self.record_max_spin = QDoubleSpinBox()
        self.record_max_spin.setRange(0, 3600)
        self.record_max_spin.setValue(0)
        self.record_max_spin.setDecimals(0)
        self.record_max_spin.setSuffix(" s")
        self.record_max_spin.setToolTip("Maximum recording duration in seconds (0 = unlimited)")
        self.record_max_spin.setFixedWidth(72)
        rec_ctrl.addWidget(self.record_max_spin)
        rec_lay.addLayout(rec_ctrl)

        self.record_status_label = QLabel("Idle")
        self.record_status_label.setStyleSheet("color: #888888; font-size: 11px;")
        rec_lay.addWidget(self.record_status_label)
        lay.addWidget(self._rec_group)

        return w

    def _on_sweep_mode_toggled(self, checked: bool):
        self._hackrf_normal_controls.setVisible(not checked)
        self._hackrf_sweep_controls.setVisible(checked)
        self._rec_group.setVisible(not checked)

    def is_sweep_mode(self) -> bool:
        return self.sweep_mode_check.isChecked()

    def _make_file_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # File browser
        browse_row = QHBoxLayout()
        self.file_browse_btn = QPushButton("Browse…")
        self.file_browse_btn.clicked.connect(self._on_browse_file)
        browse_row.addWidget(self.file_browse_btn)
        self.file_path_label = QLabel("No file loaded")
        self.file_path_label.setStyleSheet("color: #888888; font-size: 11px;")
        self.file_path_label.setWordWrap(True)
        browse_row.addWidget(self.file_path_label, stretch=1)
        lay.addLayout(browse_row)

        # Metadata line
        self.file_info_label = QLabel("")
        self.file_info_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        lay.addWidget(self.file_info_label)

        # Playback buttons + speed + loop
        pb_row = QHBoxLayout()
        self.play_btn  = QPushButton("▶")
        self.pause_btn = QPushButton("⏸")
        self.stop_btn  = QPushButton("⏹")
        for btn, tip in ((self.play_btn, "Play"), (self.pause_btn, "Pause"),
                         (self.stop_btn, "Stop / Rewind")):
            btn.setFixedWidth(32)
            btn.setToolTip(tip)
        self.play_btn.clicked.connect(self.playback_play_requested.emit)
        self.pause_btn.clicked.connect(self.playback_pause_requested.emit)
        self.stop_btn.clicked.connect(self.playback_stop_requested.emit)
        pb_row.addWidget(self.play_btn)
        pb_row.addWidget(self.pause_btn)
        pb_row.addWidget(self.stop_btn)

        pb_row.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "1x", "2x", "4x"])
        self.speed_combo.setCurrentText("1x")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        pb_row.addWidget(self.speed_combo)

        self.loop_check = QCheckBox("Loop")
        self.loop_check.toggled.connect(self.playback_loop_changed.emit)
        pb_row.addWidget(self.loop_check)
        pb_row.addStretch()
        lay.addLayout(pb_row)

        # Scrubber
        self.scrubber = QSlider(Qt.Horizontal)
        self.scrubber.setRange(0, 1000)
        self.scrubber.setValue(0)
        self.scrubber.setToolTip("Drag to seek")
        self.scrubber.sliderMoved.connect(self._on_scrubber_moved)
        self.scrubber.sliderReleased.connect(self._on_scrubber_released)
        lay.addWidget(self.scrubber)

        self.position_label = QLabel("0:00 / 0:00")
        self.position_label.setAlignment(Qt.AlignCenter)
        self.position_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        lay.addWidget(self.position_label)

        return w

    # ------------------------------------------------------------------
    # Source switching
    # ------------------------------------------------------------------

    def _on_source_changed(self, source: str):
        self._hackrf_panel.setVisible(source == "HackRF")
        self._file_panel.setVisible(source == "IQ File")

    # ------------------------------------------------------------------
    # HackRF recording
    # ------------------------------------------------------------------

    def _on_record_clicked(self):
        import time as _time
        if self.record_btn.isChecked():
            fmt = self.record_fmt_combo.currentText()
            ext_map = {'IQ': '.iq', 'RAW': '.raw', 'WAV': '.wav'}
            ext = ext_map.get(fmt, '.iq')
            ts  = _time.strftime('%Y%m%d_%H%M%S')
            default = f"recording_{ts}{ext}"
            path, _ = QFileDialog.getSaveFileName(
                self, "Save IQ Recording", default,
                f"{fmt} Files (*{ext});;All Files (*)"
            )
            if not path:
                self.record_btn.setChecked(False)
                return
            self._recording_path = path
            max_dur = float(self.record_max_spin.value())
            self.record_start_requested.emit(path, fmt, max_dur)
            self.record_btn.setText("⏹ Stop")
            self.record_status_label.setText(f"Recording: {os.path.basename(path)}")
            self.record_status_label.setStyleSheet("color: #ff5555; font-size: 11px;")
        else:
            self.record_stop_requested.emit()
            self.record_btn.setText("● Record")
            self.record_status_label.setText("Idle")
            self.record_status_label.setStyleSheet("color: #888888; font-size: 11px;")

    # ------------------------------------------------------------------
    # IQ file browsing and playback controls
    # ------------------------------------------------------------------

    def _on_browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open IQ File", "",
            "IQ Files (*.iq *.raw *.wav);;All Files (*)"
        )
        if not path:
            return

        # Try companion metadata
        center_freq = self.cf_spin.value() * 1e6
        sample_rate = int(float(self.sr_combo.currentText()) * 1e6)
        meta_path = path + '.json'
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
                center_freq = float(meta.get('center_freq', center_freq))
                sample_rate = float(meta.get('sample_rate', sample_rate))
            except Exception:
                pass

        name = os.path.basename(path)
        self.file_path_label.setText(name)
        self.file_path_label.setToolTip(path)
        self.file_path_label.setStyleSheet("color: #cccccc; font-size: 11px;")
        self.file_info_label.setText(
            f"{center_freq / 1e6:.3f} MHz  |  {sample_rate / 1e6:.1f} MHz SR"
        )

        self._file_total_samples = 0
        self._file_sample_rate = sample_rate
        self.scrubber.setValue(0)
        self.position_label.setText("0:00 / 0:00")

        self.file_source_opened.emit(path, center_freq, sample_rate)

    def _on_speed_changed(self, text: str):
        speed_map = {'0.25x': 0.25, '0.5x': 0.5, '1x': 1.0, '2x': 2.0, '4x': 4.0}
        self.playback_speed_changed.emit(speed_map.get(text, 1.0))

    def _on_scrubber_moved(self, value: int):
        self._scrubber_seeking = True
        if self._file_total_samples > 0 and self._file_sample_rate > 0:
            pos = int(value * self._file_total_samples / 1000)
            t = pos / self._file_sample_rate
            total_t = self._file_total_samples / self._file_sample_rate
            self.position_label.setText(f"{self._fmt_time(t)} / {self._fmt_time(total_t)}")

    def _on_scrubber_released(self):
        self._scrubber_seeking = False
        if self._file_total_samples > 0:
            pos = int(self.scrubber.value() * self._file_total_samples / 1000)
            self.playback_seek_requested.emit(pos)

    # ------------------------------------------------------------------
    # Public update API (called from main_window via IPC signals)
    # ------------------------------------------------------------------

    def update_playback_position(self, current: int, total: int, sample_rate: float):
        self._file_total_samples = total
        self._file_sample_rate   = sample_rate
        if not self._scrubber_seeking and total > 0:
            slider_val = int(current * 1000 / total)
            self.scrubber.blockSignals(True)
            self.scrubber.setValue(slider_val)
            self.scrubber.blockSignals(False)
        if sample_rate > 0 and total > 0:
            t       = current / sample_rate
            total_t = total   / sample_rate
            self.position_label.setText(f"{self._fmt_time(t)} / {self._fmt_time(total_t)}")

    def update_recording_status(self, recording: bool, file_path: str, bytes_written: int):
        if recording:
            mb = bytes_written / (1024 * 1024)
            self.record_status_label.setText(
                f"Recording: {os.path.basename(file_path)} ({mb:.1f} MB)"
            )
            self.record_status_label.setStyleSheet("color: #ff5555; font-size: 11px;")
        else:
            self.record_btn.setChecked(False)
            self.record_btn.setText("● Record")
            self.record_status_label.setText("Idle")
            self.record_status_label.setStyleSheet("color: #888888; font-size: 11px;")

    # ------------------------------------------------------------------
    # DevicePanel-compatible API (called from main_window / session code)
    # ------------------------------------------------------------------

    def set_connected(self, connected: bool):
        self._is_connected = connected
        text  = "Connected" if connected else "Disconnected"
        style = "color: green;" if connected else "color: red;"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(style)

    def _schedule_sweep_reconnect(self):
        if self._is_connected and self.is_sweep_mode():
            self._sweep_reconnect_timer.start()

    def _apply_sweep_params(self):
        if self._is_connected and self.is_sweep_mode():
            self.connect_requested.emit()

    def get_settings(self) -> dict:
        return {
            'center_freq':  float(self.cf_spin.value()) * 1e6,
            'sample_rate':  int(float(self.sr_combo.currentText()) * 1e6),
            'lna':          int(self.lna_slider.value()),
            'vga':          int(self.vga_slider.value()),
            'amp_enabled':  bool(self.amp_check.isChecked()),
            'autosave':     True,
        }

    def get_sweep_settings(self) -> dict:
        return {
            'start_freq':  float(self.sweep_start_spin.value()) * 1e6,
            'stop_freq':   float(self.sweep_stop_spin.value()) * 1e6,
            'sample_rate': int(float(self.sweep_step_combo.currentText()) * 1e6),
            'lna':         int(self.lna_slider.value()),
            'vga':         int(self.vga_slider.value()),
            'amp':         bool(self.amp_check.isChecked()),
            'bin_width':   int(float(self.sweep_bin_combo.currentText()) * 1e3),
        }

    def apply_settings(self, settings: dict) -> None:
        try:
            if 'center_freq' in settings:
                self.cf_spin.setValue(settings['center_freq'] / 1e6)
            if 'sample_rate' in settings:
                self.sr_combo.setCurrentText(str(int(settings['sample_rate'] / 1e6)))
            if 'lna' in settings:
                self.lna_slider.setValue(int(settings['lna']))
            if 'vga' in settings:
                self.vga_slider.setValue(int(settings['vga']))
            if 'amp_enabled' in settings:
                self.amp_check.setChecked(bool(settings['amp_enabled']))
        except Exception:
            pass

    def get_autosave_enabled(self) -> bool:
        return True

    def connect_session_signals(self, save_callback, autosave_callback=None):
        return   # no-op: session controls are in the main window menu

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Combined control panel
# ---------------------------------------------------------------------------

class ControlPanel(QWidget):
    """Right-side panel: VFO tabs on top, device settings below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initUI()

    def _initUI(self):
        from .signal_id_panel import SignalIDPanel

        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        content = QWidget()
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(8)

        self.source_panel = SourcePanel()
        self.vfo_tab = VFOTabPanel()
        self.bookmark_panel = BookmarkPanel(
            get_vfo_snapshot=self.vfo_tab.get_active_vfo_snapshot
        )
        self.signal_id_panel = SignalIDPanel()

        device_group   = CollapsibleSection("Source",    self.source_panel)
        vfo_group      = CollapsibleSection("VFOs",      self.vfo_tab)
        bookmark_group = CollapsibleSection("Bookmarks", self.bookmark_panel)
        signal_id_group = CollapsibleSection(
            "Signal ID", self.signal_id_panel, expanded=False
        )

        content_layout.addWidget(device_group,    stretch=0)
        content_layout.addWidget(vfo_group,       stretch=0)
        content_layout.addWidget(bookmark_group,  stretch=0)
        content_layout.addWidget(signal_id_group, stretch=0)

        content_layout.addStretch()
        content.setLayout(content_layout)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        self.setLayout(outer_layout)
