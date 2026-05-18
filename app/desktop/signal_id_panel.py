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
from typing import Callable, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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
    vlay.setContentsMargins(8, 8, 8, 8)
    vlay.setSpacing(8)

    # Name
    name_lbl = QLabel(f"<b>{data['name']}</b>")
    name_lbl.setWordWrap(True)
    name_lbl.setStyleSheet("font-size: 14px; color: #00ffff;")
    vlay.addWidget(name_lbl)

    # Categories
    cats = data.get("cats", [])
    if cats:
        cat_text = " · ".join(r["VALUE"] for r in cats if r["VALUE"])
        cat_lbl = QLabel(cat_text)
        cat_lbl.setWordWrap(True)
        cat_lbl.setStyleSheet("color: #aaaaaa; font-size: 10px;")
        vlay.addWidget(cat_lbl)

    # URL
    url = data.get("url", "")
    if url:
        url_lbl = QLabel(f'<a href="{url}" style="color:#7dd3fc;">{url}</a>')
        url_lbl.setOpenExternalLinks(True)
        url_lbl.setWordWrap(True)
        vlay.addWidget(url_lbl)

    # Description
    description = data.get("description", "")
    if description:
        desc = QTextEdit()
        desc.setReadOnly(True)
        desc.setPlainText(description)
        if desc_max_height > 0:
            desc.setMaximumHeight(desc_max_height)
        desc.setStyleSheet(
            "background:#252525; color:#cccccc;"
            " border:1px solid #404040; border-radius:3px;"
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
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(3)
        for i, (key, val) in enumerate(props):
            k = QLabel(f"{key}:")
            k.setStyleSheet("color:#7dd3fc; font-weight:bold; font-size:11px;")
            k.setAlignment(Qt.AlignTop | Qt.AlignRight)
            v = QLabel(val)
            v.setWordWrap(True)
            v.setStyleSheet("color:#ffffff; font-size:11px;")
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
            lbl.setStyleSheet("color:#aaaaaa; font-size:10px;")
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
        row_w = QHBoxLayout()
        audio_lbl = QLabel(f"♪ {doc['NAME'] or 'Audio sample'}")
        audio_lbl.setStyleSheet("color:#cccccc; font-size:11px;")
        row_w.addWidget(audio_lbl)
        play_btn = QPushButton("▶ Play")
        play_btn.setFixedWidth(60)
        play_btn.setCheckable(True)

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
        row_w.addStretch()
        vlay.addLayout(row_w)

        # Volume slider connects to the audio output owned by the caller;
        # pass a setter so we don't need a reference to it here.
        vol_slider.setProperty("_play_fn_vol", True)

    vlay.addStretch()
    return content


# ---------------------------------------------------------------------------
# Detail window
# ---------------------------------------------------------------------------

class SignalDetailWindow(QMainWindow):
    """Standalone window showing the full details of one Artemis signal."""

    def __init__(self, data: dict, db_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(data["name"])
        self.resize(760, 640)
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1a1a1a; color: #ffffff;
            }
            QTextEdit {
                background-color: #252525; color: #cccccc;
                border: 1px solid #404040; border-radius: 3px;
            }
            QPushButton {
                background-color: #0066cc; color: white;
                border: none; padding: 4px 8px; border-radius: 3px;
            }
            QPushButton:hover  { background-color: #0052a3; }
            QPushButton:checked { background-color: #cc4400; }
            QSlider::groove:horizontal {
                background: #252525; border: 1px solid #404040;
                height: 6px; border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #0066cc; border: 1px solid #0052a3;
                width: 16px; margin: -5px 0; border-radius: 8px;
            }
        """)

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
            img_width=700,
            desc_max_height=-1,
        )
        # Wire volume sliders to this window's audio output
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

class SignalIDPanel(QWidget):
    """
    Searches the Artemis signal database for signals overlapping the active
    VFO frequency and shows their details — one tab per match.
    Each tab has an "Open ↗" button to pop the signal out into a larger window.
    """

    def __init__(self, parent=None):
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

        self._initUI()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        db_row = QHBoxLayout()
        self._db_label = QLabel("No database loaded")
        self._db_label.setStyleSheet("color: #888888; font-size: 10px;")
        self._db_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        db_row.addWidget(self._db_label)
        self._browse_btn = QPushButton("Load DB")
        self._browse_btn.setFixedWidth(60)
        self._browse_btn.setToolTip(
            "Open the Artemis data.sqlite file.\n"
            "Download Artemis from https://github.com/AresValley/Artemis"
        )
        self._browse_btn.clicked.connect(self._on_browse)
        db_row.addWidget(self._browse_btn)
        layout.addLayout(db_row)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Freq:"))
        self._freq_label = QLabel("100.000000 MHz")
        self._freq_label.setStyleSheet("color: #00ffff; font-weight: bold;")
        freq_row.addWidget(self._freq_label)
        freq_row.addStretch()
        self._search_btn = QPushButton("Search")
        self._search_btn.setFixedWidth(55)
        self._search_btn.clicked.connect(self._do_search)
        freq_row.addWidget(self._search_btn)
        self._popout_btn = QPushButton("⊞")
        self._popout_btn.setFixedWidth(28)
        self._popout_btn.setToolTip("Open Signal ID in a separate window")
        self._popout_btn.clicked.connect(self._open_in_window)
        freq_row.addWidget(self._popout_btn)
        layout.addLayout(freq_row)

        self._status_label = QLabel("Load the Artemis database to identify signals.")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(self._status_label)

        self._result_tabs = QTabWidget()
        self._result_tabs.setMinimumHeight(220)
        self._result_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._result_tabs)

        try:
            self._load_db("ARTEMIS/data.sqlite")
        except Exception as exc:
            logger.info("Attempted to load Artemis database but failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_frequency(self, freq_hz: float):
        self._current_freq_hz = freq_hz
        self._freq_label.setText(f"{freq_hz / 1e6:.6f} MHz")
        if self._db_conn is not None and not self._keyword_mode:
            self._search_timer.start(600)
        if self._pop_window is not None and self._pop_window.isVisible():
            self._pop_window.panel.set_frequency(freq_hz)

    def _open_in_window(self):
        if self._pop_window is not None and self._pop_window.isVisible():
            self._pop_window.raise_()
            self._pop_window.activateWindow()
            return
        self._pop_window = SignalIDWindow(self._db_path, self._current_freq_hz)
        self._pop_window.show()

    def search_keywords(self, text: str):
        """Search signals by keyword. Empty text reverts to frequency-based results."""
        text = text.strip()
        if not text:
            self._keyword_mode = False
            self._do_search()
            return

        self._keyword_mode = True
        self._result_tabs.clear()
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
                self._result_tabs.addTab(widget, tab_name[:18])

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
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT COUNT(*) FROM signals")
        except Exception as exc:
            self._db_label.setText("Load failed")
            self._db_label.setStyleSheet("color: #cc2222; font-size: 10px;")
            self._status_label.setText(str(exc))
            logger.error("Artemis DB load failed: %s", exc)
            return

        if self._db_conn:
            self._db_conn.close()
        self._db_conn = conn
        self._db_path = path
        self._db_label.setText(os.path.basename(path))
        self._db_label.setStyleSheet("color: #00cc44; font-size: 10px;")
        self._status_label.setText("Database loaded.")
        self._do_search()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        self._result_tabs.clear()
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
            self._status_label.setText(f"No signals found at {freq_hz / 1e6:.4f} MHz")
            return

        sig_ids = self._rank_by_proximity(sig_ids, freq_hz)
        self._status_label.setText(f"{len(sig_ids)} signal(s) at {freq_hz / 1e6:.4f} MHz")

        for sig_id in sig_ids:
            result = self._build_signal_tab(sig_id)
            if result is not None:
                tab_name, widget = result
                self._result_tabs.addTab(widget, tab_name[:18])

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_signal_data(self, sig_id: int) -> Optional[dict]:
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
        data = self._load_signal_data(sig_id)
        if data is None:
            return None

        # Outer wrapper: "Open ↗" toolbar + scrollable content
        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Toolbar row
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 0)
        toolbar.addStretch()
        open_btn = QPushButton("Open ↗")
        open_btn.setFixedWidth(70)
        open_btn.setToolTip("Open in a larger window")

        def _open(sig_id=sig_id, data=data):
            self._open_detail_window(sig_id, data)

        open_btn.clicked.connect(_open)
        toolbar.addWidget(open_btn)
        outer.addLayout(toolbar)

        # Compact scrollable content
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
        outer.addWidget(scroll)

        return data["name"], wrapper

    def _open_detail_window(self, sig_id: int, data: dict):
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

class SignalIDWindow(QMainWindow):
    """Standalone window containing a full SignalIDPanel at a larger size."""

    def __init__(self, db_path: Optional[str], freq_hz: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signal ID")
        self.resize(900, 720)
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1a1a1a; color: #ffffff; }
            QGroupBox { color: #00ffff; border: 1px solid #404040;
                        border-radius: 5px; margin-top: 10px; padding-top: 10px; }
            QPushButton { background-color: #0066cc; color: white;
                          border: none; padding: 4px 8px; border-radius: 3px; }
            QPushButton:hover    { background-color: #0052a3; }
            QPushButton:checked  { background-color: #cc4400; }
            QLineEdit, QDoubleSpinBox, QTextEdit {
                background-color: #252525; color: #ffffff;
                border: 1px solid #404040; border-radius: 3px; padding: 3px; }
            QTabWidget::pane     { border: 1px solid #404040; }
            QTabBar::tab         { background-color: #252525; color: #aaaaaa;
                                   border: 1px solid #404040; padding: 4px 10px;
                                   border-radius: 3px 3px 0 0; }
            QTabBar::tab:selected { background-color: #1a1a1a; color: #00ffff; }
            QSlider::groove:horizontal { background: #252525; border: 1px solid #404040;
                                         height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #0066cc; border: 1px solid #0052a3;
                                         width: 16px; margin: -5px 0; border-radius: 8px; }
        """)

        self.panel = SignalIDPanel()
        self.panel._popout_btn.setVisible(False)  # no nesting

        # Search bar
        self._kw_timer = QTimer()
        self._kw_timer.setSingleShot(True)
        self._kw_timer.timeout.connect(
            lambda: self.panel.search_keywords(self._search_edit.text())
        )

        search_bar = QWidget()
        search_row = QHBoxLayout(search_bar)
        search_row.setContentsMargins(6, 6, 6, 2)
        search_row.setSpacing(6)
        search_lbl = QLabel("Search:")
        search_lbl.setStyleSheet("color:#aaaaaa;")
        search_row.addWidget(search_lbl)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Signal name, modulation, category…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(lambda _: self._kw_timer.start(350))
        search_row.addWidget(self._search_edit)

        container = QWidget()
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(search_bar)
        vlay.addWidget(self.panel)
        self.setCentralWidget(container)

        if db_path:
            self.panel._load_db(db_path)
        self.panel.set_frequency(freq_hz)
