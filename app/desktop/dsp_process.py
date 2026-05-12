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

logger = logging.getLogger(__name__)

DISPLAY_FFT_SIZE = 32768  # must match sdr_worker.DISPLAY_FFT_SIZE


# ---------------------------------------------------------------------------
# Subprocess entry point — module-level so Windows spawn can pickle it
# ---------------------------------------------------------------------------

def _dsp_worker_main(cmd_q, result_q, spec_shm_name: str, wf_shm_name: str, disp_gen):
    """
    DSP subprocess entry point.
    Must be a module-level function (not a lambda or nested function) for
    Windows multiprocessing spawn to pickle it correctly.
    """
    import logging
    logging.basicConfig(level=logging.INFO,
                        format='[DSP %(process)d] %(levelname)s %(name)s: %(message)s')

    spec_shm = SharedMemory(name=spec_shm_name)
    wf_shm   = SharedMemory(name=wf_shm_name)
    spec_arr = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.float32, buffer=spec_shm.buf)
    wf_arr   = np.ndarray((DISPLAY_FFT_SIZE,), dtype=np.uint8,   buffer=wf_shm.buf)

    from app.desktop.sdr_worker import DSPWorker
    worker = DSPWorker(cmd_q, result_q, spec_arr, wf_arr, disp_gen)
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

    def __init__(self):
        # Use explicit 'spawn' context for cross-platform safety (required on Windows)
        self._ctx      = mp.get_context('spawn')
        self._cmd_q    = self._ctx.Queue()
        self._result_q = self._ctx.Queue()

        # Shared memory: spectrum (float32) and waterfall row (uint8)
        self._spec_shm = SharedMemory(create=True, size=DISPLAY_FFT_SIZE * 4)
        self._wf_shm   = SharedMemory(create=True, size=DISPLAY_FFT_SIZE * 1)
        self._disp_gen = self._ctx.Value('L', 0)  # generation counter

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
