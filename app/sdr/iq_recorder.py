"""
Non-blocking IQ data recorder.

Chunks are queued from the DSP hot-path and written to disk in a dedicated
background thread so recording never stalls signal processing.

Supported formats
-----------------
IQ  – raw complex64 binary (I+jQ as float32 pairs)
RAW – int8 interleaved (HackRF native: IQIQIQ…)
WAV – 16-bit stereo PCM (I = left, Q = right) at the capture sample rate
"""

import json
import logging
import queue
import threading
import time
from typing import Optional, Callable

import numpy as np

logger = logging.getLogger(__name__)

FORMATS = ('IQ', 'RAW', 'WAV')


class IQRecorder:
    """Records IQ chunks to disk in a background write thread.

    Usage::

        rec = IQRecorder()
        rec.on_status = my_callback      # (recording: bool, path: str, bytes: int)
        rec.start('capture.iq', 'IQ', center_freq=100e6, sample_rate=20e6)
        # … call rec.write(chunk) from DSP thread …
        rec.stop()
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._file_path: str = ''
        self._format: str = 'IQ'
        self._center_freq: float = 100e6
        self._sample_rate: float = 20e6
        self._max_duration: float = 0.0   # seconds; 0 = unlimited
        self._start_time: float = 0.0
        self._bytes_written: int = 0

        self.on_status: Optional[Callable[[bool, str, int], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, file_path: str, fmt: str, center_freq: float,
              sample_rate: float, max_duration: float = 0.0) -> None:
        if self._running:
            return
        self._file_path = file_path
        self._format = fmt.upper()
        self._center_freq = center_freq
        self._sample_rate = sample_rate
        self._max_duration = max_duration
        self._bytes_written = 0
        self._start_time = time.monotonic()
        self._running = True
        self._thread = threading.Thread(
            target=self._write_loop, daemon=True, name='IQRecorder'
        )
        self._thread.start()
        self._save_metadata()
        logger.info("IQRecorder: started → %s  format=%s  SR=%.1f MHz",
                    file_path, self._format, sample_rate / 1e6)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("IQRecorder: stopped — %d bytes written", self._bytes_written)

    def write(self, iq_data: np.ndarray) -> None:
        """Queue a chunk for writing. Non-blocking; drops silently when full."""
        if not self._running:
            return
        if self._max_duration > 0 and time.monotonic() - self._start_time >= self._max_duration:
            self.stop()
            return
        try:
            self._queue.put_nowait(iq_data.copy())
        except queue.Full:
            logger.debug("IQRecorder: write queue full — dropping chunk")

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _save_metadata(self) -> None:
        meta = {
            'center_freq': self._center_freq,
            'sample_rate': self._sample_rate,
            'format': self._format,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        try:
            with open(self._file_path + '.json', 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.warning("IQRecorder: metadata write failed: %s", e)

    def _write_loop(self) -> None:
        try:
            if self._format == 'WAV':
                self._write_wav()
            elif self._format == 'RAW':
                self._write_raw()
            else:
                self._write_iq()
        except Exception as e:
            logger.error("IQRecorder: write loop error: %s", e, exc_info=True)
        finally:
            if self.on_status:
                try:
                    self.on_status(False, self._file_path, self._bytes_written)
                except Exception:
                    pass

    def _dequeue_all(self):
        """Yield chunks until sentinel or queue drained after stop."""
        while True:
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if chunk is None:
                break
            yield chunk

    def _write_iq(self) -> None:
        with open(self._file_path, 'wb') as f:
            for chunk in self._dequeue_all():
                data = np.asarray(chunk, dtype=np.complex64)
                data.tofile(f)
                self._bytes_written += data.nbytes
                if self.on_status:
                    self.on_status(True, self._file_path, self._bytes_written)

    def _write_raw(self) -> None:
        with open(self._file_path, 'wb') as f:
            for chunk in self._dequeue_all():
                cf = np.asarray(chunk, dtype=np.complex64)
                raw = np.empty(len(cf) * 2, dtype=np.int8)
                raw[0::2] = np.clip(cf.real * 128, -128, 127).astype(np.int8)
                raw[1::2] = np.clip(cf.imag * 128, -128, 127).astype(np.int8)
                raw.tofile(f)
                self._bytes_written += raw.nbytes
                if self.on_status:
                    self.on_status(True, self._file_path, self._bytes_written)

    def _write_wav(self) -> None:
        import wave
        with wave.open(self._file_path, 'w') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)   # 16-bit
            wf.setframerate(int(self._sample_rate))
            for chunk in self._dequeue_all():
                cf = np.asarray(chunk, dtype=np.complex64)
                pcm = np.empty(len(cf) * 2, dtype=np.int16)
                pcm[0::2] = np.clip(cf.real * 32767, -32768, 32767).astype(np.int16)
                pcm[1::2] = np.clip(cf.imag * 32767, -32768, 32767).astype(np.int16)
                wf.writeframes(pcm.tobytes())
                self._bytes_written += pcm.nbytes
                if self.on_status:
                    self.on_status(True, self._file_path, self._bytes_written)
