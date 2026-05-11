"""
SDR signal processing worker thread.

IQ pipeline (one FFT per block, shared between display and all VFOs):

  IQ block (FFT_SIZE samples)
      │
      ├─ np.fft.fftshift(np.fft.fft(block))          ← single FFT
      │       │
      │       ├── display (spectrum + waterfall)       ← rate-limited
      │       │
      │       └── per VFO: extract [lo:hi] bins        ← O(n_bins)
      │                 └── IFFT extracted slice        ← narrowband IQ at ~VFO bandwidth
      │                           └── demodulate + resample → 48 kHz audio

This eliminates the channel LPF (128-tap FIR × full sample rate) and integer
pre-decimation that were the bottleneck at 20 MHz (35% audio production rate).
"""

import queue
import time
import numpy as np
from PySide6.QtCore import QThread, Signal
import logging
from .audio_output import CHUNK

logger = logging.getLogger(__name__)

FFT_SIZE    = 4096   # shared block size for display and audio extraction
DISPLAY_HZ  = 20     # target spectrum/waterfall update rate
MIN_BINS    = 4      # minimum frequency bins to extract per VFO


class SDRWorkerThread(QThread):
    """Background thread for SDR signal processing."""

    spectrum_updated      = Signal(np.ndarray)
    waterfall_updated     = Signal(np.ndarray)
    device_status_changed = Signal(dict)
    error_occurred        = Signal(str)
    audio_ready           = Signal(int, object)       # vfo_id, np.ndarray
    decoder_result        = Signal(int, str, str)     # vfo_id, name, text

    DEMO_SAMPLE_RATE = 96_000
    DEMO_TICK        = 0.05

    def __init__(self, vfo_manager=None, audio_mixer=None, display_buffers=None, parent=None):
        super().__init__(parent)
        self.vfo_manager = vfo_manager
        self.audio_mixer = audio_mixer
        # Optional dict of SharedLatest buffers: {'spectrum': SharedLatest, 'waterfall': SharedLatest}
        self.display_buffers = display_buffers
        self.receiver    = None
        self.running     = False

        self.fft_size   = FFT_SIZE
        self._iq_buf    = np.zeros(FFT_SIZE, dtype=np.complex64)
        self._buf_idx   = 0

        # Set once sample rate is known (run() or _do_sample_rate)
        self._sample_rate   = 20e6
        self._display_skip  = 1    # FFT blocks between display updates
        self._display_tick  = 0
        self._audio_target  = 20   # 48 kHz samples per FFT block

        self._cmd_queue: queue.Queue = queue.Queue()
        # Per-VFO audio accumulation: maps vfo_id -> list[np.ndarray] (pending fragments)
        self._vfo_audio_accum = {}

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self):
        try:
            use_hackrf = self._try_init_hackrf()
            self.running = True

            sr = self.receiver.config.sample_rate if use_hackrf else self.DEMO_SAMPLE_RATE
            self._update_rate_params(sr)

            self.device_status_changed.emit({
                'connected':   True,
                'frequency':   100e6,
                'sample_rate': sr,
            })

            if use_hackrf:
                self._real_hw_loop()
            else:
                logger.info("Running in demo/simulation mode")
                self._demo_loop()

        except Exception as e:
            logger.error(f"SDR worker error: {e}", exc_info=True)
            self.error_occurred.emit(str(e))
        finally:
            self._cleanup()

    def _update_rate_params(self, sr: float):
        """Recompute rate-dependent constants whenever sample rate changes."""
        self._sample_rate  = sr
        blocks_per_sec     = sr / FFT_SIZE
        self._display_skip = max(1, round(blocks_per_sec / DISPLAY_HZ))
        # How many 48 kHz samples each FFT block produces (constant per SR)
        self._audio_target = max(1, round(48_000 * FFT_SIZE / sr))
        logger.info(
            f"SDRWorker rate params: SR={sr/1e6:.3f}MHz  "
            f"bin={sr/FFT_SIZE:.0f}Hz  "
            f"display_skip={self._display_skip}  "
            f"audio_target={self._audio_target} smp/block"
        )

    def _try_init_hackrf(self) -> bool:
        try:
            from app.sdr.hackrf_receiver import HackRFReceiver
        except ImportError:
            logger.warning("HackRF module not available, using demo mode")
            return False

        self.receiver = HackRFReceiver()
        self.receiver.on_iq_data = self._process_iq

        if not self.receiver.connect():
            logger.warning("HackRF connect failed, using demo mode")
            self.receiver = None
            return False

        if self.receiver.device is None:
            logger.info("No real HackRF hardware — using demo loop")
            self.receiver = None
            return False

        self.receiver.start_receiver()
        return True

    # ------------------------------------------------------------------
    # Hardware / demo loops
    # ------------------------------------------------------------------

    def _real_hw_loop(self):
        while self.running:
            self._drain_commands()
            self.msleep(20)

    def _demo_loop(self):
        sr         = self.DEMO_SAMPLE_RATE
        dt         = self.DEMO_TICK
        chunk_size = int(sr * dt)

        if self.vfo_manager:
            self.vfo_manager.update_sample_rate(sr)

        next_tick = time.monotonic()

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

            sleep_ms = max(1, int((next_tick - time.monotonic()) * 1000))
            self.msleep(min(sleep_ms, 20))

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------

    def _post_command(self, fn):
        self._cmd_queue.put_nowait(fn)

    def _drain_commands(self):
        while True:
            try:
                fn = self._cmd_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    logger.error(f"Command error: {e}", exc_info=True)
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # IQ ingestion — accumulate into FFT-sized blocks
    # ------------------------------------------------------------------

    def _process_iq(self, iq_data: np.ndarray):
        if not self.running:
            return
        try:
            pos = 0
            while pos < len(iq_data):
                space    = FFT_SIZE - self._buf_idx
                to_copy  = min(len(iq_data) - pos, space)
                self._iq_buf[self._buf_idx:self._buf_idx + to_copy] = iq_data[pos:pos + to_copy]
                self._buf_idx += to_copy
                pos           += to_copy

                if self._buf_idx >= FFT_SIZE:
                    self._process_block(self._iq_buf)
                    self._buf_idx = 0
        except Exception as e:
            logger.error(f"IQ processing error: {e}")

    # ------------------------------------------------------------------
    # Core per-block processing — one FFT for everything
    # ------------------------------------------------------------------

    def _process_block(self, block: np.ndarray):
        start_t = time.monotonic()
        sr     = self._sample_rate
        N      = FFT_SIZE
        bin_hz = sr / N
        logger.debug(
            "[SDRWorker] block: sr=%s N=%s bin_hz=%.2f audio_target=%s",
            sr, N, bin_hz, self._audio_target,
        )

        # Single FFT — shared between display and VFO audio extraction
        fft_out = np.fft.fftshift(np.fft.fft(block))

        # ---- Display (rate-limited) ----
        self._display_tick += 1
        if self._display_tick >= self._display_skip:
            self._display_tick = 0
            spectrum = (10.0 * np.log10(np.abs(fft_out) ** 2 + 1e-10)).astype(np.float32)
            # Prefer writing to shared buffers if provided to avoid filling the Qt event queue.
            if self.display_buffers:
                try:
                    self.display_buffers.get('spectrum') and self.display_buffers['spectrum'].set(spectrum)
                    self.display_buffers.get('waterfall') and self.display_buffers['waterfall'].set(
                        self._make_waterfall_row(spectrum)
                    )
                except Exception:
                    # Fall back to Qt signals on unexpected error
                    self.spectrum_updated.emit(spectrum)
                    self.waterfall_updated.emit(self._make_waterfall_row(spectrum))
            else:
                self.spectrum_updated.emit(spectrum)
                self.waterfall_updated.emit(self._make_waterfall_row(spectrum))

        # ---- Audio: per-VFO channelizer ----
        if self.vfo_manager is None:
            return

        audio_target = self._audio_target
        center_freq  = self.vfo_manager.center_freq

        for vfo in self.vfo_manager.get_all_vfos():
            iq_nb, nb_sr = self._extract_vfo_iq(fft_out, vfo, sr, N, bin_hz, center_freq)
            if iq_nb is None:
                continue

            logger.debug(
                "[SDRWorker] VFO%s: iq_nb_len=%d nb_sr=%.2f bandwidth=%.2f mode=%s",
                vfo.id, len(iq_nb), nb_sr, vfo.settings.bandwidth, vfo.settings.demod_mode,
            )

            audio, dec_results = vfo.process_narrowband_iq(iq_nb, nb_sr, audio_target)

            if audio is not None:
                logger.debug(
                    "[SDRWorker] VFO%s: demod audio_len=%d dtype=%s",
                    vfo.id, len(audio), audio.dtype,
                )

            if audio is not None and len(audio) > 0:
                self._accumulate_and_push(vfo.id, audio)

            for result in dec_results:
                self.decoder_result.emit(vfo.id, result.decoder_name, str(result.data))

        # Measure total time for this block (FFT + display prep + per-VFO work).
        try:
            elapsed = (time.monotonic() - start_t) * 1000.0
            # if elapsed > 10.0:
                # logger.warning(f"[SDRWorker] slow block processing: {elapsed:.1f} ms")
        except Exception:
            pass

    def _extract_vfo_iq(self, fft_shifted, vfo, sr, N, bin_hz, center_freq):
        """
        Slice VFO bins from the fftshifted output and IFFT → narrowband IQ.

        In the fftshifted array, index N//2 = DC (center_freq).
        Positive offset → higher index; negative offset → lower index.
        """
        if not vfo.settings.enabled:
            return None, None

        offset_hz  = vfo.settings.frequency - center_freq
        n_bins     = max(MIN_BINS, round(vfo.settings.bandwidth / bin_hz))
        center_bin = N // 2 + round(offset_hz / bin_hz)
        lo         = center_bin - n_bins // 2
        hi         = lo + n_bins

        if lo < 0 or hi > N:
            return None, None

        # Extract and IFFT — ifftshift moves the VFO center to DC in the output
        extracted    = fft_shifted[lo:hi]
        narrowband   = np.fft.ifft(np.fft.ifftshift(extracted))
        # Amplitude correction: ifft divides by n_bins, compensate for bin count vs block size
        narrowband   = (narrowband * (N / n_bins)).astype(np.complex64)

        nb_sr = bin_hz * n_bins
        logger.debug(
            "[SDRWorker] VFO%s extract: offset_hz=%.2f n_bins=%d lo=%d hi=%d nb_sr=%.2f",
            vfo.id, offset_hz, n_bins, lo, hi, nb_sr,
        )
        return narrowband, nb_sr

    # ------------------------------------------------------------------
    # Audio push
    # ------------------------------------------------------------------

    _diag_push_count   = 0
    _diag_push_samples = 0
    _diag_push_nans    = 0
    _diag_last_t       = 0.0

    def _push_audio(self, vfo_id: int, audio: np.ndarray):
        self._diag_push_count   += 1
        self._diag_push_samples += len(audio)
        if not np.isfinite(audio).all():
            self._diag_push_nans += 1

        now = time.monotonic()
        if self._diag_last_t == 0.0:
            self._diag_last_t = now
        elif now - self._diag_last_t >= 3.0:
            dt   = now - self._diag_last_t
            rate = self._diag_push_samples / dt
            logger.warning(
                f"[SDRWorker] push_audio: {self._diag_push_count} calls in {dt:.1f}s  "
                f"rate={rate:.0f} smp/s ({rate/48000*100:.0f}% of 48kHz)  "
                f"nan_chunks={self._diag_push_nans}"
            )
            self._diag_push_count = self._diag_push_samples = self._diag_push_nans = 0
            self._diag_last_t = now

        if self.audio_mixer is not None:
            self.audio_mixer.push_audio(vfo_id, audio)
        else:
            self.audio_ready.emit(vfo_id, audio)

    def _accumulate_and_push(self, vfo_id: int, audio: np.ndarray):
        """Accumulate 48 kHz audio fragments and push CHUNK-sized blocks.

        process_narrowband_iq already resamples to audio_target samples at 48 kHz,
        so fragments are already in the correct sample domain — no resampling needed.
        """
        if audio is None or len(audio) == 0:
            return

        if vfo_id not in self._vfo_audio_accum:
            self._vfo_audio_accum[vfo_id] = []

        self._vfo_audio_accum[vfo_id].append(np.asarray(audio, dtype=np.float32))

        total_len = sum(len(f) for f in self._vfo_audio_accum[vfo_id])
        if total_len < CHUNK:
            return

        buf = np.concatenate(self._vfo_audio_accum[vfo_id])

        pos = 0
        while pos + CHUNK <= len(buf):
            self._push_audio(vfo_id, buf[pos:pos + CHUNK])
            pos += CHUNK

        self._vfo_audio_accum[vfo_id] = [buf[pos:]] if pos < len(buf) else []

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _make_waterfall_row(self, spectrum: np.ndarray) -> np.ndarray:
        if len(spectrum) == 0:
            return np.zeros(1, dtype=np.uint8)
        s_min, s_max = spectrum.min(), spectrum.max()
        if s_max > s_min:
            normalized = (spectrum - s_min) / (s_max - s_min)
        else:
            normalized = np.zeros_like(spectrum)
        # Lightweight horizontal smoothing to reduce blocky appearance along x-axis.
        # Use a small triangular kernel (cheap convolution) that preserves edges.
        try:
            kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
            smoothed = np.convolve(normalized, kernel, mode='same')
            smoothed = np.clip(smoothed, 0.0, 1.0)
        except Exception:
            smoothed = normalized
        return (smoothed * 255).astype(np.uint8)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self):
        self.running = False
        self.wait()

    def _cleanup(self):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
            except Exception:
                pass
        self.device_status_changed.emit({'connected': False})

    # ------------------------------------------------------------------
    # Public control methods (thread-safe via command queue)
    # ------------------------------------------------------------------

    def set_center_frequency(self, freq_hz: float):
        if self.vfo_manager:
            self.vfo_manager.update_center_freq(float(freq_hz))
        self._post_command(lambda: self._do_center_freq(float(freq_hz)))
        self.device_status_changed.emit({'frequency': freq_hz})

    def _do_center_freq(self, freq_hz: float):
        if self.receiver:
            try:
                self.receiver.set_center_frequency(freq_hz)
            except Exception as e:
                logger.error(f"Center freq change failed: {e}")
                self.error_occurred.emit(f"Center freq change failed: {e}")

    def set_frequency(self, freq_hz: float):
        self.set_center_frequency(freq_hz)

    def set_sample_rate(self, sample_rate: float):
        if self.vfo_manager:
            self.vfo_manager.sample_rate = float(sample_rate)
        self._post_command(lambda: self._do_sample_rate(float(sample_rate)))
        self.device_status_changed.emit({'sample_rate': sample_rate})

    def _do_sample_rate(self, sample_rate: float):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
                self.receiver.config.sample_rate = sample_rate
                if self.vfo_manager:
                    self.vfo_manager.update_sample_rate(sample_rate)
                if self.receiver.connect():
                    self.receiver.start_receiver()
                else:
                    logger.error("Reconnect failed after sample rate change")
            except Exception as e:
                logger.error(f"Sample rate change failed: {e}")
                self.error_occurred.emit(f"Sample rate change failed: {e}")
        self._update_rate_params(sample_rate)

    def set_lna_gain(self, value: float):
        self._post_command(lambda: self._do_lna_gain(float(value)))

    def _do_lna_gain(self, value: float):
        if self.receiver:
            try:
                self.receiver.set_lna_gain(value)
            except Exception as e:
                logger.error(f"LNA gain change failed: {e}")

    def set_vga_gain(self, value: float):
        self._post_command(lambda: self._do_vga_gain(float(value)))

    def _do_vga_gain(self, value: float):
        if self.receiver:
            try:
                self.receiver.set_vga_gain(value)
            except Exception as e:
                logger.error(f"VGA gain change failed: {e}")

    def set_amp_enable(self, enabled: bool):
        self._post_command(lambda: self._do_amp_enable(bool(enabled)))

    def _do_amp_enable(self, enabled: bool):
        if self.receiver:
            try:
                self.receiver.stop_receiver()
                self.receiver.config.amp_enabled = enabled
                if self.receiver.connect():
                    self.receiver.start_receiver()
                else:
                    logger.error("Reconnect failed after amp enable change")
                    self.error_occurred.emit("Reconnect failed after amp enable change")
            except Exception as e:
                logger.error(f"Amp enable change failed: {e}")
                self.error_occurred.emit(f"Amp enable change failed: {e}")

    def set_vfo_frequency(self, vfo_id: int, freq_hz: float):
        if self.vfo_manager:
            vfo = self.vfo_manager.get_vfo(vfo_id)
            if vfo:
                vfo.set_frequency(float(freq_hz))

    def set_vfo_demod(self, vfo_id: int, mode: str):
        if self.vfo_manager:
            vfo = self.vfo_manager.get_vfo(vfo_id)
            if vfo:
                vfo.set_demod_mode(mode)

    def set_vfo_muted(self, vfo_id: int, muted: bool):
        """Toggle audio production for a specific VFO (mute/unmute).

        This stops the heavy demod/resample work when muted.
        """
        if self.vfo_manager:
            vfo = self.vfo_manager.get_vfo(vfo_id)
            if vfo:
                vfo.set_audio_enabled(not bool(muted))

    def set_vfo_bandwidth(self, vfo_id: int, bandwidth_hz: float):
        if self.vfo_manager:
            vfo = self.vfo_manager.get_vfo(vfo_id)
            if vfo:
                vfo.set_bandwidth(float(bandwidth_hz))

    def set_vfo_squelch(self, vfo_id: int, level_db: float):
        if self.vfo_manager:
            vfo = self.vfo_manager.get_vfo(vfo_id)
            if vfo:
                vfo.set_squelch(level_db)
