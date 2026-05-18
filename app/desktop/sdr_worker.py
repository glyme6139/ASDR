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
MIN_BINS         = 4

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
                 wf_arr: np.ndarray, disp_gen, profiler: TimingProfiler | None = None):
        self._cmd_q    = cmd_q
        self._result_q = result_q
        self._spec_arr = spec_arr   # shared memory float32 view
        self._wf_arr   = wf_arr     # shared memory uint8 view
        self._disp_gen = disp_gen   # mp.Value('L', 0)
        self._profiler = profiler or profiler_from_config(TimingConfig())

        from app.sdr.vfo import VFOManager
        from app.desktop.audio_output import AudioMixer, AUDIO_RATE, CHUNK
        global _AUDIO_RATE, _CHUNK
        _AUDIO_RATE = AUDIO_RATE
        _CHUNK      = CHUNK

        self.vfo_manager = VFOManager(center_freq=100e6, sample_rate=20e6, max_vfos=10)
        self.audio_mixer = AudioMixer()

        self.receiver  = None
        self.running   = False

        self._iq_buf  = np.zeros(FFT_SIZE, dtype=np.complex64)
        self._buf_idx = 0

        self._sample_rate  = 20e6
        self._display_skip = 1
        self._display_tick = 0

        self._vfo_iq_accum: dict = {}
        self._vfo_iq_nb_sr: dict = {}

        self._disp_buf    = np.zeros(DISPLAY_FFT_SIZE, dtype=np.complex64)
        self._disp_write  = 0
        self._disp_window = np.hanning(DISPLAY_FFT_SIZE).astype(np.float32)
        self._wf_floor    = None

        self._last_sig_t  = 0.0

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        try:
            use_hackrf = self._try_init_hackrf()
            self.running = True
            self.audio_mixer.start()

            sr = self.receiver.config.sample_rate if use_hackrf else self.DEMO_SAMPLE_RATE
            self._update_rate_params(sr)

            self._emit({'type': 'device_status',
                        'data': {'connected': True, 'frequency': 100e6, 'sample_rate': sr}})

            if use_hackrf:
                self._real_hw_loop()
            else:
                logger.info("DSPWorker: demo mode")
                self._demo_loop()

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
    # Main loops
    # ------------------------------------------------------------------

    def _real_hw_loop(self):
        while self.running:
            self._drain_commands()
            time.sleep(0.02)

    def _demo_loop(self):
        sr         = self.DEMO_SAMPLE_RATE
        dt         = self.DEMO_TICK
        chunk_size = int(sr * dt)
        self.vfo_manager.update_sample_rate(sr)
        next_tick  = time.monotonic()

        while self.running:
            self._drain_commands()
            now = time.monotonic()
            if now >= next_tick:
                t   = np.arange(chunk_size) / sr
                f1  = sr * 0.05
                f2  = sr * 0.15
                mod = np.sin(2 * np.pi * 300 * t)
                sig = (
                    0.6 * np.exp(2j * np.pi * (f1 * t + 0.3 * mod)) +
                    0.3 * np.exp(2j * np.pi * (f2 * t + 0.2 * np.sin(2 * np.pi * 440 * t)))
                )
                noise = 0.05 * (np.random.randn(chunk_size) + 1j * np.random.randn(chunk_size))
                self._process_iq((sig + noise).astype(np.complex64))
                next_tick += dt
            time.sleep(max(0.001, min(next_tick - time.monotonic(), 0.02)))

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
        elif cmd == 'set_timing_sample_count':
            self._profiler.set_sample_count(msg['n'])

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

    def _do_toggle_decoder(self, vfo_id: int, decoder_name: str, enabled: bool):
        vfo = self.vfo_manager.get_vfo(vfo_id)
        if vfo is None:
            return
        if enabled:
            dec = self._make_decoder(decoder_name)
            if dec:
                vfo.add_decoder(dec)
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
            elif name == 'TETRA':
                from app.decoders.tetra import TETRADecoder
                return TETRADecoder()
            elif name == 'ACARS':
                from app.decoders.acars import ACARSDecoder
                return ACARSDecoder()
            elif name == 'DMR':
                from app.decoders.dmr import DMRDecoder
                return DMRDecoder()
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
            sr     = self._sample_rate
            N      = FFT_SIZE
            bin_hz = sr / N

            with self._profiler.measure("DSP / block fft"):
                fft_out = np.fft.fftshift(np.fft.fft(block))

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
            center_freq = self.vfo_manager.center_freq
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
                            self.audio_mixer.push_audio(vfo_id, _resample_8k_to_48k(tetra_pcm))
                        elif audio is not None and len(audio) > 0:
                            self.audio_mixer.push_audio(vfo_id, audio)

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

            # Signal strength (throttled to ~150 ms)
            now = time.monotonic()
            if now - self._last_sig_t >= 0.15:
                self._last_sig_t = now
                updates = {vfo.id: (vfo.signal_db, vfo.is_active)
                           for vfo in self.vfo_manager.get_all_vfos()}
                if updates:
                    self._emit({'type': 'signal_strength', 'updates': updates})

    def _extract_vfo_iq(self, fft_shifted, vfo, sr, N, bin_hz, center_freq):
        if not vfo.settings.enabled:
            return None, None
        offset_hz  = vfo.settings.frequency - center_freq
        n_bins     = max(MIN_BINS, round(vfo.settings.bandwidth / bin_hz))
        center_bin = N // 2 + round(offset_hz / bin_hz)
        lo         = center_bin - n_bins // 2
        hi         = lo + n_bins
        if lo < 0 or hi > N:
            return None, None
        extracted  = fft_shifted[lo:hi]
        narrowband = np.fft.ifft(np.fft.ifftshift(extracted))
        narrowband = (narrowband * (N / n_bins)).astype(np.complex64)
        return narrowband, bin_hz * n_bins

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
        if self.receiver:
            try: self.receiver.stop_receiver()
            except Exception: pass
        try: self.audio_mixer.stop()
        except Exception: pass
        self._emit({'type': 'device_status', 'data': {'connected': False}})
