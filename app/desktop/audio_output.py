"""
Audio mixer: combines per-VFO audio streams and plays through the default output device.

Uses a per-VFO ring buffer (pre-filled with silence) driven by a sounddevice callback.
Two-slice contiguous copy avoids per-call index array allocation in the ring buffer.
Callback state is cached as an immutable tuple and rebuilt only on VFO config changes,
eliminating all allocations inside the hot audio callback path.
"""

import threading
import time
import numpy as np
import logging
from typing import Dict, FrozenSet, List, Tuple

logger = logging.getLogger(__name__)

AUDIO_RATE   = 48_000
CHUNK        = 2048               # callback block size (≈42.7 ms)
RING_SIZE    = AUDIO_RATE * 2     # 2 s of headroom per VFO
PREFILL      = AUDIO_RATE // 4    # 250 ms silence pre-fill — headroom for GIL stalls
STATS_PERIOD = 3.0                # seconds between diagnostic log lines


class _RingBuffer:
    """Thread-safe ring buffer for float32 audio (single-producer, single-consumer).

    Uses two contiguous slices for every read/write — no index-array allocation.
    """

    def __init__(self, capacity: int):
        self._buf      = np.zeros(capacity, dtype=np.float32)
        self._cap      = capacity
        self._head     = 0   # write position
        self._tail     = 0   # read position
        self._lock     = threading.Lock()
        self.underruns = 0

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
            h   = self._head
            end = h + n
            if end <= self._cap:
                self._buf[h:end] = data
            else:
                first = self._cap - h
                self._buf[h:]       = data[:first]
                self._buf[:n-first] = data[first:]
            self._head = end % self._cap

    def read(self, n: int) -> np.ndarray:
        out  = np.empty(n, dtype=np.float32)
        take = 0
        with self._lock:
            avail = (self._head - self._tail) % self._cap
            take  = min(n, avail)
            if take > 0:
                t   = self._tail
                end = t + take
                if end <= self._cap:
                    out[:take] = self._buf[t:end]
                else:
                    first = self._cap - t
                    out[:first]     = self._buf[t:]
                    out[first:take] = self._buf[:take-first]
                self._tail = (t + take) % self._cap
        if take < n:
            self.underruns += 1
            out[take:] = 0.0
        return out


class AudioMixer:
    """
    Receives audio chunks from multiple VFOs via push_audio(), mixes them,
    and outputs through the system default audio device using sounddevice.

    Thread-safety: push_audio() is called from the SDR worker thread;
    _callback() runs in the sounddevice audio thread. Each VFO has its own
    ring buffer — push and read for a given VFO never race.
    The callback reads an immutable state snapshot that is rebuilt atomically
    on any VFO/volume/mute change, avoiding all allocations in the hot path.
    """

    def __init__(self):
        self._rings:   Dict[int, _RingBuffer] = {}
        self._volumes: Dict[int, float]       = {}
        self._muted:   set                    = set()
        self._stream   = None
        self._lock     = threading.Lock()
        self._started  = False
        # Immutable snapshot used inside _callback — replaced atomically.
        # Tuple: (vfo_id_list, volumes_dict, muted_frozenset)
        self._cb_state: Tuple = ([], {}, frozenset())
        # Pre-allocated mix buffer to avoid allocation in callback
        self._mix_buf = np.zeros(CHUNK, dtype=np.float32)
        # Diagnostics
        self._cb_count     = 0
        self._last_stats_t = 0.0

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
                latency='high',   # large OS buffer absorbs GIL stalls from rendering
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

    def _rebuild_cb_state(self):
        """Rebuild the immutable callback snapshot. Must be called with self._lock held."""
        self._cb_state = (
            list(self._rings.keys()),
            dict(self._volumes),
            frozenset(self._muted),
        )

    def add_vfo(self, vfo_id: int, volume: float = 1.0):
        ring = _RingBuffer(RING_SIZE)
        ring.write(np.zeros(PREFILL, dtype=np.float32))
        with self._lock:
            self._rings[vfo_id]   = ring
            self._volumes[vfo_id] = float(volume)
            self._rebuild_cb_state()
        logger.debug(f"AudioMixer: added VFO {vfo_id}")

    def remove_vfo(self, vfo_id: int):
        with self._lock:
            self._rings.pop(vfo_id, None)
            self._volumes.pop(vfo_id, None)
            self._muted.discard(vfo_id)
            self._rebuild_cb_state()
        logger.debug(f"AudioMixer: removed VFO {vfo_id}")

    def set_volume(self, vfo_id: int, volume: float):
        with self._lock:
            self._volumes[vfo_id] = max(0.0, min(1.0, float(volume)))
            self._rebuild_cb_state()

    def set_muted(self, vfo_id: int, muted: bool):
        with self._lock:
            if muted:
                self._muted.add(vfo_id)
            else:
                self._muted.discard(vfo_id)
            self._rebuild_cb_state()

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

        # Atomic tuple read — no lock, no allocation
        vfo_ids, volumes, muted = self._cb_state

        mixed = self._mix_buf[:frames]
        mixed[:] = 0.0

        for vfo_id in vfo_ids:
            if vfo_id in muted:
                continue
            ring = self._rings.get(vfo_id)
            if ring is None:
                continue
            chunk = ring.read(frames)
            vol   = volumes.get(vfo_id, 1.0)
            if vol == 1.0:
                mixed += chunk
            else:
                mixed += chunk * vol

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
