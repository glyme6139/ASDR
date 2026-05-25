"""
Main application window for ASDR desktop client.
"""

import logging
from html import escape

from PySide6.QtWidgets import (
    QMainWindow,
    QPushButton,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QStatusBar,
    QLabel,
    QTextBrowser,
    QFileDialog,
    QCheckBox,
)
from PySide6.QtCore import Qt, Signal

from .visualizations import VisualizationPanel, VFO_COLORS
from .control_panels import ControlPanel
from .dsp_process import DSPProcess
from .ipc_adapter import IPCAdapterThread
from .eye_diagram_window import EyeDiagramWindow
from .oscilloscope_window import OscilloscopeWindow
from .signal_decoder_window import SignalDecoderWindow
from .signal_rate_window import SignalRateWindow
from .timing import TimingConfig, profiler_from_config
from .timing_window import TimingWindow

logger = logging.getLogger(__name__)

DEFAULT_CENTER_HZ   = 100e6
DEFAULT_SAMPLE_RATE = 20e6


class ASURMainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, timing: TimingConfig | None = None):
        super().__init__()
        self._timing = timing or TimingConfig()
        self._profiler = profiler_from_config(
            self._timing,
            logger=logger,
            prefix="UI",
            report_handler=self._handle_timing_report,
        )
        self.setWindowTitle("Advanced SDR (ASDR)")
        self.setGeometry(100, 100, 1400, 900)

        # Shadow state: tracks per-VFO freq/bandwidth in the UI process
        # so spectrum markers can be updated without round-tripping the DSP process.
        self._vfo_state: dict = {}   # {vfo_id: {'freq_hz': float, 'bandwidth_hz': float}}
        self._paused_vfos: set = set()  # VFO IDs whose capture is frozen
        # Optional decoder visualization windows: (vfo_id, decoder_name) → window
        self._decoder_windows: dict = {}
        self._center_hz   = DEFAULT_CENTER_HZ
        self._sample_rate = DEFAULT_SAMPLE_RATE
        # Decoder output logging
        self._log_decoder_enabled: bool = False
        self._log_decoder_file_path: str | None = None
        self._log_decoder_file = None

        self.timing_window = TimingWindow()
        self.eye_window = EyeDiagramWindow(self)
        self.osc_window = OscilloscopeWindow(self)
        self.decoder_win = SignalDecoderWindow(self)
        self.rate_window = SignalRateWindow(self)

        self.dsp = DSPProcess(timing=self._timing)
        self.ipc = IPCAdapterThread(self.dsp.result_queue, profiler=self._profiler)

        self._initUI()
        self._connect_signals()
        # session controls moved to main menu
        self._apply_stylesheet()

        # Wire shared-memory display buffers into the visualization panel
        self.vis_panel.set_process_display(
            self.dsp.spectrum_buf,
            self.dsp.waterfall_buf,
            self.dsp.display_gen,
        )

        # VFOTabPanel creates tab 0 during __init__ before vfo_added is connected,
        # so bootstrap any pre-existing tabs now.
        for vfo_id in sorted(self.ctrl_panel.vfo_tab._tabs.keys()):
            self._on_vfo_added(vfo_id)
        self._sync_vfo_choices()

        self.dsp.start()
        self.ipc.start()

        # Load session if available and apply settings
        try:
            from app import session
            self._apply_session(session.load_session())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        central = QWidget()
        self.setCentralWidget(central)

        top_widget = QWidget()
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(4, 4, 4, 4)
        top_layout.setSpacing(4)

        self.vis_panel = VisualizationPanel(
            timing=self._timing,
            report_handler=self._handle_timing_report,
        )
        top_layout.addWidget(self.vis_panel, stretch=2)

        self.ctrl_panel = ControlPanel()
        top_layout.addWidget(self.ctrl_panel, stretch=1)
        try:
            self.ctrl_panel.signal_id_panel.set_timing_report_handler(self._handle_timing_report)
        except Exception:
            pass

        top_widget.setLayout(top_layout)

        self.decoder_panel = self._create_decoder_panel()
        self.decoder_panel.log_toggled.connect(self._on_decoder_log_toggled)

        vlayout = QVBoxLayout()
        vlayout.setContentsMargins(4, 4, 4, 4)
        vlayout.setSpacing(4)
        vlayout.addWidget(top_widget, stretch=7)
        vlayout.addWidget(self.decoder_panel, stretch=3)
        central.setLayout(vlayout)

        self.status_bar   = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Initializing…")
        self.status_bar.addWidget(self.status_label)

        self.vis_panel.set_freq_range(DEFAULT_CENTER_HZ, DEFAULT_SAMPLE_RATE)

        # --- Session menu (Load / Save / Autosave) ---
        try:
            from PySide6.QtGui import QAction
            menubar = self.menuBar()
            session_menu = menubar.addMenu("Session")

            self._new_session_action = QAction("New Session", self)
            self._new_session_action.triggered.connect(self._on_new_session)
            session_menu.addAction(self._new_session_action)

            session_menu.addSeparator()

            self._load_session_action = QAction("Load Session...", self)
            self._load_session_action.triggered.connect(self._on_load_session)
            session_menu.addAction(self._load_session_action)

            self._save_session_action = QAction("Save Session...", self)
            self._save_session_action.triggered.connect(self._on_manual_save)
            session_menu.addAction(self._save_session_action)

            session_menu.addSeparator()

            self._autosave_action = QAction("Autosave on exit", self)
            self._autosave_action.setCheckable(True)
            # default checked; will be set from loaded session if present
            self._autosave_action.setChecked(True)
            session_menu.addAction(self._autosave_action)
        except Exception:
            pass

        try:
            menubar = self.menuBar()
            view_menu = menubar.addMenu("View")
            self._timing_action = view_menu.addAction("Performance Timing")
            self._timing_action.triggered.connect(self._show_timing_window)
            self._eye_action = view_menu.addAction("Eye Diagram")
            self._eye_action.triggered.connect(self._show_eye_window)
            self._osc_action = view_menu.addAction("Oscilloscope")
            self._osc_action.triggered.connect(self._show_osc_window)
            self._rate_action = view_menu.addAction("Signal Rate")
            self._rate_action.triggered.connect(self._show_rate_window)
            self._decoder_win_action = view_menu.addAction("Signal Decoder")
            self._decoder_win_action.triggered.connect(self._show_decoder_win)
        except Exception:
            pass

    def _create_decoder_panel(self):
        class DecoderAggregator(QWidget):
            log_toggled = Signal(bool)

            def __init__(self, parent=None):
                super().__init__(parent)
                layout = QVBoxLayout(self)
                layout.setContentsMargins(4, 4, 4, 4)
                layout.setSpacing(4)

                header_row = QHBoxLayout()
                header_row.setContentsMargins(0, 0, 0, 0)
                header = QLabel("Decoder Output (aggregated)")
                header.setAlignment(Qt.AlignLeft)
                header_row.addWidget(header)
                header_row.addStretch()
                self.log_check = QCheckBox("Log")
                self.log_check.setToolTip("Log decoder output to file")
                self.log_check.stateChanged.connect(lambda: self.log_toggled.emit(self.log_check.isChecked()))
                header_row.addWidget(self.log_check)
                self.clear_btn = QPushButton("Clear")
                self.clear_btn.setToolTip("Clear aggregated decoder output")
                header_row.addWidget(self.clear_btn)
                layout.addLayout(header_row)

                self.output = QTextBrowser()
                self.output.setReadOnly(True)
                self.output.document().setMaximumBlockCount(1000)
                layout.addWidget(self.output)

                # Wire clear button after output exists
                self.clear_btn.clicked.connect(self.output.clear)

            def append(self, vfo_name: str, decoder_name: str, text: str, vfo_color: str):
                import time
                ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                line = (
                    f'<span style="color:#9aa0a6">[{escape(ts)}]</span> '
                    f'<span style="color:{vfo_color}; font-weight:600">{escape(vfo_name)}</span> '
                    f'<span style="color:#7dd3fc">[{escape(decoder_name)}]</span> '
                    f'<span style="color:#ffffff">{escape(text)}</span>'
                )
                self.output.append(line)

        return DecoderAggregator()

    def _connect_signals(self):
        Q = Qt.QueuedConnection

        # ---- IPC adapter → UI ----
        self.ipc.device_status_changed.connect(self._on_device_status, Q)
        self.ipc.error_occurred.connect(self._on_error, Q)
        self.ipc.decoder_result.connect(self._on_decoder_result, Q)
        self.ipc.decoder_data.connect(self._on_decoder_data, Q)
        self.ipc.signal_strength.connect(self._on_signal_strength, Q)
        self.ipc.timing_report.connect(self._handle_timing_report, Q)
        self.ipc.vfo_bandwidth_update.connect(self._on_vfo_bandwidth_update, Q)
        self.ipc.eye_samples.connect(self._on_eye_samples, Q)

        # ---- VFO tab lifecycle ----
        self.ctrl_panel.vfo_tab.vfo_added.connect(self._on_vfo_added)
        self.ctrl_panel.vfo_tab.vfo_removed.connect(self._on_vfo_removed)
        self.ctrl_panel.vfo_tab.active_vfo_changed.connect(
            self.vis_panel.set_active_vfo_marker
        )
        # VFO rename → update visualization labels
        try:
            self.ctrl_panel.vfo_tab.vfo_renamed.connect(self._on_vfo_renamed)
        except Exception:
            pass

        # ---- VFO settings → DSP ----
        self.ctrl_panel.vfo_tab.frequency_changed.connect(self._on_vfo_frequency_changed)
        self.ctrl_panel.vfo_tab.demod_changed.connect(self.dsp.set_vfo_demod)
        self.ctrl_panel.vfo_tab.bandwidth_changed.connect(self._on_vfo_bandwidth_changed)
        self.ctrl_panel.vfo_tab.squelch_changed.connect(self.dsp.set_vfo_squelch)
        self.ctrl_panel.vfo_tab.squelch_enabled_changed.connect(self.dsp.set_vfo_squelch_enabled)

        # ---- VFO audio controls → DSP ----
        self.ctrl_panel.vfo_tab.volume_changed.connect(
            lambda vid, v: self.dsp.set_volume(vid, v)
        )
        self.ctrl_panel.vfo_tab.mute_changed.connect(
            lambda vid, m: self.dsp.set_vfo_muted(vid, m)
        )
        self.ctrl_panel.vfo_tab.paused_changed.connect(self._on_vfo_paused_changed)

        # ---- Decoder toggles ----
        self.ctrl_panel.vfo_tab.decoder_toggled.connect(self._on_decoder_toggled)

        # ---- Decoder visualization windows ----
        self.ctrl_panel.vfo_tab.open_window_requested.connect(self._on_open_decoder_window)

        # ---- Bookmarks ----
        self.ctrl_panel.bookmark_panel.bookmark_add_requested.connect(
            self._on_bookmark_add_requested
        )

        # ---- Source panel (HackRF controls) ----
        sp = self.ctrl_panel.source_panel
        sp.center_freq_changed.connect(self._on_center_freq_changed)
        sp.sample_rate_changed.connect(self._on_sample_rate_changed)
        sp.lna_gain_changed.connect(lambda v: self.dsp.set_lna_gain(float(v)))
        sp.vga_gain_changed.connect(lambda v: self.dsp.set_vga_gain(float(v)))
        sp.amp_enabled_changed.connect(self.dsp.set_amp_enable)

        # ---- Source panel (new: connect/disconnect, file source, recording, playback) ----
        sp.connect_requested.connect(self._on_hackrf_connect)
        sp.disconnect_requested.connect(self.dsp.disconnect_hackrf)
        sp.sweep_connect_requested.connect(self._on_sweep_connect)
        sp.sweep_disconnect_requested.connect(self.dsp.disconnect_hackrf)
        sp.file_source_opened.connect(
            lambda path, cf, sr: self.dsp.set_source('file', path, cf, sr)
        )
        sp.record_start_requested.connect(
            lambda path, fmt, dur: self.dsp.start_recording(path, fmt, dur)
        )
        sp.record_stop_requested.connect(self.dsp.stop_recording)
        sp.playback_play_requested.connect(self.dsp.playback_resume)
        sp.playback_pause_requested.connect(self.dsp.playback_pause)
        sp.playback_stop_requested.connect(self.dsp.playback_stop)
        sp.playback_speed_changed.connect(self.dsp.set_playback_speed)
        sp.playback_seek_requested.connect(self.dsp.playback_seek)
        sp.playback_loop_changed.connect(self.dsp.set_playback_loop)

        # ---- IPC → source panel (playback position + recording status) ----
        self.ipc.playback_position.connect(sp.update_playback_position, Q)
        self.ipc.recording_status.connect(sp.update_recording_status, Q)

        # ---- Spectrum click → tune active VFO ----
        self.vis_panel.spectrum.frequency_clicked.connect(self._on_spectrum_clicked)
        # ---- Spectrum VFO marker drag/click → target that specific VFO ----
        self.vis_panel.spectrum.vfo_selected.connect(self._on_vfo_selected_from_spectrum)
        self.vis_panel.spectrum.vfo_marker_changed.connect(self._on_vfo_marker_changed)

        # ---- Signal ID panel — track active VFO frequency ----
        self.ctrl_panel.vfo_tab.frequency_changed.connect(self._on_vfo_freq_for_signal_id)
        self.ctrl_panel.vfo_tab.active_vfo_changed.connect(self._on_active_vfo_for_signal_id)
        self.ctrl_panel.vfo_tab.active_vfo_changed.connect(self._on_active_vfo_changed)

        self.timing_window.sample_count_changed.connect(self._on_timing_sample_count_changed)
        self.eye_window.visibility_changed.connect(self._on_eye_window_visibility_changed)
        self.eye_window.vfo_changed.connect(self._on_eye_window_vfo_changed)
        self.osc_window.visibility_changed.connect(self._on_osc_window_visibility_changed)
        self.osc_window.vfo_changed.connect(self._on_osc_window_vfo_changed)
        self.decoder_win.visibility_changed.connect(self._on_decoder_win_visibility_changed)
        self.decoder_win.vfo_changed.connect(self._on_decoder_win_vfo_changed)

    # ------------------------------------------------------------------
    # VFO lifecycle
    # ------------------------------------------------------------------

    def _on_vfo_added(self, vfo_id: int):
        freq  = self._center_hz
        bw    = 12_500.0
        color = VFO_COLORS[vfo_id % len(VFO_COLORS)]

        self._vfo_state[vfo_id] = {'freq_hz': freq, 'bandwidth_hz': bw}
        label = f"VFO {vfo_id + 1}"
        self._vfo_state[vfo_id]['name'] = label
        self.vis_panel.add_vfo_marker(
            vfo_id, freq_hz=freq, bandwidth_hz=bw, color=color, label=label
        )
        self.ctrl_panel.vfo_tab.set_vfo_color(vfo_id, color)
        self.dsp.add_vfo(vfo_id, freq)
        self._check_vfo_ranges()
        self._sync_vfo_choices()

    def _on_vfo_removed(self, vfo_id: int):
        self._vfo_state.pop(vfo_id, None)
        self._paused_vfos.discard(vfo_id)
        self.vis_panel.remove_vfo_marker(vfo_id)
        self.dsp.remove_vfo(vfo_id)
        for key in list(self._decoder_windows.keys()):
            if key[0] == vfo_id:
                win = self._decoder_windows.pop(key)
                win.close()
        self._sync_vfo_choices()

    # ------------------------------------------------------------------
    # VFO control handlers
    # ------------------------------------------------------------------

    def _on_vfo_frequency_changed(self, vfo_id: int, freq_hz: float):
        self.dsp.set_vfo_frequency(vfo_id, freq_hz)
        if vfo_id in self._vfo_state:
            self._vfo_state[vfo_id]['freq_hz'] = freq_hz
        bw = self._vfo_state.get(vfo_id, {}).get('bandwidth_hz', 12_500)
        self.vis_panel.update_vfo_marker(vfo_id, freq_hz, bw)
        self._check_vfo_ranges()

    def _on_vfo_bandwidth_changed(self, vfo_id: int, bandwidth_hz: float):
        self.dsp.set_vfo_bandwidth(vfo_id, bandwidth_hz)
        if vfo_id in self._vfo_state:
            self._vfo_state[vfo_id]['bandwidth_hz'] = bandwidth_hz
            freq_hz = self._vfo_state[vfo_id]['freq_hz']
            self.vis_panel.update_vfo_marker(vfo_id, freq_hz, bandwidth_hz)

    def _on_vfo_bandwidth_update(self, vfo_id: int, bandwidth_hz: float):
        """Handle backend-initiated bandwidth change (e.g. TETRA auto-set)."""
        if vfo_id in self._vfo_state:
            self._vfo_state[vfo_id]['bandwidth_hz'] = bandwidth_hz
            freq_hz = self._vfo_state[vfo_id]['freq_hz']
            self.vis_panel.update_vfo_marker(vfo_id, freq_hz, bandwidth_hz)
        self.ctrl_panel.vfo_tab.set_vfo_bandwidth(vfo_id, bandwidth_hz)

    def _on_vfo_renamed(self, vfo_id: int, name: str):
        """Handle user-initiated VFO rename: update visual labels and state."""
        try:
            if vfo_id in self._vfo_state:
                self._vfo_state[vfo_id]['name'] = name
            # Update visualization labels
            self.vis_panel.set_vfo_label(vfo_id, name)
        except Exception:
            pass

    def _on_spectrum_clicked(self, freq_hz: float):
        active_id = self.ctrl_panel.vfo_tab.active_vfo_id()
        if active_id is None:
            return
        self.ctrl_panel.vfo_tab.set_frequency(active_id, freq_hz)
        self.dsp.set_vfo_frequency(active_id, freq_hz)
        if active_id in self._vfo_state:
            self._vfo_state[active_id]['freq_hz'] = freq_hz
        bw = self._vfo_state.get(active_id, {}).get('bandwidth_hz', 12_500)
        self.vis_panel.update_vfo_marker(active_id, freq_hz, bw)
        self._check_vfo_ranges()
        self.ctrl_panel.signal_id_panel.set_frequency(freq_hz)

    def _on_vfo_selected_from_spectrum(self, vfo_id: int):
        self.ctrl_panel.vfo_tab.set_active_vfo(vfo_id)

    def _on_vfo_marker_changed(self, vfo_id: int, freq_hz: float, bw_hz: float):
        """Apply freq/bw from a spectrum marker drag to the correct VFO."""
        self.ctrl_panel.vfo_tab.set_frequency(vfo_id, freq_hz)
        self.dsp.set_vfo_frequency(vfo_id, freq_hz)
        if vfo_id in self._vfo_state:
            self._vfo_state[vfo_id]['freq_hz'] = freq_hz
        current_bw = self._vfo_state.get(vfo_id, {}).get('bandwidth_hz', 12_500)
        if abs(bw_hz - current_bw) > 1.0:
            self.ctrl_panel.vfo_tab.set_vfo_bandwidth(vfo_id, bw_hz)
            self.dsp.set_vfo_bandwidth(vfo_id, bw_hz)
            if vfo_id in self._vfo_state:
                self._vfo_state[vfo_id]['bandwidth_hz'] = bw_hz
        self.vis_panel.waterfall.update_vfo_marker(vfo_id, freq_hz)
        self._check_vfo_ranges()
        self.ctrl_panel.signal_id_panel.set_frequency(freq_hz)

    def _on_vfo_freq_for_signal_id(self, vfo_id: int, freq_hz: float):
        if vfo_id == self.ctrl_panel.vfo_tab.active_vfo_id():
            self.ctrl_panel.signal_id_panel.set_frequency(freq_hz)

    def _on_active_vfo_for_signal_id(self, vfo_id: int):
        freq_hz = self._vfo_state.get(vfo_id, {}).get('freq_hz', 100e6)
        self.ctrl_panel.signal_id_panel.set_frequency(freq_hz)

    def _on_active_vfo_changed(self, vfo_id: int):
        if self.eye_window.isVisible() and self.eye_window._current_vfo is None:
            self.eye_window.set_active_vfo(vfo_id)
        if self.osc_window.isVisible() and self.osc_window._current_vfo is None:
            self.osc_window.set_active_vfo(vfo_id)
        if self.decoder_win.isVisible() and self.decoder_win._current_vfo is None:
            self.decoder_win.set_active_vfo(vfo_id)

    def _on_signal_strength(self, updates: dict):
        for vfo_id, (db, is_active) in updates.items():
            self.ctrl_panel.vfo_tab.update_signal_strength(vfo_id, db, is_active)
            self.rate_window.push_squelch_state(vfo_id, bool(is_active))

    # ------------------------------------------------------------------
    # Device control handlers
    # ------------------------------------------------------------------

    def _on_hackrf_connect(self):
        s = self.ctrl_panel.source_panel.get_settings()
        self.dsp.connect_hackrf(
            center_freq=s['center_freq'],
            sample_rate=s['sample_rate'],
            lna=s['lna'],
            vga=s['vga'],
            amp=s['amp_enabled'],
        )

    def _on_sweep_connect(self):
        s = self.ctrl_panel.source_panel.get_sweep_settings()
        self.dsp.connect_sweep(
            start_freq=s['start_freq'],
            stop_freq=s['stop_freq'],
            sample_rate=s['sample_rate'],
            lna=s['lna'],
            vga=s['vga'],
            amp=s['amp'],
        )

    def _on_center_freq_changed(self, freq_hz: float):
        self._center_hz = freq_hz
        self.dsp.set_center_frequency(freq_hz)
        self.vis_panel.set_freq_range(freq_hz, self._sample_rate)
        self._check_vfo_ranges()

    def _on_sample_rate_changed(self, sample_rate: float):
        self._sample_rate = sample_rate
        self.dsp.set_sample_rate(sample_rate)
        self.vis_panel.set_freq_range(self._center_hz, sample_rate)
        self._check_vfo_ranges()
        try:
            self.timing_window.set_dsp_context(sample_rate=sample_rate)
        except Exception:
            pass
        self.eye_window.set_sample_rate(sample_rate)
        self.osc_window.set_sample_rate(sample_rate)
        self.decoder_win.set_sample_rate(sample_rate)

    # ------------------------------------------------------------------
    # VFO range enforcement
    # ------------------------------------------------------------------

    def _check_vfo_ranges(self):
        half_bw = self._sample_rate / 2
        for vfo_id, state in self._vfo_state.items():
            out_of_range = abs(state['freq_hz'] - self._center_hz) > half_bw
            self.ctrl_panel.vfo_tab.set_vfo_out_of_range(vfo_id, out_of_range)

    # ------------------------------------------------------------------
    # Bookmark handlers
    # ------------------------------------------------------------------

    def _on_bookmark_add_requested(self, settings: dict):
        vfo_id = self.ctrl_panel.vfo_tab.add_vfo()
        self.ctrl_panel.vfo_tab.apply_settings_to_vfo(vfo_id, settings)

    # ------------------------------------------------------------------
    # Decoder handlers
    # ------------------------------------------------------------------

    def _on_decoder_toggled(self, vfo_id: int, decoder_name: str, enabled: bool):
        self.dsp.toggle_decoder(vfo_id, decoder_name, enabled)

    def _on_decoder_data(self, vfo_id: int, decoder_name: str, data: dict):
        key = (vfo_id, decoder_name)
        win = self._decoder_windows.get(key)
        if win is not None and win.isVisible():
            win.push_result(data)

    def _on_open_decoder_window(self, vfo_id: int, decoder_name: str):
        from app.desktop.decoder_windows import create_window, has_window
        if not has_window(decoder_name):
            return
        key = (vfo_id, decoder_name)
        win = self._decoder_windows.get(key)
        if win is None:
            win = create_window(decoder_name, vfo_id)
            if win is None:
                return
            self._decoder_windows[key] = win
        win.show()
        win.raise_()
        win.activateWindow()

    def _on_decoder_result(self, vfo_id: int, decoder_name: str, text: str):
        with self._profiler.measure("UI / decoder result"):
            self.ctrl_panel.vfo_tab.add_decoder_output(vfo_id, decoder_name, text)
            try:
                if self.decoder_panel is not None:
                    vfo_color = VFO_COLORS[vfo_id % len(VFO_COLORS)]
                    vfo_name = self._vfo_state.get(vfo_id, {}).get('name', f'VFO {vfo_id + 1}')
                    self.decoder_panel.append(vfo_name, decoder_name, text, vfo_color)
            except Exception:
                logger.exception("Failed to append to global decoder panel")
            # Write to log file if logging is enabled
            if self._log_decoder_enabled and self._log_decoder_file:
                try:
                    import time
                    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    vfo_name = self._vfo_state[vfo_id]['name']
                    log_line = f"[{ts}] {vfo_name} [{decoder_name}] {text}\n"
                    self._log_decoder_file.write(log_line)
                    self._log_decoder_file.flush()
                except Exception as e:
                    logger.exception(f"Failed to write to decoder log: {e}")

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def _on_device_status(self, status: dict):
        with self._profiler.measure("UI / device status"):
            if 'connected' in status:
                self.ctrl_panel.source_panel.set_connected(status['connected'])

            connected = status.get('connected', True)
            center    = status.get('frequency', self._center_hz)
            sr        = status.get('sample_rate', self._sample_rate)

            if 'frequency' in status:
                self._center_hz = center
            if 'sample_rate' in status:
                self._sample_rate = sr

            if connected:
                self.status_label.setText(
                    f"Connected | {center/1e6:.3f} MHz | {sr/1e6:.1f} MHz SR"
                )
            else:
                self.status_label.setText("Disconnected")

            if 'frequency' in status or 'sample_rate' in status:
                self.vis_panel.set_freq_range(center, sr)
                self._check_vfo_ranges()
                try:
                    self.timing_window.set_dsp_context(sample_rate=sr)
                except Exception:
                    pass
            if 'sample_rate' in status:
                self.eye_window.set_sample_rate(sr)
                self.osc_window.set_sample_rate(sr)
                self.decoder_win.set_sample_rate(sr)

    def _on_error(self, error_msg: str):
        logger.error(f"DSP Error: {error_msg}")
        self.status_label.setText(f"Error: {error_msg}")

    def _on_vfo_paused_changed(self, vfo_id: int, paused: bool) -> None:
        if paused:
            self._paused_vfos.add(vfo_id)
        else:
            self._paused_vfos.discard(vfo_id)

    def _on_eye_samples(self, vfo_id: int, samples):
        if vfo_id in self._paused_vfos:
            return
        self.eye_window.push_samples(vfo_id, samples)
        self.osc_window.push_samples(vfo_id, samples)
        self.decoder_win.push_samples(vfo_id, samples)

    def _on_eye_window_vfo_changed(self, vfo_id: object):
        if vfo_id is None:
            self.dsp.set_eye_stream(False, None)
            return
        self.dsp.set_eye_stream(self.eye_window.isVisible(), int(vfo_id))

    def _on_osc_window_vfo_changed(self, vfo_id: object):
        if vfo_id is None:
            self.dsp.set_eye_stream(False, None)
            return
        self.dsp.set_eye_stream(self.osc_window.isVisible(), int(vfo_id))

    def _on_decoder_win_vfo_changed(self, vfo_id: object):
        if vfo_id is None:
            return
        self.dsp.set_eye_stream(self.decoder_win.isVisible(), int(vfo_id))

    def _on_decoder_win_visibility_changed(self, visible: bool):
        if visible:
            if self.decoder_win._current_vfo is None:
                vfo_id = self.ctrl_panel.vfo_tab.active_vfo_id()
                if vfo_id is not None:
                    self.decoder_win.set_active_vfo(vfo_id)
                    return
            self.dsp.set_eye_stream(True, self.decoder_win._current_vfo)
            return
        # Only stop the stream if no other consumer is visible
        if not self.eye_window.isVisible() and not self.osc_window.isVisible():
            self.dsp.set_eye_stream(False, None)

    def _on_timing_sample_count_changed(self, n: int):
        self._profiler.set_sample_count(n)
        self.vis_panel._profiler.set_sample_count(n)
        self.dsp.set_timing_sample_count(n)

    def _handle_timing_report(self, report: dict):
        try:
            self.timing_window.report_received.emit(report)
        except Exception:
            pass

    def _show_timing_window(self):
        self.timing_window.show()
        self.timing_window.raise_()
        self.timing_window.activateWindow()

    def _show_eye_window(self):
        self._sync_vfo_choices()
        self.eye_window.show()
        self.eye_window.raise_()
        self.eye_window.activateWindow()

    def _show_osc_window(self):
        self._sync_vfo_choices()
        self.osc_window.set_sample_rate(self._sample_rate)
        self.osc_window.show()
        self.osc_window.raise_()
        self.osc_window.activateWindow()

    def _show_rate_window(self):
        self._sync_vfo_choices()
        self.rate_window.show()
        self.rate_window.raise_()
        self.rate_window.activateWindow()

    def _show_decoder_win(self):
        self._sync_vfo_choices()
        self.decoder_win.set_sample_rate(self._sample_rate)
        self.decoder_win.show()
        self.decoder_win.raise_()
        self.decoder_win.activateWindow()

    def _on_eye_window_visibility_changed(self, visible: bool):
        if visible:
            if self.eye_window._current_vfo is None:
                vfo_id = self.ctrl_panel.vfo_tab.active_vfo_id()
                if vfo_id is not None:
                    self.eye_window.set_active_vfo(vfo_id)
                    return
            self.dsp.set_eye_stream(True, self.eye_window._current_vfo)
            return
        if not self.osc_window.isVisible():
            self.dsp.set_eye_stream(False, None)

    def _on_osc_window_visibility_changed(self, visible: bool):
        if visible:
            if self.osc_window._current_vfo is None:
                vfo_id = self.ctrl_panel.vfo_tab.active_vfo_id()
                if vfo_id is not None:
                    self.osc_window.set_active_vfo(vfo_id)
                    return
            self.dsp.set_eye_stream(True, self.osc_window._current_vfo)
            return
        if not self.eye_window.isVisible():
            self.dsp.set_eye_stream(False, None)

    def _sync_vfo_choices(self):
        choices = []
        for vfo_id in sorted(self._vfo_state.keys()):
            name = self._vfo_state.get(vfo_id, {}).get('name', f'VFO {vfo_id + 1}')
            bw = self._vfo_state.get(vfo_id, {}).get('bandwidth_hz', 12_500.0)
            freq = self._vfo_state.get(vfo_id, {}).get('freq_hz', self._center_hz)
            label = f"{name}  |  {freq/1e6:.3f} MHz  |  {bw/1e3:.1f} kHz"
            choices.append((vfo_id, label))
        active = self.ctrl_panel.vfo_tab.active_vfo_id()
        self.eye_window.set_vfo_choices(choices, active_vfo_id=active)
        self.osc_window.set_vfo_choices(choices, active_vfo_id=active)
        self.decoder_win.set_vfo_choices(choices, active_vfo_id=active)
        self.rate_window.set_vfo_choices(choices, active_vfo_id=active)

    # ------------------------------------------------------------------
    # Decoder logging
    # ------------------------------------------------------------------

    def _on_decoder_log_toggled(self, checked: bool):
        if checked:
            # Auto-generate timestamped filename
            import time
            ts = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime())
            fname = f"decoder_log_{ts}.txt"
            try:
                self._log_decoder_file = open(fname, 'w', encoding='utf-8')
                self._log_decoder_file_path = fname
                self._log_decoder_enabled = True
                logger.info(f"Decoder logging started: {fname}")
            except Exception as e:
                logger.error(f"Failed to open decoder log file: {e}")
                self.decoder_panel.log_check.setChecked(False)
                self._log_decoder_enabled = False
        else:
            # Close the file
            if self._log_decoder_file:
                try:
                    self._log_decoder_file.close()
                    logger.info(f"Decoder logging stopped: {self._log_decoder_file_path}")
                except Exception:
                    pass
                self._log_decoder_file = None
            self._log_decoder_enabled = False
            self._log_decoder_file_path = None

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        logger.info("Closing ASDR…")
        # Close decoder log file if open
        if self._log_decoder_file:
            try:
                self._log_decoder_file.close()
            except Exception:
                pass
            self._log_decoder_file = None
        # Save session
        try:
            # only autosave if enabled
            autosave = True
            try:
                autosave = bool(getattr(self, '_autosave_action').isChecked())
            except Exception:
                autosave = True
            if autosave:
                from app import session
                sess = {
                    'device': self.ctrl_panel.source_panel.get_settings(),
                    'vfos': list(self.ctrl_panel.vfo_tab.get_all_vfo_settings().values()),
                }
                session.save_session(sess)
        except Exception:
            pass

        for win in self._decoder_windows.values():
            win.close()
        self._decoder_windows.clear()
        self.eye_window.close()
        self.osc_window.close()
        self.decoder_win.close()
        self.rate_window.close()

        self.ipc.stop()
        self.dsp.stop()
        event.accept()

    # ------------------------------------------------------------------
    # Manual save handler
    # ------------------------------------------------------------------
    def _on_manual_save(self):
        try:
            fname, _ = QFileDialog.getSaveFileName(self, "Save session as...", str(), "JSON Files (*.json);;All Files (*)")
            if not fname:
                return
            from app import session
            sess = {
                'device': self.ctrl_panel.source_panel.get_settings(),
                'vfos': list(self.ctrl_panel.vfo_tab.get_all_vfo_settings().values()),
            }
            session.save_session(sess, fname)
        except Exception:
            logger.exception("Manual save session failed")

    def _apply_session(self, sess: dict) -> None:
        dev = sess.get('device', {})
        if dev:
            self.ctrl_panel.source_panel.apply_settings(dev)
            try:
                if hasattr(self, '_autosave_action') and 'autosave' in dev:
                    self._autosave_action.setChecked(bool(dev.get('autosave', True)))
            except Exception:
                pass
        vfos = sess.get('vfos', [])
        if vfos and isinstance(vfos, list):
            try:
                self.ctrl_panel.vfo_tab.replace_all_vfos(vfos)
            except Exception:
                pass

    def _on_new_session(self):
        try:
            from app import session
            self._apply_session(session.default_session())
        except Exception:
            logger.exception("New session failed")

    def _on_load_session(self):
        try:
            fname, _ = QFileDialog.getOpenFileName(self, "Load session...", str(), "JSON Files (*.json);;All Files (*)")
            if not fname:
                return
            from app import session
            self._apply_session(session.load_session(fname))
        except Exception:
            logger.exception("Load session failed")

    # ------------------------------------------------------------------
    # Stylesheet
    # ------------------------------------------------------------------

    def _apply_stylesheet(self):
        self.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #1a1a1a;
            color: #ffffff;
        }
        QGroupBox {
            color: #00ffff;
            border: 1px solid #404040;
            border-radius: 5px;
            margin-top: 10px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 3px;
        }
        QPushButton {
            background-color: #0066cc;
            color: white;
            border: none;
            padding: 4px 8px;
            border-radius: 3px;
        }
        QPushButton:hover { background-color: #0052a3; }
        QPushButton:pressed { background-color: #003d7a; }
        QPushButton:checked { background-color: #cc4400; }
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
            background-color: #252525;
            color: #ffffff;
        }
        QListWidget {
            background-color: #252525;
            color: #ffffff;
            border: 1px solid #404040;
            border-radius: 3px;
        }
        QTabWidget::pane { border: 1px solid #404040; }
        QTabBar::tab {
            background-color: #252525;
            color: #aaaaaa;
            border: 1px solid #404040;
            padding: 4px 10px;
            border-radius: 3px 3px 0 0;
        }
        QTabBar::tab:selected {
            background-color: #1a1a1a;
            color: #00ffff;
        }
        QSlider::groove:horizontal {
            background-color: #252525;
            border: 1px solid #404040;
            height: 6px;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background-color: #0066cc;
            border: 1px solid #0052a3;
            width: 16px;
            margin: -5px 0;
            border-radius: 8px;
        }
        QStatusBar {
            background-color: #252525;
            color: #ffffff;
            border-top: 1px solid #404040;
        }
        """)
