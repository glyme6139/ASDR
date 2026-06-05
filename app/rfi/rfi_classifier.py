"""
RFI classifier: enriches scan_result signal dicts with band identification,
source guesses, and estimated power metrics, then logs new events to JSONL.

Power estimates require calibration (see rfi_sources.json → calibration.offset_db).
Without calibration, dBFS ≈ dBm is a rough approximation; voltage/current values
are proportional but not absolute.
"""

from __future__ import annotations

import json
import math
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent / "rfi_sources.json"


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
        self._cal_offset_db: float = 0.0
        self._antenna_ohms: float = 50.0
        self._log_enabled: bool = True
        self._log_dir: str = "logs/rfi"
        self._min_snr_db: float = 8.0
        self._dedup_hz: float = 50_000.0
        self._dedup_seconds: float = 60.0
        self._log_file = None
        self._dedup_cache: dict[int, float] = {}   # bucket → last logged time

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

        # Sort bands by start_hz for binary search
        self._bands = sorted(cfg.get("bands", []), key=lambda b: b.get("start_hz", 0))

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
            source_name       — Matched band name, or 'Unknown'
            likely_sources    — List of probable emitter descriptions
            rfi_risk          — 'low' | 'medium' | 'high' | 'unknown'
            category          — Band category string
            band_notes        — Human-readable note from config
            dbm_est           — Estimated dBm (needs calibration)
            power_mw_est      — Estimated power in milliwatts
            voltage_mv_est    — Estimated RMS voltage in millivolts (into antenna_impedance)
            current_ma_est    — Estimated RMS current in milliamps (into antenna_impedance)
        """
        now = time.time()
        events: list[dict] = []
        for sig in signals:
            band = self._match_band(sig.get("center_hz", 0.0))
            power = self._estimate_power(sig.get("peak_db", -120.0))
            event = {
                **sig,
                "timestamp": now,
                "source_name": band["name"] if band else "Unknown",
                "likely_sources": band.get("likely_sources", ["Unknown RFI source"]) if band else ["Unknown RFI source"],
                "rfi_risk": band.get("rfi_risk", "unknown") if band else "unknown",
                "category": band.get("category", "unknown") if band else "unknown",
                "band_notes": band.get("notes", "") if band else "",
                **power,
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
        Binary search for the narrowest band containing center_hz.
        When multiple bands overlap, returns the first match found.
        """
        bands = self._bands
        lo, hi = 0, len(bands) - 1
        best: Optional[dict] = None
        best_width = float("inf")

        while lo <= hi:
            mid = (lo + hi) // 2
            band = bands[mid]
            start = band.get("start_hz", 0)
            stop = band.get("stop_hz", 0)

            if center_hz < start:
                hi = mid - 1
            elif center_hz > stop:
                lo = mid + 1
            else:
                # Found a containing band — prefer narrower (more specific)
                width = stop - start
                if width < best_width:
                    best = band
                    best_width = width
                # Continue searching both directions for overlapping bands
                # (simple: just keep the one found, binary search is approximate for overlaps)
                break

        return best

    def _estimate_power(self, peak_db: float) -> dict:
        """
        Convert dBFS to rough electrical estimates.

        These are estimates only. Set calibration.offset_db in rfi_sources.json
        to compensate for your antenna + receiver gain chain.
        """
        dbm_est = peak_db + self._cal_offset_db
        # P_mW = 10^(dBm/10)
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

    def _dedup_bucket(self, center_hz: float) -> int:
        dz = max(self._dedup_hz, 1.0)
        return int(center_hz / dz)

    def _maybe_log(self, event: dict) -> None:
        if self._log_file is None:
            return
        bucket = self._dedup_bucket(event["center_hz"])
        now = event["timestamp"]
        if now - self._dedup_cache.get(bucket, 0.0) < self._dedup_seconds:
            return
        self._dedup_cache[bucket] = now
        try:
            self._log_file.write(json.dumps(event, default=float) + "\n")
            self._log_file.flush()
        except Exception as exc:
            logger.error("RFI log write error: %s", exc)
