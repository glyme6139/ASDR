"""
Standalone bitstream analysis window.

Any decoder that produces binary symbol streams can push bits here via push_bits().
The window owns a large ring buffer and runs analysis on a QTimer, independent of
whichever decoder is feeding it.

Metrics computed on the buffer:
  H(bit)     — Shannon entropy per symbol (0 = idle/constant, 1 = fully random)
  H(byte)    — Byte-level entropy (max 8; high → encrypted / compressed data)
  Mark ratio — Fraction of 1-bits (idle async serial = ~1.0, active = ~0.5)
  Mean run   — Average consecutive same-symbol run length
  Max run    — Longest run (spike = long idle stretch or stuck signal)
  Trans/bit  — Transition density (0 = stuck, ~0.5 = random, 1 = alternating)
  Quality    — Single label summarising the above
"""

from __future__ import annotations

from collections import deque
from typing import List

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

_MONO_SM = QFont('Consolas', 9)

_STYLE_BTN = (
    'QPushButton {'
    '  background: #22262a; color: #d0d0d0; border: 1px solid #444;'
    '  border-radius: 3px; padding: 3px 10px;'
    '}'
    'QPushButton:hover { background: #2e3236; }'
)
_STYLE_LBL  = 'color: #a0a8a0; font-size: 10px;'
_STYLE_VAL  = 'color: #70c898; font-size: 10px; font-weight: bold;'
_STYLE_HINT = 'color: #555; font-size: 10px; font-style: italic;'

_QUALITY_COLORS = {
    'idle':        '#808080',
    'biased':      '#c87040',
    'stuck':       '#c07030',
    'random/enc':  '#a080e0',
    'structured':  '#70c898',
    'weak signal': '#c8c870',
}

_DEFAULT_BUF = 16_384
_MAX_HISTORY  = 120   # samples at 500 ms = 60 s of history


def _lbl(text: str, style: str = _STYLE_LBL) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(style)
    return l


class BitstreamAnalysisWindow(QMainWindow):
    """Floating bitstream analysis window.  Feed it with push_bits()."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Bitstream Analyzer')
        self.setMinimumSize(720, 620)
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self._buf: deque[int]   = deque(maxlen=_DEFAULT_BUF)
        self._h_history: deque  = deque(maxlen=_MAX_HISTORY)
        self._mr_history: deque = deque(maxlen=_MAX_HISTORY)

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_bits(self, bits: List[int], source: str = '') -> None:
        """Append new symbols to the ring buffer (call from the UI thread)."""
        self._buf.extend(bits)
        if source:
            self._src_lbl.setText(f'Source: {source}')

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet('background: #0f1012; color: #d0d0d0;')

        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(8)

        vlay.addLayout(self._make_toolbar())
        vlay.addLayout(self._make_stats_row())
        vlay.addWidget(self._make_rl_section(),      stretch=2)
        vlay.addWidget(self._make_history_section(), stretch=1)

        self.resize(800, 680)

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self._src_lbl = _lbl('Source: —', 'color: #7ec8a0; font-size: 10px;')
        bar.addWidget(self._src_lbl)
        bar.addSpacing(16)

        bar.addWidget(_lbl('Buffer:'))
        self._buf_spin = QSpinBox()
        self._buf_spin.setRange(1024, 131072)
        self._buf_spin.setValue(_DEFAULT_BUF)
        self._buf_spin.setSingleStep(1024)
        self._buf_spin.setSuffix(' bits')
        self._buf_spin.setStyleSheet(
            'QSpinBox { background: #22262a; color: #d0d0d0; border: 1px solid #444;'
            ' border-radius: 3px; padding: 2px 6px; min-width: 110px; }'
        )
        self._buf_spin.valueChanged.connect(self._on_buf_size_changed)
        bar.addWidget(self._buf_spin)

        bar.addStretch()

        self._fill_lbl = _lbl('0 / 16384 bits', 'color: #606870; font-size: 10px;')
        bar.addWidget(self._fill_lbl)
        bar.addSpacing(12)

        clr_btn = QPushButton('Clear')
        clr_btn.setFixedWidth(56)
        clr_btn.setStyleSheet(_STYLE_BTN)
        clr_btn.clicked.connect(self._clear)
        bar.addWidget(clr_btn)

        return bar

    def _make_stats_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        def _stat(label: str) -> QLabel:
            row.addWidget(_lbl(label + ':', _STYLE_LBL))
            v = QLabel('—')
            v.setFont(_MONO_SM)
            v.setStyleSheet(_STYLE_VAL)
            row.addWidget(v)
            row.addSpacing(10)
            return v

        self._s_entropy = _stat('H(bit)')
        self._s_byte_h  = _stat('H(byte)')
        self._s_balance = _stat('Mark')
        self._s_meanrun = _stat('Mean run')
        self._s_maxrun  = _stat('Max run')
        self._s_trans   = _stat('Trans/bit')

        row.addSpacing(12)
        row.addWidget(_lbl('Signal:', _STYLE_LBL))
        self._s_quality = QLabel('—')
        self._s_quality.setFont(QFont('Consolas', 10, QFont.Weight.Bold))
        self._s_quality.setStyleSheet('color: #808080;')
        row.addWidget(self._s_quality)
        row.addStretch()
        return row

    def _make_rl_section(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(4)
        vlay.addWidget(_lbl(
            'Run-length distribution  (consecutive identical symbols, bins 1–32)',
            _STYLE_HINT,
        ))
        self._rl_plot = pg.PlotWidget()
        self._rl_plot.setBackground('#0a0c0e')
        self._rl_plot.showGrid(x=False, y=True, alpha=0.15)
        self._rl_plot.setLabel('bottom', 'Run length (symbols)')
        self._rl_plot.setLabel('left',   'Count')
        self._rl_plot.getAxis('bottom').setTicks(
            [[(i, str(i)) for i in range(1, 33, 2)]]
        )
        vlay.addWidget(self._rl_plot)
        return w

    def _make_history_section(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(4)
        vlay.addWidget(_lbl(
            'History (last 60 s @ 2 Hz) — H(bit) green, mark ratio orange',
            _STYLE_HINT,
        ))
        self._hist_plot = pg.PlotWidget()
        self._hist_plot.setBackground('#0a0c0e')
        self._hist_plot.showGrid(x=True, y=True, alpha=0.15)
        self._hist_plot.setLabel('left',   'Value')
        self._hist_plot.setLabel('bottom', 'Samples ago')
        self._hist_plot.setYRange(0.0, 1.05)
        self._hist_curve_h  = self._hist_plot.plot(
            pen=pg.mkPen('#70c898', width=1.5), name='H(bit)')
        self._hist_curve_mr = self._hist_plot.plot(
            pen=pg.mkPen('#f0a870', width=1.5), name='Mark ratio')
        vlay.addWidget(self._hist_plot)
        return w

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_buf_size_changed(self, val: int) -> None:
        new_buf: deque = deque(list(self._buf)[-val:], maxlen=val)
        self._buf = new_buf

    def _clear(self) -> None:
        self._buf.clear()
        self._h_history.clear()
        self._mr_history.clear()
        self._reset_stats_labels()
        self._rl_plot.clear()
        self._hist_curve_h.setData([], [])
        self._hist_curve_mr.setData([], [])

    # ------------------------------------------------------------------
    # Analysis loop
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if not self.isVisible():
            return
        n    = len(self._buf)
        maxn = self._buf.maxlen or _DEFAULT_BUF
        self._fill_lbl.setText(f'{n} / {maxn} bits')
        if n < 32:
            return
        ba = self._analyse()
        self._update_stats(ba)
        self._update_rl(ba)
        self._h_history.append(ba.get('entropy', 0.0))
        self._mr_history.append(ba.get('mark_ratio', 0.5))
        self._update_history()

    def _analyse(self) -> dict:
        arr = np.array(self._buf, dtype=np.uint8)
        n   = len(arr)

        p1 = float(np.mean(arr))
        p0 = 1.0 - p1
        h  = 0.0
        if p1 > 0.0: h -= p1 * np.log2(p1)
        if p0 > 0.0: h -= p0 * np.log2(p0)

        nb = n // 8
        if nb >= 4:
            packed = np.packbits(arr[:nb * 8]).astype(np.uint8)
            cnts   = np.bincount(packed, minlength=256).astype(np.float64)
            probs  = cnts[cnts > 0] / nb
            byte_h = float(-np.sum(probs * np.log2(probs)))
        else:
            byte_h = 0.0

        chg = np.flatnonzero(np.diff(arr))
        if len(chg):
            runs     = np.diff(np.concatenate([[-1], chg, [n - 1]]))
            mean_run = float(np.mean(runs))
            max_run  = int(np.max(runs))
            run_hist = [int(np.sum(runs == i)) for i in range(1, 33)]
        else:
            mean_run = float(n)
            max_run  = n
            run_hist = [0] * 32

        trans = len(chg) / max(n - 1, 1)

        if h < 0.10:
            quality = 'idle'
        elif abs(p1 - 0.5) > 0.40 or h < 0.50:
            quality = 'biased'
        elif trans < 0.02:
            quality = 'stuck'
        elif byte_h > 6.5:
            quality = 'random/enc'
        elif h > 0.70:
            quality = 'structured'
        else:
            quality = 'weak signal'

        return {
            'entropy':       round(h, 3),
            'byte_entropy':  round(byte_h, 3),
            'mark_ratio':    round(p1, 3),
            'mean_run':      round(mean_run, 2),
            'max_run':       max_run,
            'run_hist':      run_hist,
            'trans_density': round(trans, 3),
            'quality':       quality,
        }

    # ------------------------------------------------------------------
    # Display updates
    # ------------------------------------------------------------------

    def _update_stats(self, ba: dict) -> None:
        self._s_entropy.setText(f'{ba.get("entropy", 0):.3f}')
        self._s_byte_h.setText(f'{ba.get("byte_entropy", 0):.2f}')
        self._s_balance.setText(f'{ba.get("mark_ratio", 0):.3f}')
        self._s_meanrun.setText(f'{ba.get("mean_run", 0):.1f}')
        self._s_maxrun.setText(str(ba.get('max_run', 0)))
        self._s_trans.setText(f'{ba.get("trans_density", 0):.3f}')
        q     = ba.get('quality', '—')
        color = _QUALITY_COLORS.get(q, '#808080')
        self._s_quality.setText(q.upper())
        self._s_quality.setStyleSheet(f'color: {color}; font-weight: bold;')

    def _update_rl(self, ba: dict) -> None:
        run_hist = ba.get('run_hist')
        if not run_hist:
            return
        x = np.arange(1, 33, dtype=float)
        y = np.array(run_hist, dtype=float)
        self._rl_plot.clear()
        self._rl_plot.addItem(
            pg.BarGraphItem(x=x, height=y, width=0.7,
                            brush='#3a6080', pen=pg.mkPen('#5a8898', width=1))
        )

    def _update_history(self) -> None:
        h  = list(self._h_history)
        mr = list(self._mr_history)
        x  = list(range(len(h)))
        self._hist_curve_h.setData(x, h)
        self._hist_curve_mr.setData(x, mr)

    def _reset_stats_labels(self) -> None:
        for w in (self._s_entropy, self._s_byte_h, self._s_balance,
                  self._s_meanrun, self._s_maxrun, self._s_trans):
            w.setText('—')
        self._s_quality.setText('—')
        self._s_quality.setStyleSheet('color: #808080;')
        maxn = self._buf.maxlen or _DEFAULT_BUF
        self._fill_lbl.setText(f'0 / {maxn} bits')
