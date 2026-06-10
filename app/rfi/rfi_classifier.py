"""
RFI classifier: enriches scan_result signal dicts with band identification,
source guesses, and estimated power metrics, then logs new events to JSONL.

Power estimates require calibration (see rfi_sources.json → calibration.offset_db).
Without calibration, dBFS ≈ dBm is a rough approximation; voltage/current values
are proportional but not absolute.
"""

from __future__ import annotations

import bisect
import json
import math
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent / "rfi_sources.json"

# dBm thresholds for human-readable signal strength labels
_STRENGTH_THRESHOLDS = [
    (-40, "very strong"),
    (-60, "strong"),
    (-80, "moderate"),
    (-100, "weak"),
    (float("-inf"), "very weak"),
]


def _fmt_hz(hz: float) -> str:
    """Format a frequency as a compact human-readable string."""
    if hz >= 1e9:
        return f"{hz / 1e9:.3f} GHz".rstrip("0").rstrip(".")
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz".rstrip("0").rstrip(".")
    if hz >= 1e3:
        return f"{hz / 1e3:.1f} kHz"
    return f"{hz:.0f} Hz"


def _strength_label(dbm: float) -> str:
    for threshold, label in _STRENGTH_THRESHOLDS:
        if dbm >= threshold:
            return label
    return "very weak"


class RFIClassifier:
    """
    Classifies detected signals against a configurable frequency-band database.

    Usage:
        clf = RFIClassifier()                  # uses bundled rfi_sources.json
        clf = RFIClassifier("my_config.json")  # custom config

        events = clf.classify(scan_result_signals)  # list[dict]
        clf.close()   # flush and close the log file
    """

    def __init__(self, config_path: Path | str | None = None):
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG
        self._bands: list[dict] = []
        self._band_starts: list[float] = []  # parallel list for bisect
        self._cal_offset_db: float = 0.0
        self._antenna_ohms: float = 50.0
        self._log_enabled: bool = True
        self._log_dir: str = "logs/rfi"
        self._min_snr_db: float = 8.0
        self._dedup_hz: float = 50_000.0
        self._dedup_seconds: float = 60.0
        self._log_file = None
        self._dedup_cache: dict[int, float] = {}  # bucket → last logged time

        self._load_config()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        try:
            with open(self._config_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            logger.warning("RFI config not found at %s — using defaults", self._config_path)
            cfg = {}
        except Exception as exc:
            logger.error("Failed to parse RFI config: %s", exc)
            cfg = {}

        cal = cfg.get("calibration", {})
        self._cal_offset_db = float(cal.get("offset_db", 0.0))
        self._antenna_ohms = float(cal.get("antenna_impedance_ohms", 50.0))

        log_cfg = cfg.get("logging", {})
        self._log_enabled = bool(log_cfg.get("enabled", True))
        self._log_dir = str(log_cfg.get("log_dir", "logs/rfi"))
        self._min_snr_db = float(log_cfg.get("min_snr_db", 8.0))
        self._dedup_hz = float(log_cfg.get("deduplicate_hz", 50_000.0))
        self._dedup_seconds = float(log_cfg.get("deduplicate_seconds", 60.0))

        # Sort by start_hz; keep a parallel list of start values for bisect
        self._bands = sorted(cfg.get("bands", []), key=lambda b: b.get("start_hz", 0))
        self._band_starts = [b.get("start_hz", 0) for b in self._bands]

        if self._log_enabled and self._log_file is None:
            self._open_log_file()

    def _open_log_file(self) -> None:
        try:
            log_dir = Path(self._log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            path = log_dir / f"rfi_{ts}.jsonl"
            self._log_file = open(path, "w", encoding="utf-8")
            logger.info("RFI log: %s", path)
        except Exception as exc:
            logger.error("Cannot open RFI log file: %s", exc)
            self._log_file = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, signals: list[dict]) -> list[dict]:
        """
        Enrich each signal dict from scan_result with RFI fields.

        Input dict keys (from SignalAnalyzerProcess):
            center_hz, bandwidth_hz, peak_db, snr_db, modulation_hint

        Added keys:
            timestamp         — Unix time of classification
            frequency_label   — Human-readable frequency, e.g. "433.92 MHz"
            band_id           — Matched band id from config, or None
            source_name       — Matched band name, or 'Unknown'
            likely_sources    — List of probable emitter descriptions
            rfi_risk          — 'low' | 'medium' | 'high' | 'unknown'
            category          — Band category string
            band_notes        — Human-readable note from config
            dbm_est           — Estimated dBm (needs calibration)
            power_mw_est      — Estimated power in milliwatts
            voltage_mv_est    — Estimated RMS voltage in millivolts (into antenna_impedance)
            current_ma_est    — Estimated RMS current in milliamps (into antenna_impedance)
            signal_strength   — 'very strong' | 'strong' | 'moderate' | 'weak' | 'very weak'
            summary           — One-line human-readable description for UI display
        """
        now = time.time()
        events: list[dict] = []
        for sig in signals:
            center_hz = sig.get("center_hz", 0.0)
            band = self._match_band(center_hz)
            power = self._estimate_power(sig.get("peak_db", -120.0))
            event = {
                **sig,
                "timestamp": now,
                "frequency_label": _fmt_hz(center_hz),
                "band_id": band.get("id") if band else None,
                "source_name": band["name"] if band else "Unknown",
                "likely_sources": band.get("likely_sources", ["Unknown RFI source"]) if band else ["Unknown RFI source"],
                "rfi_risk": band.get("rfi_risk", "unknown") if band else "unknown",
                "category": band.get("category", "unknown") if band else "unknown",
                "band_notes": band.get("notes", "") if band else "",
                **power,
                "signal_strength": _strength_label(power["dbm_est"]),
                "summary": self._build_summary(sig, band, power),
            }
            events.append(event)
            if sig.get("snr_db", 0.0) >= self._min_snr_db:
                self._maybe_log(event)
        return events

    def reload_config(self) -> None:
        """Re-read rfi_sources.json without restarting the application."""
        old_file = self._log_file
        self._log_file = None
        if old_file:
            try:
                old_file.close()
            except Exception:
                pass
        self._dedup_cache.clear()
        self._load_config()

    def close(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _match_band(self, center_hz: float) -> Optional[dict]:
        """
        Return the narrowest configured band that contains center_hz.

        Bands are sorted by start_hz. bisect_right finds the first index where
        start_hz > center_hz, so everything before that has start_hz ≤ center_hz.
        We scan left from there, picking the matching band with the smallest width.

        This correctly handles overlapping bands (e.g. a broad 5–30 MHz RFI zone
        containing a narrower 14–14.35 MHz amateur band) — the narrower, more
        specific band is always preferred.
        """
        best: Optional[dict] = None
        best_width = float("inf")
        idx = bisect.bisect_right(self._band_starts, center_hz) - 1
        while idx >= 0:
            band = self._bands[idx]
            stop = band.get("stop_hz", 0)
            if stop >= center_hz:
                width = stop - band.get("start_hz", 0)
                if width < best_width:
                    best = band
                    best_width = width
            idx -= 1
        return best

    def _estimate_power(self, peak_db: float) -> dict:
        """
        Convert dBFS to rough electrical estimates.

        These are estimates only. Set calibration.offset_db in rfi_sources.json
        to compensate for your antenna + receiver gain chain.
        """
        dbm_est = peak_db + self._cal_offset_db
        try:
            p_mw = 10.0 ** (dbm_est / 10.0)
        except OverflowError:
            p_mw = float("inf")
        p_w = p_mw * 1e-3
        r = max(self._antenna_ohms, 1.0)
        v_v = math.sqrt(max(0.0, p_w * r))
        i_a = v_v / r
        return {
            "dbm_est": round(dbm_est, 1),
            "power_mw_est": round(p_mw, 4),
            "voltage_mv_est": round(v_v * 1000.0, 4),
            "current_ma_est": round(i_a * 1000.0, 6),
        }

    def _build_summary(self, sig: dict, band: Optional[dict], power: dict) -> str:
        """One-line description suitable for UI display."""
        parts = [_fmt_hz(sig.get("center_hz", 0.0))]
        bw_hz = sig.get("bandwidth_hz", 0.0)
        if bw_hz:
            parts.append(f"BW {_fmt_hz(bw_hz)}")
        mod = sig.get("modulation_hint", "")
        if mod:
            parts.append(f"[{mod}]")
        name = band["name"] if band else "Unknown band"
        snr = sig.get("snr_db")
        snr_str = f" SNR {snr:.1f} dB" if snr is not None else ""
        return f"{' '.join(parts)} — {name}{snr_str} ({power['dbm_est']:.0f} dBm est.)"

    def _dedup_bucket(self, center_hz: float) -> int:
        return int(center_hz / max(self._dedup_hz, 1.0))

    def _prune_dedup_cache(self, now: float) -> None:
        cutoff = now - self._dedup_seconds
        stale = [k for k, t in self._dedup_cache.items() if t < cutoff]
        for k in stale:
            del self._dedup_cache[k]

    def _maybe_log(self, event: dict) -> None:
        if self._log_file is None:
            return
        bucket = self._dedup_bucket(event["center_hz"])
        now = event["timestamp"]
        if now - self._dedup_cache.get(bucket, 0.0) < self._dedup_seconds:
            return
        self._dedup_cache[bucket] = now
        if len(self._dedup_cache) > 500:
            self._prune_dedup_cache(now)
        try:
            self._log_file.write(json.dumps(event, default=float) + "\n")
            self._log_file.flush()
        except Exception as exc:
            logger.error("RFI log write error: %s", exc)
