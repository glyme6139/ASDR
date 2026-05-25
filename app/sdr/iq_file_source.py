"""
IQ file playback source.

Drop-in replacement for HackRFReceiver: exposes the same on_iq_data callback
and start/stop/pause/resume interface so the DSP worker needs no structural
changes to switch between live capture and recorded playback.

Supported formats
-----------------
.iq  – complex64 binary (memory-mapped for large files)
.raw – int8 interleaved IQIQ… (HackRF native, loaded & converted)
.wav – 16-bit stereo PCM (I=left, Q=right)

A companion <file>.json is read automatically if present and used to restore
center_freq and sample_rate metadata saved by IQRecorder.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

import numpy as np

logger = logging.getLogger(__name__)

_CHUNK = 65536              # samples per on_iq_data callback — matches HackRF transfer size
_POSITION_UPDATE_HZ = 10   # max rate for scrubber / position IPC messages


@dataclass
class IQFileConfig:
    file_path: str
    center_freq: float = 100e6
    sample_rate: float = 20e6
    speed: float = 1.0
    loop: bool = False


class IQFileSource:
    """Reads IQ files and feeds them via on_iq_data at real-time pace.

    The playback thread sleeps between chunks to honour the original sample
    rate scaled by *speed*.  Seeking and pause/resume are thread-safe.
    """

    def __init__(self, config: Optional[IQFileConfig] = None):
        self.config = config or IQFileConfig(file_path='')
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._pause_event = threading.Event()
        self._pause_event.set()   # set = not paused; clear = paused
        self._lock = threading.Lock()
        self._seek_to: Optional[int] = None

        # Callbacks (same names as HackRFReceiver for drop-in compatibility)
        self.on_iq_data: Optional[Callable] = None
        self.on_error: Optional[Callable] = None
        self.on_connected: Optional[Callable] = None
        self.on_position_update: Optional[Callable] = None  # (current, total, sample_rate)

        self._data: Optional[np.ndarray] = None
        self._total: int = 0
        self._pos: int = 0

        # Truthy .device attribute so existing status checks work the same way
        self.device = True

    # ------------------------------------------------------------------
    # HackRFReceiver-compatible lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        path = self.config.file_path
        try:
            self._load_metadata(path)
            self._data = self._load(path)
            self._total = len(self._data)
            self._pos = 0
            logger.info(
                "IQFileSource: loaded %s — %d samples, %.3f MHz center, %.1f MHz SR",
                path, self._total,
                self.config.center_freq / 1e6,
                self.config.sample_rate / 1e6,
            )
            if self.on_connected:
                self.on_connected()
            return True
        except Exception as e:
            logger.error("IQFileSource: cannot load %s: %s", path, e)
            if self.on_error:
                self.on_error(str(e))
            return False

    def start_receiver(self) -> None:
        if self.is_running or self._data is None:
            return
        self.is_running = True
        self._pause_event.set()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='IQFileSource'
        )
        self._thread.start()

    def stop_receiver(self) -> None:
        self.is_running = False
        self._pause_event.set()   # unblock if waiting
        if self._thread:
            self._thread.join(timeout=2.0)
        self._pos = 0

    def pause_streaming(self) -> None:
        self._pause_event.clear()

    def resume_streaming(self) -> None:
        self._pause_event.set()
        if not self.is_running and self._data is not None:
            # File ended naturally — restart from the beginning
            self._pos = 0
            self.is_running = True
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name='IQFileSource'
            )
            self._thread.start()

    def disconnect(self) -> None:
        self.stop_receiver()

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def seek(self, pos_samples: int) -> None:
        with self._lock:
            self._seek_to = max(0, min(pos_samples, max(0, self._total - 1)))

    def set_speed(self, speed: float) -> None:
        self.config.speed = max(0.05, float(speed))

    def set_loop(self, loop: bool) -> None:
        self.config.loop = loop

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def get_duration_seconds(self) -> float:
        if self.config.sample_rate:
            return self._total / self.config.sample_rate
        return 0.0

    def get_device_info(self) -> dict:
        return {
            'name': f'IQ File: {os.path.basename(self.config.file_path)}',
            'serial': '',
            'connected': True,
        }

    def get_stats(self) -> dict:
        return {
            'total_samples': self._total,
            'current_pos': self._pos,
            'sample_rate': self.config.sample_rate,
            'center_freq': self.config.center_freq,
            'is_running': self.is_running,
        }

    # ------------------------------------------------------------------
    # Internal: file loading
    # ------------------------------------------------------------------

    def _load_metadata(self, path: str) -> None:
        meta_path = path + '.json'
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
                self.config.center_freq = float(meta.get('center_freq', self.config.center_freq))
                self.config.sample_rate = float(meta.get('sample_rate', self.config.sample_rate))
            except Exception as e:
                logger.warning("IQFileSource: metadata read error for %s: %s", meta_path, e)

    def _load(self, path: str) -> np.ndarray:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.wav':
            return self._load_wav(path)
        elif ext == '.raw':
            return self._load_raw(path)
        else:
            # Memory-mapped: efficient for large files, no RAM copy needed
            return np.memmap(path, dtype=np.complex64, mode='r')

    def _load_raw(self, path: str) -> np.ndarray:
        raw = np.fromfile(path, dtype=np.int8)
        iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0
        return iq.astype(np.complex64)

    def _load_wav(self, path: str) -> np.ndarray:
        import wave
        with wave.open(path, 'r') as wf:
            sr = wf.getframerate()
            sw = wf.getsampwidth()
            nc = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
        self.config.sample_rate = float(sr)
        dtype = np.int16 if sw == 2 else np.int8
        scale = 32767.0 if sw == 2 else 127.0
        pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
        if nc == 2:
            return (pcm[0::2] + 1j * pcm[1::2]).astype(np.complex64)
        return pcm.astype(np.complex64)

    # ------------------------------------------------------------------
    # Playback thread
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        sr = self.config.sample_rate
        min_pos_interval = 1.0 / _POSITION_UPDATE_HZ
        last_pos_t = 0.0

        while self.is_running:
            # Apply pending seek immediately — works even while paused
            with self._lock:
                if self._seek_to is not None:
                    self._pos = self._seek_to
                    self._seek_to = None
                    last_pos_t = 0.0  # force position update on next pass
                    if self.on_position_update:
                        try:
                            self.on_position_update(self._pos, self._total, sr)
                        except Exception:
                            pass

            # Wait if paused (blocks until event is set)
            if not self._pause_event.wait(timeout=0.1):
                continue
            if not self.is_running:
                break

            if self._pos >= self._total:
                if self.config.loop:
                    self._pos = 0
                    continue
                break

            end = min(self._pos + _CHUNK, self._total)
            # Use a view — _process_iq copies data into its own buffers immediately,
            # so no array allocation is needed here.
            block = self._data[self._pos:end]
            self._pos = end

            if self.on_iq_data:
                try:
                    self.on_iq_data(block)
                except Exception as e:
                    logger.error("IQFileSource: on_iq_data error: %s", e)

            # Throttle position updates to _POSITION_UPDATE_HZ to avoid flooding
            # the IPC result queue (file playback can run at 300+ chunks/sec).
            now = time.monotonic()
            if self.on_position_update and (now - last_pos_t) >= min_pos_interval:
                last_pos_t = now
                try:
                    self.on_position_update(self._pos, self._total, sr)
                except Exception:
                    pass

            # Pace playback to real-time / speed factor
            sleep_s = len(block) / sr / max(0.01, self.config.speed)
            time.sleep(max(0.0, sleep_s))

        self.is_running = False
        # Final position update regardless of throttle
        if self.on_position_update:
            try:
                self.on_position_update(self._pos, self._total, self.config.sample_rate)
            except Exception:
                pass
