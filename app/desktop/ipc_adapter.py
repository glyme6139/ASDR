"""
IPC adapter: bridges the DSP result queue to Qt signals.

Runs in a dedicated QThread so the main thread is never blocked on Queue.get().
All signals are connected with Qt.QueuedConnection in main_window so slots
always execute on the main (UI) thread.
"""

import queue
import logging
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class IPCAdapterThread(QThread):
    """Polls the DSP result queue and re-emits messages as Qt signals."""

    device_status_changed = Signal(object)          # dict — object avoids Shiboken copy-convert warning
    decoder_result        = Signal(int, str, str)   # vfo_id, decoder_name, text
    error_occurred        = Signal(str)
    signal_strength       = Signal(object)          # dict — same reason

    def __init__(self, result_queue, parent=None):
        super().__init__(parent)
        self._queue   = result_queue
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            try:
                msg = self._queue.get(timeout=0.05)
                t   = msg.get('type')
                if   t == 'device_status':
                    self.device_status_changed.emit(msg['data'])
                elif t == 'decoder_result':
                    self.decoder_result.emit(msg['vfo_id'], msg['name'], msg['text'])
                elif t == 'error':
                    self.error_occurred.emit(msg['message'])
                elif t == 'signal_strength':
                    self.signal_strength.emit(msg['updates'])
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"IPCAdapterThread error: {e}", exc_info=True)

    def stop(self):
        self._running = False
        self.wait(2000)
