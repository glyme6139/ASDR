"""
Core VFO (Virtual Frequency Oscillator) management system.

Key design: before demodulation, IQ is pre-decimated to a TARGET_PROC_RATE
(≈200 kHz). This keeps resample_poly ratios small regardless of the HackRF
sample rate, fixing the chronic under-production at 20 MHz where the naive
resample ratio (3:1250) required a 25 001-tap FIR on every callback.
"""
import threading
import numpy as np
from dataclasses import dataclass, field
from math import gcd
from typing import Callable, Optional, List, Tuple, Dict
from scipy import signal
from scipy.signal import resample_poly, firwin, lfilter
from scipy.signal import resample as scipy_resample
import logging

logger = logging.getLogger(__name__)

AUDIO_RATE  = 48_000
WFM_MAX_DEV = 75_000   # Hz — broadcast FM standard
TARGET_PROC_RATE = 200_000  # Hz — keeps resample_poly ratios small for any HackRF sample


@dataclass
class VFOSettings:
    frequency:       float     = 100_000_000
    name:            str       = ''
    demod_mode:      str       = 'NFM'
    bandwidth:       float     = 12_500
    volume:          float     = 1.0
    squelch_level:   float     = -100.0
    squelch_enabled: bool      = True
    enabled:         bool      = True
    tags:            List[str] = field(default_factory=list)


class VFO:
    """Independent demodulation channel with fully stateful DSP pipeline."""

    def __init__(self, vfo_id: int, center_freq: float, sample_rate: float):
        self.id          = vfo_id
        self.settings    = VFOSettings(frequency=center_freq)
        self.sample_rate = sample_rate
        self.center_freq = center_freq

        self.spectrum_data = np.zeros(512)
        self.is_running    = False

        self.on_audio_ready:    Optional[Callable] = None
        self.on_spectrum_ready: Optional[Callable] = None
        self.on_squelch_change: Optional[Callable] = None

        from app.decoders.base import BaseDecoder
        self.decoders: List[BaseDecoder] = []

        self.signal_strength = -100.0
        self.signal_db       = -100.0   # current signal in dBFS (for UI polling)
        self.is_active       = False
        self._sq_state       = False    # hysteresis state: True = squelch open

        # Stateful DSP — all preserved across process_iq() calls
        self._osc_phase:   float       = 0.0                    # frequency-shift oscillator
        self._prev_sample: np.complex64 = np.complex64(1 + 0j)  # FM discriminator (legacy)
        # Whether this VFO should produce audio. When False, demodulation
        # and audio resampling are skipped (useful for mute to save CPU).
        self.audio_enabled: bool = True
        
        # Filter coefficients and state (built by _build_filters)
        self._ch_filter: Optional[np.ndarray] = None
        self._ch_zi_i:   Optional[np.ndarray] = None
        self._ch_zi_q:   Optional[np.ndarray] = None
        self._deemph_b:  Optional[np.ndarray] = None
        self._deemph_a:  Optional[np.ndarray] = None
        self._deemph_zi: Optional[np.ndarray] = None
        self._cw_b:      Optional[np.ndarray] = None
        self._cw_a:      Optional[np.ndarray] = None
        self._cw_zi:     Optional[np.ndarray] = None

        # Set by _build_filters
        self._iq_decim:    int   = 1
        self._proc_rate:   float = sample_rate
        self._resample_up: int   = 1
        self._resample_down: int = 1

        # Channel filter for the FFT-channelizer path (process_narrowband_iq).
        # Decimates the intermediate IQ (at ~200 kHz) down to ~2× bandwidth.
        self._nb_ch_taps:  Optional[np.ndarray] = None
        self._nb_ch_decim: int   = 1
        self._nb_ch_zi_i:  Optional[np.ndarray] = None
        self._nb_ch_zi_q:  Optional[np.ndarray] = None
        self._nb_ch_nb_sr: float = 0.0
        self._nb_ch_bw:    float = 0.0

        self._build_filters()
        logger.info(f"VFO {vfo_id} initialized at {center_freq/1e6:.2f} MHz  "
                    f"decim={self._iq_decim}  proc_rate={self._proc_rate/1e3:.0f} kHz")

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_resample_ratio(from_rate: int, to_rate: int) -> Tuple[int, int]:
        g = gcd(to_rate, from_rate)
        return to_rate // g, from_rate // g

    # ------------------------------------------------------------------
    # Filter / rate setup
    # ------------------------------------------------------------------

    def _build_filters(self):
        sr = self.sample_rate
        bw = self.settings.bandwidth

        # IQ pre-decimation factor: bring IQ down to ~TARGET_PROC_RATE.
        # Never decimate below 8× the VFO bandwidth to avoid aliasing the signal.
        min_proc = max(bw * 8, AUDIO_RATE * 2)
        d = max(1, int(sr / max(TARGET_PROC_RATE, min_proc)))
        self._iq_decim  = d
        self._proc_rate = sr / d

        # Channel LPF at full sample rate (anti-alias before IQ decimation).
        # Cutoff = bandwidth/2; normalized to [0,1] where 1 = Nyquist.
        cutoff = min(0.99, max(0.001, (bw / 2.0) / (sr / 2.0)))
        self._ch_filter = firwin(128, cutoff, window='hamming')
        zi = len(self._ch_filter) - 1
        self._ch_zi_i   = np.zeros(zi)
        self._ch_zi_q   = np.zeros(zi)

        # Resample: proc_rate → AUDIO_RATE  (small ratio, fast filter)
        self._resample_up, self._resample_down = self._compute_resample_ratio(
            int(self._proc_rate), AUDIO_RATE
        )

        # WFM de-emphasis at proc_rate: single-pole IIR, τ = 75 µs
        tau   = 75e-6
        alpha = (1.0 / self._proc_rate) / (tau + 1.0 / self._proc_rate)
        self._deemph_b  = np.array([alpha],               dtype=np.float64)
        self._deemph_a  = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
        self._deemph_zi = np.zeros(1, dtype=np.float64)

        # CW envelope filter at proc_rate
        try:
            cw_cut      = min(0.99, 1_000.0 / (self._proc_rate / 2.0))
            self._cw_b, self._cw_a = signal.butter(4, cw_cut)
            self._cw_zi = np.zeros(max(len(self._cw_a), len(self._cw_b)) - 1)
        except Exception:
            self._cw_b = self._cw_a = self._cw_zi = None

        logger.debug(
            f"VFO {self.id}: SR={sr/1e6:.2f}MHz  decim=÷{d}  "
            f"proc={self._proc_rate/1e3:.0f}kHz  "
            f"resample({self._resample_up},{self._resample_down})"
        )

    def _rebuild_nb_channel_filter(self, nb_sr: float) -> None:
        """Design FIR LPF + decimation for the FFT-channelizer (process_narrowband_iq) path.

        Decimates from the intermediate rate (~200 kHz) down to ~2× bandwidth,
        matching what _build_filters does for the time-domain path.
        """
        bw = self.settings.bandwidth
        self._nb_ch_nb_sr = nb_sr
        self._nb_ch_bw    = bw
        # Determine decimation: bring nb_sr down to ~2× BW, but never below AUDIO_RATE
        decim = max(1, int(nb_sr / max(bw * 2.0, AUDIO_RATE)))
        if decim <= 1:
            self._nb_ch_taps  = None
            self._nb_ch_decim = 1
            return
        nyq    = nb_sr / 2.0
        cutoff = min(0.95, (bw / 2.0) / nyq)
        n_taps = min(255, max(31, 6 * decim) | 1)  # odd-length linear-phase FIR
        self._nb_ch_taps  = firwin(n_taps, cutoff, window='hamming')
        self._nb_ch_decim = decim
        zi_len = len(self._nb_ch_taps) - 1
        self._nb_ch_zi_i  = np.zeros(zi_len)
        self._nb_ch_zi_q  = np.zeros(zi_len)
        logger.debug(
            "VFO %s nb-filter: nb_sr=%.0f Hz  bw=%.0f Hz  decim=÷%d  taps=%d  "
            "final=%.0f Hz",
            self.id, nb_sr, bw, decim, n_taps, nb_sr / decim,
        )

    # ------------------------------------------------------------------
    # Decoder management
    # ------------------------------------------------------------------

    def add_decoder(self, decoder) -> None:
        if not any(d.name == decoder.name for d in self.decoders):
            self.decoders.append(decoder)

    def remove_decoder(self, name: str) -> None:
        self.decoders = [d for d in self.decoders if d.name != name]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def update_sample_rate(self, rate: float):
        self.sample_rate  = rate
        self._prev_sample = np.complex64(1 + 0j)
        self._osc_phase   = 0.0
        self._build_filters()

    def set_frequency(self, freq: float):
        self.settings.frequency = freq
        self._osc_phase = 0.0   # reset oscillator when retuning

    def set_demod_mode(self, mode: str):
        self.settings.demod_mode = mode
        self._prev_sample = np.complex64(1 + 0j)

    def set_bandwidth(self, bw: float):
        self.settings.bandwidth = bw
        self._build_filters()

    def set_volume(self, volume: float):
        self.settings.volume = max(0.0, min(1.0, volume))

    def set_audio_enabled(self, enabled: bool):
        """Enable or disable audio production for this VFO."""
        self.audio_enabled = bool(enabled)
        logger.debug("VFO %s: audio_enabled=%s", self.id, self.audio_enabled)

    def set_squelch(self, level: float, enabled: bool = True):
        self.settings.squelch_level   = level
        self.settings.squelch_enabled = enabled
        if self.on_squelch_change:
            self.on_squelch_change(self.id, level, enabled)

    def set_squelch_enabled(self, enabled: bool):
        self.settings.squelch_enabled = bool(enabled)

    def set_name(self, name: str):
        """Set a human-readable name for this VFO."""
        try:
            self.settings.name = str(name)
        except Exception:
            self.settings.name = ''

    def enable(self):
        self.settings.enabled = True
        self.is_running = True

    def disable(self):
        self.settings.enabled = False
        self.is_running = False

    # ------------------------------------------------------------------
    # IQ pipeline
    # ------------------------------------------------------------------

    def process_iq(self, iq_data: np.ndarray, profiler=None) -> Tuple[Optional[np.ndarray], list]:
        if not self.settings.enabled:
            return None, []
        if not self.audio_enabled:
            return None, []

        # 1. Coherent frequency shift (maintains phase across block boundaries)
        offset = self.settings.frequency - self.center_freq
        if abs(offset) > 1:
            n      = len(iq_data)
            phases = self._osc_phase + np.arange(n, dtype=np.float64) * (
                        -2.0 * np.pi * offset / self.sample_rate)
            iq_data = (iq_data * np.exp(1j * phases)).astype(np.complex64)
            self._osc_phase = (self._osc_phase
                               - 2.0 * np.pi * offset / self.sample_rate * n) % (2.0 * np.pi)

        # 2. Signal strength & squelch (on full-rate IQ)
        self.signal_strength = float(np.mean(np.abs(iq_data) ** 2))
        signal_db = 10.0 * np.log10(max(self.signal_strength, 1e-10))
        if self.settings.squelch_enabled and signal_db < self.settings.squelch_level:
            self.is_active = False
            return None, []
        self.is_active = True

        # 3. Channel LPF at full rate (stateful, anti-aliases before decimation)
        iq_filtered = self._apply_channel_filter(iq_data)

        # 4. Integer decimation to proc_rate
        if self._iq_decim > 1:
            iq_filtered = iq_filtered[::self._iq_decim]

        # 5. Demodulate at proc_rate
        audio = self._demodulate(iq_filtered)
        if audio is None:
            return None, []

        audio = audio * self.settings.volume

        # 6. Resample proc_rate → AUDIO_RATE  (small ratio, fast)
        audio_resampled = self._resample_audio(audio)

        self._update_spectrum(audio_resampled)

        decoder_results = []
        for dec in self.decoders:
            if not dec.is_enabled:
                continue
            try:
                if profiler is not None:
                    with profiler.measure(f"decoder / {dec.name}"):
                        result = dec.process(iq_data, audio_resampled)
                else:
                    result = dec.process(iq_data, audio_resampled)
                if result is not None:
                    decoder_results.append(result)
            except Exception as e:
                logger.error(f"VFO {self.id} decoder {dec.name} error: {e}")

        return audio_resampled, decoder_results

    # ------------------------------------------------------------------
    # FFT-channelizer path (called by sdr_worker instead of process_iq)
    # ------------------------------------------------------------------

    def process_narrowband_iq(
        self,
        iq: np.ndarray,
        nb_sr: float,
        audio_target: int,
        profiler=None,
    ) -> Tuple[Optional[np.ndarray], list]:
        """
        Process pre-channelized IQ that has already been:
          - frequency-shifted to 0 Hz (by the bin-extraction IFFT)
          - decimated to nb_sr ≈ VFO bandwidth

        Skips frequency shift, channel LPF, and integer pre-decimation.
        Uses scipy.signal.resample (FFT-based, arbitrary ratio) to hit
        audio_target samples, which is constant per sample-rate/FFT-size
        combination regardless of VFO bandwidth.
        """
        if not self.settings.enabled or len(iq) == 0:
            return None, []
        if not self.audio_enabled:
            return None, []

        # Stage 1 — channel filter + decimation.
        # Run unconditionally (before squelch) so filter state stays continuous
        # even when squelch is closed, preventing transients on re-open.
        # The FFT channelizer extracts a wide intermediate slice (~200 kHz) for
        # good spectral resolution; decimate to ~2× bandwidth here.
        intermediate_nb_sr = nb_sr
        bw_changed = abs(self.settings.bandwidth - self._nb_ch_bw) / (self._nb_ch_bw + 1.0) > 0.02
        sr_changed = abs(nb_sr - self._nb_ch_nb_sr) / (self._nb_ch_nb_sr + 1.0) > 0.02
        if bw_changed or sr_changed:
            self._rebuild_nb_channel_filter(nb_sr)
        if self._nb_ch_taps is not None and self._nb_ch_decim > 1:
            i_filt, self._nb_ch_zi_i = lfilter(
                self._nb_ch_taps, 1.0, np.real(iq), zi=self._nb_ch_zi_i)
            q_filt, self._nb_ch_zi_q = lfilter(
                self._nb_ch_taps, 1.0, np.imag(iq), zi=self._nb_ch_zi_q)
            iq    = (i_filt[::self._nb_ch_decim] + 1j * q_filt[::self._nb_ch_decim]).astype(np.complex64)
            nb_sr = nb_sr / self._nb_ch_decim

        # Signal strength + squelch — measured on the band-limited IQ so only
        # in-channel energy counts, not adjacent interference or wideband noise.
        # Normalization by (intermediate_nb_sr / sample_rate)^2 compensates for
        # the FFT channelizer's amplitude inflation (N/inter_bins = sr/nb_sr),
        # making the dBFS reading sample-rate independent.
        _norm = (intermediate_nb_sr / max(self.sample_rate, 1.0)) ** 2
        self.signal_strength = float(np.mean(np.abs(iq) ** 2)) * _norm
        signal_db = 10.0 * np.log10(max(self.signal_strength, 1e-10))
        self.signal_db = signal_db

        if self.settings.squelch_enabled:
            was_open = self._sq_state
            if self._sq_state:
                # Close if signal drops 3 dB below threshold
                if signal_db < self.settings.squelch_level - 3.0:
                    self._sq_state = False
            else:
                # Open if signal reaches threshold
                if signal_db >= self.settings.squelch_level:
                    self._sq_state = True
            if not self._sq_state:
                self.is_active = False
                return None, []
            self.is_active = True
        else:
            was_open = self._sq_state
            self._sq_state = True
            self.is_active = True

        # Update proc_rate used by demodulators for normalisation.
        # Rebuild de-emphasis filter only when rate changes significantly.
        if abs(nb_sr - self._proc_rate) / (self._proc_rate + 1.0) > 0.02:
            self._proc_rate = nb_sr
            tau   = 75e-6
            alpha = (1.0 / nb_sr) / (tau + 1.0 / nb_sr)
            self._deemph_b  = np.array([alpha],               dtype=np.float64)
            self._deemph_a  = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
            self._deemph_zi = np.zeros(1, dtype=np.float64)

        audio = self._demodulate(iq)
        if audio is None:
            return None, []

        # Fade in when squelch just opened to avoid click artifact
        if not was_open and self._sq_state:
            fade = np.linspace(0.0, 1.0, len(audio), dtype=np.float32)
            audio = audio * fade

        audio = (audio * self.settings.volume).astype(np.float64)

        # FFT-based resample to audio_target samples (handles any ratio cleanly)
        if audio_target != len(audio):
            audio = scipy_resample(audio, audio_target)

        audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
        self._update_spectrum(audio)

        # Run registered decoders on the audio (and optionally IQ).
        # The audio has been resampled to audio_target samples, which the mixer
        # plays at AUDIO_RATE — so decoders must be told AUDIO_RATE, not nb_sr.
        decoder_results = []
        for decoder in self.decoders:
            try:
                decoder.set_sample_rate(AUDIO_RATE)
                if profiler is not None:
                    with profiler.measure(f"decoder / {decoder.name}"):
                        result = decoder.process(iq, audio=audio)
                else:
                    result = decoder.process(iq, audio=audio)
                if result is not None:
                    decoder_results.append(result)
            except Exception as e:
                logger.error(f"Decoder {decoder.name} error: {e}", exc_info=True)

        return audio, decoder_results

    # ------------------------------------------------------------------
    # Channel filter (I/Q separately to keep dtype simple)
    # ------------------------------------------------------------------

    def _apply_channel_filter(self, iq_data: np.ndarray) -> np.ndarray:
        if self._ch_filter is None or len(iq_data) == 0:
            return iq_data
        try:
            i_out, self._ch_zi_i = lfilter(self._ch_filter, 1.0,
                                            np.real(iq_data), zi=self._ch_zi_i)
            q_out, self._ch_zi_q = lfilter(self._ch_filter, 1.0,
                                            np.imag(iq_data), zi=self._ch_zi_q)
            return (i_out + 1j * q_out).astype(np.complex64)
        except Exception as e:
            logger.error(f"VFO {self.id} channel filter error: {e}")
            return iq_data

    # ------------------------------------------------------------------
    # Resample
    # ------------------------------------------------------------------

    def _resample_audio(self, audio: np.ndarray) -> np.ndarray:
        try:
            return resample_poly(audio, self._resample_up, self._resample_down).astype(np.float32)
        except Exception as e:
            logger.error(f"VFO {self.id} resample error: {e}")
            return audio.astype(np.float32)

    # ------------------------------------------------------------------
    # Demodulation
    # ------------------------------------------------------------------

    def _demodulate(self, iq_data: np.ndarray) -> Optional[np.ndarray]:
        mode = self.settings.demod_mode
        try:
            if mode in ('NFM', 'FM'): return self._demod_fm(iq_data)
            elif mode == 'WFM':       return self._demod_wfm(iq_data)
            elif mode == 'AM':        return self._demod_am(iq_data)
            elif mode == 'USB':       return self._demod_usb(iq_data)
            elif mode == 'LSB':       return self._demod_lsb(iq_data)
            elif mode == 'DSB':       return self._demod_dsb(iq_data)
            elif mode == 'CW':        return self._demod_cw(iq_data)
            elif mode == 'IQ':        return np.real(iq_data).astype(np.float32)
            else:                     return self._demod_fm(iq_data)
        except Exception as e:
            logger.error(f"VFO {self.id} demodulation error ({mode}): {e}")
            return None

    # FM discriminator: conjugate-multiply, stateful across blocks
    def _fm_discriminator(self, iq_data: np.ndarray) -> np.ndarray:
        iq_ext     = np.empty(len(iq_data) + 1, dtype=np.complex64)
        iq_ext[0]  = self._prev_sample
        iq_ext[1:] = iq_data
        self._prev_sample = iq_data[-1]
        return np.angle(iq_ext[1:] * np.conj(iq_ext[:-1]))

    def _demod_fm(self, iq_data: np.ndarray) -> np.ndarray:
        fm_rad = self._fm_discriminator(iq_data)
        max_dev = 2.0 * np.pi * (self.settings.bandwidth / 2.0) / self._proc_rate
        audio = fm_rad / max(max_dev, 1e-9)
        audio -= np.mean(audio)
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def _demod_wfm(self, iq_data: np.ndarray) -> np.ndarray:
        fm_rad  = self._fm_discriminator(iq_data)
        max_dev = 2.0 * np.pi * WFM_MAX_DEV / self._proc_rate
        audio   = fm_rad / max(max_dev, 1e-9)
        if self._deemph_b is not None:
            audio, self._deemph_zi = lfilter(
                self._deemph_b, self._deemph_a, audio, zi=self._deemph_zi)
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def _demod_am(self, iq_data: np.ndarray) -> np.ndarray:
        audio = np.abs(iq_data) - np.mean(np.abs(iq_data))
        peak  = np.max(np.abs(audio))
        return (audio / peak if peak > 0 else audio).astype(np.float32)

    def _demod_usb(self, iq_data: np.ndarray) -> np.ndarray:
        audio = np.real(iq_data) + np.imag(iq_data)
        peak  = np.max(np.abs(audio))
        return (audio / peak if peak > 0 else audio).astype(np.float32)

    def _demod_lsb(self, iq_data: np.ndarray) -> np.ndarray:
        audio = np.real(iq_data) - np.imag(iq_data)
        peak  = np.max(np.abs(audio))
        return (audio / peak if peak > 0 else audio).astype(np.float32)

    def _demod_dsb(self, iq_data: np.ndarray) -> np.ndarray:
        return np.clip(np.real(iq_data) + np.imag(iq_data), -1.0, 1.0).astype(np.float32)

    def _demod_cw(self, iq_data: np.ndarray) -> np.ndarray:
        audio = np.abs(iq_data).astype(np.float64)
        if self._cw_b is not None and self._cw_zi is not None:
            audio, self._cw_zi = lfilter(self._cw_b, self._cw_a, audio, zi=self._cw_zi)
        return audio.astype(np.float32)

    # ------------------------------------------------------------------
    # Spectrum display
    # ------------------------------------------------------------------

    def _update_spectrum(self, audio: np.ndarray):
        if len(audio) > 256:
            fft = np.fft.fft(audio[:512])
            self.spectrum_data = np.abs(fft[:256])
            if self.on_spectrum_ready:
                self.on_spectrum_ready(self.id, self.spectrum_data.copy())

    def get_status(self) -> dict:
        return {
            'id':              self.id,
            'name':            self.settings.name,
            'frequency':       self.settings.frequency,
            'demod_mode':      self.settings.demod_mode,
            'volume':          self.settings.volume,
            'squelch_level':   self.settings.squelch_level,
            'squelch_enabled': self.settings.squelch_enabled,
            'enabled':         self.settings.enabled,
            'signal_strength': float(self.signal_strength),
            'is_active':       self.is_active,
            'bandwidth':       self.settings.bandwidth,
            'decoders':        [d.name for d in self.decoders],
        }


class VFOManager:
    """Manages multiple VFOs."""

    def __init__(self, center_freq: float, sample_rate: float, max_vfos: int = 10):
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.max_vfos    = max_vfos
        self.vfos:   Dict[int, VFO] = {}
        self._lock   = threading.RLock()
        self._next_id = 0

    def create_vfo(self, frequency: float, vfo_id: int = None) -> Optional[VFO]:
        with self._lock:
            if len(self.vfos) >= self.max_vfos:
                return None
            if vfo_id is None:
                vfo_id = self._next_id
            self._next_id  = max(self._next_id, vfo_id + 1)
            vfo            = VFO(vfo_id, self.center_freq, self.sample_rate)
            vfo.set_frequency(frequency)
            self.vfos[vfo_id] = vfo
            return vfo

    def delete_vfo(self, vfo_id: int) -> bool:
        with self._lock:
            if vfo_id in self.vfos:
                del self.vfos[vfo_id]
                return True
            return False

    def update_sample_rate(self, rate: float):
        self.sample_rate = rate
        with self._lock:
            for vfo in self.vfos.values():
                vfo.update_sample_rate(rate)

    def update_center_freq(self, freq: float):
        self.center_freq = freq
        with self._lock:
            for vfo in self.vfos.values():
                vfo.center_freq = freq

    def get_vfo(self, vfo_id: int) -> Optional[VFO]:
        return self.vfos.get(vfo_id)

    def get_all_vfos(self) -> List[VFO]:
        with self._lock:
            return list(self.vfos.values())

    def process_iq(self, iq_data: np.ndarray) -> Dict[int, Tuple[Optional[np.ndarray], list]]:
        results = {}
        with self._lock:
            for vfo_id, vfo in self.vfos.items():
                audio, dec = vfo.process_iq(iq_data.copy())
                results[vfo_id] = (audio, dec)
        return results

    def get_status_all(self) -> dict:
        with self._lock:
            return {vfo_id: vfo.get_status() for vfo_id, vfo in self.vfos.items()}
