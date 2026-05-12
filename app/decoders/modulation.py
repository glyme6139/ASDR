"""
Best-effort digital modulation decoders.

These decoders are intentionally generic: they derive a compact feature stream
from incoming IQ or audio, quantize it, and expose the result as hex, binary,
and ASCII. The goal is to provide practical digital-modulation monitors for the
common families named in the UI, not protocol-accurate symbol recovery.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

import numpy as np

from .base import BaseDecoder, DecoderResult


MODULATION_DECODER_SPECS: Dict[str, Dict[str, Any]] = {
    "ASK": {"feature_mode": "envelope", "levels": 2},
    "APSK": {"feature_mode": "constellation", "levels": 8},
    "CPM": {"feature_mode": "phase_diff", "levels": 2},
    "FSK": {"feature_mode": "phase_diff", "levels": 2},
    "MFSK": {"feature_mode": "spectrum", "levels": 8},
    "MSK": {"feature_mode": "phase_diff", "levels": 2},
    "OOK": {"feature_mode": "envelope", "levels": 2},
    "PPM": {"feature_mode": "envelope", "levels": 4},
    "PSK": {"feature_mode": "phase", "levels": 4},
    "QAM": {"feature_mode": "constellation", "levels": 16},
    "SC-FDE": {"feature_mode": "constellation", "levels": 16},
    "TCM": {"feature_mode": "constellation", "levels": 8},
    "TC-PAM": {"feature_mode": "envelope", "levels": 4},
    "WDM": {"feature_mode": "spectrum", "levels": 8},
}

MODULATION_DECODER_NAMES = tuple(MODULATION_DECODER_SPECS.keys())


def _as_complex_iq(data: np.ndarray) -> Optional[np.ndarray]:
    if data is None or len(data) == 0:
        return None
    if np.iscomplexobj(data):
        return np.asarray(data, dtype=np.complex64)
    return None


def _as_float_audio(audio: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if audio is None or len(audio) == 0:
        return None
    return np.asarray(audio, dtype=np.float32)


def _resample_to(values: np.ndarray, target_len: int) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    if values.size == 0:
        return np.zeros(target_len, dtype=np.float32)
    if values.size == target_len:
        return values.astype(np.float32, copy=False)
    if values.size == 1:
        return np.full(target_len, float(values[0]), dtype=np.float32)

    src = np.linspace(0.0, 1.0, values.size, dtype=np.float32)
    dst = np.linspace(0.0, 1.0, target_len, dtype=np.float32)
    return np.interp(dst, src, values).astype(np.float32)


def _robust_normalize(values: np.ndarray) -> np.ndarray:
    values = np.nan_to_num(np.asarray(values, dtype=np.float32), copy=False)
    if values.size == 0:
        return values

    median = float(np.median(values))
    centered = values - median
    mad = float(np.median(np.abs(centered)))
    scale = mad * 1.4826 if mad > 1e-6 else float(np.std(values))
    if not np.isfinite(scale) or scale < 1e-6:
        scale = float(np.max(np.abs(centered)))
    if not np.isfinite(scale) or scale < 1e-6:
        scale = 1.0
    return centered / scale


def _extract_feature(iq: Optional[np.ndarray], audio: Optional[np.ndarray], mode: str) -> np.ndarray:
    if mode == "envelope":
        source = iq if iq is not None else audio
        if source is None:
            return np.zeros(0, dtype=np.float32)
        if np.iscomplexobj(source):
            feature = np.abs(source)
        else:
            feature = np.abs(np.asarray(source, dtype=np.float32))
        return np.asarray(feature, dtype=np.float32)

    if mode == "phase":
        if iq is None:
            return np.zeros(0, dtype=np.float32)
        phase = np.unwrap(np.angle(iq)).astype(np.float32)
        return _robust_normalize(phase)

    if mode == "phase_diff":
        if iq is None or len(iq) < 2:
            return np.zeros(0, dtype=np.float32)
        delta = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
        return _robust_normalize(delta)

    if mode == "constellation":
        if iq is None:
            source = audio
            if source is None:
                return np.zeros(0, dtype=np.float32)
            return _robust_normalize(np.asarray(source, dtype=np.float32))

        amp = _robust_normalize(np.abs(iq).astype(np.float32))
        phase = _robust_normalize(np.unwrap(np.angle(iq)).astype(np.float32))
        return _robust_normalize(0.7 * amp + 0.3 * phase)

    if mode == "spectrum":
        source = iq if iq is not None else audio
        if source is None or len(source) == 0:
            return np.zeros(0, dtype=np.float32)
        if np.iscomplexobj(source):
            window = np.hanning(len(source)).astype(np.float32)
            spec = np.abs(np.fft.rfft(source * window)).astype(np.float32)
        else:
            window = np.hanning(len(source)).astype(np.float32)
            spec = np.abs(np.fft.rfft(np.asarray(source, dtype=np.float32) * window)).astype(np.float32)
        return _robust_normalize(np.log1p(spec))

    raise ValueError(f"Unknown feature mode: {mode}")


def _quantize(values: np.ndarray, levels: int) -> np.ndarray:
    values = np.nan_to_num(np.asarray(values, dtype=np.float32), copy=False)
    if values.size == 0:
        return np.zeros(0, dtype=np.uint8)
    if levels <= 1:
        return np.zeros(values.size, dtype=np.uint8)

    thresholds = np.percentile(values, np.linspace(0, 100, levels + 1)[1:-1])
    thresholds = np.asarray(thresholds, dtype=np.float32)
    return np.digitize(values, thresholds, right=False).astype(np.uint8)


def _pack_symbols(symbols: np.ndarray, bits_per_symbol: int) -> bytes:
    if symbols.size == 0 or bits_per_symbol <= 0:
        return b""

    total_bits = int(symbols.size * bits_per_symbol)
    payload = bytearray((total_bits + 7) // 8)
    bit_index = 0

    for symbol in symbols.astype(np.uint8, copy=False):
        for shift in range(bits_per_symbol - 1, -1, -1):
            if symbol & (1 << shift):
                payload[bit_index // 8] |= 1 << (7 - (bit_index % 8))
            bit_index += 1

    return bytes(payload)


def _bytes_to_hex(payload: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in payload)


def _bytes_to_binary(payload: bytes) -> str:
    return " ".join(f"{byte:08b}" for byte in payload)


def _bytes_to_ascii(payload: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in payload)


class GenericModulationDecoder(BaseDecoder):
    """Best-effort digital modulation snapshot decoder."""

    def __init__(self, name: str, feature_mode: str, levels: int, frame_samples: int = 2048):
        super().__init__(name, sample_rate=48_000)
        self.feature_mode = feature_mode
        self.levels = max(2, int(levels))
        self.bits_per_symbol = max(1, int(round(math.log2(self.levels))))
        self.frame_samples = max(256, int(frame_samples))
        self._iq_buffer = np.zeros(0, dtype=np.complex64)
        self._audio_buffer = np.zeros(0, dtype=np.float32)

    def process(self, data: np.ndarray, audio: Optional[np.ndarray] = None) -> Optional[DecoderResult]:
        if not self.is_enabled:
            return None

        iq = _as_complex_iq(data)
        if iq is not None and iq.size:
            self._iq_buffer = np.concatenate([self._iq_buffer, iq])
            if self._iq_buffer.size > self.frame_samples * 4:
                self._iq_buffer = self._iq_buffer[-self.frame_samples * 4 :]

        audio_arr = _as_float_audio(audio)
        if audio_arr is not None and audio_arr.size:
            self._audio_buffer = np.concatenate([self._audio_buffer, audio_arr])
            if self._audio_buffer.size > self.frame_samples * 4:
                self._audio_buffer = self._audio_buffer[-self.frame_samples * 4 :]

        source_iq = self._iq_buffer if self._iq_buffer.size else None
        source_audio = self._audio_buffer if self._audio_buffer.size else None

        if self.feature_mode in {"phase", "phase_diff", "constellation", "spectrum"} and source_iq is not None and source_iq.size >= self.frame_samples:
            frame = source_iq[: self.frame_samples]
            self._iq_buffer = self._iq_buffer[self.frame_samples :]
            return self._decode_frame(frame, None)

        if source_audio is not None and source_audio.size >= self.frame_samples:
            frame = source_audio[: self.frame_samples]
            self._audio_buffer = self._audio_buffer[self.frame_samples :]
            return self._decode_frame(None, frame)

        if source_iq is not None and source_iq.size >= self.frame_samples:
            frame = source_iq[: self.frame_samples]
            self._iq_buffer = self._iq_buffer[self.frame_samples :]
            return self._decode_frame(frame, None)

        return None

    def reset(self):
        self._iq_buffer = np.zeros(0, dtype=np.complex64)
        self._audio_buffer = np.zeros(0, dtype=np.float32)

    def _decode_frame(self, iq: Optional[np.ndarray], audio: Optional[np.ndarray]) -> Optional[DecoderResult]:
        feature = _extract_feature(iq, audio, self.feature_mode)
        if feature.size == 0:
            return None

        symbol_count = 128
        sampled = _resample_to(feature, symbol_count)
        symbols = _quantize(sampled, self.levels)
        payload = _pack_symbols(symbols, self.bits_per_symbol)

        if not payload:
            return None

        now = time.time()
        hex_text = _bytes_to_hex(payload)
        binary_text = _bytes_to_binary(payload)
        ascii_text = _bytes_to_ascii(payload)

        return DecoderResult(
            decoder_name=self.name,
            timestamp=now,
            data={
                "family": self.name,
                "feature_mode": self.feature_mode,
                "levels": self.levels,
                "symbol_count": int(symbols.size),
                "hex": hex_text,
                "binary": binary_text,
                "ascii": ascii_text,
                "payload": payload,
                "symbols": symbols.tolist(),
            },
            confidence=min(0.99, 0.55 + float(np.clip(np.std(sampled), 0.0, 1.5)) / 3.0),
            metadata={
                "type": "digital_modulation",
                "feature_mode": self.feature_mode,
                "levels": self.levels,
            },
        )

    def format_result(self, result: DecoderResult) -> str:
        data = result.data if isinstance(result.data, dict) else {}
        hex_text = str(data.get("hex") or "")
        binary_text = str(data.get("binary") or "")
        ascii_text = str(data.get("ascii") or "")
        symbol_count = data.get("symbol_count")
        level_text = data.get("levels", self.levels)
        family = data.get("family", self.name)

        return (
            f"[{family}] levels={level_text} symbols={symbol_count} | "
            f"HEX: {hex_text} | BIN: {binary_text} | ASCII: {ascii_text}"
        )


def create_modulation_decoder(name: str) -> Optional[GenericModulationDecoder]:
    spec = MODULATION_DECODER_SPECS.get(name)
    if spec is None:
        return None
    return GenericModulationDecoder(name=name, **spec)
