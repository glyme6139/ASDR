"""
Main application window for ASDR desktop client.
"""

import logging
from html import escape

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QStatusBar,
    QLabel,
    QTextBrowser,
    QFileDialog,
)
from PySide6.QtCore import Qt

from .visualizations import VisualizationPanel, VFO_COLORS
from .control_panels import ControlPanel
from .dsp_process import DSPProcess
from .ipc_adapter import IPCAdapterThread

logger = logging.getLogger(__name__)

DEFAULT_CENTER_HZ   = 100e6
DEFAULT_SAMPLE_RATE = 20e6


class ASURMainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Advanced SDR (ASDR)")
        self.setGeometry(100, 100, 1400, 900)

        # Shadow state: tracks per-VFO freq/bandwidth in the UI process
        # so spectrum markers can be updated without round-tripping the DSP process.
        self._vfo_state: dict = {}   # {vfo_id: {'freq_hz': float, 'bandwidth_hz': float}}
        # Optional decoder visualization windows: (vfo_id, decoder_name) → window
        self._decoder_windows: dict = {}
        self._center_hz   = DEFAULT_CENTER_HZ
        self._sample_rate = DEFAULT_SAMPLE_RATE

        self.dsp = DSPProcess()
        self.ipc = IPCAdapterThread(self.dsp.result_queue)

        self._initUI()
        self._connect_signals()
        # connect session save/load handlers for manual save and autosave toggles
        try:
            self.ctrl_panel.device_panel.connect_session_signals(self._on_manual_save)
        except Exception:
            pass
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

        self.dsp.start()
        self.ipc.start()

        # Load session if available and apply settings
        try:
            from app import session
            sess = session.load_session()
            # Apply device settings
            dev = sess.get('device', {})
            if dev:
                self.ctrl_panel.device_panel.apply_settings(dev)

            # Apply VFOs
            vfos = sess.get('vfos', [])
            if vfos and isinstance(vfos, list):
                # Apply first VFO to existing first tab, create others
                first = True
                for v in vfos:
                    if first:
                        # apply to the initial tab (tab 0)
                        vid = self.ctrl_panel.vfo_tab.active_vfo_id()
                        if vid is None:
                            vid = 0
                        self.ctrl_panel.vfo_tab.apply_settings_to_vfo(vid, v)
                        # apply decoder settings
                        try:
                            tab = self.ctrl_panel.vfo_tab._tabs.get(vid)
                            if tab:
                                for i in range(tab.decoder_list.count()):
                                    item = tab.decoder_list.item(i)
                                    checked = item.text() in v.get('decoders', [])
                                    item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                                    # if checked:
                                    #     try:
                                    #         self.dsp.toggle_decoder(vid, item.text(), True)
                                    #     except Exception:
                                    #         pass
                        except Exception:
                            pass
                        first = False
                        continue

                    new_id = self.ctrl_panel.vfo_tab.add_vfo()
                    self.ctrl_panel.vfo_tab.apply_settings_to_vfo(new_id, v)
                    try:
                        tab = self.ctrl_panel.vfo_tab._tabs.get(new_id)
                        if tab:
                            for i in range(tab.decoder_list.count()):
                                item = tab.decoder_list.item(i)
                                checked = item.text() in v.get('decoders', [])
                                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                                # if checked:
                                #     try:
                                #         self.dsp.toggle_decoder(new_id, item.text(), True)
                                #     except Exception:
                                #         pass
                    except Exception:
                        pass
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

        self.vis_panel = VisualizationPanel()
        top_layout.addWidget(self.vis_panel, stretch=2)

        self.ctrl_panel = ControlPanel()
        top_layout.addWidget(self.ctrl_panel, stretch=1)

        top_widget.setLayout(top_layout)

        self.decoder_panel = self._create_decoder_panel()

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

    def _create_decoder_panel(self):
        class DecoderAggregator(QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                layout = QVBoxLayout(self)
                layout.setContentsMargins(4, 4, 4, 4)
                layout.setSpacing(4)
                header = QLabel("Decoder Output (aggregated)")
                header.setAlignment(Qt.AlignLeft)
                layout.addWidget(header)
                self.output = QTextBrowser()
                self.output.setReadOnly(True)
                self.output.document().setMaximumBlockCount(1000)
                layout.addWidget(self.output)

            def append(self, vfo_id: int, decoder_name: str, text: str, vfo_color: str):
                import time
                ts        = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                vfo_label = escape(f"VFO{vfo_id}")
                line = (
                    f'<span style="color:#9aa0a6">[{escape(ts)}]</span> '
                    f'<span style="color:{vfo_color}; font-weight:600">{vfo_label}</span> '
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

        # ---- VFO tab lifecycle ----
        self.ctrl_panel.vfo_tab.vfo_added.connect(self._on_vfo_added)
        self.ctrl_panel.vfo_tab.vfo_removed.connect(self._on_vfo_removed)
        self.ctrl_panel.vfo_tab.active_vfo_changed.connect(
            self.vis_panel.set_active_vfo_marker
        )

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

        # ---- Decoder toggles ----
        self.ctrl_panel.vfo_tab.decoder_toggled.connect(self._on_decoder_toggled)

        # ---- Decoder visualization windows ----
        self.ctrl_panel.vfo_tab.open_window_requested.connect(self._on_open_decoder_window)

        # ---- Bookmarks ----
        self.ctrl_panel.bookmark_panel.bookmark_add_requested.connect(
            self._on_bookmark_add_requested
        )

        # ---- Device panel ----
        self.ctrl_panel.device_panel.center_freq_changed.connect(self._on_center_freq_changed)
        self.ctrl_panel.device_panel.sample_rate_changed.connect(self._on_sample_rate_changed)
        self.ctrl_panel.device_panel.lna_gain_changed.connect(
            lambda v: self.dsp.set_lna_gain(float(v))
        )
        self.ctrl_panel.device_panel.vga_gain_changed.connect(
            lambda v: self.dsp.set_vga_gain(float(v))
        )
        self.ctrl_panel.device_panel.amp_enabled_changed.connect(self.dsp.set_amp_enable)

        # ---- Spectrum click → tune active VFO ----
        self.vis_panel.spectrum.frequency_clicked.connect(self._on_spectrum_clicked)

    # ------------------------------------------------------------------
    # VFO lifecycle
    # ------------------------------------------------------------------

    def _on_vfo_added(self, vfo_id: int):
        freq  = self._center_hz
        bw    = 12_500.0
        color = VFO_COLORS[vfo_id % len(VFO_COLORS)]

        self._vfo_state[vfo_id] = {'freq_hz': freq, 'bandwidth_hz': bw}
        self.vis_panel.add_vfo_marker(
            vfo_id, freq_hz=freq, bandwidth_hz=bw, color=color, label=f"VFO {vfo_id + 1}"
        )
        self.ctrl_panel.vfo_tab.set_vfo_color(vfo_id, color)
        self.dsp.add_vfo(vfo_id, freq)
        self._check_vfo_ranges()

    def _on_vfo_removed(self, vfo_id: int):
        self._vfo_state.pop(vfo_id, None)
        self.vis_panel.remove_vfo_marker(vfo_id)
        self.dsp.remove_vfo(vfo_id)
        for key in list(self._decoder_windows.keys()):
            if key[0] == vfo_id:
                win = self._decoder_windows.pop(key)
                win.close()

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

    def _on_signal_strength(self, updates: dict):
        for vfo_id, (db, is_active) in updates.items():
            self.ctrl_panel.vfo_tab.update_signal_strength(vfo_id, db, is_active)

    # ------------------------------------------------------------------
    # Device control handlers
    # ------------------------------------------------------------------

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
        self.ctrl_panel.vfo_tab.add_decoder_output(vfo_id, decoder_name, text)
        try:
            if self.decoder_panel is not None:
                vfo_color = VFO_COLORS[vfo_id % len(VFO_COLORS)]
                self.decoder_panel.append(vfo_id, decoder_name, text, vfo_color)
        except Exception:
            logger.exception("Failed to append to global decoder panel")

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def _on_device_status(self, status: dict):
        if 'connected' in status:
            self.ctrl_panel.device_panel.set_connected(status['connected'])

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

    def _on_error(self, error_msg: str):
        logger.error(f"DSP Error: {error_msg}")
        self.status_label.setText(f"Error: {error_msg}")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        logger.info("Closing ASDR…")
        # Save session
        try:
            # only autosave if enabled
            if self.ctrl_panel.device_panel.get_autosave_enabled():
                from app import session
                sess = {
                    'device': self.ctrl_panel.device_panel.get_settings(),
                    'vfos': list(self.ctrl_panel.vfo_tab.get_all_vfo_settings().values()),
                }
                session.save_session(sess)
        except Exception:
            pass

        for win in self._decoder_windows.values():
            win.close()
        self._decoder_windows.clear()

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
                'device': self.ctrl_panel.device_panel.get_settings(),
                'vfos': list(self.ctrl_panel.vfo_tab.get_all_vfo_settings().values()),
            }
            session.save_session(sess, fname)
        except Exception:
            logger.exception("Manual save session failed")

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
            border: 1px solid #404040;
            border-radius: 3px;
            padding: 3px;
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
