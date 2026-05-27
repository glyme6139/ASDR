"""
DSP subprocess manager.

The DSP process owns all signal processing: HackRF receiver, FFT, VFO
channelization, audio output, and decoders. The UI process communicates with
it via:

  - multiprocessing.shared_memory  : spectrum + waterfall numpy arrays (zero-copy)
  - cmd_q  (Queue, UI → DSP)       : control commands
  - result_q (Queue, DSP → UI)     : device status, decoder results, signal strength
"""

import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory
import numpy as np
import logging

from .timing import TimingConfig

logger = logging.getLogger(__name__)

DISPLAY_FFT_SIZE = 32768  # must match sdr_worker.DISPLAY_FFT_SIZE


# ---------------------------------------------------------------------------
# Subprocess entry point — module-level so Windows spawn can pickle it
# ---------------------------------------------------------------------------

def _dsp_worker_main(cmd_q, result_q, spec_shm_name: str, wf_shm_name: str, disp_gen, timing_config: dict | None = None):
    """
    DSP subprocess entry point.
    Must be a module-level function (not a lambda or nested function) for
    Windows multiprocessing spawn to pickle it correctly.
    """
    import logging
    from .timing import TimingConfig, profiler_from_config
    logging.basicConfig(level=logging.INFO,
                        format='[DSP %(process)d] %(levelname)s %(name)s: %(message)s')

    def emit_timing_report(report: dict):
        try:
            result_q.put_nowait({'type': 'timing_report', 'data': report})
        except Exception:
            pass

    profiler = profiler_from_config(
        TimingConfig(**(timing_config or {})),
        prefix="DSP",
        report_handler=emit_timing_report,
    )

    spec_shm = SharedMemory(name=spec_shm_name)
    wf_shm   = SharedMemory(name=wf_shm_name)
    spec_arr = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.float32, buffer=spec_shm.buf)
    wf_arr   = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.uint8,   buffer=wf_shm.buf)

    from app.desktop.sdr_worker import DSPWorker
    worker = DSPWorker(cmd_q, result_q, spec_arr, wf_arr, disp_gen, profiler=profiler)
    try:
        worker.run()
    finally:
        spec_shm.close()
        wf_shm.close()


# ---------------------------------------------------------------------------
# DSPProcess — lives in the UI process, manages the subprocess
# ---------------------------------------------------------------------------

class DSPProcess:
    """
    Manages the DSP subprocess lifecycle and exposes proxy methods for all
    control operations. Spectrum and waterfall data are exposed as numpy views
    into shared memory; the UI render timer reads them without copying.
    """

    def __init__(self, timing: TimingConfig | None = None):
        # Use explicit 'spawn' context for cross-platform safety (required on Windows)
        self._ctx      = mp.get_context('spawn')
        self._cmd_q    = self._ctx.Queue()
        self._result_q = self._ctx.Queue()

        # Shared memory: spectrum (float32) and waterfall row (uint8)
        self._spec_shm = SharedMemory(create=True, size=DISPLAY_FFT_SIZE * 4)
        self._wf_shm   = SharedMemory(create=True, size=DISPLAY_FFT_SIZE * 1)
        self._disp_gen = self._ctx.Value('L', 0)  # generation counter
        self._timing   = timing or TimingConfig()

        # Read-only numpy views for the UI side
        self.spectrum_buf  = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.float32,
                                        buffer=self._spec_shm.buf)
        self.waterfall_buf = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.uint8,
                                        buffer=self._wf_shm.buf)
        self.display_gen   = self._disp_gen

        self._process: mp.Process = None

    def start(self):
        self._process = self._ctx.Process(
            target=_dsp_worker_main,
            args=(
                self._cmd_q,
                self._result_q,
                self._spec_shm.name,
                self._wf_shm.name,
                self._disp_gen,
                {'enabled': self._timing.enabled, 'sample_count': self._timing.sample_count},
            ),
            daemon=True,
        )
        self._process.start()
        logger.info(f"DSP subprocess started (pid={self._process.pid})")

    def stop(self):
        try:
            self._cmd_q.put_nowait({'cmd': 'stop'})
        except Exception:
            pass
        if self._process and self._process.is_alive():
            self._process.join(timeout=3.0)
            if self._process.is_alive():
                logger.warning("DSP subprocess did not exit cleanly — terminating")
                self._process.terminate()
        # Release shared memory (unlink frees the OS object)
        self._spec_shm.close()
        self._wf_shm.close()
        try:
            self._spec_shm.unlink()
            self._wf_shm.unlink()
        except Exception:
            pass
        logger.info("DSP subprocess stopped")

    @property
    def result_queue(self):
        return self._result_q

    # ------------------------------------------------------------------
    # Command proxies (thread-safe: Queue.put_nowait is GIL-protected)
    # ------------------------------------------------------------------

    def _send(self, msg: dict):
        try:
            self._cmd_q.put_nowait(msg)
        except Exception as e:
            logger.warning(f"DSPProcess._send failed: {e}")

    def set_center_frequency(self, freq_hz: float):
        self._send({'cmd': 'set_center_frequency', 'freq_hz': float(freq_hz)})

    def set_sample_rate(self, rate: float):
        self._send({'cmd': 'set_sample_rate', 'rate': float(rate)})

    def set_lna_gain(self, value: float):
        self._send({'cmd': 'set_lna_gain', 'value': float(value)})

    def set_vga_gain(self, value: float):
        self._send({'cmd': 'set_vga_gain', 'value': float(value)})

    def set_amp_enable(self, enabled: bool):
        self._send({'cmd': 'set_amp_enable', 'enabled': bool(enabled)})

    def add_vfo(self, vfo_id: int, freq_hz: float):
        self._send({'cmd': 'add_vfo', 'vfo_id': int(vfo_id), 'freq_hz': float(freq_hz)})

    def remove_vfo(self, vfo_id: int):
        self._send({'cmd': 'remove_vfo', 'vfo_id': int(vfo_id)})

    def set_vfo_frequency(self, vfo_id: int, freq_hz: float):
        self._send({'cmd': 'set_vfo_frequency', 'vfo_id': int(vfo_id), 'freq_hz': float(freq_hz)})

    def set_vfo_demod(self, vfo_id: int, mode: str):
        self._send({'cmd': 'set_vfo_demod', 'vfo_id': int(vfo_id), 'mode': str(mode)})

    def set_vfo_bandwidth(self, vfo_id: int, bandwidth_hz: float):
        self._send({'cmd': 'set_vfo_bandwidth', 'vfo_id': int(vfo_id),
                    'bandwidth_hz': float(bandwidth_hz)})

    def set_vfo_squelch(self, vfo_id: int, level_db: float):
        self._send({'cmd': 'set_vfo_squelch', 'vfo_id': int(vfo_id), 'level_db': float(level_db)})

    def set_vfo_squelch_enabled(self, vfo_id: int, enabled: bool):
        self._send({'cmd': 'set_vfo_squelch_enabled', 'vfo_id': int(vfo_id), 'enabled': bool(enabled)})

    def set_vfo_muted(self, vfo_id: int, muted: bool):
        self._send({'cmd': 'set_vfo_muted', 'vfo_id': int(vfo_id), 'muted': bool(muted)})

    def set_volume(self, vfo_id: int, volume: float):
        self._send({'cmd': 'set_volume', 'vfo_id': int(vfo_id), 'volume': float(volume)})

    def toggle_decoder(self, vfo_id: int, decoder_name: str, enabled: bool):
        self._send({'cmd': 'toggle_decoder', 'vfo_id': int(vfo_id),
                    'decoder_name': str(decoder_name), 'enabled': bool(enabled)})

    def set_eye_stream(self, enabled: bool, vfo_id: int | None):
        self._send({'cmd': 'set_eye_stream', 'enabled': bool(enabled), 'vfo_id': vfo_id})

    def set_timing_sample_count(self, n: int):
        self._send({'cmd': 'set_timing_sample_count', 'n': int(n)})

    # ------------------------------------------------------------------
    # Source switching
    # ------------------------------------------------------------------

    def set_source(self, source_type: str, file_path: str = '',
                   center_freq: float = 100e6, sample_rate: float = 20e6):
        self._send({'cmd': 'set_source', 'source_type': source_type,
                    'file_path': file_path, 'center_freq': float(center_freq),
                    'sample_rate': float(sample_rate)})

    def connect_hackrf(self, center_freq: float = 100e6, sample_rate: float = 20e6,
                       lna: int = 24, vga: int = 20, amp: bool = False):
        self._send({'cmd': 'connect_hackrf',
                    'center_freq': float(center_freq), 'sample_rate': float(sample_rate),
                    'lna': int(lna), 'vga': int(vga), 'amp': bool(amp)})

    def connect_sweep(self, start_freq: float = 80e6, stop_freq: float = 108e6,
                      sample_rate: float = 20e6, lna: int = 24, vga: int = 20,
                      amp: bool = False, bin_width: int = 100_000):
        self._send({'cmd': 'connect_sweep',
                    'start_freq': float(start_freq), 'stop_freq': float(stop_freq),
                    'sample_rate': float(sample_rate),
                    'lna': int(lna), 'vga': int(vga), 'amp': bool(amp),
                    'bin_width': int(bin_width)})

    def disconnect_hackrf(self):
        self._send({'cmd': 'disconnect_hackrf'})

    # ------------------------------------------------------------------
    # IQ recording
    # ------------------------------------------------------------------

    def start_recording(self, file_path: str, fmt: str, max_duration: float = 0.0):
        self._send({'cmd': 'start_recording', 'file_path': file_path,
                    'format': fmt, 'max_duration': float(max_duration)})

    def stop_recording(self):
        self._send({'cmd': 'stop_recording'})

    # ------------------------------------------------------------------
    # File playback controls
    # ------------------------------------------------------------------

    def playback_pause(self):
        self._send({'cmd': 'playback_pause'})

    def playback_resume(self):
        self._send({'cmd': 'playback_resume'})

    def playback_stop(self):
        self._send({'cmd': 'playback_stop'})

    def set_playback_speed(self, speed: float):
        self._send({'cmd': 'playback_speed', 'speed': float(speed)})

    def playback_seek(self, pos_samples: int):
        self._send({'cmd': 'playback_seek', 'pos': int(pos_samples)})

    def set_playback_loop(self, loop: bool):
        self._send({'cmd': 'playback_loop', 'loop': bool(loop)})
