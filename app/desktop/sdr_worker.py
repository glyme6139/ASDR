"""
DSP worker — runs inside the DSP subprocess (no Qt).

IQ pipeline (one FFT per audio block, separate larger FFT for display):

  IQ stream
      │
      ├─ rolling 32768-sample window  →  Hann-windowed FFT  →  spectrum / waterfall
      │                                  (written to shared memory, 20 Hz)
      │
      └─ 4096-sample blocks  →  per-VFO bin extraction  →  IFFT  →  demod  →  48 kHz audio

Communicates with the UI process via:
  - multiprocessing queues (cmd_q / result_q)
  - shared memory numpy arrays (spectrum_buf, waterfall_buf, disp_gen counter)
"""

import queue as stdlib_queue
import time
import numpy as np
import logging

from .timing import TimingConfig, TimingProfiler, profiler_from_config

logger = logging.getLogger(__name__)

FFT_SIZE         = 4096
DISPLAY_FFT_SIZE = 32768
DISPLAY_HZ       = 20
WATERFALL_HZ     = 100   # waterfall row rate — reuses the per-block 4096-pt FFT
SIG_FAST_HZ      = 50    # signal_strength_fast emit rate for the signal rate window
N_WF_SLOTS       = 16    # must match dsp_process.N_WF_SLOTS
WF_ROW_BINS      = FFT_SIZE
MIN_BINS         = 4

# Minimum intermediate sample rate for the FFT channelizer.
# Ensures enough spectral bins to resolve the channel (e.g. FSK tones) even
# at high HackRF sample rates where bin_hz would otherwise exceed tone spacing.
# Matches vfo.TARGET_PROC_RATE so the channel filter stage sees a consistent rate.
_MIN_INTERMEDIATE_SR = 200_000

# Imported lazily inside DSPWorker to keep this module importable without audio deps
_AUDIO_RATE = 48_000
_CHUNK      = 2048


def _resample_8k_to_48k(pcm_8k: np.ndarray) -> np.ndarray:
    """
    Upsample 8 kHz int16 PCM (TETRA ACELP output) to 48 kHz float32.

    Each TCH/S burst produces 2 × 240 = 480 samples @ 8 kHz → 2 880 @ 48 kHz.
    Linear interpolation keeps the DSP worker free of scipy dependency here.
    """
    pcm_f = pcm_8k.astype(np.float32) * (1.0 / 32768.0)
    n_in  = len(pcm_f)
    n_out = n_in * 6   # 8 000 Hz × 6 = 48 000 Hz
    x_out = np.linspace(0.0, n_in - 1.0, n_out, endpoint=False)
    return np.interp(x_out, np.arange(n_in), pcm_f).astype(np.float32)


class DSPWorker:
    """Pure-Python DSP engine. No Qt — runs in a subprocess."""

    DEMO_SAMPLE_RATE = 96_000
    DEMO_TICK        = 0.05

    def __init__(self, cmd_q, result_q, spec_arr: np.ndarray,
                 wf_arr: np.ndarray, disp_gen,
                 wf_queue: np.ndarray, wf_gen,
                 profiler: TimingProfiler | None = None):
        self._cmd_q    = cmd_q
        self._result_q = result_q
        self._spec_arr = spec_arr   # shared memory float32 view
        self._wf_arr   = wf_arr     # shared memory uint8 view
        self._disp_gen = disp_gen   # mp.Value('L', 0) — spectrum generation
        self._wf_queue = wf_queue   # shared memory (N_WF_SLOTS, WF_ROW_BINS) uint8
        self._wf_gen   = wf_gen     # mp.Value('L', 0) — waterfall row counter
        self._profiler = profiler or profiler_from_config(TimingConfig())

        from app.sdr.vfo import VFOManager
        from app.desktop.audio_output import AudioMixer, AUDIO_RATE, CHUNK
        global _AUDIO_RATE, _CHUNK
        _AUDIO_RATE = AUDIO_RATE
        _CHUNK      = CHUNK

        self.vfo_manager = VFOManager(center_freq=100e6, sample_rate=20e6, max_vfos=10)
        self.audio_mixer = AudioMixer()

        self.receiver     = None
        self.recorder     = None    # IQRecorder instance when recording
        self._source_mode = 'demo'  # 'hackrf' | 'file' | 'sweep' | 'demo'
        self.running      = False

        self._sweep_start = 80e6
        self._sweep_stop  = 108e6

        self._iq_buf  = np.zeros(FFT_SIZE, dtype=np.complex64)
        self._buf_idx = 0

        self._sample_rate  = 20e6
        self._display_skip = 1
        self._display_tick = 0
        self._wf_skip      = 1
        self._wf_tick      = 0
        self._wf4_floor    = None   # separate floor tracker for 4096-pt waterfall rows

        self._vfo_iq_accum: dict = {}
        self._vfo_iq_nb_sr: dict = {}

        self._disp_buf    = np.zeros(DISPLAY_FFT_SIZE, dtype=np.complex64)
        self._disp_write  = 0
        self._disp_window = np.hanning(DISPLAY_FFT_SIZE).astype(np.float32)
        self._wf_floor    = None

        self._last_sig_t      = 0.0
        self._last_sig_fast_t = 0.0
        self._vfo_peak_db: dict = {}   # peak spectral power per VFO since last fast emit
        self._eye_enabled = False
        self._eye_vfo_id = None
        self._last_eye_t = 0.0

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        try:
            use_hackrf = self._try_init_hackrf()
            self.running = True
            self.audio_mixer.start()

            if use_hackrf:
                self._source_mode = 'hackrf'
                sr = self.receiver.config.sample_rate
            else:
                self._source_mode = 'demo'
                sr = self.DEMO_SAMPLE_RATE
                self.vfo_manager.update_sample_rate(sr)

            self._update_rate_params(sr)
            self._emit({'type': 'device_status',
                        'data': {'connected': True, 'frequency': 100e6, 'sample_rate': sr}})
            self._main_loop()

        except Exception as e:
            logger.error(f"DSPWorker error: {e}", exc_info=True)
            self._emit({'type': 'error', 'message': str(e)})
        finally:
            self._cleanup()

    def _emit(self, msg: dict):
        try:
            self._result_q.put_nowait(msg)
        except Exception:
            pass

    def _update_rate_params(self, sr: float):
        self._sample_rate  = sr
        self._display_skip = max(1, round((sr / FFT_SIZE) / DISPLAY_HZ))
        self._wf_skip      = max(1, round((sr / FFT_SIZE) / WATERFALL_HZ))

    def _try_init_hackrf(self) -> bool:
        try:
            from app.sdr.hackrf_receiver import HackRFReceiver
        except ImportError:
            return False
        self.receiver = HackRFReceiver()
        self.receiver.on_iq_data = self._process_iq
        if not self.receiver.connect():
            self.receiver = None
            return False
        if self.receiver.device is None:
            self.receiver = None
            return False
        self.receiver.start_receiver()
        return True

    # ------------------------------------------------------------------
    # Unified main loop
    # ------------------------------------------------------------------

    def _main_loop(self):
        """Single loop handling all source modes.

        HackRF / file: IQ arrives via callbacks — just drain commands and sleep.
        Demo: generate synthetic IQ inline at DEMO_SAMPLE_RATE.
        Source switches (set_source / connect_hackrf / disconnect_hackrf) happen
        inside _drain_commands() and update self._source_mode atomically.
        """
        demo_sr    = self.DEMO_SAMPLE_RATE
        demo_dt    = self.DEMO_TICK
        demo_chunk = int(demo_sr * demo_dt)
        next_tick  = time.monotonic()

        while self.running:
            self._drain_commands()

            if self._source_mode == 'demo':
                now = time.monotonic()
                if now >= next_tick:
                    t   = np.arange(demo_chunk) / demo_sr
                    f1  = demo_sr * 0.05
                    f2  = demo_sr * 0.15
                    mod = np.sin(2 * np.pi * 300 * t)
                    sig = (
                        0.6 * np.exp(2j * np.pi * (f1 * t + 0.3 * mod)) +
                        0.3 * np.exp(2j * np.pi * (f2 * t + 0.2 * np.sin(2 * np.pi * 440 * t)))
                    )
                    noise = 0.05 * (np.random.randn(demo_chunk) + 1j * np.random.randn(demo_chunk))
                    self._process_iq((sig + noise).astype(np.complex64))
                    next_tick += demo_dt
                time.sleep(max(0.001, min(next_tick - time.monotonic(), 0.02)))
            else:
                # hackrf or file: callbacks drive _process_iq
                time.sleep(0.02)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _drain_commands(self):
        while True:
            try:
                self._dispatch(self._cmd_q.get_nowait())
            except stdlib_queue.Empty:
                break
            except Exception as e:
                logger.error(f"Command error: {e}", exc_info=True)

    def _dispatch(self, msg: dict):
        cmd = msg.get('cmd')
        if   cmd == 'stop':
            self.running = False
        elif cmd == 'set_center_frequency':
            self._do_center_freq(msg['freq_hz'])
        elif cmd == 'set_sample_rate':
            self._do_sample_rate(msg['rate'])
        elif cmd == 'set_lna_gain':
            self._do_lna_gain(msg['value'])
        elif cmd == 'set_vga_gain':
            self._do_vga_gain(msg['value'])
        elif cmd == 'set_amp_enable':
            self._do_amp_enable(msg['enabled'])
        elif cmd == 'add_vfo':
            vfo_id = msg.get('vfo_id')
            freq   = msg.get('freq_hz', self.vfo_manager.center_freq)
            self.vfo_manager.create_vfo(frequency=freq, vfo_id=vfo_id)
            self.audio_mixer.add_vfo(vfo_id if vfo_id is not None else 0)
        elif cmd == 'remove_vfo':
            vid = msg['vfo_id']
            self.vfo_manager.delete_vfo(vid)
            self.audio_mixer.remove_vfo(vid)
            self._vfo_iq_accum.pop(vid, None)
            self._vfo_iq_nb_sr.pop(vid, None)
        elif cmd == 'set_vfo_frequency':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo: vfo.set_frequency(float(msg['freq_hz']))
        elif cmd == 'set_vfo_demod':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo: vfo.set_demod_mode(msg['mode'])
        elif cmd == 'set_vfo_bandwidth':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo: vfo.set_bandwidth(float(msg['bandwidth_hz']))
        elif cmd == 'set_vfo_squelch':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo: vfo.set_squelch(msg['level_db'], enabled=vfo.settings.squelch_enabled)
        elif cmd == 'set_vfo_squelch_enabled':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo: vfo.set_squelch_enabled(bool(msg['enabled']))
        elif cmd == 'set_vfo_muted':
            vfo = self.vfo_manager.get_vfo(msg['vfo_id'])
            if vfo:
                vfo.set_audio_enabled(not bool(msg['muted']))
                self.audio_mixer.set_muted(msg['vfo_id'], bool(msg['muted']))
        elif cmd == 'set_volume':
            self.audio_mixer.set_volume(msg['vfo_id'], msg['volume'])
        elif cmd == 'toggle_decoder':
            self._do_toggle_decoder(msg['vfo_id'], msg['decoder_name'], msg['enabled'])
        elif cmd == 'configure_decoder':
            self._do_configure_decoder(msg['vfo_id'], msg['decoder_name'], msg.get('params', {}))
        elif cmd == 'set_timing_sample_count':
            self._profiler.set_sample_count(msg['n'])
        elif cmd == 'set_eye_stream':
            self._eye_enabled = bool(msg.get('enabled', False))
            vfo_id = msg.get('vfo_id')
            self._eye_vfo_id = int(vfo_id) if vfo_id is not None else None
        # ---- source switching ----
        elif cmd == 'set_source':
            self._do_set_source(
                msg.get('source_type', 'demo'),
                msg.get('file_path', ''),
                msg.get('center_freq', 100e6),
                msg.get('sample_rate', 20e6),
            )
        elif cmd == 'connect_hackrf':
            self._do_connect_hackrf(msg)
        elif cmd == 'connect_sweep':
            self._do_connect_sweep(msg)
        elif cmd == 'disconnect_hackrf':
            self._do_disconnect()
        # ---- recording ----
        elif cmd == 'start_recording':
            self._do_start_recording(
                msg['file_path'], msg['format'], msg.get('max_duration', 0.0)
            )
        elif cmd == 'stop_recording':
            self._do_stop_recording()
        # ---- file playback controls ----
        elif cmd == 'playback_pause':
            if self.receiver and hasattr(self.receiver, 'pause_streaming'):
                self.receiver.pause_streaming()
        elif cmd == 'playback_resume':
            if self.receiver and hasattr(self.receiver, 'resume_streaming'):
                self.receiver.resume_streaming()
        elif cmd == 'playback_stop':
            if self.receiver and hasattr(self.receiver, 'stop_receiver'):
                self.receiver.stop_receiver()
                self._source_mode = 'demo'
        elif cmd == 'playback_speed':
            if self.receiver and hasattr(self.receiver, 'set_speed'):
                self.receiver.set_speed(float(msg.get('speed', 1.0)))
        elif cmd == 'playback_seek':
            if self.receiver and hasattr(self.receiver, 'seek'):
                self.receiver.seek(int(msg.get('pos', 0)))
        elif cmd == 'playback_loop':
            if self.receiver and hasattr(self.receiver, 'set_loop'):
                self.receiver.set_loop(bool(msg.get('loop', False)))

    # ------------------------------------------------------------------
    # Hardware commands
    # ------------------------------------------------------------------

    def _do_center_freq(self, freq_hz: float):
        self.vfo_manager.update_center_freq(freq_hz)
        if self.receiver:
            try:
                self.receiver.set_center_frequency(freq_hz)
            except Exception as e:
                logger.error(f"Center freq failed: {e}")
        self._emit({'type': 'device_status', 'data': {'frequency': freq_hz}})

    def _do_sample_rate(self, sr: float):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
                self.receiver.config.sample_rate = sr
                self.vfo_manager.update_sample_rate(sr)
                if self.receiver.connect():
                    self.receiver.start_receiver()
            except Exception as e:
                logger.error(f"Sample rate change failed: {e}")
        self._update_rate_params(sr)
        self._emit({'type': 'device_status', 'data': {'sample_rate': sr}})

    def _do_lna_gain(self, value: float):
        if self.receiver:
            try: self.receiver.set_lna_gain(value)
            except Exception as e: logger.error(f"LNA gain: {e}")

    def _do_vga_gain(self, value: float):
        if self.receiver:
            try: self.receiver.set_vga_gain(value)
            except Exception as e: logger.error(f"VGA gain: {e}")

    def _do_amp_enable(self, enabled: bool):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
                self.receiver.config.amp_enabled = enabled
                if self.receiver.connect():
                    self.receiver.start_receiver()
            except Exception as e:
                logger.error(f"Amp enable: {e}")

    # ------------------------------------------------------------------
    # Source switching helpers
    # ------------------------------------------------------------------

    def _do_set_source(self, source_type: str, file_path: str = '',
                       center_freq: float = 100e6, sample_rate: float = 20e6):
        # Stop current receiver
        if self.receiver:
            try:
                self.receiver.stop_receiver()
            except Exception as e:
                logger.warning("set_source: stop error: %s", e)
        self.receiver = None

        if source_type == 'file' and file_path:
            from app.sdr.iq_file_source import IQFileSource, IQFileConfig
            config = IQFileConfig(file_path=file_path, center_freq=center_freq,
                                  sample_rate=sample_rate)
            src = IQFileSource(config)
            src.on_iq_data         = self._process_iq
            src.on_position_update = self._on_file_position
            if src.connect():
                self.receiver = src
                cf = src.config.center_freq
                sr = src.config.sample_rate
                self.vfo_manager.update_center_freq(cf)
                self.vfo_manager.update_sample_rate(sr)
                self._update_rate_params(sr)
                self._source_mode = 'file'
                src.start_receiver()
                self._emit({'type': 'device_status',
                            'data': {'connected': True, 'frequency': cf, 'sample_rate': sr}})
                self._emit({'type': 'playback_position',
                            'current': 0, 'total': src._total, 'sample_rate': sr})
                return
            self._emit({'type': 'error', 'message': f'Failed to load IQ file: {file_path}'})

        elif source_type == 'hackrf':
            if self._try_init_hackrf():
                self._source_mode = 'hackrf'
                sr = self.receiver.config.sample_rate
                self.vfo_manager.update_sample_rate(sr)
                self._update_rate_params(sr)
                self._emit({'type': 'device_status',
                            'data': {'connected': True,
                                     'frequency': self.receiver.config.center_freq,
                                     'sample_rate': sr}})
                return

        # Fallback: demo mode
        self._to_demo_mode()

    def _do_connect_hackrf(self, msg: dict):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
            except Exception as e:
                logger.warning("connect_hackrf: stop error: %s", e)
        self.receiver = None

        from app.sdr.hackrf_receiver import HackRFReceiver, HackRFConfig
        config = HackRFConfig(
            center_freq=msg.get('center_freq', 100e6),
            sample_rate=msg.get('sample_rate', 20e6),
            lna_gain=int(msg.get('lna', 24)),
            rx_vga_gain=int(msg.get('vga', 20)),
            amp_enabled=bool(msg.get('amp', False)),
        )
        self.receiver = HackRFReceiver(config=config)
        self.receiver.on_iq_data = self._process_iq
        if not self.receiver.connect() or self.receiver.device is None:
            self.receiver = None
            self._to_demo_mode()
            return
        self.receiver.start_receiver()
        self._source_mode = 'hackrf'
        cf = config.center_freq
        sr = config.sample_rate
        self.vfo_manager.update_center_freq(cf)
        self.vfo_manager.update_sample_rate(sr)
        self._update_rate_params(sr)
        self._emit({'type': 'device_status',
                    'data': {'connected': True, 'frequency': cf, 'sample_rate': sr}})

    def _do_connect_sweep(self, msg: dict):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
            except Exception as e:
                logger.warning("connect_sweep: stop error: %s", e)
            # Give the HackRF USB device time to fully close before reopening.
            # Without this, the new pyhackrf_sweep call races the device teardown.
            time.sleep(0.3)
        self.receiver = None

        from app.sdr.hackrf_sweep_source import HackRFSweepSource, HackRFSweepConfig
        cfg = HackRFSweepConfig(
            start_freq  = msg.get('start_freq',  80e6),
            stop_freq   = msg.get('stop_freq',  108e6),
            sample_rate = int(msg.get('sample_rate', 20_000_000)),
            lna_gain    = int(msg.get('lna', 24)),
            vga_gain    = int(msg.get('vga', 20)),
            amp_enabled = bool(msg.get('amp', False)),
            bin_width   = int(msg.get('bin_width', 100_000)),
        )
        src = HackRFSweepSource(cfg)
        src.on_sweep_fft = self._on_sweep_fft

        if not src.connect() or src._device is None:
            self._to_demo_mode()
            return

        self.receiver        = src
        self._sweep_start    = cfg.start_freq
        self._sweep_stop     = cfg.stop_freq
        self._source_mode    = 'sweep'

        center = (cfg.start_freq + cfg.stop_freq) / 2
        bw     = cfg.stop_freq - cfg.start_freq
        self.vfo_manager.update_center_freq(center)
        self.vfo_manager.update_sample_rate(bw)
        self._update_rate_params(bw)

        src.start_receiver()
        self._emit({'type': 'device_status',
                    'data': {'connected': True, 'frequency': center, 'sample_rate': bw}})

    def _on_sweep_fft(self, bins_db: np.ndarray, start_hz: float, stop_hz: float):
        """Compositor: map one step's FFT slice into the shared spec_arr/wf_arr."""
        total_bw = self._sweep_stop - self._sweep_start
        if total_bw <= 0:
            return

        bin_lo = int((start_hz - self._sweep_start) / total_bw * DISPLAY_FFT_SIZE)
        bin_hi = int((stop_hz  - self._sweep_start) / total_bw * DISPLAY_FFT_SIZE)
        bin_lo = max(0, bin_lo)
        bin_hi = min(DISPLAY_FFT_SIZE, bin_hi)
        if bin_lo >= bin_hi:
            return

        n_out = bin_hi - bin_lo
        n_in  = len(bins_db)
        if n_in != n_out:
            x         = np.linspace(0, n_in - 1, n_out)
            resampled = np.interp(x, np.arange(n_in), bins_db).astype(np.float32)
        else:
            resampled = bins_db

        np.copyto(self._spec_arr[bin_lo:bin_hi], resampled)

        # Regenerate waterfall slice using the floor from the current full spectrum
        wf_full = self._make_waterfall_row(self._spec_arr)
        np.copyto(self._wf_arr, wf_full)
        # Decimate 32768→4096 for the waterfall queue (every 8th bin)
        slot = self._wf_gen.value % N_WF_SLOTS
        np.copyto(self._wf_queue[slot], wf_full[::DISPLAY_FFT_SIZE // WF_ROW_BINS])
        self._wf_gen.value  += 1
        self._disp_gen.value += 1

    def _do_disconnect(self):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
            except Exception as e:
                logger.warning("disconnect: stop error: %s", e)
        self.receiver = None
        self._to_demo_mode()

    def _to_demo_mode(self):
        self._source_mode = 'demo'
        self.vfo_manager.update_sample_rate(self.DEMO_SAMPLE_RATE)
        self._update_rate_params(self.DEMO_SAMPLE_RATE)
        self._emit({'type': 'device_status',
                    'data': {'connected': False, 'sample_rate': self.DEMO_SAMPLE_RATE}})

    def _on_file_position(self, current: int, total: int, sample_rate: float):
        self._emit({'type': 'playback_position',
                    'current': current, 'total': total, 'sample_rate': sample_rate})

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def _do_start_recording(self, file_path: str, fmt: str, max_duration: float):
        if self.recorder:
            self.recorder.stop()
        from app.sdr.iq_recorder import IQRecorder
        self.recorder = IQRecorder()
        self.recorder.on_status = self._on_recorder_status
        self.recorder.start(file_path, fmt, self.vfo_manager.center_freq,
                            self._sample_rate, max_duration)

    def _do_stop_recording(self):
        if self.recorder:
            self.recorder.stop()
            self.recorder = None

    def _on_recorder_status(self, recording: bool, file_path: str, bytes_written: int):
        self._emit({'type': 'recording_status',
                    'recording': recording, 'file_path': file_path,
                    'bytes_written': bytes_written})
        if not recording:
            self.recorder = None

    _TETRA_MIN_BW = 50_000    # Hz — TETRA needs ≥2× the 18 kbaud symbol rate
    _DVBT_MIN_BW  = 8_000_000 # Hz — DVB-T 8 MHz channel

    def _do_toggle_decoder(self, vfo_id: int, decoder_name: str, enabled: bool):
        vfo = self.vfo_manager.get_vfo(vfo_id)
        if vfo is None:
            return
        if enabled:
            dec = self._make_decoder(decoder_name)
            if dec:
                vfo.add_decoder(dec)
            if decoder_name == 'TETRA' and vfo.settings.bandwidth < self._TETRA_MIN_BW:
                old_bw = vfo.settings.bandwidth
                vfo.set_bandwidth(self._TETRA_MIN_BW)
                self._emit({
                    'type': 'vfo_bandwidth_update',
                    'vfo_id': vfo_id,
                    'bandwidth_hz': self._TETRA_MIN_BW,
                })
                logger.info(
                    "TETRA: auto-set VFO %d bandwidth to %.0f kHz (was %.1f kHz)",
                    vfo_id, self._TETRA_MIN_BW / 1e3, old_bw / 1e3,
                )
            if decoder_name == 'DVB-T' and vfo.settings.bandwidth < self._DVBT_MIN_BW:
                old_bw = vfo.settings.bandwidth
                vfo.set_bandwidth(self._DVBT_MIN_BW)
                self._emit({
                    'type': 'vfo_bandwidth_update',
                    'vfo_id': vfo_id,
                    'bandwidth_hz': self._DVBT_MIN_BW,
                })
                logger.info(
                    "DVB-T: auto-set VFO %d bandwidth to %.0f MHz (was %.1f kHz)",
                    vfo_id, self._DVBT_MIN_BW / 1e6, old_bw / 1e3,
                )
        else:
            vfo.remove_decoder(decoder_name)

    def _do_configure_decoder(self, vfo_id: int, decoder_name: str, params: dict):
        vfo = self.vfo_manager.get_vfo(vfo_id)
        if vfo is None:
            return
        for dec in vfo.decoders:
            if dec.name == decoder_name and hasattr(dec, 'configure'):
                dec.configure(params)
                break

    def _make_decoder(self, name: str):
        try:
            if name == 'POCSAG':
                from app.decoders.pocsag import POCSAGDecoder
                return POCSAGDecoder(debug=True)
            elif name == 'RDS':
                from app.decoders.rds import RDSDecoder
                return RDSDecoder()
            elif name == 'AIS':
                from app.decoders.ais import AISDecoder
                return AISDecoder()
            elif name == 'ADSB':
                from app.decoders.adsb import ADSBDecoder
                return ADSBDecoder()
            elif name == 'TETRA':
                from app.decoders.tetra import TETRADecoder
                return TETRADecoder()
            elif name == 'ACARS':
                from app.decoders.acars import ACARSDecoder
                return ACARSDecoder()
            elif name == 'DMR':
                from app.decoders.dmr import DMRDecoder
                return DMRDecoder()
            elif name == 'Manchester':
                from app.decoders.manchester import ManchesterDecoder
                return ManchesterDecoder()
            elif name == 'DVB-T':
                from app.decoders.dvbt import DVBTDecoder
                return DVBTDecoder()
            elif name == 'WEFAX':
                from app.decoders.wefax import WEFAXDecoder
                return WEFAXDecoder()
            elif name == 'FSK':
                from app.decoders.fsk import FSKDecoder
                return FSKDecoder()
            else:
                from app.decoders.modulation import create_modulation_decoder
                return create_modulation_decoder(name)
        except Exception as e:
            logger.error(f"Decoder instantiation failed ({name}): {e}")
        return None

    # ------------------------------------------------------------------
    # IQ processing
    # ------------------------------------------------------------------

    def _process_iq(self, iq_data: np.ndarray):
        if not self.running:
            return
        if self.recorder:
            self.recorder.write(iq_data)
        try:
            with self._profiler.measure("DSP / iq callback total"):
                # Fill rolling display buffer (circular)
                n = len(iq_data)
                w = self._disp_write
                if n >= DISPLAY_FFT_SIZE:
                    self._disp_buf[:] = iq_data[-DISPLAY_FFT_SIZE:]
                    self._disp_write  = 0
                else:
                    end = w + n
                    if end <= DISPLAY_FFT_SIZE:
                        self._disp_buf[w:end] = iq_data
                    else:
                        first = DISPLAY_FFT_SIZE - w
                        self._disp_buf[w:]          = iq_data[:first]
                        self._disp_buf[:n - first]  = iq_data[first:]
                    self._disp_write = end % DISPLAY_FFT_SIZE

                # Audio FFT accumulation
                pos = 0
                while pos < len(iq_data):
                    space   = FFT_SIZE - self._buf_idx
                    to_copy = min(len(iq_data) - pos, space)
                    self._iq_buf[self._buf_idx:self._buf_idx + to_copy] = iq_data[pos:pos + to_copy]
                    self._buf_idx += to_copy
                    pos           += to_copy
                    if self._buf_idx >= FFT_SIZE:
                        self._process_block(self._iq_buf)
                        self._buf_idx = 0
        except Exception as e:
            logger.error(f"IQ processing error: {e}")

    def _process_block(self, block: np.ndarray):
        with self._profiler.measure("DSP / block total"):
            sr          = self._sample_rate
            N           = FFT_SIZE
            bin_hz      = sr / N
            center_freq = self.vfo_manager.center_freq

            with self._profiler.measure("DSP / block fft"):
                fft_out = np.fft.fftshift(np.fft.fft(block))

            # High-rate waterfall queue update (100 Hz) — reuses fft_out, no extra FFT
            self._wf_tick += 1
            if self._wf_tick >= self._wf_skip:
                self._wf_tick = 0
                slot = self._wf_gen.value % N_WF_SLOTS
                self._wf_queue[slot] = self._make_wf4_row(fft_out)
                self._wf_gen.value += 1

                # Per-VFO spectral peak — reuses the same power spectrum for free
                power = fft_out.real ** 2 + fft_out.imag ** 2
                for _v in self.vfo_manager.get_all_vfos():
                    _cbin = N // 2 + round((_v.settings.frequency - center_freq) / bin_hz)
                    _hw   = max(1, round(_v.settings.bandwidth / bin_hz / 2))
                    _lo   = max(0, _cbin - _hw)
                    _hi   = min(N, _cbin + _hw)
                    if _lo < _hi:
                        _db = 10.0 * np.log10(float(power[_lo:_hi].mean()) + 1e-10)
                        if _db > self._vfo_peak_db.get(_v.id, -200.0):
                            self._vfo_peak_db[_v.id] = _db

            # High-res display FFT (rate-limited)
            self._display_tick += 1
            if self._display_tick >= self._display_skip:
                with self._profiler.measure("DSP / display fft"):
                    self._display_tick = 0
                    w = self._disp_write
                    if w == 0:
                        ordered = self._disp_buf.copy()
                    else:
                        ordered = np.empty(DISPLAY_FFT_SIZE, dtype=np.complex64)
                        ordered[:DISPLAY_FFT_SIZE - w] = self._disp_buf[w:]
                        ordered[DISPLAY_FFT_SIZE - w:] = self._disp_buf[:w]

                    fft_disp = np.fft.fftshift(np.fft.fft(ordered * self._disp_window))
                    spectrum = (10.0 * np.log10(np.abs(fft_disp) ** 2 + 1e-10)).astype(np.float32)

                    np.copyto(self._spec_arr, spectrum)
                    np.copyto(self._wf_arr,   self._make_waterfall_row(spectrum))
                    self._disp_gen.value += 1

            # Per-VFO audio channelization
            for vfo in self.vfo_manager.get_all_vfos():
                with self._profiler.measure(f"DSP / VFO {vfo.id} channelize"):
                    iq_nb, nb_sr = self._extract_vfo_iq(fft_out, vfo, sr, N, bin_hz, center_freq)
                    if iq_nb is None:
                        continue

                    vfo_id = vfo.id
                    if vfo_id in self._vfo_iq_nb_sr and abs(self._vfo_iq_nb_sr[vfo_id] - nb_sr) > 1.0:
                        self._vfo_iq_accum[vfo_id] = []
                    self._vfo_iq_nb_sr[vfo_id] = nb_sr

                    self._vfo_iq_accum.setdefault(vfo_id, []).append(iq_nb)

                    iq_needed = max(1, int(nb_sr * _CHUNK / _AUDIO_RATE))
                    total_iq  = sum(len(x) for x in self._vfo_iq_accum[vfo_id])
                    if total_iq < iq_needed:
                        continue

                    buf   = np.concatenate(self._vfo_iq_accum[vfo_id])
                    total = len(buf)
                    pos   = 0
                    while pos + iq_needed <= total:
                        with self._profiler.measure(f"DSP / VFO {vfo_id} process"):
                            audio, dec_results = vfo.process_narrowband_iq(
                                buf[pos:pos + iq_needed], nb_sr, _CHUNK, profiler=self._profiler
                            )

                        # Check whether any decoder produced PCM audio (e.g. TETRA voice).
                        # If so, use it instead of the VFO's FM-demodulated audio.
                        tetra_pcm = None
                        for result in dec_results:
                            if result.decoder_name == 'TETRA' and isinstance(result.data, dict):
                                pcm = result.data.pop('pcm', None)   # remove before IPC
                                if pcm is not None and len(pcm) > 0:
                                    tetra_pcm = pcm

                        if tetra_pcm is not None:
                            out_audio = _resample_8k_to_48k(tetra_pcm)
                            with self._profiler.measure(f"DSP / VFO {vfo_id} audio mix"):
                                self.audio_mixer.push_audio(vfo_id, out_audio)
                            self._emit_eye_samples(vfo_id, out_audio)
                        elif audio is not None and len(audio) > 0:
                            if np.iscomplexobj(audio):
                                self._emit_iq_samples(vfo_id, audio)
                            else:
                                with self._profiler.measure(f"DSP / VFO {vfo_id} audio mix"):
                                    self.audio_mixer.push_audio(vfo_id, audio)
                                self._emit_eye_samples(vfo_id, audio)

                        for result in dec_results:
                            formatted = str(result.data)
                            for dec in vfo.decoders:
                                if dec.name == result.decoder_name:
                                    try:
                                        formatted = dec.format_result(result)
                                    except Exception:
                                        pass
                                    break
                            raw_data = result.data if isinstance(result.data, dict) else {}
                            self._emit({'type': 'decoder_result', 'vfo_id': vfo_id,
                                        'name': result.decoder_name, 'text': formatted,
                                        'data': raw_data})
                        pos += iq_needed
                    self._vfo_iq_accum[vfo_id] = [buf[pos:]] if pos < total else []

            # Signal strength — two paths with different rates and consumers
            now = time.monotonic()
            # Fast path (50 Hz): spectral peak per VFO → signal rate window
            if now - self._last_sig_fast_t >= (1.0 / SIG_FAST_HZ):
                self._last_sig_fast_t = now
                if self._vfo_peak_db:
                    fast_upd = dict(self._vfo_peak_db)
                    self._vfo_peak_db.clear()
                    self._emit({'type': 'signal_strength_fast', 'updates': fast_upd})
            # Slow path (~150 ms): vfo.signal_db → squelch panels
            if now - self._last_sig_t >= 0.15:
                self._last_sig_t = now
                updates = {vfo.id: (vfo.signal_db, vfo.is_active, vfo.snr, vfo.sinad)
                           for vfo in self.vfo_manager.get_all_vfos()}
                if updates:
                    self._emit({'type': 'signal_strength', 'updates': updates})

    def _emit_eye_samples(self, vfo_id: int, audio: np.ndarray):
        if not self._eye_enabled:
            return
        if self._eye_vfo_id is not None and int(vfo_id) != int(self._eye_vfo_id):
            return

        now = time.monotonic()
        if now - self._last_eye_t < 0.08:
            return
        self._last_eye_t = now

        if audio is None or len(audio) == 0:
            return
        arr = np.asarray(audio, dtype=np.float32)
        target_n = 512
        if arr.size > target_n:
            idx = np.linspace(0, arr.size - 1, target_n, dtype=np.int32)
            arr = arr[idx]

        self._emit({'type': 'eye_samples', 'vfo_id': int(vfo_id), 'samples': arr.tolist()})

    def _emit_iq_samples(self, vfo_id: int, iq: np.ndarray):
        if not self._eye_enabled:
            return
        if self._eye_vfo_id is not None and int(vfo_id) != int(self._eye_vfo_id):
            return

        now = time.monotonic()
        if now - self._last_eye_t < 0.08:
            return
        self._last_eye_t = now

        if iq is None or len(iq) == 0:
            return
        arr = np.asarray(iq, dtype=np.complex64)
        target_n = 512
        if arr.size > target_n:
            idx = np.linspace(0, arr.size - 1, target_n, dtype=np.int32)
            arr = arr[idx]

        self._emit({'type': 'iq_samples', 'vfo_id': int(vfo_id), 'samples': arr.tolist()})

    def _extract_vfo_iq(self, fft_shifted, vfo, sr, N, bin_hz, center_freq):
        if not vfo.settings.enabled:
            return None, None
        offset_hz  = vfo.settings.frequency - center_freq
        bw_bins    = max(MIN_BINS, round(vfo.settings.bandwidth / bin_hz))
        # Use at least _MIN_INTERMEDIATE_SR worth of bins so the FFT has enough
        # spectral resolution to resolve in-channel features (e.g. FSK tones).
        # The VFO channel filter will decimate down to actual bandwidth afterwards.
        inter_bins = max(bw_bins, round(_MIN_INTERMEDIATE_SR / bin_hz))
        inter_bins = min(inter_bins, N // 2)  # can't exceed half the FFT
        center_bin = N // 2 + round(offset_hz / bin_hz)
        lo         = center_bin - inter_bins // 2
        hi         = lo + inter_bins
        pad_lo = max(0, -lo)
        pad_hi = max(0, hi - N)
        if pad_lo + pad_hi >= inter_bins:
            return None, None   # VFO entirely outside captured band
        if pad_lo > 0 or pad_hi > 0:
            # Zero-pad at the edge: out-of-band bins are naturally rolled off by
            # the hardware anti-aliasing filter, so zeros are correct there.
            extracted = np.zeros(inter_bins, dtype=fft_shifted.dtype)
            extracted[pad_lo : inter_bins - pad_hi if pad_hi else inter_bins] = \
                fft_shifted[lo + pad_lo : hi - pad_hi if pad_hi else hi]
        else:
            extracted  = fft_shifted[lo:hi]
        narrowband = np.fft.ifft(np.fft.ifftshift(extracted))
        narrowband = (narrowband * (N / inter_bins)).astype(np.complex64)
        return narrowband, bin_hz * inter_bins

    def _make_wf4_row(self, fft_shifted: np.ndarray) -> np.ndarray:
        """Build a WF_ROW_BINS-element uint8 waterfall row from the 4096-pt FFT.
        Uses a separate floor tracker so it doesn't interfere with the display FFT path.
        """
        power = fft_shifted.real ** 2 + fft_shifted.imag ** 2
        spec  = (10.0 * np.log10(power.astype(np.float32) + 1e-10))
        floor = float(np.percentile(spec, 15))
        if self._wf4_floor is None:
            self._wf4_floor = floor
        else:
            # IIR matched to display path: same ~1 s time constant at WATERFALL_HZ
            self._wf4_floor += (DISPLAY_HZ / WATERFALL_HZ) * 0.05 * (floor - self._wf4_floor)
        return (np.clip((spec - self._wf4_floor) / 70.0, 0.0, 1.0) * 255).astype(np.uint8)

    def _make_waterfall_row(self, spectrum: np.ndarray) -> np.ndarray:
        if len(spectrum) == 0:
            return np.zeros(DISPLAY_FFT_SIZE, dtype=np.uint8)
        floor = float(np.percentile(spectrum, 15))
        if self._wf_floor is None:
            self._wf_floor = floor
        else:
            self._wf_floor += 0.05 * (floor - self._wf_floor)
        return (np.clip((spectrum - self._wf_floor) / 70.0, 0.0, 1.0) * 255).astype(np.uint8)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self):
        if self.recorder:
            try: self.recorder.stop()
            except Exception: pass
        if self.receiver:
            try: self.receiver.stop_receiver()
            except Exception: pass
        try: self.audio_mixer.stop()
        except Exception: pass
        self._emit({'type': 'device_status', 'data': {'connected': False}})
