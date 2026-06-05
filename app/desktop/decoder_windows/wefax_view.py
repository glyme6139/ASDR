"""WEFAX visualization window — displays scan lines as a live scrolling image."""
from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFileDialog, QSizePolicy, QComboBox, QCheckBox,
    QGroupBox,
)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtCore import Qt

from .base import BaseDecoderWindow

_LPM_OPTIONS  = [60, 90, 120, 240]
_IOC_OPTIONS  = [288, 576]


class WEFAXWindow(BaseDecoderWindow):

    MAX_LINES  = 1500
    LINE_WIDTH = 1809   # IOC 576: int(576 × π)

    def __init__(self, vfo_id: int):
        super().__init__(f'WEFAX — VFO {vfo_id}', vfo_id, 'WEFAX')
        self.setMinimumSize(900, 600)
        self._line_width    = self.LINE_WIDTH
        self._img_buf       = np.zeros((self.MAX_LINES, self.LINE_WIDTH), dtype=np.uint8)
        self._num_lines     = 0
        self._arr_ref       = None   # keep numpy buffer alive while QImage holds it
        self._configure_fn  = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # BaseDecoderWindow interface
    # ------------------------------------------------------------------

    def set_configure_fn(self, fn) -> None:
        self._configure_fn = fn

    def push_result(self, data: dict) -> None:
        t = data.get('type', '')

        if t == 'start':
            self._line_width = data.get('line_width', self.LINE_WIDTH)
            self._img_buf    = np.zeros((self.MAX_LINES, self._line_width), dtype=np.uint8)
            self._num_lines  = 0
            self._arr_ref    = None
            lpm  = data.get('lpm', '?')
            ioc  = data.get('ioc', '?')
            cont = ' (continuous)' if data.get('continuous') else ''
            self._status.setText(f"Receiving{cont} — {lpm} LPM  IOC {ioc}")

        elif t == 'line':
            pixels = data.get('pixels')
            if pixels is not None and self._num_lines < self.MAX_LINES:
                row  = np.array(pixels, dtype=np.uint8)
                w    = self._line_width
                rlen = min(len(row), w)
                self._img_buf[self._num_lines, :rlen] = row[:rlen]
                self._num_lines += 1
                self._render()
                state = data.get('state', '')
                idx   = data.get('line_index', self._num_lines)
                label = 'Phasing' if state == 'phasing' else 'Image'
                self._status.setText(
                    f"{label} — line {idx}  ({self._lpm_combo.currentText()} LPM"
                    f"  IOC {self._ioc_combo.currentText()})"
                )

        elif t == 'complete':
            count = data.get('line_count', self._num_lines)
            self._status.setText(f"Complete — {count} lines received")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(6, 6, 6, 6)
        vbox.setSpacing(4)

        # ---- settings bar ----
        settings_box = QGroupBox("Settings")
        srow = QHBoxLayout(settings_box)
        srow.setContentsMargins(6, 4, 6, 4)

        srow.addWidget(QLabel("LPM:"))
        self._lpm_combo = QComboBox()
        for v in _LPM_OPTIONS:
            self._lpm_combo.addItem(str(v), v)
        self._lpm_combo.setCurrentIndex(_LPM_OPTIONS.index(120))
        srow.addWidget(self._lpm_combo)

        srow.addWidget(QLabel("IOC:"))
        self._ioc_combo = QComboBox()
        for v in _IOC_OPTIONS:
            self._ioc_combo.addItem(str(v), v)
        self._ioc_combo.setCurrentIndex(_IOC_OPTIONS.index(576))
        srow.addWidget(self._ioc_combo)

        self._continuous_cb = QCheckBox("Continuous (no start/stop tones)")
        srow.addWidget(self._continuous_cb)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self._apply_settings)
        srow.addWidget(self._apply_btn)
        srow.addStretch()
        vbox.addWidget(settings_box)

        # ---- status ----
        self._status = QLabel("Waiting for WEFAX signal…")
        vbox.addWidget(self._status)

        # ---- image ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._img_label = QLabel()
        self._img_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._img_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._img_label)
        vbox.addWidget(self._scroll, 1)

        # ---- buttons ----
        hbox = QHBoxLayout()
        self._save_btn  = QPushButton("Save Image")
        self._clear_btn = QPushButton("Clear")
        self._save_btn.clicked.connect(self._save)
        self._clear_btn.clicked.connect(self._clear)
        hbox.addWidget(self._save_btn)
        hbox.addWidget(self._clear_btn)
        hbox.addStretch()
        vbox.addLayout(hbox)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _apply_settings(self):
        if self._configure_fn is None:
            return
        params = {
            'lpm':        self._lpm_combo.currentData(),
            'ioc':        self._ioc_combo.currentData(),
            'continuous': self._continuous_cb.isChecked(),
        }
        self._configure_fn(params)
        # Reset display for fresh image with new geometry
        ioc = params['ioc']
        import math
        self._line_width = int(ioc * math.pi)
        self._img_buf    = np.zeros((self.MAX_LINES, self._line_width), dtype=np.uint8)
        self._num_lines  = 0
        self._arr_ref    = None
        self._img_label.clear()
        cont = ' (continuous)' if params['continuous'] else ''
        self._status.setText(
            f"Settings applied{cont} — {params['lpm']} LPM  IOC {params['ioc']} — waiting…"
        )

    def _render(self):
        n = self._num_lines
        w = self._line_width
        arr = np.ascontiguousarray(self._img_buf[:n, :w])
        self._arr_ref = arr   # prevent GC while QImage holds the buffer pointer
        img    = QImage(arr.data, w, n, w, QImage.Format.Format_Grayscale8)
        pixmap = QPixmap.fromImage(img)
        self._img_label.setPixmap(pixmap)
        self._img_label.setFixedSize(pixmap.size())
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _save(self):
        if self._num_lines == 0:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save WEFAX Image", "wefax.png",
            "PNG (*.png);;JPEG (*.jpg *.jpeg)",
        )
        if not path:
            return
        n, w  = self._num_lines, self._line_width
        arr   = np.ascontiguousarray(self._img_buf[:n, :w])
        img   = QImage(arr.data, w, n, w, QImage.Format.Format_Grayscale8)
        img.save(path)

    def _clear(self):
        self._num_lines = 0
        self._arr_ref   = None
        self._img_label.clear()
        self._status.setText("Waiting for WEFAX signal…")
