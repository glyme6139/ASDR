"""
HackRF frequency sweep source — backed by python_hackrf's native pyhackrf_sweep.

pyhackrf_sweep handles all frequency stepping, IQ capture, FFT, and power
conversion internally. This class wraps it in the standard
connect / start_receiver / stop_receiver lifecycle and delivers results via:

    on_sweep_fft(bins_db, start_hz, stop_hz)

Each call covers one step of the sweep; the DSP worker composites them into
the full wideband spectrum display.
"""

import queue
import threading
import logging
import numpy as np
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class HackRFSweepConfig:
    start_freq:  float = 80e6
    stop_freq:   float = 108e6
    sample_rate: int   = 20_000_000   # also the step width; must be even MHz
    lna_gain:    int   = 24           # must be multiple of 8, 0–40 dB
    vga_gain:    int   = 20           # must be multiple of 2, 0–62 dB
    amp_enabled: bool  = False
    bin_width:   int   = 100_000      # FFT resolution in Hz (e.g. 100 kHz)


class HackRFSweepSource:
    """
    Wraps python_hackrf.pyhackrf_tools.pyhackrf_sweep for use as a drop-in
    spectrum source inside the ASDR DSP subprocess.

    Lifecycle (same as HackRFReceiver / IQFileSource):
        connect() → start_receiver() → stop_receiver()
    """

    def __init__(self, config: Optional[HackRFSweepConfig] = None):
        self.config = config or HackRFSweepConfig()
        self.is_running = False
        # Truthy sentinel so existing `if self.receiver.device` checks pass
        self.device  = True
        self._device = None   # set to True in connect() on success

        # Callbacks
        self.on_sweep_fft: Optional[Callable] = None   # (bins_db, start_hz, stop_hz)
        self.on_error:     Optional[Callable] = None
        self.on_connected: Optional[Callable] = None

        self._q: Optional[queue.Queue] = None
        self._sweep_thread:    Optional[threading.Thread] = None
        self._consumer_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Verify the sweep library is importable. Device opens inside start_receiver."""
        try:
            from python_hackrf.pyhackrf_tools import pyhackrf_sweep  # noqa: F401
        except ImportError as e:
            msg = f"python_hackrf sweep tool not available: {e}"
            logger.error(msg)
            if self.on_error:
                self.on_error(msg)
            return False

        self._device = True   # sentinel: library present
        logger.info(
            "HackRFSweepSource ready: %.1f–%.1f MHz, step %d MHz, bin %.0f kHz",
            self.config.start_freq / 1e6,
            self.config.stop_freq  / 1e6,
            self.config.sample_rate // 1_000_000,
            self.config.bin_width   / 1e3,
        )
        if self.on_connected:
            self.on_connected()
        return True

    def start_receiver(self) -> None:
        if self.is_running or self._device is None:
            return
        self.is_running = True
        self._q = queue.Queue(maxsize=128)

        self._sweep_thread = threading.Thread(
            target=self._run_sweep, daemon=True, name="HackRFSweep"
        )
        self._consumer_thread = threading.Thread(
            target=self._consume, daemon=True, name="HackRFSweepConsumer"
        )
        self._sweep_thread.start()
        self._consumer_thread.start()

    def stop_receiver(self) -> None:
        self.is_running = False
        try:
            from python_hackrf.pyhackrf_tools.pyhackrf_sweep import stop_all
            stop_all()
        except Exception:
            pass
        # Unblock the consumer thread
        if self._q is not None:
            try:
                self._q.put_nowait(None)
            except Exception:
                pass
        if self._sweep_thread:
            self._sweep_thread.join(timeout=4.0)
        if self._consumer_thread:
            self._consumer_thread.join(timeout=1.0)
        self._device = None

    def disconnect(self) -> None:
        self.stop_receiver()

    # ------------------------------------------------------------------
    # Sweep thread: runs pyhackrf_sweep (blocking)
    # ------------------------------------------------------------------

    def _run_sweep(self) -> None:
        import sys

        class _SignalWarningFilter:
            """Suppresses the harmless 'signal only works in main thread' stderr line."""
            def __init__(self, wrapped):
                self._w = wrapped
            def write(self, s):
                if 'signal only works in main thread' not in s:
                    self._w.write(s)
            def flush(self):
                self._w.flush()
            def __getattr__(self, name):
                return getattr(self._w, name)

        old_stderr = sys.stderr
        sys.stderr = _SignalWarningFilter(old_stderr)
        try:
            from python_hackrf.pyhackrf_tools.pyhackrf_sweep import pyhackrf_sweep
            pyhackrf_sweep(
                frequencies=[
                    int(self.config.start_freq / 1e6),
                    int(self.config.stop_freq  / 1e6),
                ],
                sample_rate=int(self.config.sample_rate),
                lna_gain=int(self.config.lna_gain),
                vga_gain=int(self.config.vga_gain),
                amp_enable=self.config.amp_enabled,
                bin_width=int(self.config.bin_width),
                queue=self._q,
                print_to_console=False,
            )
        except Exception as e:
            logger.error("HackRFSweepSource._run_sweep: %s", e)
            if self.on_error:
                try:
                    self.on_error(str(e))
                except Exception:
                    pass
        finally:
            sys.stderr = old_stderr
            # Signal consumer to exit
            if self._q is not None:
                try:
                    self._q.put_nowait(None)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Consumer thread: reads queue, fires on_sweep_fft callback
    # ------------------------------------------------------------------

    def _consume(self) -> None:
        while self.is_running:
            try:
                entry = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            if entry is None:
                break

            if not isinstance(entry, dict):
                continue

            if self.on_sweep_fft:
                try:
                    self.on_sweep_fft(
                        np.asarray(entry['dbfs'], dtype=np.float32),
                        float(entry['start_frequency']),
                        float(entry['stop_frequency']),
                    )
                except Exception as e:
                    logger.error("on_sweep_fft callback error: %s", e)
