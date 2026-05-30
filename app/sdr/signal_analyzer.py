"""
Signal analyzer subprocess.

Reads the FFT spectrum from the DSP shared-memory block (read-only, zero-copy),
finds connected regions above the estimated noise floor, characterises each
region (centre frequency, -6 dB bandwidth, peak power, SNR, coarse modulation
family) and forwards results to the UI process via a result queue.

Runs at ANALYZER_HZ (3 Hz) — well below the display rate so it cannot compete
with audio or spectrum rendering.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import List

import numpy as np

logger = logging.getLogger(__name__)

ANALYZER_HZ        = 3        # analysis rate (Hz)
_NOISE_PCTILE      = 20       # percentile used as noise-floor proxy
_THRESH_ABOVE      = 10.0     # dB above noise floor to open a detection region
_MIN_SNR_DB        = 6.0      # minimum SNR — filters residual noise bumps
_MIN_BW_HZ         = 500.0    # ignore spurs narrower than this (after merging)
_MAX_SIGNALS       = 20       # cap to avoid flooding the UI
_MIN_SIGNAL_GAP_HZ = 2_000    # merge adjacent regions within this gap (Hz)
_SMOOTH_BINS_MAX   = 50       # cap on smoothing kernel width (bins)


@dataclass
class DetectedSignal:
    center_hz:       float
    bandwidth_hz:    float
    peak_db:         float
    snr_db:          float
    modulation_hint: str


# ---------------------------------------------------------------------------
# Coarse modulation classification
# ---------------------------------------------------------------------------

def _guess_modulation(region_db: np.ndarray, bw_hz: float) -> str:
    """
    Return a coarse modulation-family label from a spectral region.

    Uses spectral flatness, carrier prominence and bandwidth as proxies.
    Deliberately simple — the goal is narrowing Artemis search results,
    not precise demodulation.
    """
    if len(region_db) < 3:
        return "Unknown"

    region_lin = np.power(10.0, region_db / 10.0)
    arith_mean = float(np.mean(region_lin))
    if arith_mean <= 0.0:
        return "Unknown"

    # Spectral flatness (Wiener entropy): 0 = pure tone, ~1 = white noise
    log_mean  = float(np.mean(np.log(region_lin + 1e-12)))
    geom_mean = float(np.exp(log_mean))
    flatness  = min(1.0, geom_mean / arith_mean)

    # Carrier prominence: peak of central 1/4 vs average of outer bins (dB)
    mid    = len(region_db) // 2
    margin = max(1, len(region_db) // 8)
    lo_i   = max(0, mid - margin)
    hi_i   = min(len(region_db), mid + margin)
    centre_peak = float(np.max(region_db[lo_i:hi_i]))
    sides       = np.concatenate([region_db[:lo_i], region_db[hi_i:]])
    sides_avg   = float(np.mean(sides)) if sides.size > 0 else centre_peak
    carrier_prom = centre_peak - sides_avg

    # Decision tree (intentionally coarse)
    if bw_hz < 1_200:
        return "CW / Beacon"
    if bw_hz < 6_000:
        if carrier_prom > 10 and flatness < 0.4:
            return "SSB / AM"
        return "Narrow Digital"
    if carrier_prom > 12 and flatness < 0.35 and bw_hz < 25_000:
        return "AM"
    if flatness > 0.65:
        return "Wideband Digital" if bw_hz > 80_000 else "Digital (FSK/PSK)"
    if bw_hz > 100_000:
        return "WFM"
    if bw_hz < 20_000:
        return "NFM"
    if bw_hz < 60_000:
        return "NFM / Digital"
    return "Analog"


# ---------------------------------------------------------------------------
# Spectrum analysis
# ---------------------------------------------------------------------------

def analyze_spectrum(
    spectrum_db:  np.ndarray,
    center_hz:    float,
    sample_rate:  float,
) -> List[DetectedSignal]:
    """
    Detect signals in a dB-scale FFT power spectrum.

    Args:
        spectrum_db : float32 ndarray (N,), DC-centred, values in dBFS.
        center_hz   : centre frequency of the spectrum (Hz).
        sample_rate : total span represented by the spectrum (Hz).

    Returns:
        List[DetectedSignal] (at most _MAX_SIGNALS entries).
    """
    n = len(spectrum_db)
    if n < 16:
        return []

    hz_per_bin = sample_rate / n

    # Sanitise: NaN / ±inf appear during startup before real data arrives
    spec = np.nan_to_num(spectrum_db, nan=-120.0, posinf=-20.0, neginf=-120.0)

    # Guard: shared memory not yet written (all float32 zeros).
    # Real FFT output is never exactly 0 because the DSP adds 1e-10 before log10.
    if not np.any(spec):
        return []

    # Smooth the spectrum before thresholding.
    # A Hann-tapered kernel targeting ~2 kHz in frequency space eliminates
    # intra-signal ripple (sidebands, modulation spectral shape) that would
    # otherwise split one physical signal into many detected regions.
    smooth_bins = min(_SMOOTH_BINS_MAX, max(3, int(2_000 / hz_per_bin)))
    kernel = np.hanning(smooth_bins).astype(np.float32)
    kernel /= kernel.sum()
    spec_smooth = np.convolve(spec, kernel, mode='same')

    noise_floor_db = float(np.percentile(spec_smooth, _NOISE_PCTILE))
    threshold_db   = noise_floor_db + _THRESH_ABOVE
    above          = spec_smooth > threshold_db

    # Collect raw connected regions from the smoothed spectrum
    raw_regions: List[List[int]] = []
    i = 0
    while i < n:
        if not above[i]:
            i += 1
            continue
        start = i
        while i < n and above[i]:
            i += 1
        raw_regions.append([start, i])

    if not raw_regions:
        return []

    # Merge regions whose gap is narrower than _MIN_SIGNAL_GAP_HZ.
    # This handles cases where spectral dips inside a single wideband signal
    # briefly fall below threshold, creating spurious splits.
    min_gap_bins = max(1, int(_MIN_SIGNAL_GAP_HZ / hz_per_bin))
    merged = [raw_regions[0][:]]
    for start, end in raw_regions[1:]:
        if start - merged[-1][1] <= min_gap_bins:
            merged[-1][1] = end   # extend previous region
        else:
            merged.append([start, end])

    # Characterise each merged region using the original (unsmoothed) spectrum
    results: List[DetectedSignal] = []
    for start, end in merged:
        if len(results) >= _MAX_SIGNALS:
            break

        region    = spec[start:end]
        raw_bw_hz = (end - start) * hz_per_bin
        if raw_bw_hz < _MIN_BW_HZ:
            continue

        # Power-weighted centre (more accurate than peak bin)
        power      = np.power(10.0, region / 10.0)
        total_pwr  = float(power.sum())
        bins       = np.arange(start, end, dtype=np.float64)
        centre_bin = float(np.dot(bins, power) / total_pwr) if total_pwr > 0 else (start + end) / 2.0
        centre_hz  = center_hz + (centre_bin - n / 2.0) * hz_per_bin

        # −6 dB bandwidth
        peak_db = float(region.max())
        idxs    = np.where(region >= peak_db - 6.0)[0]
        bw6_hz  = (int(idxs[-1]) - int(idxs[0]) + 1) * hz_per_bin if idxs.size else raw_bw_hz

        snr_db = peak_db - noise_floor_db
        if snr_db < _MIN_SNR_DB:
            continue

        results.append(DetectedSignal(
            center_hz       = centre_hz,
            bandwidth_hz    = bw6_hz,
            peak_db         = peak_db,
            snr_db          = snr_db,
            modulation_hint = _guess_modulation(region, bw6_hz),
        ))

    return results


# ---------------------------------------------------------------------------
# Subprocess entry point — module-level so Windows spawn can pickle it
# ---------------------------------------------------------------------------

def _analyzer_worker_main(
    cmd_q,
    result_q,
    spec_shm_name:    str,
    display_fft_size: int,
    disp_gen,          # mp.Value('L') — incremented by DSP worker each display update
    center_hz_val,     # mp.Value('d') — current centre frequency (Hz)
    sample_rate_val,   # mp.Value('d') — current sample rate (Hz)
):
    """
    Signal analyzer subprocess entry point.

    Attaches (read-only) to the spectrum shared-memory block written by the DSP
    subprocess, snapshots data whenever disp_gen advances, runs analyze_spectrum()
    and forwards results to result_q.
    """
    import logging
    from multiprocessing.shared_memory import SharedMemory

    logging.basicConfig(
        level=logging.INFO,
        format='[Analyzer %(process)d] %(levelname)s: %(message)s',
    )
    log = logging.getLogger(__name__)

    try:
        shm  = SharedMemory(name=spec_shm_name)
        spec = np.ndarray((display_fft_size,), dtype=np.float32, buffer=shm.buf)
    except Exception as exc:
        log.error("Cannot attach to spectrum shared memory: %s", exc)
        return

    interval  = 1.0 / ANALYZER_HZ
    last_gen  = -1
    running   = True
    next_tick = time.monotonic() + interval

    log.info("Signal analyzer started (%.1f Hz)", ANALYZER_HZ)

    while running:
        # Non-blocking drain of command queue
        while True:
            try:
                msg = cmd_q.get_nowait()
                if isinstance(msg, dict) and msg.get('cmd') == 'stop':
                    running = False
            except Exception:
                break

        if not running:
            break

        # Rate-limit
        now     = time.monotonic()
        sleep_s = next_tick - now
        if sleep_s > 0:
            time.sleep(sleep_s)
        next_tick = time.monotonic() + interval

        gen = int(disp_gen.value)
        if gen == last_gen:
            continue
        last_gen = gen

        try:
            snapshot    = spec.copy()
            center_hz   = float(center_hz_val.value)
            sample_rate = float(sample_rate_val.value)

            signals = analyze_spectrum(snapshot, center_hz, sample_rate)

            result_q.put_nowait({
                'type':    'scan_result',
                'signals': [
                    {
                        'center_hz':       s.center_hz,
                        'bandwidth_hz':    s.bandwidth_hz,
                        'peak_db':         s.peak_db,
                        'snr_db':          s.snr_db,
                        'modulation_hint': s.modulation_hint,
                    }
                    for s in signals
                ],
            })
        except Exception as exc:
            log.error("Analysis error: %s", exc, exc_info=True)

    try:
        shm.close()
    except Exception:
        pass
    log.info("Signal analyzer stopped")
