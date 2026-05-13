"""
TETRA decoder visualization window.

Shows the live burst stream with the most useful channel metadata that can be
extracted from the decoder: BCCH vs TCH/S classification, broadcast-block
fields, sync error count, inversion, codec availability, and PCM frame counts.
"""

from __future__ import annotations

import json
import time
from collections import deque
from html import escape
from typing import Deque, Dict, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from .base import BaseDecoderWindow

_REFRESH_MS = 250
_MAX_EVENTS = 250
_MAX_DISPLAY_ROWS = 50


class TETRAWindow(BaseDecoderWindow):
    """Live TETRA burst/channel inspector."""

    def __init__(self, vfo_id: int):
        super().__init__(f"TETRA Channel - VFO {vfo_id + 1}", vfo_id, "TETRA")
        self._events: Deque[dict] = deque(maxlen=_MAX_EVENTS)
        self._latest: Dict = {}
        self._selected_burst: Optional[int] = None
        self._freeze = False
        self._show_bcch = True
        self._show_traffic = True
        self._auto_follow = True

        self._metric_labels: Dict[str, QLabel] = {}
        self._table: QTableWidget | None = None
        self._details: QTextBrowser | None = None
        self._status_label: QLabel | None = None
        self._freeze_btn: QPushButton | None = None
        self._clear_btn: QPushButton | None = None
        self._show_bcch_cb: QCheckBox | None = None
        self._show_traffic_cb: QCheckBox | None = None
        self._auto_follow_cb: QCheckBox | None = None

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_REFRESH_MS)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("TETRA Channel Inspector")
        title.setObjectName("decoderTitle")
        title.setStyleSheet("font-size:18px; font-weight:700; color:#f5f7fa;")
        self._status_label = QLabel("Waiting for TETRA bursts...")
        self._status_label.setStyleSheet("color:#9aa0a6; font-weight:600;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._status_label)
        root.addLayout(header)

        metrics = self._build_metrics()
        root.addWidget(metrics)

        controls = QGroupBox("View Options")
        controls.setStyleSheet(
            "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; }"
        )
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 16, 8, 8)
        controls_layout.setSpacing(10)

        self._freeze_btn = QPushButton("Freeze")
        self._freeze_btn.setCheckable(True)
        self._freeze_btn.toggled.connect(self._on_freeze_toggled)
        controls_layout.addWidget(self._freeze_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear_events)
        controls_layout.addWidget(self._clear_btn)

        self._auto_follow_cb = QCheckBox("Auto follow latest")
        self._auto_follow_cb.setChecked(True)
        self._auto_follow_cb.toggled.connect(self._on_auto_follow_toggled)
        controls_layout.addWidget(self._auto_follow_cb)

        self._show_bcch_cb = QCheckBox("Show BCCH")
        self._show_bcch_cb.setChecked(True)
        self._show_bcch_cb.toggled.connect(self._on_filter_changed)
        controls_layout.addWidget(self._show_bcch_cb)

        self._show_traffic_cb = QCheckBox("Show TCH/S")
        self._show_traffic_cb.setChecked(True)
        self._show_traffic_cb.toggled.connect(self._on_filter_changed)
        controls_layout.addWidget(self._show_traffic_cb)

        controls_layout.addStretch()
        root.addWidget(controls)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        table_group = QGroupBox("Recent Bursts")
        table_group.setStyleSheet(
            "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; }"
        )
        table_layout = QVBoxLayout(table_group)
        table_layout.setContentsMargins(8, 16, 8, 8)
        self._table = QTableWidget(0, 9)
        self._table.setHorizontalHeaderLabels(
            ["Time", "Burst", "Type", "CC", "TS", "SW err", "Inv", "Voice", "PCM"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(
            "QTableWidget { background:#131416; color:#e0e0e0; border:1px solid #444; }"
            "QHeaderView::section { background:#202124; color:#f0f0f0; padding:4px; border:1px solid #444; }"
            "QTableWidget::item:selected { background:#34507a; }"
        )
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        table_layout.addWidget(self._table)
        left_layout.addWidget(table_group, stretch=3)

        details_group = QGroupBox("Selected Burst Details")
        details_group.setStyleSheet(
            "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; }"
        )
        details_layout = QVBoxLayout(details_group)
        details_layout.setContentsMargins(8, 16, 8, 8)
        self._details = QTextBrowser()
        self._details.setStyleSheet(
            "QTextBrowser { background:#111214; color:#e0e0e0; border:1px solid #444; }"
        )
        self._details.setFont(QFont("Consolas", 10))
        self._details.setOpenExternalLinks(False)
        details_layout.addWidget(self._details)
        left_layout.addWidget(details_group, stretch=2)

        splitter.addWidget(left)
        splitter.addWidget(self._build_channel_notes())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self.resize(1180, 820)
        self.setStyleSheet("background:#0f1012;")

    def _build_metrics(self) -> QGroupBox:
        group = QGroupBox("Channel Summary")
        group.setStyleSheet(
            "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; }"
        )
        layout = QGridLayout(group)
        layout.setContentsMargins(8, 16, 8, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)

        cards = [
            ("Latest burst", "burst"),
            ("Channel type", "burst_type"),
            ("Burst kind", "burst_kind"),
            ("Train seq", "train_seq"),
            ("Mode", "mode"),
            ("Errors", "have_errors"),
            ("Layout", "burst_layout"),
            ("System code", "system_code"),
            ("Colour code", "colour_code"),
            ("Timeslot", "timeslot"),
            ("Sync errors", "sw_errors"),
            ("Training errors", "training_errors"),
            ("Sync quality", "sync_quality"),
            ("Inverted", "inverted"),
            ("Voice burst", "voice_burst"),
            ("PCM samples", "pcm_samples"),
            ("Codec", "codec_available"),
            ("Broadcast block", "bb_hex"),
            ("Confidence", "confidence"),
        ]

        for idx, (label_text, key) in enumerate(cards):
            card = self._make_metric_card(label_text, key)
            layout.addWidget(card, idx // 4, idx % 4)

        return group

    def _make_metric_card(self, title: str, key: str) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { background:#17181b; border:1px solid #32353b; border-radius:6px; }"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("color:#8a9099; font-size:11px; text-transform:uppercase;")
        value_lbl = QLabel("-")
        value_lbl.setStyleSheet("color:#f1f3f4; font-size:15px; font-weight:700;")
        value_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)
        layout.addWidget(value_lbl)

        self._metric_labels[key] = value_lbl
        return frame

    def _build_channel_notes(self) -> QGroupBox:
        group = QGroupBox("Channel Notes")
        group.setStyleSheet(
            "QGroupBox { color:#e0e0e0; border:1px solid #444; border-radius:6px; margin-top:8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left:8px; padding:0 4px; }"
        )
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 16, 8, 8)

        notes = QTextBrowser()
        notes.setStyleSheet(
            "QTextBrowser { background:#111214; color:#d7dae0; border:1px solid #444; }"
        )
        notes.setFont(QFont("Consolas", 10))
        notes.setHtml(
            "<h3 style='margin-top:0;color:#f1f3f4;'>TETRA burst inspector</h3>"
            "<p>Use this window to watch the Broadcast Block metadata and the traffic-channel"
            " speech path separately.</p>"
            "<ul>"
            "<li><b>BCCH</b>: system code 1, with colour code and timeslot extracted from the BB field.</li>"
            "<li><b>TCH/S</b>: traffic bursts with optional ACELP decode output.</li>"
            "<li><b>Codec</b>: indicates whether the TETRA speech codec wrapper is loaded.</li>"
            "</ul>"
            "<p>Filter buttons above can hide BCCH or traffic bursts if you only want one side of the channel.</p>"
        )
        layout.addWidget(notes)
        return group

    # ------------------------------------------------------------------
    # Data ingress
    # ------------------------------------------------------------------

    def push_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        event = dict(data)
        event["_ts"] = time.time()
        self._events.append(event)
        self._latest = event
        if self._auto_follow and not self._freeze:
            self._selected_burst = int(event.get("burst", 0)) if event.get("burst") is not None else None

    # ------------------------------------------------------------------
    # Refresh/render
    # ------------------------------------------------------------------

    def _refresh(self):
        if self._freeze:
            return
        self._render_metrics()
        self._render_table()
        self._render_details()
        self._render_status()

    def _render_metrics(self):
        latest = self._latest or {}
        for key, value in self._metric_labels.items():
            value.setText(self._format_metric(key, latest.get(key)))

    def _render_status(self):
        if self._status_label is None:
            return
        total = len(self._events)
        voice = sum(1 for e in self._events if e.get("voice_burst"))
        bcch = sum(1 for e in self._events if e.get("burst_type") == "BCCH")
        pcm = sum(1 for e in self._events if e.get("pcm_samples", 0))
        if total == 0:
            self._status_label.setText("Waiting for TETRA bursts...")
        else:
            self._status_label.setText(
                f"{total} bursts | BCCH {bcch} | TCH/S {voice} | PCM {pcm}"
            )

    def _render_table(self):
        if self._table is None:
            return
        rows = list(self._filtered_events())[:_MAX_DISPLAY_ROWS]
        # Batch updates and reuse QTableWidgetItem objects to avoid churn.
        self._table.blockSignals(True)
        self._table.setUpdatesEnabled(False)
        try:
            current_rows = self._table.rowCount()
            if current_rows != len(rows):
                self._table.setRowCount(len(rows))

            selected_burst = self._selected_burst
            selected_row = -1
            for row_idx, event in enumerate(rows):
                burst = event.get("burst")
                texts = [
                    time.strftime("%H:%M:%S", time.localtime(event.get("_ts", time.time()))),
                    str(burst) if burst is not None else "-",
                    str(event.get("burst_type", "-")),
                    self._format_metric("colour_code", event.get("colour_code")),
                    self._format_metric("timeslot", event.get("timeslot")),
                    str(event.get("sw_errors", "-")),
                    "yes" if event.get("inverted") else "no",
                    "yes" if event.get("voice_burst") else "no",
                    self._format_metric("pcm_samples", event.get("pcm_samples")),
                ]

                # Reuse or create items, set text only
                for col, text in enumerate(texts):
                    item = self._table.item(row_idx, col)
                    if item is None:
                        item = QTableWidgetItem(text)
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self._table.setItem(row_idx, col, item)
                    else:
                        item.setText(text)

                # Style the row once (apply to all columns)
                fg = None
                bg = None
                burst_type = event.get("burst_type")
                if burst_type == "BCCH":
                    fg = QBrush(QColor("#8ab4f8"))
                elif event.get("voice_burst"):
                    fg = QBrush(QColor("#7ee787"))
                if event.get("inverted"):
                    bg = QBrush(QColor("#2b2315"))
                elif burst_type == "BCCH":
                    bg = QBrush(QColor("#142235"))
                else:
                    bg = QBrush(QColor("#122017"))

                for col in range(self._table.columnCount()):
                    it = self._table.item(row_idx, col)
                    if it is not None:
                        if fg is not None:
                            it.setForeground(fg)
                        else:
                            it.setForeground(QBrush(QColor("#e0e0e0")))
                        if bg is not None:
                            it.setBackground(bg)

                if burst is not None and selected_burst is not None and burst == selected_burst:
                    selected_row = row_idx

            if selected_row >= 0:
                self._table.selectRow(selected_row)
            elif rows and self._auto_follow:
                self._table.selectRow(0)
                top = rows[0].get("burst")
                self._selected_burst = int(top) if top is not None else None
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.blockSignals(False)

    def _render_details(self):
        if self._details is None:
            return
        event = self._selected_event()
        if not event:
            self._details.setHtml(
                "<div style='color:#9aa0a6; font-family:Consolas;'>No burst selected.</div>"
            )
            return

        summary_rows = []
        for key in [
            "burst", "burst_type", "burst_kind", "train_seq", "mode",
            "burst_received", "have_errors", "burst_layout", "system_code", "bb_hex",
            "colour_code", "timeslot", "sw_errors", "training_errors",
            "sync_error_ratio", "sync_quality", "inverted", "voice_burst",
            "codec_available", "pcm_samples", "confidence"
        ]:
            summary_rows.append(
                f"<tr><td style='padding:2px 10px 2px 0;color:#8a9099'>{escape(str(key))}</td>"
                f"<td style='padding:2px 0;color:#f1f3f4'>{escape(self._format_metric(key, event.get(key)))}</td></tr>"
            )

        raw_json = escape(json.dumps(self._serializable_event(event), indent=2, sort_keys=True))
        html = (
            "<div style='font-family:Consolas,monospace;color:#e0e0e0;'>"
            f"<h3 style='margin-top:0;color:#f1f3f4;'>Burst #{escape(self._format_metric('burst', event.get('burst')))}</h3>"
            "<table style='border-collapse:collapse;margin-bottom:10px;'>"
            + ''.join(summary_rows)
            + "</table>"
            "<div style='margin-top:10px;color:#8a9099;'>Raw event</div>"
            f"<pre style='white-space:pre-wrap;background:#111214;border:1px solid #333;padding:10px;border-radius:4px;'>{raw_json}</pre>"
            "</div>"
        )
        self._details.setHtml(html)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_freeze_toggled(self, checked: bool):
        self._freeze = checked
        if self._freeze_btn is not None:
            self._freeze_btn.setText("Unfreeze" if checked else "Freeze")
        if not checked:
            self._refresh()

    def _on_auto_follow_toggled(self, checked: bool):
        self._auto_follow = checked
        if checked:
            self._selected_burst = self._latest.get("burst") if self._latest else None
            self._refresh()

    def _on_filter_changed(self, _checked: bool):
        if self._show_bcch_cb is not None:
            self._show_bcch = self._show_bcch_cb.isChecked()
        if self._show_traffic_cb is not None:
            self._show_traffic = self._show_traffic_cb.isChecked()
        self._refresh()

    def _clear_events(self):
        self._events.clear()
        self._latest = {}
        self._selected_burst = None
        if self._table is not None:
            self._table.clearContents()
            self._table.setRowCount(0)
        if self._details is not None:
            self._details.setHtml("<div style='color:#9aa0a6; font-family:Consolas;'>Cleared.</div>")
        self._render_status()
        self._render_metrics()

    def _on_selection_changed(self):
        event = self._selected_event()
        if event is not None:
            burst = event.get("burst")
            self._selected_burst = int(burst) if burst is not None else None
            self._render_details()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _filtered_events(self):
        for event in reversed(self._events):
            burst_type = event.get("burst_type")
            if burst_type == "BCCH" and not self._show_bcch:
                continue
            if burst_type != "BCCH" and not self._show_traffic:
                continue
            yield event

    def _selected_event(self) -> Optional[dict]:
        if self._selected_burst is None:
            if self._events:
                return self._events[-1]
            return None
        for event in reversed(self._events):
            if event.get("burst") == self._selected_burst:
                return event
        return self._events[-1] if self._events else None

    @staticmethod
    def _serializable_event(event: dict) -> dict:
        data = {}
        for key, value in event.items():
            if key.startswith("_"):
                continue
            if key == "pcm" and value is not None:
                data[key] = f"<{len(value)} samples>"
            elif hasattr(value, "tolist"):
                data[key] = value.tolist()
            else:
                data[key] = value
        return data

    @staticmethod
    def _item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    @staticmethod
    def _format_metric(key: str, value) -> str:
        if value is None:
            return "-"
        if key in ("inverted", "voice_burst", "codec_available", "have_errors", "burst_received"):
            return "yes" if bool(value) else "no"
        if key == "confidence":
            try:
                return f"{float(value):.2f}"
            except Exception:
                return str(value)
        if key == "sync_error_ratio":
            try:
                return f"{float(value):.3f}"
            except Exception:
                return str(value)
        if key == "sync_quality":
            try:
                return f"{float(value) * 100.0:.1f}%"
            except Exception:
                return str(value)
        if key == "timeslot" and value is not None:
            return f"TS{int(value)}"
        if key == "burst_type" and value:
            return str(value)
        if key == "burst_kind" and value:
            return str(value)
        if key == "train_seq" and value:
            return str(value)
        if key == "mode" and value:
            return str(value)
        if key == "burst_layout" and isinstance(value, dict):
            kind = value.get("kind", "-")
            segs = value.get("segments", [])
            seg_names = ",".join(str(seg.get("name", "?")) for seg in segs) if isinstance(segs, list) else "-"
            return f"{kind}: {seg_names}"
        if key == "colour_code" and value is not None:
            return f"CC{int(value)}"
        if key == "pcm_samples" and value is not None:
            return f"{int(value)} samples"
        return str(value)

    def _style_row_item(self, item: QTableWidgetItem, event: dict):
        burst_type = event.get("burst_type")
        if burst_type == "BCCH":
            item.setForeground(QBrush(QColor("#8ab4f8")))
        elif event.get("voice_burst"):
            item.setForeground(QBrush(QColor("#7ee787")))
        if event.get("inverted"):
            item.setBackground(QBrush(QColor("#2b2315")))
        elif burst_type == "BCCH":
            item.setBackground(QBrush(QColor("#142235")))
        else:
            item.setBackground(QBrush(QColor("#122017")))

    def closeEvent(self, event):
        event.ignore()
        self.hide()
