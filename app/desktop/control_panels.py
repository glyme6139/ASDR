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
    QCheckBox, QFileDialog, QInputDialog,
)
from PySide6.QtWidgets import QScrollArea
from .widgets import AcceptCommaDoubleSpinBox
from app.decoders.modulation import MODULATION_DECODER_NAMES
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

DECODER_NAMES = ['POCSAG', 'RDS', 'AIS', 'ADSB', 'TETRA'] + list(MODULATION_DECODER_NAMES)

_BOOKMARK_FILE = 'bookmarks.json'


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
    decoder_toggled = Signal(int, str, bool)  # vfo_id, decoder_name, enabled
    open_window_requested = Signal(int, str)  # vfo_id, decoder_name

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

    # Re-emitted per-VFO signals
    frequency_changed = Signal(int, float)
    demod_changed = Signal(int, str)
    bandwidth_changed = Signal(int, float)
    volume_changed = Signal(int, float)
    squelch_changed = Signal(int, float)
    squelch_enabled_changed = Signal(int, bool)
    mute_changed = Signal(int, bool)
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
                self._tab_widget.setTabText(idx, name[:12])

        decoder_names = set(settings.get('decoders', []) or [])
        for i in range(tab.decoder_list.count()):
            item = tab.decoder_list.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.text() in decoder_names else Qt.CheckState.Unchecked)

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

        group = QGroupBox("Bookmarks")
        g = QVBoxLayout()
        g.setContentsMargins(6, 6, 6, 6)
        g.setSpacing(4)

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
        g.addLayout(toolbar)

        # ── bookmark list ──
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(140)
        self.list_widget.setMinimumHeight(140)
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setToolTip("Double-click a bookmark to add it as a new VFO")
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._on_add_to_vfo())
        g.addWidget(self.list_widget)

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
        g.addLayout(action_row)

        group.setLayout(g)
        outer.addWidget(group)
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

        # Session controls: Save and Autosave
        sess_row = QHBoxLayout()
        self.save_btn = QPushButton("Save Session...")
        self.save_btn.setToolTip("Save current session to a file")
        sess_row.addWidget(self.save_btn)

        self.autosave_check = QCheckBox("Autosave on exit")
        self.autosave_check.setChecked(True)
        sess_row.addWidget(self.autosave_check)

        sess_row.addStretch()
        layout.addLayout(sess_row)

        self.setLayout(layout)

    def set_connected(self, connected: bool):
        if connected:
            self.status_label.setText("Connected")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: red;")

    def get_settings(self) -> dict:
        return {
            'center_freq': float(self.cf_spin.value()) * 1e6,
            'sample_rate': int(float(self.sr_combo.currentText()) * 1e6),
            'lna': int(self.lna_slider.value()),
            'vga': int(self.vga_slider.value()),
            'amp_enabled': bool(self.amp_check.isChecked()),
            'autosave': bool(self.autosave_check.isChecked()),
        }

    def apply_settings(self, settings: dict) -> None:
        try:
            if 'center_freq' in settings:
                self.cf_spin.setValue(settings.get('center_freq', 100e6) / 1e6)
            if 'sample_rate' in settings:
                sr_mhz = int(settings.get('sample_rate', 20_000_000) / 1e6)
                self.sr_combo.setCurrentText(str(sr_mhz))
            if 'lna' in settings:
                self.lna_slider.setValue(int(settings.get('lna', 24)))
            if 'vga' in settings:
                self.vga_slider.setValue(int(settings.get('vga', 20)))
            if 'amp_enabled' in settings:
                self.amp_check.setChecked(bool(settings.get('amp_enabled', False)))
            if 'autosave' in settings:
                try:
                    self.autosave_check.setChecked(bool(settings.get('autosave', True)))
                except Exception:
                    pass
        except Exception:
            pass

    def get_autosave_enabled(self) -> bool:
        return bool(self.autosave_check.isChecked())

    def connect_session_signals(self, save_callback, autosave_callback=None):
        """Connect callbacks for Save button and autosave toggles.

        - save_callback: callable invoked when Save button clicked
        - autosave_callback: optional callable(bool) called when autosave toggled
        """
        self.save_btn.clicked.connect(lambda: save_callback())
        if autosave_callback is not None:
            self.autosave_check.toggled.connect(lambda v: autosave_callback(bool(v)))


# ---------------------------------------------------------------------------
# Combined control panel
# ---------------------------------------------------------------------------

class ControlPanel(QWidget):
    """Right-side panel: VFO tabs on top, device settings below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initUI()

    def _initUI(self):
        # Make the control panel scrollable to give a less compact interface
        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        content = QWidget()
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(8)

        # Device group at the bottom
        device_group = QGroupBox("Device")
        device_layout = QVBoxLayout()
        self.device_panel = DevicePanel()
        device_layout.addWidget(self.device_panel)
        device_group.setLayout(device_layout)
        content_layout.addWidget(device_group, stretch=0)

        # VFO tabs occupy most of the space
        self.vfo_tab = VFOTabPanel()
        content_layout.addWidget(self.vfo_tab, stretch=0)

        # Bookmark panel — always visible between VFO tabs and device settings
        self.bookmark_panel = BookmarkPanel(
            get_vfo_snapshot=self.vfo_tab.get_active_vfo_snapshot
        )
        content_layout.addWidget(self.bookmark_panel, stretch=0)

        content_layout.addStretch()
        content.setLayout(content_layout)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        self.setLayout(outer_layout)
