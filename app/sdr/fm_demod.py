"""
High-performance FM demodulator with proven realtime pipeline.

Implements the DSP chain from testpocsag_realtime.py which successfully decodes
POCSAG in real-time:

  IQ → Channel LPF → Decimate ×10 → Resample to 50kHz IF → DC Blocker
    → atan2(Q,I) FM Demod → Post-demod LPF → Resample to 48kHz → Audio

This matches BrowSDR/SDR++ processor.rs architecture.
"""

import logging
import numpy as np
from scipy.signal import firwin, lfilter, resample_poly
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FMDemodConfig:
    """Configuration for FM demodulator."""
    sample_rate: float = 240_000  # Input sample rate (after pre-decimation)
    audio_rate: float = 48_000    # Output audio rate
    if_rate: float = 50_000       # Intermediate IF rate
    bandwidth: float = 12_500     # Channel bandwidth
    channel_lpf_cutoff: float = 12_000  # Hz
    post_demod_lpf_cutoff: float = 6_000  # Hz
    decim_factor: int = 1         # Additional decimation factor (for full-rate inputs)


class FMDemod:
    """
    Stateful FM demodulator using atan2-based discriminator at IF rate.
    
    Preserves filter state and phase across process() calls for continuous
    demodulation of streaming data.
    """

    def __init__(self, config: FMDemodConfig = None):
        if config is None:
            config = FMDemodConfig()
        self.cfg = config

        # Calculate FM deviation (half of bandwidth)
        self.fm_deviation = config.bandwidth / 2.0

        # Build channel LPF (at input sample rate).
        # Clamp normalized cutoff to valid (0, 1) range to avoid SciPy errors
        norm_ch_cut = float(config.channel_lpf_cutoff) / (config.sample_rate / 2.0)
        clamped_ch = False
        if norm_ch_cut < 1e-4:
            norm_ch_cut = 1e-4
            clamped_ch = True
        elif norm_ch_cut > 0.99:
            norm_ch_cut = 0.99
            clamped_ch = True
        self.ch_lpf_taps = firwin(128, norm_ch_cut)
        self.ch_lpf_zi_i = np.zeros(len(self.ch_lpf_taps) - 1, dtype=np.float32)
        self.ch_lpf_zi_q = np.zeros(len(self.ch_lpf_taps) - 1, dtype=np.float32)
        logger.debug(
            "FMDemod init: sample_rate=%s if_rate=%s audio_rate=%s bandwidth=%s "
            "channel_lpf_cutoff=%s post_demod_lpf_cutoff=%s decim=%s",
            config.sample_rate, config.if_rate, config.audio_rate, config.bandwidth,
            config.channel_lpf_cutoff, config.post_demod_lpf_cutoff, config.decim_factor,
        )
        if clamped_ch:
            logger.debug("FMDemod: channel LPF normalized cutoff was clamped to %s", norm_ch_cut)

        # Build post-demod LPF (at IF rate). Clamp to valid normalized range.
        norm_post_cut = float(config.post_demod_lpf_cutoff) / (config.if_rate / 2.0)
        clamped_post = False
        if norm_post_cut < 1e-4:
            norm_post_cut = 1e-4
            clamped_post = True
        elif norm_post_cut > 0.99:
            norm_post_cut = 0.99
            clamped_post = True
        self.post_dem_lpf_taps = firwin(64, norm_post_cut)
        self.post_dem_lpf_zi = np.zeros(len(self.post_dem_lpf_taps) - 1, dtype=np.float32)
        if clamped_post:
            logger.debug("FMDemod: post-demod LPF normalized cutoff was clamped to %s", norm_post_cut)

        # FM discriminator normalization: phase_diff * (1 / (2π * deviation))
        self.inv_deviation = 1.0 / (2.0 * np.pi * self.fm_deviation / config.if_rate)

        # Filter state
        self.dc_alpha_if = 1.0 - (10.0 / config.if_rate)
        self.dc_avg_i = 0.0
        self.dc_avg_q = 0.0
        self.prev_phase_fm = 0.0

        # Compute resample ratios dynamically based on actual input rate
        # input_rate -> IF_RATE
        def _compute_ratio(in_rate, out_rate):
            from math import gcd as _gcd
            i = int(round(out_rate))
            j = int(round(in_rate))
            d = _gcd(i, j)
            return i // d, j // d

        self.resamp_if_up, self.resamp_if_down = _compute_ratio(config.sample_rate, config.if_rate)
        self.resamp_audio_up, self.resamp_audio_down = _compute_ratio(config.if_rate, config.audio_rate)
        logger.debug(
            "FMDemod resample ratios: sample->if %s/%s    if->audio %s/%s",
            self.resamp_if_up, self.resamp_if_down,
            self.resamp_audio_up, self.resamp_audio_down,
        )

    def process(self, iq_data: np.ndarray) -> np.ndarray:
        """
        Demodulate IQ data and return 48 kHz audio.
        
        Args:
            iq_data: Complex64 IQ samples at sample_rate (240 kS/s by default)
            
        Returns:
            Float32 audio samples at 48 kHz
        """
        if len(iq_data) == 0:
            logger.debug("FMDemod.process called with empty buffer")
            return np.array([], dtype=np.float32)

        logger.debug("FMDemod.process: in_len=%d dtype=%s sample_rate=%s", len(iq_data), iq_data.dtype, self.cfg.sample_rate)

        # ─── Stage 1: Channel LPF ───
        iq_i = np.real(iq_data)
        iq_q = np.imag(iq_data)
        iq_i_filt, self.ch_lpf_zi_i = lfilter(self.ch_lpf_taps, 1.0, iq_i, zi=self.ch_lpf_zi_i)
        iq_q_filt, self.ch_lpf_zi_q = lfilter(self.ch_lpf_taps, 1.0, iq_q, zi=self.ch_lpf_zi_q)
        iq_filt = iq_i_filt + 1j * iq_q_filt

        # ─── Stage 2: Decimate ×10 ───
        iq_dec = iq_filt[::self.cfg.decim_factor]
        logger.debug("FMDemod: after decim factor=%s decimated_len=%d", self.cfg.decim_factor, len(iq_dec))

        # ─── Stage 3: Rational Resample (240 kS → 50 kHz IF) ───
        try:
            iq_i_if = resample_poly(np.real(iq_dec), self.resamp_if_up, self.resamp_if_down,
                                     window=('kaiser', 5.0))
            iq_q_if = resample_poly(np.imag(iq_dec), self.resamp_if_up, self.resamp_if_down,
                                     window=('kaiser', 5.0))
        except Exception as e:
            logger.exception("FMDemod: resample_poly to IF failed: %s", e)
            raise
        iq_if = iq_i_if + 1j * iq_q_if
        logger.debug("FMDemod: after resample to IF len=%d", len(iq_i_if))

        # ─── Stage 4: DC Blocker ───
        for i in range(len(iq_if)):
            i_val = iq_if[i].real
            q_val = iq_if[i].imag
            self.dc_avg_i = self.dc_avg_i * self.dc_alpha_if + i_val * (1.0 - self.dc_alpha_if)
            self.dc_avg_q = self.dc_avg_q * self.dc_alpha_if + q_val * (1.0 - self.dc_alpha_if)
            iq_if[i] = (i_val - self.dc_avg_i) + 1j * (q_val - self.dc_avg_q)

        # ─── Stage 5: FM Quadrature Discriminator ───
        fm_audio = np.zeros(len(iq_if), dtype=np.float32)
        for i in range(len(iq_if)):
            cur_phase = np.arctan2(iq_if[i].imag, iq_if[i].real)
            phase_diff = cur_phase - self.prev_phase_fm

            # Normalize phase difference to [-π, π]
            if phase_diff > np.pi:
                phase_diff -= 2.0 * np.pi
            elif phase_diff <= -np.pi:
                phase_diff += 2.0 * np.pi

            fm_audio[i] = phase_diff * self.inv_deviation
            self.prev_phase_fm = cur_phase

        # ─── Stage 6: Post-Demod LPF ───
        try:
            fm_audio_filt, self.post_dem_lpf_zi = lfilter(self.post_dem_lpf_taps, 1.0,
                                                           fm_audio, zi=self.post_dem_lpf_zi)
        except Exception as e:
            logger.exception("FMDemod: post-demod lfilter failed: %s", e)
            raise
        logger.debug("FMDemod: after post-demod lfilter len=%d", len(fm_audio_filt))

        # ─── Stage 7: Resample to 48 kHz Audio ───
        try:
            audio = resample_poly(fm_audio_filt, self.resamp_audio_up, self.resamp_audio_down,
                                  window=('kaiser', 5.0))
        except Exception as e:
            logger.exception("FMDemod: resample_poly to audio failed: %s", e)
            raise
        logger.debug("FMDemod: final audio len=%d", len(audio))

        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def reset(self):
        """Reset all filter state and phase tracking."""
        self.ch_lpf_zi_i = np.zeros(len(self.ch_lpf_taps) - 1, dtype=np.float32)
        self.ch_lpf_zi_q = np.zeros(len(self.ch_lpf_taps) - 1, dtype=np.float32)
        self.post_dem_lpf_zi = np.zeros(len(self.post_dem_lpf_taps) - 1, dtype=np.float32)
        self.dc_avg_i = 0.0
        self.dc_avg_q = 0.0
        self.prev_phase_fm = 0.0
