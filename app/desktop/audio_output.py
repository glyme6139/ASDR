"""
Audio mixer: combines per-VFO audio streams and plays through the default output device.

Uses a per-VFO ring buffer (pre-filled with silence) driven by a sounddevice callback,
mirroring the approach in teststream.py. The callback always runs at the hardware clock
rate; the ring buffer decouples DSP production from audio consumption so brief SDR
processing hiccups don't cause gaps.
"""

import threading
import time
import numpy as np
import logging
from typing import Dict, Set

logger = logging.getLogger(__name__)

AUDIO_RATE   = 48_000
CHUNK        = 2048               # callback block size (≈42.7 ms)
RING_SIZE    = AUDIO_RATE * 2     # 2 s of headroom per VFO
PREFILL      = AUDIO_RATE // 10   # 100 ms silence pre-fill avoids startup underrun
STATS_PERIOD = 3.0                # seconds between diagnostic log lines


class _RingBuffer:
    """Thread-safe ring buffer for float32 audio (single-producer, single-consumer)."""

    def __init__(self, capacity: int):
        self._buf      = np.zeros(capacity, dtype=np.float32)
        self._cap      = capacity
        self._head     = 0   # write position
        self._tail     = 0   # read position
        self._lock     = threading.Lock()
        self.underruns = 0   # callbacks that returned zeros due to empty buffer

    @property
    def fill(self) -> int:
        with self._lock:
            return (self._head - self._tail) % self._cap

    def write(self, data: np.ndarray):
        n = len(data)
        with self._lock:
            avail = (self._head - self._tail) % self._cap
            space = self._cap - avail - 1
            if n > space:
                # Drop oldest samples to keep latency bounded
                self._tail = (self._tail + (n - space)) % self._cap
            idx = np.arange(self._head, self._head + n) % self._cap
            self._buf[idx] = data
            self._head = (self._head + n) % self._cap

    def read(self, n: int) -> np.ndarray:
        with self._lock:
            avail = (self._head - self._tail) % self._cap
            take  = min(n, avail)
            if take == 0:
                self.underruns += 1
                return np.zeros(n, dtype=np.float32)
            idx = np.arange(self._tail, self._tail + take) % self._cap
            out = self._buf[idx].copy()
            self._tail = (self._tail + take) % self._cap
        if take < n:
            self.underruns += 1
            out = np.concatenate([out, np.zeros(n - take, dtype=np.float32)])
        return out


class AudioMixer:
    """
    Receives audio chunks from multiple VFOs via push_audio(), mixes them,
    and outputs through the system default audio device using sounddevice.

    Thread-safety: push_audio() is called from the SDR worker thread;
    _callback() runs in the sounddevice audio thread. Each VFO has its own
    ring buffer — push and read for a given VFO never race.
    """

    def __init__(self):
        self._rings:   Dict[int, _RingBuffer] = {}
        self._volumes: Dict[int, float]       = {}
        self._muted:   Set[int]               = set()
        self._stream   = None
        self._lock     = threading.Lock()
        self._started  = False
        # Diagnostics
        self._cb_count      = 0
        self._last_stats_t  = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(
                samplerate=AUDIO_RATE,
                channels=1,
                dtype='float32',
                blocksize=CHUNK,
                latency='low',
                callback=self._callback,
            )
            self._stream.start()
            self._started = True
            logger.info("AudioMixer started (ring-buffer callback mode)")
            return True
        except Exception as e:
            logger.error(f"AudioMixer start failed: {e}")
            return False

    def stop(self):
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"AudioMixer stop error: {e}")
            self._stream = None
        self._started = False
        logger.info("AudioMixer stopped")

    # ------------------------------------------------------------------
    # VFO management
    # ------------------------------------------------------------------

    def add_vfo(self, vfo_id: int, volume: float = 1.0):
        ring = _RingBuffer(RING_SIZE)
        ring.write(np.zeros(PREFILL, dtype=np.float32))   # pre-fill with silence
        with self._lock:
            self._rings[vfo_id]   = ring
            self._volumes[vfo_id] = float(volume)
        logger.debug(f"AudioMixer: added VFO {vfo_id}")

    def remove_vfo(self, vfo_id: int):
        with self._lock:
            self._rings.pop(vfo_id, None)
            self._volumes.pop(vfo_id, None)
            self._muted.discard(vfo_id)
        logger.debug(f"AudioMixer: removed VFO {vfo_id}")

    def set_volume(self, vfo_id: int, volume: float):
        with self._lock:
            self._volumes[vfo_id] = max(0.0, min(1.0, float(volume)))

    def set_muted(self, vfo_id: int, muted: bool):
        with self._lock:
            if muted:
                self._muted.add(vfo_id)
            else:
                self._muted.discard(vfo_id)

    # ------------------------------------------------------------------
    # Audio push (called from SDR worker thread)
    # ------------------------------------------------------------------

    def push_audio(self, vfo_id: int, audio: np.ndarray):
        ring = self._rings.get(vfo_id)
        if ring is not None:
            ring.write(audio.astype(np.float32))

    # ------------------------------------------------------------------
    # sounddevice callback (audio thread — paced by hardware clock)
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int, _time, status):
        if status:
            logger.warning(f"AudioMixer callback status: {status}")

        mixed = np.zeros(frames, dtype=np.float32)

        with self._lock:
            vfo_ids = list(self._rings.keys())
            volumes = dict(self._volumes)
            muted   = set(self._muted)

        for vfo_id in vfo_ids:
            if vfo_id in muted:
                continue
            ring = self._rings.get(vfo_id)
            if ring is None:
                continue
            chunk  = ring.read(frames)
            mixed += chunk * volumes.get(vfo_id, 1.0)

        np.clip(mixed, -1.0, 1.0, out=mixed)
        outdata[:, 0] = mixed

        self._cb_count += 1
        now = time.monotonic()
        if now - self._last_stats_t >= STATS_PERIOD:
            self._last_stats_t = now
            parts = []
            for vfo_id in vfo_ids:
                ring = self._rings.get(vfo_id)
                if ring is None:
                    continue
                fill_ms   = ring.fill / AUDIO_RATE * 1000
                underruns = ring.underruns
                parts.append(f"VFO{vfo_id}: fill={fill_ms:.0f}ms underruns={underruns}")
            logger.warning(
                f"[AudioMixer] callbacks={self._cb_count}  " + ("  ".join(parts) or "(no VFOs)")
            )
