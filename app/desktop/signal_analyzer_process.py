"""
Signal analyzer process manager + Qt IPC bridge.

SignalAnalyzerProcess
    Creates the signal-analyzer subprocess and exposes notify_freq_change()
    so the UI can keep centre_hz / sample_rate current without a round-trip
    through the DSP subprocess.  The subprocess shares the DSP spectrum
    shared-memory block (read-only) so no extra data copies are needed.

SignalAnalyzerIPCThread
    QThread that blocks on the result queue and re-emits scan_result as a
    Qt signal so scan results are delivered on the main thread.
    Mirrors the design of IPCAdapterThread.
"""
from __future__ import annotations

import multiprocessing as mp
import queue
import logging

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

_DISPLAY_FFT_SIZE = 32768   # must match dsp_process.DISPLAY_FFT_SIZE


class SignalAnalyzerProcess:
    """Manages the signal-analyzer subprocess lifecycle."""

    def __init__(
        self,
        spec_shm_name: str,
        disp_gen,                 # mp.Value('L') from DSPProcess
        center_hz:   float = 100e6,
        sample_rate: float =  20e6,
    ):
        self._ctx           = mp.get_context('spawn')
        self._cmd_q         = self._ctx.Queue()
        self._result_q      = self._ctx.Queue()
        # Shared values let the UI update tuning without a subprocess round-trip
        self._center_hz     = self._ctx.Value('d', center_hz)
        self._sample_rate   = self._ctx.Value('d', sample_rate)
        self._spec_shm_name = spec_shm_name
        self._disp_gen      = disp_gen
        self._process: mp.Process | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        from app.sdr.signal_analyzer import _analyzer_worker_main
        self._process = self._ctx.Process(
            target=_analyzer_worker_main,
            args=(
                self._cmd_q,
                self._result_q,
                self._spec_shm_name,
                _DISPLAY_FFT_SIZE,
                self._disp_gen,
                self._center_hz,
                self._sample_rate,
            ),
            daemon=True,
        )
        self._process.start()
        logger.info("Signal analyzer subprocess started (pid=%d)", self._process.pid)

    def stop(self):
        try:
            self._cmd_q.put_nowait({'cmd': 'stop'})
        except Exception:
            pass
        if self._process and self._process.is_alive():
            self._process.join(timeout=2.0)
            if self._process.is_alive():
                logger.warning("Signal analyzer did not exit cleanly — terminating")
                self._process.terminate()
        logger.info("Signal analyzer subprocess stopped")

    # ------------------------------------------------------------------
    # Live configuration (mp.Value writes are GIL-safe)
    # ------------------------------------------------------------------

    def notify_freq_change(self, center_hz: float, sample_rate: float):
        """Inform the analyzer subprocess of a tuning change."""
        self._center_hz.value   = center_hz
        self._sample_rate.value = sample_rate

    @property
    def result_queue(self):
        return self._result_q


class SignalAnalyzerIPCThread(QThread):
    """
    Polls the signal-analyzer result queue and re-emits results as Qt signals.

    scan_result carries a list[dict], each dict having the keys:
        center_hz, bandwidth_hz, peak_db, snr_db, modulation_hint
    """

    scan_result = Signal(object)   # list[dict]

    def __init__(self, result_queue, parent=None):
        super().__init__(parent)
        self._queue   = result_queue
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            try:
                msg = self._queue.get(timeout=0.15)
                if isinstance(msg, dict) and msg.get('type') == 'scan_result':
                    self.scan_result.emit(msg['signals'])
            except queue.Empty:
                pass
            except Exception as exc:
                logger.error("SignalAnalyzerIPCThread error: %s", exc)

    def stop(self):
        self._running = False
        self.wait(2000)
