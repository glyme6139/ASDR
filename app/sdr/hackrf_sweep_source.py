"""
HackRF frequency sweep source (spectrum display only).

Steps through start_freq → stop_freq in steps of sample_rate, captures a
fixed burst of IQ at each step, runs a per-step FFT, and fires:

    on_sweep_fft(bins_db, center_hz, bandwidth_hz)

The DSP worker composites the per-step results into the shared spectrum
buffer so the full sweep range is visible in the waterfall/spectrum view.

No VFO audio is produced in this mode — the sweep moves too fast to demodulate.
"""

import threading
import logging
import numpy as np
from dataclasses import dataclass
from typing import Optional, Callable, List

logger = logging.getLogger(__name__)

_SAMPLES_PER_STEP = 131072   # one libhackrf USB transfer = ~6.5 ms at 20 MHz
_SKIP_SAMPLES     = 16384    # discard leading samples while PLL settles (~0.8 ms)
_FFT_SIZE         = 65536    # power-of-2 FFT applied to each step


@dataclass
class HackRFSweepConfig:
    start_freq:  float = 80e6
    stop_freq:   float = 108e6
    sample_rate: float = 20e6    # also the step width
    lna_gain:    int   = 24
    vga_gain:    int   = 20
    amp_enabled: bool  = False


class HackRFSweepSource:
    """
    Sweeps HackRF across a frequency range and delivers per-step FFT results.

    Lifecycle (same as HackRFReceiver / IQFileSource):
        connect() → start_receiver() → stop_receiver()
    """

    def __init__(self, config: Optional[HackRFSweepConfig] = None):
        self.config = config or HackRFSweepConfig()
        self.is_running = False
        # Truthy .device so existing status checks work unchanged
        self.device = True
        self._device  = None
        self._thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_sweep_fft: Optional[Callable] = None   # (bins_db, center_hz, bw_hz)
        self.on_error:     Optional[Callable] = None
        self.on_connected: Optional[Callable] = None

        # Per-step sample collection shared between sweep loop and RX callback
        self._step_buf:       Optional[np.ndarray] = None
        self._step_collected: int = 0
        self._step_target:    int = 0
        self._step_done       = threading.Event()
        self._lock            = threading.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _step_freqs(self) -> List[float]:
        """Center frequency of each step across the sweep range."""
        sr  = self.config.sample_rate
        # First step centre = start + half step; last covers stop
        f   = self.config.start_freq + sr / 2
        out = []
        while f < self.config.stop_freq + sr / 2:
            out.append(f)
            f += sr
        return out or [self.config.start_freq + sr / 2]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        try:
            from python_hackrf import pyhackrf
        except ImportError:
            msg = "python_hackrf not installed — cannot use HackRF sweep"
            logger.error(msg)
            if self.on_error:
                self.on_error(msg)
            return False
        try:
            pyhackrf.pyhackrf_init()
            self._device = pyhackrf.pyhackrf_open()

            sr = int(self.config.sample_rate)
            try:
                bw = pyhackrf.pyhackrf_compute_baseband_filter_bw_round_down_lt(sr)
                self._device.pyhackrf_set_baseband_filter_bandwidth(bw)
            except Exception:
                pass

            self._device.pyhackrf_set_sample_rate(sr)
            self._device.pyhackrf_set_lna_gain(int(self.config.lna_gain))
            self._device.pyhackrf_set_vga_gain(int(self.config.vga_gain))
            self._device.pyhackrf_set_antenna_enable(False)
            self._device.pyhackrf_set_amp_enable(self.config.amp_enabled)

            logger.info(
                "HackRFSweepSource connected: %.1f–%.1f MHz, step %.0f MHz, "
                "LNA %d dB, VGA %d dB",
                self.config.start_freq / 1e6,
                self.config.stop_freq  / 1e6,
                self.config.sample_rate / 1e6,
                self.config.lna_gain,
                self.config.vga_gain,
            )
            if self.on_connected:
                self.on_connected()
            return True
        except Exception as e:
            logger.error("HackRFSweepSource.connect failed: %s", e)
            if self.on_error:
                self.on_error(str(e))
            return False

    def start_receiver(self) -> None:
        if self.is_running or self._device is None:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._sweep_loop, daemon=True, name="HackRFSweep"
        )
        self._thread.start()

    def stop_receiver(self) -> None:
        self.is_running = False
        self._step_done.set()   # unblock any waiting capture
        if self._device:
            try:
                self._device.pyhackrf_stop_rx()
            except Exception:
                pass
            try:
                self._device.pyhackrf_close()
            except Exception:
                pass
            self._device = None
        if self._thread:
            self._thread.join(timeout=4.0)

    def disconnect(self) -> None:
        self.stop_receiver()

    # ------------------------------------------------------------------
    # RX callback — called on libhackrf's internal thread
    # ------------------------------------------------------------------

    def _rx_callback(self, device, buffer, buffer_length, valid_length):
        if not self.is_running:
            return 0
        remaining = self._step_target - self._step_collected
        if remaining <= 0:
            return 0

        n   = min(valid_length // 2, remaining)
        raw = np.frombuffer(buffer[:n * 2], dtype=np.int8)
        iq  = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0

        with self._lock:
            end = self._step_collected + n
            self._step_buf[self._step_collected:end] = iq[:n].astype(np.complex64)
            self._step_collected = end
            if end >= self._step_target:
                self._step_done.set()
        return 0

    # ------------------------------------------------------------------
    # Per-step capture
    # ------------------------------------------------------------------

    def _capture_step(self, center_hz: float) -> Optional[np.ndarray]:
        """Tune to center_hz, collect _SAMPLES_PER_STEP IQ samples, return array."""
        n = _SAMPLES_PER_STEP
        self._step_buf       = np.empty(n, dtype=np.complex64)
        self._step_collected = 0
        self._step_target    = n
        self._step_done.clear()

        try:
            self._device.pyhackrf_set_freq(int(center_hz))
            self._device.set_rx_callback(self._rx_callback)
            self._device.pyhackrf_start_rx()

            # Wait up to 4× expected capture time before giving up
            timeout = n / self.config.sample_rate * 4 + 0.2
            self._step_done.wait(timeout=timeout)

            try:
                self._device.pyhackrf_stop_rx()
            except Exception:
                pass

            with self._lock:
                collected = self._step_collected

            if collected < _SKIP_SAMPLES + _FFT_SIZE // 4:
                return None
            return self._step_buf[:collected].copy()

        except Exception as e:
            logger.error("Sweep step failed at %.1f MHz: %s", center_hz / 1e6, e)
            return None

    # ------------------------------------------------------------------
    # Sweep loop
    # ------------------------------------------------------------------

    def _sweep_loop(self) -> None:
        window = np.hanning(_FFT_SIZE).astype(np.float32)
        freqs  = self._step_freqs

        while self.is_running:
            for center_hz in freqs:
                if not self.is_running:
                    break

                iq = self._capture_step(center_hz)
                if iq is None:
                    continue

                # Skip leading samples (PLL settle transient), then FFT
                iq = iq[_SKIP_SAMPLES:]
                if len(iq) >= _FFT_SIZE:
                    block = iq[-_FFT_SIZE:].copy()
                else:
                    block = np.zeros(_FFT_SIZE, dtype=np.complex64)
                    block[-len(iq):] = iq

                fft_out = np.fft.fftshift(np.fft.fft(block * window))
                bins_db = (10.0 * np.log10(np.abs(fft_out) ** 2 + 1e-10)).astype(np.float32)

                if self.on_sweep_fft and self.is_running:
                    try:
                        self.on_sweep_fft(bins_db, center_hz, self.config.sample_rate)
                    except Exception as e:
                        logger.error("on_sweep_fft callback error: %s", e)
