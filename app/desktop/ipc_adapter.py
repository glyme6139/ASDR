"""
IPC adapter: bridges the DSP result queue to Qt signals.

Runs in a dedicated QThread so the main thread is never blocked on Queue.get().
All signals are connected with Qt.QueuedConnection in main_window so slots
always execute on the main (UI) thread.
"""

import queue
import logging
from PySide6.QtCore import QThread, Signal

from .timing import TimingConfig, TimingProfiler, profiler_from_config

logger = logging.getLogger(__name__)


class IPCAdapterThread(QThread):
    """Polls the DSP result queue and re-emits messages as Qt signals."""

    device_status_changed = Signal(object)          # dict — object avoids Shiboken copy-convert warning
    decoder_result        = Signal(int, str, str)   # vfo_id, decoder_name, text
    decoder_data          = Signal(int, str, object) # vfo_id, decoder_name, data-dict
    error_occurred        = Signal(str)
    signal_strength       = Signal(object)          # dict — same reason
    timing_report         = Signal(object)
    vfo_bandwidth_update  = Signal(int, float)      # vfo_id, bandwidth_hz
    eye_samples           = Signal(int, object)     # vfo_id, list[float]

    def __init__(self, result_queue, parent=None, profiler: TimingProfiler | None = None):
        super().__init__(parent)
        self._queue   = result_queue
        self._running = False
        self._profiler = profiler or profiler_from_config(TimingConfig(), prefix="UI")

    def run(self):
        self._running = True
        while self._running:
            try:
                msg = self._queue.get(timeout=0.05)
                with self._profiler.measure("UI / ipc dispatch"):
                    t   = msg.get('type')
                    if   t == 'device_status':
                        self.device_status_changed.emit(msg['data'])
                    elif t == 'decoder_result':
                        self.decoder_result.emit(msg['vfo_id'], msg['name'], msg['text'])
                        if msg.get('data'):
                            self.decoder_data.emit(msg['vfo_id'], msg['name'], msg['data'])
                    elif t == 'error':
                        self.error_occurred.emit(msg['message'])
                    elif t == 'signal_strength':
                        self.signal_strength.emit(msg['updates'])
                    elif t == 'timing_report':
                        self.timing_report.emit(msg['data'])
                    elif t == 'vfo_bandwidth_update':
                        self.vfo_bandwidth_update.emit(msg['vfo_id'], msg['bandwidth_hz'])
                    elif t == 'eye_samples':
                        self.eye_samples.emit(msg['vfo_id'], msg['samples'])
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"IPCAdapterThread error: {e}", exc_info=True)

    def stop(self):
        self._running = False
        self.wait(2000)
