"""FSK / AFSK decoder window — text output, hex dump, raw bitstream, and live configuration."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from .base import BaseDecoderWindow
from app.decoders.fsk import PRESETS, FSK_PRESET_NAMES

_MONO    = QFont('Consolas', 10)
_MONO_LG = QFont('Consolas', 11)

_MAX_TEXT_CHARS = 20_000
_MAX_HEX_LINES  = 500

_STYLE_EDIT = (
    'QPlainTextEdit {'
    '  background: #0d1014;'
    '  color: #c8d8c0;'
    '  border: 1px solid #333;'
    '  border-radius: 3px;'
    '}'
)
_STYLE_BITS = (
    'QPlainTextEdit {'
    '  background: #0a0c0e;'
    '  color: #7ec8a0;'
    '  border: 1px solid #333;'
    '}'
)
_STYLE_HEX = (
    'QPlainTextEdit {'
    '  background: #0d1014;'
    '  color: #a0b8d0;'
    '  border: 1px solid #333;'
    '}'
)
_STYLE_BTN = (
    'QPushButton {'
    '  background: #22262a;'
    '  color: #d0d0d0;'
    '  border: 1px solid #444;'
    '  border-radius: 3px;'
    '  padding: 3px 10px;'
    '}'
    'QPushButton:hover { background: #2e3236; }'
    'QPushButton:checked { background: #5a3d1a; border-color: #c87941; }'
)
_STYLE_COMBO = (
    'QComboBox {'
    '  background: #22262a;'
    '  color: #d0d0d0;'
    '  border: 1px solid #444;'
    '  border-radius: 3px;'
    '  padding: 2px 6px;'
    '  min-width: 90px;'
    '}'
    'QComboBox::drop-down { border: none; }'
    'QComboBox QAbstractItemView { background: #22262a; color: #d0d0d0; selection-background-color: #3a4a5a; }'
)
_STYLE_TABS = (
    'QTabBar::tab { background: #1a1d21; color: #aaa; padding: 4px 14px;'
    '  border: 1px solid #444; border-bottom: none; border-radius: 3px 3px 0 0; }'
    'QTabBar::tab:selected { background: #202326; color: #e0e0e0; }'
    'QTabWidget::pane { border: 1px solid #444; }'
)
_STYLE_LBL   = 'color: #a0a8a0; font-size: 10px;'
_STYLE_VAL   = 'color: #70c898; font-size: 10px; font-weight: bold;'
_STYLE_HINT  = 'color: #555; font-size: 10px; font-style: italic;'
_STYLE_GROUP = (
    'QGroupBox { color: #b0b8b0; border: 1px solid #333; border-radius: 4px; margin-top: 6px; }'
    'QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }'
)


def _lbl(text: str, style: str = _STYLE_LBL) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(style)
    return l


class FSKWindow(BaseDecoderWindow):
    """Floating FSK/AFSK decoder window with live config, text, hex, and bit-stream views."""

    def __init__(self, vfo_id: int) -> None:
        super().__init__(f'FSK — VFO {vfo_id}', vfo_id, 'FSK')
        self._configure_fn: Optional[Callable] = None
        self._total_chars  = 0
        self._paused       = False
        self._pending: List[dict] = []
        self._hex_line_count = 0
        self._build_ui()
        # Refresh status bar on a timer in case results arrive in bursts
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)

    # ------------------------------------------------------------------
    # BaseDecoderWindow overrides
    # ------------------------------------------------------------------

    def set_configure_fn(self, fn: Callable) -> None:
        self._configure_fn = fn

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        if self._paused:
            self._pending.append(data)
            return
        self._apply_result(data)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet('background: #0f1012; color: #d0d0d0;')

        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(8, 8, 8, 4)
        vlay.setSpacing(6)

        vlay.addLayout(self._make_toolbar())
        vlay.addWidget(self._make_info_bar())
        vlay.addWidget(self._make_tabs(), stretch=1)
        vlay.addWidget(self._make_status_bar())

        self.resize(860, 600)

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        bar.addWidget(_lbl('Preset:'))
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet(_STYLE_COMBO)
        for name in FSK_PRESET_NAMES:
            self._preset_combo.addItem(name)
        self._preset_combo.setCurrentText('Bell202')
        self._preset_combo.currentTextChanged.connect(self._on_preset_changed)
        bar.addWidget(self._preset_combo)

        bar.addSpacing(12)

        self._invert_cb = QCheckBox('Invert bits')
        self._invert_cb.setStyleSheet('color: #a0a8a0; font-size: 10px;')
        self._invert_cb.toggled.connect(self._on_invert_toggled)
        bar.addWidget(self._invert_cb)

        bar.addStretch()

        hint = _lbl('NBFM VFO → fsk mode  |  USB/LSB VFO → afsk mode', _STYLE_HINT)
        bar.addWidget(hint)

        bar.addStretch()

        self._pause_btn = QPushButton('Pause')
        self._pause_btn.setCheckable(True)
        self._pause_btn.setFixedWidth(64)
        self._pause_btn.setStyleSheet(_STYLE_BTN)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        bar.addWidget(self._pause_btn)

        clear_btn = QPushButton('Clear')
        clear_btn.setFixedWidth(56)
        clear_btn.setStyleSheet(_STYLE_BTN)
        clear_btn.clicked.connect(self._clear)
        bar.addWidget(clear_btn)

        return bar

    def _make_info_bar(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(24)
        hlay = QHBoxLayout(w)
        hlay.setContentsMargins(4, 0, 4, 0)
        hlay.setSpacing(16)

        self._lbl_mode  = _lbl('mode: —', _STYLE_VAL)
        self._lbl_baud  = _lbl('baud: —', _STYLE_VAL)
        self._lbl_mark  = _lbl('mark: —', _STYLE_LBL)
        self._lbl_space = _lbl('space: —', _STYLE_LBL)
        self._lbl_enc   = _lbl('enc: —', _STYLE_LBL)

        for lbl in (self._lbl_mode, self._lbl_baud, self._lbl_mark, self._lbl_space, self._lbl_enc):
            hlay.addWidget(lbl)

        hlay.addStretch()
        return w

    def _make_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setStyleSheet(_STYLE_TABS)
        tabs.addTab(self._make_text_tab(),    'Text')
        tabs.addTab(self._make_hex_tab(),     'Hex')
        tabs.addTab(self._make_bits_tab(),    'Bitstream')
        tabs.addTab(self._make_measure_tab(), 'Measure')
        return tabs

    def _make_text_tab(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 4, 4, 4)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFont(_MONO_LG)
        self._text_edit.setStyleSheet(_STYLE_EDIT)
        self._text_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        vlay.addWidget(self._text_edit)
        return w

    def _make_hex_tab(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 4, 4, 4)

        self._hex_edit = QPlainTextEdit()
        self._hex_edit.setReadOnly(True)
        self._hex_edit.setFont(_MONO)
        self._hex_edit.setStyleSheet(_STYLE_HEX)
        vlay.addWidget(self._hex_edit)
        return w

    def _make_bits_tab(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 4, 4, 4)

        hint = _lbl(
            'Last 256 recovered symbols  (1 = mark / high, 0 = space / low)',
            _STYLE_HINT,
        )
        vlay.addWidget(hint)

        self._bits_edit = QPlainTextEdit()
        self._bits_edit.setReadOnly(True)
        self._bits_edit.setFont(_MONO)
        self._bits_edit.setStyleSheet(_STYLE_BITS)
        self._bits_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._bits_edit.setMaximumHeight(160)
        vlay.addWidget(self._bits_edit)
        vlay.addStretch()
        return w

    def _make_measure_tab(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(8)

        # ── Readout row ──────────────────────────────────────────────
        row = QHBoxLayout()
        row.setSpacing(20)

        def _counter(title: str, color: str):
            box = QGroupBox(title)
            box.setStyleSheet(_STYLE_GROUP)
            il = QVBoxLayout(box)
            il.setContentsMargins(8, 4, 8, 4)
            lbl = QLabel('—')
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont('Consolas', 16, QFont.Weight.Bold))
            lbl.setStyleSheet(f'color: {color};')
            il.addWidget(lbl)
            return lbl, box

        self._meas_baud,  b = _counter('Baud rate',  '#70c898')
        row.addWidget(b)
        self._meas_mark,  b = _counter('Mark freq',  '#a0c8f0')
        row.addWidget(b)
        self._meas_space, b = _counter('Space freq', '#f0a870')
        row.addWidget(b)
        self._meas_dev,   b = _counter('Deviation',  '#c898c8')
        row.addWidget(b)
        row.addStretch()
        vlay.addLayout(row)

        # ── Spectrum plot ────────────────────────────────────────────
        spec_lbl = _lbl('Audio spectrum  (mark / space peaks shown in colour)', _STYLE_HINT)
        vlay.addWidget(spec_lbl)

        self._spec_plot = pg.PlotWidget()
        self._spec_plot.setBackground('#0a0c0e')
        self._spec_plot.showGrid(x=True, y=True, alpha=0.15)
        self._spec_plot.setLabel('bottom', 'Frequency', units='Hz')
        self._spec_plot.setLabel('left',   'Power', units='dBFS')
        self._spec_plot.setMinimumHeight(160)

        self._spec_curve = self._spec_plot.plot(pen=pg.mkPen('#4a8060', width=1))
        self._mark_line  = pg.InfiniteLine(angle=90, movable=False,
                                           pen=pg.mkPen('#a0c8f0', width=1, style=Qt.PenStyle.DashLine))
        self._space_line = pg.InfiniteLine(angle=90, movable=False,
                                           pen=pg.mkPen('#f0a870', width=1, style=Qt.PenStyle.DashLine))
        self._spec_plot.addItem(self._mark_line)
        self._spec_plot.addItem(self._space_line)
        vlay.addWidget(self._spec_plot, stretch=1)

        # ── Autocorrelation plot ─────────────────────────────────────
        ac_lbl = _lbl('Transition autocorrelation  (dominant peak = symbol period)', _STYLE_HINT)
        vlay.addWidget(ac_lbl)

        nrz_lbl = _lbl(
            'I&D score vs candidate baud rate  (peak = best estimate; green marker)',
            _STYLE_HINT,
        )
        vlay.addWidget(nrz_lbl)

        self._ac_plot = pg.PlotWidget()
        self._ac_plot.setBackground('#0a0c0e')
        self._ac_plot.showGrid(x=True, y=True, alpha=0.15)
        self._ac_plot.setLabel('bottom', 'Candidate baud rate', units='bd')
        self._ac_plot.setLabel('left',   'I&D score')
        self._ac_plot.setMinimumHeight(140)

        self._ac_curve     = self._ac_plot.plot(pen=pg.mkPen('#6888a8', width=1))
        self._ac_peak_line = pg.InfiniteLine(angle=90, movable=False,
                                             pen=pg.mkPen('#70c898', width=2, style=Qt.PenStyle.DashLine))
        self._ac_plot.addItem(self._ac_peak_line)
        vlay.addWidget(self._ac_plot, stretch=1)

        return w

    def _update_measure(self, meas: dict) -> None:
        """Refresh the Measure tab from a measurement dict produced by FSKDecoder."""
        baud  = meas.get('baud_est')
        mark  = meas.get('mark_est')
        space = meas.get('space_est')
        dev   = meas.get('dev_est')

        if baud  is not None: self._meas_baud.setText(f'{baud} bd')
        if mark  is not None: self._meas_mark.setText(f'{mark:.0f} Hz')
        if space is not None: self._meas_space.setText(f'{space:.0f} Hz')
        if dev   is not None: self._meas_dev.setText(f'{dev:.0f} Hz')

        psd_db = meas.get('psd_db')
        psd_f  = meas.get('psd_freqs')
        if psd_db and psd_f:
            import numpy as np
            self._spec_curve.setData(psd_f, psd_db)
        if mark  is not None: self._mark_line.setValue(mark)
        if space is not None: self._space_line.setValue(space)

        scores = meas.get('score_curve')
        s_baud = meas.get('score_bauds')
        null_f = meas.get('null_freq')
        if scores and s_baud:
            self._ac_curve.setData(s_baud, scores)
        if null_f is not None:
            self._ac_peak_line.setValue(null_f)

    def _make_status_bar(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        hlay = QHBoxLayout(w)
        hlay.setContentsMargins(4, 0, 4, 0)

        self._status_lbl = _lbl('Waiting for data…', _STYLE_LBL)
        hlay.addWidget(self._status_lbl)
        hlay.addStretch()

        self._chars_lbl = _lbl('Characters: 0', _STYLE_LBL)
        hlay.addWidget(self._chars_lbl)
        return w

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def _apply_result(self, data: dict) -> None:
        text     = str(data.get('text', ''))
        hex_str  = str(data.get('hex', ''))
        bits_str = str(data.get('bits', ''))
        n_chars  = int(data.get('char_count', 0))
        baud     = data.get('baud', '?')
        mode     = data.get('mode', '?')
        enc      = data.get('encoding', '')
        mark     = data.get('mark_freq', '')
        space    = data.get('space_freq', '')

        # Update info labels
        self._lbl_mode.setText(f'mode: {mode}')
        self._lbl_baud.setText(f'baud: {baud}')
        if mark:  self._lbl_mark.setText(f'mark: {mark:.0f} Hz')
        if space: self._lbl_space.setText(f'space: {space:.0f} Hz')
        if enc:   self._lbl_enc.setText(f'enc: {enc}')

        # Text tab — appendPlainText already inserts at the end
        if text:
            self._text_edit.appendPlainText(text)
            doc = self._text_edit.document()
            if doc.characterCount() > _MAX_TEXT_CHARS:
                from PySide6.QtGui import QTextCursor
                cur = self._text_edit.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.Start)
                cur.movePosition(QTextCursor.MoveOperation.Right,
                                 QTextCursor.MoveMode.KeepAnchor, 2000)
                cur.removeSelectedText()

        # Hex tab
        if hex_str:
            ts = datetime.now().strftime('%H:%M:%S')
            line = f'{ts}  {hex_str}'
            self._hex_edit.appendPlainText(line)
            self._hex_line_count += 1
            if self._hex_line_count > _MAX_HEX_LINES:
                from PySide6.QtGui import QTextCursor
                cur = self._hex_edit.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.Start)
                cur.movePosition(QTextCursor.MoveOperation.Down,
                                 QTextCursor.MoveMode.KeepAnchor, 50)
                cur.removeSelectedText()
                self._hex_line_count -= 50

        # Bits tab
        if bits_str:
            # Insert spaces every 8 bits for readability
            spaced = ' '.join(bits_str[i:i+8] for i in range(0, len(bits_str), 8))
            self._bits_edit.setPlainText(spaced)

        self._total_chars += n_chars

        meas = data.get('meas')
        if meas:
            self._update_measure(meas)

    # ------------------------------------------------------------------
    # Status refresh
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        self._chars_lbl.setText(f'Characters: {self._total_chars}')
        if self._paused:
            self._status_lbl.setText(f'Paused — {len(self._pending)} queued')
        else:
            preset = self._preset_combo.currentText()
            self._status_lbl.setText(f'Preset: {preset}  |  Total chars: {self._total_chars}')

    # ------------------------------------------------------------------
    # Preset info update
    # ------------------------------------------------------------------

    def _update_info_from_preset(self, preset_name: str) -> None:
        p = PRESETS.get(preset_name)
        if not p:
            return
        self._lbl_mode.setText(f'mode: {p.get("mode", "?")}')
        self._lbl_baud.setText(f'baud: {p.get("baud", "?")}')
        mark  = p.get('mark', 0)
        space = p.get('space', 0)
        if mark:  self._lbl_mark.setText(f'mark: {mark:.0f} Hz')
        if space: self._lbl_space.setText(f'space: {space:.0f} Hz')
        self._lbl_enc.setText(f'enc: {p.get("encoding", "?")}')

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_preset_changed(self, name: str) -> None:
        self._update_info_from_preset(name)
        if self._configure_fn:
            self._configure_fn({'preset': name})

    def _on_invert_toggled(self, checked: bool) -> None:
        if self._configure_fn:
            self._configure_fn({'invert': checked})

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self._pause_btn.setText('Resume' if checked else 'Pause')
        if not checked:
            for d in self._pending:
                self._apply_result(d)
            self._pending.clear()

    def _clear(self) -> None:
        self._text_edit.clear()
        self._hex_edit.clear()
        self._bits_edit.clear()
        self._total_chars  = 0
        self._hex_line_count = 0
        self._pending.clear()
        self._chars_lbl.setText('Characters: 0')
        self._status_lbl.setText('Cleared')
        self._meas_baud.setText('—')
        self._meas_mark.setText('—')
        self._meas_space.setText('—')
        self._meas_dev.setText('—')
        self._spec_curve.setData([], [])
        self._ac_curve.setData([], [])
        self._ac_peak_line.setValue(0)
