"""
wfm_radio.py  –  Live WFM FM-radio demodulator for HackRF
----------------------------------------------------------
Usage
-----
    python wfm_radio.py                    # defaults to 100.0 MHz
    python wfm_radio.py --freq 98.5
    python wfm_radio.py --freq 103.1 --volume 1.5
    python wfm_radio.py --deemph-tau 50e-6   # 50 µs for Europe

Dependencies
------------
    pip install python-hackrf numpy sounddevice scipy
"""

import argparse
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from scipy.signal import firwin, lfilter

from python_hackrf import pyhackrf  # type: ignore

# ── SDR parameters ─────────────────────────────────────────────────────────────
SAMPLE_RATE  = 2_400_000
BASEBAND_BW  = 1_750_000
LNA_GAIN     = 32
VGA_GAIN     = 40


# ── Decimation chain ───────────────────────────────────────────────────────────
# 2_400_000 ──÷10──► 240_000 ──÷5──► 48_000  (exact, no resampling needed)
AUDIO_RATE   = 48_000
DECIM_1      = 10          # → 240 kS/s  (intermediate)
DECIM_2      = 5           # → 48 kS/s   (audio)
INTER_RATE   = SAMPLE_RATE // DECIM_1   # 240_000

# FM max deviation for WFM broadcast
FM_MAX_DEV   = 75_000  # Hz

# ── Ring buffer ────────────────────────────────────────────────────────────────
RING_SIZE    = AUDIO_RATE * 2   # 2 seconds headroom


class RingBuffer:
    def __init__(self, capacity: int):
        self._buf  = np.zeros(capacity, dtype=np.float32)
        self._cap  = capacity
        self._head = 0
        self._tail = 0
        self._lock = threading.Lock()

    def _available(self):
        return (self._head - self._tail) % self._cap

    def write(self, data: np.ndarray):
        n = len(data)
        with self._lock:
            space = self._cap - self._available() - 1
            if n > space:
                self._tail = (self._tail + (n - space)) % self._cap
            idx = np.arange(self._head, self._head + n) % self._cap
            self._buf[idx] = data
            self._head = (self._head + n) % self._cap

    def read(self, n: int) -> np.ndarray:
        with self._lock:
            avail = self._available()
            take  = min(n, avail)
            if take == 0:
                return np.zeros(n, dtype=np.float32)
            idx = np.arange(self._tail, self._tail + take) % self._cap
            out = self._buf[idx].copy()
            self._tail = (self._tail + take) % self._cap
        if take < n:
            out = np.concatenate([out, np.zeros(n - take, dtype=np.float32)])
        return out

    @property
    def fill(self):
        with self._lock:
            return self._available()


def _make_deemph(tau: float, fs: float):
    """Single-pole RC de-emphasis IIR."""
    dt    = 1.0 / fs
    alpha = dt / (tau + dt)
    return np.array([alpha]), np.array([1.0, -(1.0 - alpha)])


def parse_args():
    p = argparse.ArgumentParser(description="HackRF WFM live radio")
    p.add_argument("--freq",       type=float, default=100.0)
    p.add_argument("--volume",     type=float, default=1.0)
    p.add_argument("--deemph-tau", type=float, default=75e-6,
                   help="75e-6 for Americas/Asia, 50e-6 for Europe")
    return p.parse_args()


def main():
    args        = parse_args()
    center_freq = args.freq * 1e6
    volume      = args.volume

    # ── Pre-build filters ─────────────────────────────────────────────────────

    # 1. Channel LPF applied BEFORE first decimation (at 2.4 MS/s)
    #    Cutoff = 100 kHz (passes the ±75 kHz WFM channel + guard)
    #    Nyquist = SAMPLE_RATE / 2 = 1.2 MHz
    lpf_channel = firwin(128, 100_000 / (SAMPLE_RATE / 2))

    # 2. Audio LPF applied BEFORE second decimation (at 240 kS/s)
    #    Cutoff = 15 kHz (audio bandwidth), Nyquist = 120 kHz
    lpf_audio = firwin(64, 15_000 / (INTER_RATE / 2))

    # 3. De-emphasis runs at intermediate rate (240 kS/s), on the audio signal
    #    before the second decimation
    b_de, a_de = _make_deemph(args.deemph_tau, INTER_RATE)

    # FM discriminator gain: converts radians/sample → normalised amplitude
    # angle() gives radians; at max deviation the phase shift per sample is:
    #   2π * FM_MAX_DEV / SAMPLE_RATE
    # Dividing by that normalises the output to ±1 at full deviation.
    fm_gain = SAMPLE_RATE / (2 * np.pi * FM_MAX_DEV)

    # Carry-over state across blocks
    prev_sample = np.complex64(1 + 0j)
    lpf_ch_zi   = np.zeros(len(lpf_channel) - 1)
    lpf_au_zi   = np.zeros(len(lpf_audio)   - 1)
    deemph_zi   = np.zeros(len(a_de) - 1)

    # ── Shared state ──────────────────────────────────────────────────────────
    iq_queue: queue.Queue = queue.Queue(maxsize=128)
    ring       = RingBuffer(RING_SIZE)
    stop_event = threading.Event()

    # ── DSP thread ────────────────────────────────────────────────────────────
    def dsp_loop():
        nonlocal prev_sample, lpf_ch_zi, lpf_au_zi, deemph_zi

        while not stop_event.is_set():
            try:
                iq = iq_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # ── Step 1: channel low-pass (at full 2.4 MS/s) ──────────────────
            # Removes out-of-channel interference before decimation
            iq_filtered, lpf_ch_zi = lfilter(lpf_channel, 1.0, iq, zi=lpf_ch_zi)

            # ── Step 2: FM discriminator ──────────────────────────────────────
            # Prepend last sample from previous block for seamless phase continuity
            iq_ext    = np.empty(len(iq_filtered) + 1, dtype=np.complex64)
            iq_ext[0] = prev_sample
            iq_ext[1:] = iq_filtered
            prev_sample = iq_filtered[-1]

            # Phase difference between consecutive samples → instantaneous frequency
            fm = np.angle(iq_ext[1:] * np.conj(iq_ext[:-1])) * fm_gain

            # ── Step 3: decimate ×10 → 240 kS/s ─────────────────────────────
            fm_inter = fm[::DECIM_1]

            # ── Step 4: audio LPF at 15 kHz (at 240 kS/s) ───────────────────
            fm_inter, lpf_au_zi = lfilter(lpf_audio, 1.0, fm_inter, zi=lpf_au_zi)

            # ── Step 5: de-emphasis (at 240 kS/s) ────────────────────────────
            fm_inter, deemph_zi = lfilter(b_de, a_de, fm_inter, zi=deemph_zi)

            # ── Step 6: decimate ×5 → 48 kS/s ────────────────────────────────
            audio = fm_inter[::DECIM_2]

            ring.write((audio * volume).astype(np.float32))

    dsp_thread = threading.Thread(target=dsp_loop, daemon=True, name="dsp")

    # ── Audio callback ────────────────────────────────────────────────────────
    def audio_callback(outdata, frames, _time, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        outdata[:, 0] = ring.read(frames)

    # ── HackRF callback ───────────────────────────────────────────────────────
    def rx_callback(device, buffer, buffer_length, valid_length):
        raw = buffer[:valid_length].astype(np.int8)
        iq  = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64) / 128.0
        try:
            iq_queue.put_nowait(iq)
        except queue.Full:
            pass
        return 0

    # ── Start ─────────────────────────────────────────────────────────────────
    print(f"[radio] Tuning to {args.freq:.1f} MHz …")
    pyhackrf.pyhackrf_init()
    sdr = pyhackrf.pyhackrf_open()

    allowed_bw = pyhackrf.pyhackrf_compute_baseband_filter_bw_round_down_lt(BASEBAND_BW)
    sdr.pyhackrf_set_sample_rate(SAMPLE_RATE)
    sdr.pyhackrf_set_baseband_filter_bandwidth(allowed_bw)
    sdr.pyhackrf_set_freq(int(center_freq))
    sdr.pyhackrf_set_amp_enable(True)
    sdr.pyhackrf_set_antenna_enable(False)
    sdr.pyhackrf_set_lna_gain(LNA_GAIN)
    sdr.pyhackrf_set_vga_gain(VGA_GAIN)

    print(f"[radio] {SAMPLE_RATE/1e6:.1f} MS/s  bw={allowed_bw/1e6:.3f} MHz  "
          f"LNA={LNA_GAIN} dB  VGA={VGA_GAIN} dB  "
          f"decim={DECIM_1}×{DECIM_2}={DECIM_1*DECIM_2} → {AUDIO_RATE//1000} kHz audio")

    sdr.set_rx_callback(rx_callback)

    stream = sd.OutputStream(
        samplerate=AUDIO_RATE,
        channels=1,
        dtype="float32",
        blocksize=1024,
        callback=audio_callback,
    )

    ring.write(np.zeros(AUDIO_RATE // 10, dtype=np.float32))  # pre-fill 100 ms silence

    dsp_thread.start()
    stream.start()
    sdr.pyhackrf_start_rx()

    print("[radio] Streaming – press Ctrl-C to stop …\n")
    try:
        while True:
            time.sleep(0.5)
            fill_ms = ring.fill / AUDIO_RATE * 1000
            iq_q    = iq_queue.qsize()
            print(f"\r[radio] ring={fill_ms:5.1f} ms  iq_queue={iq_q:3d}   ", end="", flush=True)
    except KeyboardInterrupt:
        print("\n[radio] Stopping …")
    finally:
        stop_event.set()
        sdr.pyhackrf_stop_rx()
        sdr.pyhackrf_close()
        pyhackrf.pyhackrf_exit()
        stream.stop()
        stream.close()
        dsp_thread.join(timeout=2)
        print("[radio] Done.")


if __name__ == "__main__":
    main()