"""
Signal identification panel backed by an Artemis SQLite database.

The panel queries the FREQ_RANGE view for signals whose documented
frequency range brackets the current VFO frequency and renders each
hit as a separate tab with description, properties, spectrum images and
audio playback. Each tab can be popped out into a larger detail window.
"""

import logging
import os
import sqlite3
from contextlib import nullcontext
from typing import Callable, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .timing import TimingConfig, TimingProfiler, profiler_from_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers shared by panel and detail window
# ---------------------------------------------------------------------------

def _fmt_freq(hz) -> str:
    if hz is None:
        return "?"
    hz = int(hz)
    if hz < 1_000:
        return f"{hz} Hz"
    if hz < 1_000_000:
        return f"{hz / 1e3:.3f} kHz"
    if hz < 1_000_000_000:
        return f"{hz / 1e6:.6f} MHz"
    return f"{hz / 1e9:.6f} GHz"


def _render_signal_content(
    data: dict,
    db_dir: str,
    play_fn: Callable,
    img_width: int,
    desc_max_height: int,
) -> QWidget:
    """
    Build and return the inner content QWidget for a signal.

    play_fn(path, btn) — called when a Play button is clicked.
    img_width          — images are scaled to this width.
    desc_max_height    — max height of description box; -1 = unlimited.
    """
    content = QWidget()
    vlay = QVBoxLayout(content)
    vlay.setContentsMargins(10, 10, 10, 10)
    vlay.setSpacing(8)

    # Name
    name_lbl = QLabel(data['name'])
    name_lbl.setWordWrap(True)
    name_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #e2e8f0;")
    vlay.addWidget(name_lbl)

    # Categories
    cats = data.get("cats", [])
    if cats:
        cat_text = "  ·  ".join(r["VALUE"] for r in cats if r["VALUE"])
        cat_lbl = QLabel(cat_text)
        cat_lbl.setWordWrap(True)
        cat_lbl.setStyleSheet("color: #7dd3fc; font-size: 10px; letter-spacing: 0.5px;")
        vlay.addWidget(cat_lbl)

    # URL
    url = data.get("url", "")
    if url:
        url_lbl = QLabel(f'<a href="{url}" style="color:#60a5fa;">{url}</a>')
        url_lbl.setOpenExternalLinks(True)
        url_lbl.setWordWrap(True)
        url_lbl.setStyleSheet("font-size: 10px;")
        vlay.addWidget(url_lbl)

    # Separator
    sep = QWidget()
    sep.setFixedHeight(1)
    sep.setStyleSheet("background: #2d3748;")
    vlay.addWidget(sep)

    # Description
    description = data.get("description", "")
    if description:
        desc = QTextEdit()
        desc.setReadOnly(True)
        desc.setPlainText(description)
        if desc_max_height > 0:
            desc.setMaximumHeight(desc_max_height)
        desc.setStyleSheet(
            "background: #1e2530; color: #cbd5e1;"
            " border: 1px solid #2d3748; border-radius: 4px;"
            " font-size: 11px; padding: 4px;"
        )
        vlay.addWidget(desc)

    # Properties grid
    props = []
    freqs = data.get("freqs", [])
    bws   = data.get("bws", [])
    mods  = data.get("mods", [])
    modes = data.get("modes", [])
    locs  = data.get("locs", [])
    acfs  = data.get("acfs", [])

    if freqs:
        parts = []
        for f in freqs:
            s = _fmt_freq(f["VALUE"])
            if f["DESCRIPTION"]:
                s += f" ({f['DESCRIPTION']})"
            parts.append(s)
        props.append(("Frequency", " / ".join(parts)))
    if bws:
        parts = []
        for b in bws:
            s = _fmt_freq(b["VALUE"])
            if b["DESCRIPTION"]:
                s += f" ({b['DESCRIPTION']})"
            parts.append(s)
        props.append(("Bandwidth", " / ".join(parts)))
    if mods:
        props.append(("Modulation", " / ".join(r["VALUE"] for r in mods if r["VALUE"])))
    if modes:
        props.append(("Mode", " / ".join(r["VALUE"] for r in modes if r["VALUE"])))
    if locs:
        props.append(("Location", " / ".join(r["VALUE"] for r in locs if r["VALUE"])))
    if acfs:
        props.append(("ACF", " / ".join(str(r["VALUE"]) for r in acfs if r["VALUE"] is not None)))

    if props:
        grid_w = QWidget()
        grid_w.setStyleSheet("background: #1a2035; border-radius: 4px;")
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setSpacing(4)
        for i, (key, val) in enumerate(props):
            k = QLabel(f"{key}")
            k.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: bold;")
            k.setAlignment(Qt.AlignTop | Qt.AlignRight)
            v = QLabel(val)
            v.setWordWrap(True)
            v.setStyleSheet("color: #e2e8f0; font-size: 11px;")
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
        grid.setColumnStretch(1, 1)
        vlay.addWidget(grid_w)

    # Spectrum images
    docs = data.get("docs", [])
    for doc in docs:
        if doc["TYPE"] != "Image":
            continue
        img_path = os.path.join(db_dir, "media", f"{doc['DOC_ID']}.{doc['EXTENSION']}")
        if not os.path.exists(img_path):
            continue
        pixmap = QPixmap(img_path)
        if pixmap.isNull():
            continue
        if doc["NAME"]:
            lbl = QLabel(doc["NAME"])
            lbl.setStyleSheet("color: #64748b; font-size: 10px; margin-top: 4px;")
            vlay.addWidget(lbl)
        img_lbl = QLabel()
        img_lbl.setPixmap(pixmap.scaledToWidth(img_width, Qt.SmoothTransformation))
        img_lbl.setAlignment(Qt.AlignCenter)
        vlay.addWidget(img_lbl)

    # Audio samples
    for doc in docs:
        if doc["TYPE"] != "Audio":
            continue
        audio_path = os.path.join(db_dir, "media", f"{doc['DOC_ID']}.{doc['EXTENSION']}")
        if not os.path.exists(audio_path):
            continue
        audio_w = QWidget()
        audio_w.setStyleSheet("background: #1e2530; border-radius: 4px;")
        row_w = QHBoxLayout(audio_w)
        row_w.setContentsMargins(8, 6, 8, 6)
        row_w.setSpacing(8)
        audio_lbl = QLabel(f"♪  {doc['NAME'] or 'Audio sample'}")
        audio_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        row_w.addWidget(audio_lbl)
        row_w.addStretch()
        play_btn = QPushButton("▶ Play")
        play_btn.setFixedWidth(64)
        play_btn.setCheckable(True)
        play_btn.setStyleSheet("""
            QPushButton { background: #1e40af; color: white; border: none;
                          padding: 3px 8px; border-radius: 3px; font-size: 10px; }
            QPushButton:hover   { background: #1d4ed8; }
            QPushButton:checked { background: #b91c1c; }
        """)

        def _make_handler(p, btn):
            def _handler(*_):
                play_fn(p, btn)
            return _handler

        play_btn.clicked.connect(_make_handler(audio_path, play_btn))
        row_w.addWidget(play_btn)

        vol_slider = QSlider(Qt.Horizontal)
        vol_slider.setRange(0, 100)
        vol_slider.setValue(100)
        vol_slider.setFixedWidth(80)
        vol_slider.setToolTip("Volume")
        row_w.addWidget(vol_slider)
        vlay.addWidget(audio_w)

        vol_slider.setProperty("_play_fn_vol", True)

    vlay.addStretch()
    return content


# ---------------------------------------------------------------------------
# Detail window
# ---------------------------------------------------------------------------

_DETAIL_STYLE = """
    QMainWindow, QWidget { background-color: #0f1724; color: #e2e8f0; }
    QScrollArea { border: none; background: transparent; }
    QTextEdit {
        background: #1e2530; color: #cbd5e1;
        border: 1px solid #2d3748; border-radius: 4px;
    }
    QPushButton {
        background: #1e40af; color: white;
        border: none; padding: 4px 10px; border-radius: 3px;
    }
    QPushButton:hover   { background: #1d4ed8; }
    QPushButton:checked { background: #b91c1c; }
    QSlider::groove:horizontal {
        background: #2d3748; border: none; height: 4px; border-radius: 2px;
    }
    QSlider::handle:horizontal {
        background: #3b82f6; width: 14px; margin: -5px 0; border-radius: 7px;
    }
"""


class SignalDetailWindow(QMainWindow):
    """Standalone window showing the full details of one Artemis signal."""

    def __init__(self, data: dict, db_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(data["name"])
        self.resize(800, 680)
        self.setStyleSheet(_DETAIL_STYLE)

        self._audio_out = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(1.0)
        self._active_play_btn: Optional[QPushButton] = None
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)

        db_dir = os.path.dirname(db_path)
        content = _render_signal_content(
            data=data,
            db_dir=db_dir,
            play_fn=self._toggle_audio,
            img_width=740,
            desc_max_height=-1,
        )
        self._wire_volume_sliders(content)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)

    def _wire_volume_sliders(self, widget: QWidget):
        for child in widget.findChildren(QSlider):
            if child.property("_play_fn_vol"):
                child.valueChanged.connect(
                    lambda v: self._audio_out.setVolume(v / 100.0)
                )

    def _toggle_audio(self, path: str, btn: QPushButton):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
            if self._active_play_btn is not None and self._active_play_btn is not btn:
                self._active_play_btn.setText("▶ Play")
                self._active_play_btn.setChecked(False)
            self._active_play_btn = None
            if btn.isChecked():
                self._start_audio(path, btn)
            else:
                btn.setText("▶ Play")
        else:
            self._start_audio(path, btn)

    def _start_audio(self, path: str, btn: QPushButton):
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        btn.setText("■ Stop")
        btn.setChecked(True)
        self._active_play_btn = btn

    def _on_playback_state_changed(self, state):
        if state != QMediaPlayer.PlaybackState.PlayingState:
            if self._active_play_btn is not None:
                self._active_play_btn.setText("▶ Play")
                self._active_play_btn.setChecked(False)
                self._active_play_btn = None

    def closeEvent(self, event):
        self._player.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

_PANEL_STYLE = """
    QListWidget {
        background: #0d1520; border: none; border-right: 1px solid #1e2d45;
        color: #94a3b8; font-size: 10px; outline: none;
    }
    QListWidget::item {
        padding: 5px 8px; border-bottom: 1px solid #131e2e;
    }
    QListWidget::item:selected {
        background: #1e3a5f; color: #7dd3fc;
        border-left: 2px solid #3b82f6;
    }
    QListWidget::item:hover:!selected { background: #182030; }
    QScrollBar:vertical {
        background: #0d1520; width: 6px; margin: 0;
    }
    QScrollBar::handle:vertical {
        background: #2d3748; border-radius: 3px; min-height: 20px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class SignalIDPanel(QWidget):
    """
    Searches the Artemis signal database for signals overlapping the active
    VFO frequency and shows their details — one entry per match in a sidebar list.
    Each entry can be popped out into a larger window.
    """

    def __init__(self, parent=None, timing_report_handler=None):
        super().__init__(parent)
        self._db_path: Optional[str] = None
        self._db_conn: Optional[sqlite3.Connection] = None
        self._current_freq_hz: float = 100e6
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._do_search)

        # Shared audio player for in-panel playback
        self._audio_out = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(1.0)
        self._active_play_btn: Optional[QPushButton] = None
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)

        # Open detail windows: sig_id → SignalDetailWindow
        self._detail_windows: dict[int, SignalDetailWindow] = {}

        # Pop-out window for the whole panel (created on demand)
        self._pop_window: Optional["SignalIDWindow"] = None

        # When True, frequency changes don't overwrite keyword search results
        self._keyword_mode: bool = False
        self._timing_report_handler = timing_report_handler
        self._profiler: Optional[TimingProfiler] = None
        if timing_report_handler is not None:
            self._profiler = profiler_from_config(
                TimingConfig(enabled=True),
                logger=logger,
                prefix="UI / Signal ID",
                report_handler=timing_report_handler,
            )

        self._initUI()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Top bar ────────────────────────────────────────────────────
        header = QWidget()
        self._header_bar = header
        header.setStyleSheet("background: #0a1220;")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(8, 6, 8, 6)
        header_lay.setSpacing(4)

        # DB row
        db_row = QHBoxLayout()
        self._db_label = QLabel("No database loaded")
        self._db_label.setStyleSheet("color: #4a5568; font-size: 10px;")
        self._db_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        db_row.addWidget(self._db_label)
        self._browse_btn = QPushButton("Load DB")
        self._browse_btn.setFixedWidth(58)
        self._browse_btn.setToolTip(
            "Open the Artemis data.sqlite file.\n"
            "Download Artemis from https://github.com/AresValley/Artemis"
        )
        self._browse_btn.setStyleSheet("""
            QPushButton { background: #1e3a5f; color: #7dd3fc; border: none;
                          padding: 2px 6px; border-radius: 3px; font-size: 10px; }
            QPushButton:hover { background: #1e40af; color: white; }
        """)
        self._browse_btn.clicked.connect(self._on_browse)
        db_row.addWidget(self._browse_btn)
        header_lay.addLayout(db_row)

        # Freq row
        freq_row = QHBoxLayout()
        freq_prefix = QLabel("Freq")
        freq_prefix.setStyleSheet("color: #4a5568; font-size: 10px;")
        freq_row.addWidget(freq_prefix)
        self._freq_label = QLabel("100.000000 MHz")
        self._freq_label.setStyleSheet(
            "color: #22d3ee; font-weight: bold; font-size: 10px; margin-left: 4px;"
        )
        freq_row.addWidget(self._freq_label)
        freq_row.addStretch()
        self._search_btn = QPushButton("Search")
        self._search_btn.setFixedWidth(52)
        self._search_btn.setStyleSheet("""
            QPushButton { background: #1e3a5f; color: #7dd3fc; border: none;
                          padding: 2px 6px; border-radius: 3px; font-size: 10px; }
            QPushButton:hover { background: #1e40af; color: white; }
        """)
        self._search_btn.clicked.connect(self._do_search)
        freq_row.addWidget(self._search_btn)
        self._popout_btn = QPushButton("⊞")
        self._popout_btn.setFixedWidth(26)
        self._popout_btn.setToolTip("Open Signal ID in a separate window")
        self._popout_btn.setStyleSheet("""
            QPushButton { background: #1e2d45; color: #64748b; border: none;
                          padding: 2px 4px; border-radius: 3px; font-size: 12px; }
            QPushButton:hover { background: #1e3a5f; color: #7dd3fc; }
        """)
        self._popout_btn.clicked.connect(self._open_in_window)
        freq_row.addWidget(self._popout_btn)
        header_lay.addLayout(freq_row)

        layout.addWidget(header)

        # Status label
        self._status_label = QLabel("Load the Artemis database to identify signals.")
        self._status_label.setWordWrap(True)
        self._status_label.setContentsMargins(8, 3, 8, 3)
        self._status_label.setStyleSheet(
            "color: #4a5568; font-size: 10px; background: #0a1220;"
        )
        layout.addWidget(self._status_label)

        # Divider
        div = QWidget()
        div.setFixedHeight(1)
        div.setStyleSheet("background: #1e2d45;")
        layout.addWidget(div)

        # ── Main area: list (left) + content (right) ──────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background: #1e2d45; }")
        splitter.setChildrenCollapsible(False)

        self._result_list = QListWidget()
        self._result_list.setStyleSheet(_PANEL_STYLE)
        self._result_list.setMinimumWidth(100)
        self._result_list.setMaximumWidth(200)
        self._result_list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._result_list.currentRowChanged.connect(self._on_result_selected)
        splitter.addWidget(self._result_list)

        self._result_stack = QStackedWidget()
        self._result_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._result_stack.setStyleSheet("background: #0f1724;")
        splitter.addWidget(self._result_stack)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([150, 850])
        layout.addWidget(splitter, 1)

        try:
            self._load_db("ARTEMIS/data.sqlite")
        except Exception as exc:
            logger.info("Attempted to load Artemis database but failed: %s", exc)

    # ------------------------------------------------------------------
    # Result list helpers
    # ------------------------------------------------------------------

    def _clear_results(self):
        self._result_list.blockSignals(True)
        self._result_list.clear()
        self._result_list.blockSignals(False)
        while self._result_stack.count():
            w = self._result_stack.widget(0)
            self._result_stack.removeWidget(w)
            w.deleteLater()

    def _add_result(self, widget: QWidget, name: str):
        idx = self._result_stack.count()
        self._result_stack.addWidget(widget)
        item = QListWidgetItem(name)
        item.setToolTip(name)
        self._result_list.addItem(item)
        if idx == 0:
            self._result_list.setCurrentRow(0)
            self._result_stack.setCurrentIndex(0)

    def _on_result_selected(self, row: int):
        if row >= 0:
            self._result_stack.setCurrentIndex(row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _timed(self, stage: str):
        if self._profiler is None:
            return nullcontext()
        return self._profiler.measure(stage)

    def set_frequency(self, freq_hz: float):
        with self._timed("Signal ID / set_frequency"):
            self._current_freq_hz = freq_hz
            self._freq_label.setText(f"{freq_hz / 1e6:.6f} MHz")
            if self._pop_window is not None and self._pop_window.isVisible():
                self._pop_window.panel.set_frequency(freq_hz)

    def set_timing_report_handler(self, timing_report_handler):
        """Enable timing reports from Signal ID searches and rebuild the profiler."""
        self._timing_report_handler = timing_report_handler
        if timing_report_handler is None:
            self._profiler = None
            return
        self._profiler = profiler_from_config(
            TimingConfig(enabled=True),
            logger=logger,
            prefix="UI / Signal ID",
            report_handler=timing_report_handler,
        )

    def _open_in_window(self):
        if self._pop_window is not None and self._pop_window.isVisible():
            self._pop_window.raise_()
            self._pop_window.activateWindow()
            return
        self._pop_window = SignalIDWindow(
            self._db_path,
            self._current_freq_hz,
            timing_report_handler=self._timing_report_handler,
        )
        self._pop_window.show()

    def search_keywords(self, text: str):
        """Search signals by keyword. Empty text reverts to frequency-based results."""
        with self._timed("Signal ID / keyword search"):
            text = text.strip()
            if not text:
                self._keyword_mode = False
                self._do_search()
                return

            self._keyword_mode = True
            self._clear_results()
            if self._db_conn is None:
                return

            words = text.split()
            try:
                matched: Optional[set] = None
                for word in words:
                    term = f"%{word}%"
                    rows = self._db_conn.execute(
                        "SELECT DISTINCT s.SIG_ID FROM signals s"
                        " LEFT JOIN modulation m ON m.SIG_ID = s.SIG_ID"
                        " LEFT JOIN category c ON c.SIG_ID = s.SIG_ID"
                        " LEFT JOIN category_label cl ON cl.CLB_ID = c.CLB_ID"
                        " WHERE s.NAME LIKE ? OR s.DESCRIPTION LIKE ?"
                        "    OR m.VALUE LIKE ? OR cl.VALUE LIKE ?",
                        (term, term, term, term),
                    ).fetchall()
                    word_ids = {row[0] for row in rows}
                    matched = word_ids if matched is None else matched & word_ids
            except Exception as exc:
                self._status_label.setText(f"Search error: {exc}")
                logger.error("Keyword search failed: %s", exc)
                return

            sig_ids = sorted(matched or [])
            if not sig_ids:
                self._status_label.setText(f'No results for "{text}"')
                return

            self._status_label.setText(f'{len(sig_ids)} result(s) for "{text}"')
            for sig_id in sig_ids:
                result = self._build_signal_tab(sig_id)
                if result is not None:
                    tab_name, widget = result
                    self._add_result(widget, tab_name)

    def search_with_filters(self, modulations: list, modes: list, locations: list,
                           categories: list, bandwidth_min_hz: Optional[float] = None,
                           bandwidth_max_hz: Optional[float] = None) -> list:
        """
        Search signals applying multiple metadata filters.
        Empty lists = no filter for that category.
        Returns list of matching SIG_IDs.
        """
        if self._db_conn is None:
            return []

        with self._timed("Signal ID / filter search"):
            try:
                base_query = "SELECT DISTINCT s.SIG_ID FROM signals s"
                conditions = []
                params = []

                if modulations:
                    ph = ",".join("?" * len(modulations))
                    base_query += " LEFT JOIN modulation m ON m.SIG_ID = s.SIG_ID"
                    conditions.append(f"m.VALUE IN ({ph})")
                    params.extend(modulations)

                if modes:
                    ph = ",".join("?" * len(modes))
                    base_query += " LEFT JOIN mode mo ON mo.SIG_ID = s.SIG_ID"
                    conditions.append(f"mo.VALUE IN ({ph})")
                    params.extend(modes)

                if locations:
                    ph = ",".join("?" * len(locations))
                    base_query += " LEFT JOIN location l ON l.SIG_ID = s.SIG_ID"
                    conditions.append(f"l.VALUE IN ({ph})")
                    params.extend(locations)

                if categories:
                    ph = ",".join("?" * len(categories))
                    base_query += (
                        " LEFT JOIN category c ON c.SIG_ID = s.SIG_ID"
                        " LEFT JOIN category_label cl ON cl.CLB_ID = c.CLB_ID"
                    )
                    conditions.append(f"cl.VALUE IN ({ph})")
                    params.extend(categories)

                if bandwidth_min_hz is not None or bandwidth_max_hz is not None:
                    base_query += " LEFT JOIN bandwidth b ON b.SIG_ID = s.SIG_ID"
                    if bandwidth_min_hz is not None and bandwidth_max_hz is not None:
                        conditions.append("(b.VALUE >= ? AND b.VALUE <= ?)")
                        params.extend([bandwidth_min_hz, bandwidth_max_hz])
                    elif bandwidth_min_hz is not None:
                        conditions.append("b.VALUE >= ?")
                        params.append(bandwidth_min_hz)
                    elif bandwidth_max_hz is not None:
                        conditions.append("b.VALUE <= ?")
                        params.append(bandwidth_max_hz)

                if conditions:
                    base_query += " WHERE " + " AND ".join(conditions)

                base_query += " ORDER BY s.SIG_ID"
                rows = self._db_conn.execute(base_query, params).fetchall()
                return [row[0] for row in rows]
            except Exception as exc:
                logger.error("Filter search failed: %s", exc)
                return []

    def get_filter_values(self) -> dict:
        """Retrieve unique values for all filter types from the database."""
        if self._db_conn is None:
            return {}

        with self._timed("Signal ID / load filter values"):
            result = {}
            try:
                rows = self._db_conn.execute(
                    "SELECT DISTINCT VALUE FROM modulation ORDER BY VALUE"
                ).fetchall()
                result['modulations'] = sorted([r[0] for r in rows if r[0]])

                rows = self._db_conn.execute(
                    "SELECT DISTINCT VALUE FROM mode ORDER BY VALUE"
                ).fetchall()
                result['modes'] = sorted([r[0] for r in rows if r[0]])

                rows = self._db_conn.execute(
                    "SELECT DISTINCT VALUE FROM location ORDER BY VALUE"
                ).fetchall()
                result['locations'] = sorted([r[0] for r in rows if r[0]])

                rows = self._db_conn.execute(
                    "SELECT DISTINCT cl.VALUE FROM category_label cl ORDER BY cl.VALUE"
                ).fetchall()
                result['categories'] = sorted([r[0] for r in rows if r[0]])
            except Exception as exc:
                logger.warning("Failed to load filter values: %s", exc)

            return result

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Artemis Database",
            "",
            "SQLite Database (*.sqlite *.db);;All files (*)",
        )
        if path:
            self._load_db(path)

    def _load_db(self, path: str):
        with self._timed("Signal ID / load database"):
            try:
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                conn.execute("SELECT COUNT(*) FROM signals")
            except Exception as exc:
                self._db_label.setText("Load failed")
                self._db_label.setStyleSheet("color: #ef4444; font-size: 10px;")
                self._status_label.setText(str(exc))
                logger.error("Artemis DB load failed: %s", exc)
                return

            if self._db_conn:
                self._db_conn.close()
            self._db_conn = conn
            self._db_path = path
            self._db_label.setText(os.path.basename(path))
            self._db_label.setStyleSheet("color: #22c55e; font-size: 10px;")
            self._status_label.setText("Database loaded.")
            self._do_search()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        with self._timed("Signal ID / frequency search"):
            self._clear_results()
            if self._db_conn is None:
                return

            freq_hz = int(self._current_freq_hz)
            try:
                cur = self._db_conn.execute(
                    "SELECT SIG_ID FROM FREQ_RANGE"
                    " WHERE (? >= MIN_VALUE) AND (? <= MAX_VALUE)",
                    (freq_hz, freq_hz),
                )
                sig_ids = [row[0] for row in cur.fetchall()]
            except Exception as exc:
                self._status_label.setText(f"Query error: {exc}")
                logger.error("Artemis query failed: %s", exc)
                return

            if not sig_ids:
                self._status_label.setText(f"No signals at {freq_hz / 1e6:.4f} MHz")
                return

            sig_ids = self._rank_by_proximity(sig_ids, freq_hz)
            self._status_label.setText(f"{len(sig_ids)} signal(s) at {freq_hz / 1e6:.4f} MHz")

            for sig_id in sig_ids:
                result = self._build_signal_tab(sig_id)
                if result is not None:
                    tab_name, widget = result
                    self._add_result(widget, tab_name)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_signal_data(self, sig_id: int) -> Optional[dict]:
        with self._timed("Signal ID / load signal data"):
            conn = self._db_conn
            try:
                sig = conn.execute(
                    "SELECT NAME, DESCRIPTION, URL FROM signals WHERE SIG_ID = ?",
                    (sig_id,),
                ).fetchone()
                if not sig:
                    return None
                return {
                    "sig_id":      sig_id,
                    "name":        sig["NAME"] or f"Signal {sig_id}",
                    "description": sig["DESCRIPTION"] or "",
                    "url":         sig["URL"] or "",
                    "freqs": conn.execute(
                        "SELECT VALUE, DESCRIPTION FROM frequency"
                        " WHERE SIG_ID = ? ORDER BY VALUE", (sig_id,)
                    ).fetchall(),
                    "bws": conn.execute(
                        "SELECT VALUE, DESCRIPTION FROM bandwidth WHERE SIG_ID = ?",
                        (sig_id,),
                    ).fetchall(),
                    "mods": conn.execute(
                        "SELECT VALUE FROM modulation WHERE SIG_ID = ?", (sig_id,)
                    ).fetchall(),
                    "modes": conn.execute(
                        "SELECT VALUE FROM mode WHERE SIG_ID = ?", (sig_id,)
                    ).fetchall(),
                    "locs": conn.execute(
                        "SELECT VALUE FROM location WHERE SIG_ID = ?", (sig_id,)
                    ).fetchall(),
                    "acfs": conn.execute(
                        "SELECT VALUE FROM acf WHERE SIG_ID = ?", (sig_id,)
                    ).fetchall(),
                    "cats": conn.execute(
                        "SELECT cl.VALUE FROM category c"
                        " JOIN category_label cl ON c.CLB_ID = cl.CLB_ID"
                        " WHERE c.SIG_ID = ?", (sig_id,),
                    ).fetchall(),
                    "docs": conn.execute(
                        "SELECT DOC_ID, EXTENSION, NAME, TYPE, PREVIEW"
                        " FROM documents WHERE SIG_ID = ? ORDER BY TYPE",
                        (sig_id,),
                    ).fetchall(),
                }
            except Exception as exc:
                logger.error("Failed to load signal %d: %s", sig_id, exc)
                return None

    # ------------------------------------------------------------------
    # Tab builder
    # ------------------------------------------------------------------

    def _build_signal_tab(self, sig_id: int) -> Optional[Tuple[str, QWidget]]:
        with self._timed("Signal ID / build tab"):
            data = self._load_signal_data(sig_id)
            if data is None:
                return None

            wrapper = QWidget()
            wrapper.setStyleSheet("background: #0f1724;")
            outer = QVBoxLayout(wrapper)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            # Open ↗ button in top-right corner
            toolbar = QHBoxLayout()
            toolbar.setContentsMargins(0, 4, 6, 0)
            toolbar.addStretch()
            open_btn = QPushButton("Open ↗")
            open_btn.setFixedWidth(68)
            open_btn.setToolTip("Open in a larger window")
            open_btn.setStyleSheet("""
                QPushButton { background: #1e2d45; color: #64748b; border: none;
                              padding: 2px 6px; border-radius: 3px; font-size: 10px; }
                QPushButton:hover { background: #1e3a5f; color: #7dd3fc; }
            """)

            def _open(sig_id=sig_id, data=data):
                self._open_detail_window(sig_id, data)

            open_btn.clicked.connect(_open)
            toolbar.addWidget(open_btn)
            outer.addLayout(toolbar)

            content = _render_signal_content(
                data=data,
                db_dir=os.path.dirname(self._db_path),
                play_fn=self._toggle_audio,
                img_width=260,
                desc_max_height=80,
            )
            self._wire_volume_sliders(content)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            scroll.setStyleSheet(
                "QScrollArea { border: none; background: transparent; }"
                "QScrollBar:vertical { background: #0d1520; width: 6px; }"
                "QScrollBar::handle:vertical { background: #2d3748; border-radius: 3px; min-height: 20px; }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            )
            outer.addWidget(scroll)

            return data["name"], wrapper

    def _open_detail_window(self, sig_id: int, data: dict):
        with self._timed("Signal ID / open detail window"):
            win = self._detail_windows.get(sig_id)
            if win is not None and not win.isHidden():
                win.raise_()
                win.activateWindow()
                return
            win = SignalDetailWindow(data, self._db_path)
            win.setAttribute(Qt.WA_DeleteOnClose, False)
            self._detail_windows[sig_id] = win
            win.show()
            win.raise_()

    def _wire_volume_sliders(self, widget: QWidget):
        for child in widget.findChildren(QSlider):
            if child.property("_play_fn_vol"):
                child.valueChanged.connect(
                    lambda v: self._audio_out.setVolume(v / 100.0)
                )

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    def _rank_by_proximity(self, sig_ids: list, freq_hz: int) -> list:
        ph = ",".join("?" * len(sig_ids))
        try:
            rows = self._db_conn.execute(
                f"SELECT SIG_ID, MIN(ABS(VALUE - ?)) AS dist"
                f" FROM frequency WHERE SIG_ID IN ({ph})"
                f" GROUP BY SIG_ID ORDER BY dist ASC",
                [freq_hz, *sig_ids],
            ).fetchall()
        except Exception as exc:
            logger.warning("Ranking query failed: %s", exc)
            return sig_ids
        ranked = [row[0] for row in rows]
        missing = [s for s in sig_ids if s not in set(ranked)]
        return ranked + missing

    # ------------------------------------------------------------------
    # Audio player (panel)
    # ------------------------------------------------------------------

    def _toggle_audio(self, path: str, btn: QPushButton):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
            if self._active_play_btn is not None and self._active_play_btn is not btn:
                self._active_play_btn.setText("▶ Play")
                self._active_play_btn.setChecked(False)
            self._active_play_btn = None
            if btn.isChecked():
                self._start_audio(path, btn)
            else:
                btn.setText("▶ Play")
        else:
            self._start_audio(path, btn)

    def _start_audio(self, path: str, btn: QPushButton):
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        btn.setText("■ Stop")
        btn.setChecked(True)
        self._active_play_btn = btn

    def _on_playback_state_changed(self, state):
        if state != QMediaPlayer.PlaybackState.PlayingState:
            if self._active_play_btn is not None:
                self._active_play_btn.setText("▶ Play")
                self._active_play_btn.setChecked(False)
                self._active_play_btn = None


# ---------------------------------------------------------------------------
# Pop-out window wrapping a full SignalIDPanel
# ---------------------------------------------------------------------------

_WINDOW_STYLE = """
    QMainWindow, QWidget { background-color: #0f1724; color: #e2e8f0; }
    QScrollArea { border: none; background: transparent; }
    QTextEdit {
        background: #1e2530; color: #cbd5e1;
        border: 1px solid #2d3748; border-radius: 4px;
    }
    QPushButton {
        background: #1e40af; color: white;
        border: none; padding: 4px 10px; border-radius: 3px; font-size: 11px;
    }
    QPushButton:hover   { background: #1d4ed8; }
    QPushButton:checked { background: #b91c1c; }
    QLineEdit {
        background: #1e2530; color: #e2e8f0;
        border: 1px solid #2d3748; border-radius: 4px;
        padding: 4px 8px; font-size: 11px;
    }
    QLineEdit:focus { border-color: #3b82f6; }
    QComboBox {
        background: #1e2530; color: #e2e8f0;
        border: 1px solid #2d3748; border-radius: 3px;
        padding: 3px 6px; font-size: 10px;
    }
    QComboBox::drop-down { border: none; width: 18px; }
    QSlider::groove:horizontal {
        background: #2d3748; border: none; height: 4px; border-radius: 2px;
    }
    QSlider::handle:horizontal {
        background: #3b82f6; width: 14px; margin: -5px 0; border-radius: 7px;
    }
"""

_FILTER_LIST_STYLE = """
    QListWidget {
        background: #131e2e; border: 1px solid #1e2d45;
        border-radius: 4px; color: #94a3b8;
        font-size: 10px; outline: none;
    }
    QListWidget::item { padding: 3px 6px; }
    QListWidget::item:selected { background: #1e3a5f; color: #7dd3fc; }
    QListWidget::item:hover:!selected { background: #182030; }
    QScrollBar:vertical { background: #131e2e; width: 5px; margin: 0; }
    QScrollBar::handle:vertical { background: #2d3748; border-radius: 2px; min-height: 16px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class SignalIDWindow(QMainWindow):
    """Standalone window containing a full SignalIDPanel with search and filters."""

    def __init__(self, db_path: Optional[str], freq_hz: float, parent=None,
                 timing_report_handler=None):
        super().__init__(parent)
        self.setWindowTitle("Signal ID — Artemis")
        self.resize(1100, 820)
        self.setStyleSheet(_WINDOW_STYLE)

        self.panel = SignalIDPanel(timing_report_handler=timing_report_handler)
        self.panel._popout_btn.setVisible(False)
        self.panel._header_bar.setVisible(False)

        self._kw_timer = QTimer()
        self._kw_timer.setSingleShot(True)
        self._kw_timer.timeout.connect(self._apply_all_filters)

        # ── Info bar: DB path + freq display ──────────────────────────
        info_bar = QWidget()
        info_bar.setStyleSheet("background: #0a1220; border-bottom: 1px solid #1e2d45;")
        info_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        info_row = QHBoxLayout(info_bar)
        info_row.setContentsMargins(8, 5, 8, 5)
        info_row.setSpacing(8)

        # Lift DB label + button out of the panel into this bar
        info_row.addWidget(self.panel._db_label)
        info_row.addWidget(self.panel._browse_btn)

        sep = QLabel("·")
        sep.setStyleSheet("color: #2d3748; font-size: 14px;")
        info_row.addWidget(sep)

        freq_prefix = QLabel("Freq")
        freq_prefix.setStyleSheet("color: #4a5568; font-size: 10px;")
        info_row.addWidget(freq_prefix)
        info_row.addWidget(self.panel._freq_label)
        info_row.addStretch()
        info_row.addWidget(self.panel._search_btn)

        # ── Search toolbar ────────────────────────────────────────────
        search_bar = QWidget()
        search_bar.setStyleSheet("background: #0a1220; border-bottom: 1px solid #1e2d45;")
        search_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        search_row = QHBoxLayout(search_bar)
        search_row.setContentsMargins(8, 5, 8, 5)
        search_row.setSpacing(6)

        search_icon = QLabel("🔍")
        search_icon.setStyleSheet("font-size: 13px;")
        search_row.addWidget(search_icon)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search by name, modulation, category…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(lambda _: self._kw_timer.start(350))
        search_row.addWidget(self._search_edit)

        self._filter_toggle = QToolButton()
        self._filter_toggle.setText("Filters")
        self._filter_toggle.setCheckable(True)
        self._filter_toggle.setChecked(False)
        self._filter_toggle.setArrowType(Qt.RightArrow)
        self._filter_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._filter_toggle.setStyleSheet("""
            QToolButton {
                background: #1e2d45; color: #7dd3fc; border: none;
                padding: 4px 10px; border-radius: 3px; font-size: 11px;
            }
            QToolButton:hover { background: #1e3a5f; }
            QToolButton:checked { background: #1e3a5f; color: #22d3ee; }
        """)
        self._filter_toggle.toggled.connect(self._on_filter_toggle)
        search_row.addWidget(self._filter_toggle)

        # ── Filter panel (collapsible) ────────────────────────────────
        self._filter_panel = self._build_filter_panel()
        self._filter_panel.setVisible(False)

        # Lift status label out of panel so it sits just above the results
        self.panel._status_label.setStyleSheet(
            "color: #64748b; font-size: 10px; background: #0a1220;"
            " padding: 3px 8px; border-bottom: 1px solid #1e2d45;"
        )

        # ── Container ─────────────────────────────────────────────────
        container = QWidget()
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(info_bar, 0)
        vlay.addWidget(search_bar, 0)
        vlay.addWidget(self._filter_panel, 0)
        vlay.addWidget(self.panel._status_label, 0)
        vlay.addWidget(self.panel, 1)
        self.setCentralWidget(container)

        if db_path:
            self.panel._load_db(db_path)
        self.panel.set_frequency(freq_hz)

    # ------------------------------------------------------------------
    # Filter panel
    # ------------------------------------------------------------------

    def _build_filter_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background: #0a1220; border-bottom: 1px solid #1e2d45;")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(8)

        filter_values = self.panel.get_filter_values()

        def _make_filter_list(values: list) -> QListWidget:
            lw = QListWidget()
            lw.setStyleSheet(_FILTER_LIST_STYLE)
            lw.setSelectionMode(QAbstractItemView.MultiSelection)
            lw.setMaximumHeight(120)
            lw.addItems(values)
            lw.itemSelectionChanged.connect(lambda: self._kw_timer.start(200))
            return lw

        # Four category columns
        columns_row = QHBoxLayout()
        columns_row.setSpacing(12)

        for title, values, attr in [
            ("Modulation", filter_values.get('modulations', []), '_mod_list'),
            ("Mode",       filter_values.get('modes', []),       '_mode_list'),
            ("Location",   filter_values.get('locations', []),   '_loc_list'),
            ("Category",   filter_values.get('categories', []),  '_cat_list'),
        ]:
            col = QVBoxLayout()
            col.setSpacing(4)
            lbl = QLabel(title)
            lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: bold; letter-spacing: 0.5px;")
            col.addWidget(lbl)
            lw = _make_filter_list(values)
            setattr(self, attr, lw)
            col.addWidget(lw)
            columns_row.addLayout(col)

        outer.addLayout(columns_row)

        # Bandwidth row + reset
        bw_row = QHBoxLayout()
        bw_row.setSpacing(8)
        bw_lbl = QLabel("Bandwidth")
        bw_lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: bold;")
        bw_row.addWidget(bw_lbl)

        self._bw_min_combo = QComboBox()
        self._bw_min_combo.addItem("Any min")
        for bw in [0.5, 1, 5, 10, 12.5, 25, 50, 100]:
            self._bw_min_combo.addItem(f"≥ {bw} kHz", bw * 1000)
        self._bw_min_combo.setFixedWidth(100)
        self._bw_min_combo.currentIndexChanged.connect(self._apply_all_filters)
        bw_row.addWidget(self._bw_min_combo)

        self._bw_max_combo = QComboBox()
        self._bw_max_combo.addItem("Any max")
        for bw in [1, 5, 10, 12.5, 25, 50, 100, 200, 500, 1000]:
            self._bw_max_combo.addItem(f"≤ {bw} kHz", bw * 1000)
        self._bw_max_combo.setFixedWidth(100)
        self._bw_max_combo.currentIndexChanged.connect(self._apply_all_filters)
        bw_row.addWidget(self._bw_max_combo)

        bw_row.addStretch()

        reset_btn = QPushButton("Reset filters")
        reset_btn.setFixedWidth(90)
        reset_btn.setStyleSheet("""
            QPushButton { background: #1e2d45; color: #94a3b8; border: none;
                          padding: 4px 8px; border-radius: 3px; font-size: 10px; }
            QPushButton:hover { background: #1e3a5f; color: #e2e8f0; }
        """)
        reset_btn.clicked.connect(self._reset_filters)
        bw_row.addWidget(reset_btn)

        outer.addLayout(bw_row)
        return panel

    def _on_filter_toggle(self, checked: bool):
        self._filter_panel.setVisible(checked)
        self._filter_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _apply_all_filters(self):
        keyword = self._search_edit.text().strip()
        mods  = [item.text() for item in self._mod_list.selectedItems()]
        modes = [item.text() for item in self._mode_list.selectedItems()]
        locs  = [item.text() for item in self._loc_list.selectedItems()]
        cats  = [item.text() for item in self._cat_list.selectedItems()]

        bw_min = self._bw_min_combo.currentData() if self._bw_min_combo.currentIndex() > 0 else None
        bw_max = self._bw_max_combo.currentData() if self._bw_max_combo.currentIndex() > 0 else None

        self.panel._clear_results()
        if self.panel._db_conn is None:
            return

        if keyword:
            self.panel.search_keywords(keyword)
        elif mods or modes or locs or cats or bw_min is not None or bw_max is not None:
            sig_ids = self.panel.search_with_filters(
                modulations=mods, modes=modes, locations=locs, categories=cats,
                bandwidth_min_hz=bw_min, bandwidth_max_hz=bw_max,
            )
            if not sig_ids:
                self.panel._status_label.setText("No signals match the selected filters")
                return
            self.panel._status_label.setText(f"{len(sig_ids)} signal(s) match filters")
            for sig_id in sig_ids:
                result = self.panel._build_signal_tab(sig_id)
                if result is not None:
                    tab_name, widget = result
                    self.panel._add_result(widget, tab_name)
        else:
            self.panel._do_search()

    def _reset_filters(self):
        for lw in (self._mod_list, self._mode_list, self._loc_list, self._cat_list):
            lw.clearSelection()
        self._bw_min_combo.setCurrentIndex(0)
        self._bw_max_combo.setCurrentIndex(0)
        self._search_edit.clear()
        self.panel._do_search()
