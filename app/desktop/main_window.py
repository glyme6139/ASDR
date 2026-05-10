"""
Main application window for ASDR desktop client.
"""

import logging

from PySide6.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QStatusBar, QLabel
from PySide6.QtCore import Qt

from .visualizations import VisualizationPanel, VFO_COLORS
from .control_panels import ControlPanel
from .sdr_worker import SDRWorkerThread
from .audio_output import AudioMixer
from app.sdr.vfo import VFOManager

logger = logging.getLogger(__name__)

DEFAULT_CENTER_HZ = 100e6
DEFAULT_SAMPLE_RATE = 20e6


class ASURMainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Advanced SDR (ASDR)")
        self.setGeometry(100, 100, 1400, 900)

        self.vfo_manager = VFOManager(
            center_freq=DEFAULT_CENTER_HZ,
            sample_rate=DEFAULT_SAMPLE_RATE,
            max_vfos=10,
        )
        self.audio_mixer = AudioMixer()
        self.sdr_worker = SDRWorkerThread(
            vfo_manager=self.vfo_manager,
            audio_mixer=self.audio_mixer,
        )

        self._initUI()
        self._connect_signals()
        self._apply_stylesheet()

        # VFOTabPanel creates tab 0 during __init__ before vfo_added is connected,
        # so the signal was missed. Bootstrap any pre-existing tabs now.
        for vfo_id in sorted(self.ctrl_panel.vfo_tab._tabs.keys()):
            self._on_vfo_added(vfo_id)

        self.audio_mixer.start()
        self.sdr_worker.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _initUI(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        self.vis_panel = VisualizationPanel()
        main_layout.addWidget(self.vis_panel, stretch=2)

        self.ctrl_panel = ControlPanel()
        main_layout.addWidget(self.ctrl_panel, stretch=1)

        central.setLayout(main_layout)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Initializing…")
        self.status_bar.addWidget(self.status_label)

        # Set initial frequency range on both spectrum and waterfall
        self.vis_panel.set_freq_range(DEFAULT_CENTER_HZ, DEFAULT_SAMPLE_RATE)

    def _connect_signals(self):
        # ---- SDR worker → UI ----
        # Force queued connections so slots always execute on the main thread,
        # regardless of whether the signal is emitted from the QThread worker,
        # the HackRF callback thread, or any other background thread.
        Q = Qt.QueuedConnection
        self.sdr_worker.spectrum_updated.connect(self._on_spectrum_update, Q)
        self.sdr_worker.waterfall_updated.connect(self._on_waterfall_update, Q)
        self.sdr_worker.device_status_changed.connect(self._on_device_status, Q)
        self.sdr_worker.error_occurred.connect(self._on_error, Q)

        # audio_ready signal not connected: the worker pushes directly to
        # audio_mixer.push_audio() to avoid Qt event-queue latency.

        # ---- SDR worker → decoder output ----
        self.sdr_worker.decoder_result.connect(self._on_decoder_result, Q)

        # ---- VFO tab lifecycle ----
        self.ctrl_panel.vfo_tab.vfo_added.connect(self._on_vfo_added)
        self.ctrl_panel.vfo_tab.vfo_removed.connect(self._on_vfo_removed)
        self.ctrl_panel.vfo_tab.active_vfo_changed.connect(
            self.vis_panel.set_active_vfo_marker
        )

        # ---- VFO settings → backend ----
        self.ctrl_panel.vfo_tab.frequency_changed.connect(self._on_vfo_frequency_changed)
        self.ctrl_panel.vfo_tab.demod_changed.connect(self.sdr_worker.set_vfo_demod)
        self.ctrl_panel.vfo_tab.bandwidth_changed.connect(self._on_vfo_bandwidth_changed)
        self.ctrl_panel.vfo_tab.squelch_changed.connect(self.sdr_worker.set_vfo_squelch)

        # ---- VFO audio controls → mixer ----
        self.ctrl_panel.vfo_tab.volume_changed.connect(
            lambda vid, v: self.audio_mixer.set_volume(vid, v)
        )
        self.ctrl_panel.vfo_tab.mute_changed.connect(
            lambda vid, m: self.audio_mixer.set_muted(vid, m)
        )

        # ---- Decoder toggles ----
        self.ctrl_panel.vfo_tab.decoder_toggled.connect(self._on_decoder_toggled)

        # ---- Device panel ----
        self.ctrl_panel.device_panel.center_freq_changed.connect(self._on_center_freq_changed)
        self.ctrl_panel.device_panel.sample_rate_changed.connect(self._on_sample_rate_changed)
        self.ctrl_panel.device_panel.lna_gain_changed.connect(self._on_lna_gain_changed)
        self.ctrl_panel.device_panel.vga_gain_changed.connect(self._on_vga_gain_changed)
        self.ctrl_panel.device_panel.amp_enabled_changed.connect(self._on_amp_enabled_changed)

        # ---- Spectrum click → tune active VFO ----
        self.vis_panel.spectrum.frequency_clicked.connect(self._on_spectrum_clicked)

    # ------------------------------------------------------------------
    # VFO lifecycle
    # ------------------------------------------------------------------

    def _on_vfo_added(self, vfo_id: int):
        vfo = self.vfo_manager.create_vfo(frequency=self.vfo_manager.center_freq)
        if vfo is None:
            logger.warning("Could not create VFO (limit reached)")
            return

        color = VFO_COLORS[vfo_id % len(VFO_COLORS)]
        self.vis_panel.add_vfo_marker(
            vfo_id,
            freq_hz=vfo.settings.frequency,
            bandwidth_hz=vfo.settings.bandwidth,
            color=color,
            label=f"VFO {vfo_id + 1}",
        )
        self.audio_mixer.add_vfo(vfo_id)
        self._check_vfo_ranges()

    def _on_vfo_removed(self, vfo_id: int):
        self.vfo_manager.delete_vfo(vfo_id)
        self.vis_panel.remove_vfo_marker(vfo_id)
        self.audio_mixer.remove_vfo(vfo_id)

    # ------------------------------------------------------------------
    # VFO control handlers
    # ------------------------------------------------------------------

    def _on_vfo_frequency_changed(self, vfo_id: int, freq_hz: float):
        self.sdr_worker.set_vfo_frequency(vfo_id, freq_hz)
        vfo = self.vfo_manager.get_vfo(vfo_id)
        bw = vfo.settings.bandwidth if vfo else None
        self.vis_panel.update_vfo_marker(vfo_id, freq_hz, bw)
        self._check_vfo_ranges()

    def _on_vfo_bandwidth_changed(self, vfo_id: int, bandwidth_hz: float):
        self.sdr_worker.set_vfo_bandwidth(vfo_id, bandwidth_hz)
        vfo = self.vfo_manager.get_vfo(vfo_id)
        if vfo:
            self.vis_panel.update_vfo_marker(vfo_id, vfo.settings.frequency, bandwidth_hz)

    def _on_spectrum_clicked(self, freq_hz: float):
        """Tune the active VFO to the clicked spectrum frequency."""
        active_id = self.ctrl_panel.vfo_tab.active_vfo_id()
        if active_id is None:
            return
        self.ctrl_panel.vfo_tab.set_frequency(active_id, freq_hz)
        self.sdr_worker.set_vfo_frequency(active_id, freq_hz)
        vfo = self.vfo_manager.get_vfo(active_id)
        bw = vfo.settings.bandwidth if vfo else 12_500
        self.vis_panel.update_vfo_marker(active_id, freq_hz, bw)
        self._check_vfo_ranges()

    # ------------------------------------------------------------------
    # Device control handlers
    # ------------------------------------------------------------------

    def _on_center_freq_changed(self, freq_hz: float):
        self.sdr_worker.set_center_frequency(freq_hz)
        self.vis_panel.set_freq_range(freq_hz, self.vfo_manager.sample_rate)
        self._check_vfo_ranges()

    def _on_sample_rate_changed(self, sample_rate: float):
        self.sdr_worker.set_sample_rate(sample_rate)
        self.vis_panel.set_freq_range(self.vfo_manager.center_freq, sample_rate)
        self._check_vfo_ranges()

    # ------------------------------------------------------------------
    # VFO range enforcement
    # ------------------------------------------------------------------

    def _check_vfo_ranges(self):
        """Disable VFOs whose frequency falls outside center ± sample_rate/2."""
        center = self.vfo_manager.center_freq
        half_bw = self.vfo_manager.sample_rate / 2
        for vfo in self.vfo_manager.get_all_vfos():
            out_of_range = abs(vfo.settings.frequency - center) > half_bw
            vfo.settings.enabled = not out_of_range
            self.ctrl_panel.vfo_tab.set_vfo_out_of_range(vfo.id, out_of_range)

    # ------------------------------------------------------------------
    # Decoder handlers
    # ------------------------------------------------------------------

    def _on_decoder_toggled(self, vfo_id: int, decoder_name: str, enabled: bool):
        vfo = self.vfo_manager.get_vfo(vfo_id)
        if vfo is None:
            return
        if enabled:
            decoder = self._make_decoder(decoder_name)
            if decoder:
                vfo.add_decoder(decoder)
        else:
            vfo.remove_decoder(decoder_name)

    def _make_decoder(self, name: str):
        try:
            if name == 'POCSAG':
                from app.decoders.pocsag import POCSAGDecoder
                return POCSAGDecoder()
            elif name == 'RDS':
                from app.decoders.rds import RDSDecoder
                return RDSDecoder()
            elif name == 'AIS':
                from app.decoders.ais import AISDecoder
                return AISDecoder()
            elif name == 'ADSB':
                from app.decoders.adsb import ADSBDecoder
                return ADSBDecoder()
        except Exception as e:
            logger.error(f"Could not instantiate decoder {name}: {e}")
        return None

    def _on_decoder_result(self, vfo_id: int, decoder_name: str, text: str):
        self.ctrl_panel.vfo_tab.add_decoder_output(vfo_id, decoder_name, text)

    # ------------------------------------------------------------------
    # Spectrum / waterfall
    # ------------------------------------------------------------------

    def _on_spectrum_update(self, spectrum):
        self.vis_panel.update_spectrum(spectrum)

    def _on_waterfall_update(self, row):
        self.vis_panel.update_waterfall(row)

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def _on_device_status(self, status: dict):
        # Only update connection indicator when the key is explicitly present;
        # partial status dicts (e.g. just {'sample_rate': ...}) must not reset it.
        if 'connected' in status:
            self.ctrl_panel.device_panel.set_connected(status['connected'])

        connected = status.get('connected', True)  # assume connected for partial updates
        center = status.get('frequency', self.vfo_manager.center_freq)
        sr = status.get('sample_rate', self.vfo_manager.sample_rate)

        if connected:
            self.status_label.setText(
                f"Connected | {center/1e6:.3f} MHz | {sr/1e6:.1f} MHz SR"
            )
        else:
            self.status_label.setText("Disconnected")

        # Keep visualization in sync whenever freq or sample_rate is reported
        if 'frequency' in status or 'sample_rate' in status:
            self.vis_panel.set_freq_range(center, sr)
            self._check_vfo_ranges()

    def _on_error(self, error_msg: str):
        logger.error(f"SDR Error: {error_msg}")
        self.status_label.setText(f"Error: {error_msg}")

    def _on_lna_gain_changed(self, value: int):
        self.sdr_worker.set_lna_gain(float(value))

    def _on_vga_gain_changed(self, value: int):
        self.sdr_worker.set_vga_gain(float(value))

    def _on_amp_enabled_changed(self, enabled: bool):
        self.sdr_worker.set_amp_enable(enabled)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        logger.info("Closing ASDR…")
        self.sdr_worker.stop()
        self.audio_mixer.stop()
        event.accept()

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
