"""
TETRA ACELP 4.8 kbps codec wrapper.

The TETRA speech codec is the ETSI EN 300 395-2 reference implementation.
osmo-tetra provides patches for it in etsi_codec-patches/ but the source
must be downloaded from ETSI separately (proprietary reference code):

    cd etsi_codec-patches
    ./download_and_patch.sh          # downloads + patches → ../codec/
    cd ../codec && make              # builds the patched ETSI sources

The resulting object files must be linked into a shared library alongside
the osmo-tetra lower_mac code (viterbi_tch.c, tch_reordering.c, etc.).
No pre-built DLL/SO is distributed — you must build it yourself.

Input per frame  : 137 bits (uint8, values 0/1) — post-reordering
Output per frame : 240 int16 samples at 8 000 Hz (30 ms)

One NDB burst (B1 + B2 = 432 raw bits) yields 2 ACELP frames (274 bits
after Viterbi decoding → reordering → 2 × 137 bits), so one burst call
produces 2 × 240 = 480 PCM samples.
"""

import sys
import ctypes
import numpy as np
import logging

logger = logging.getLogger(__name__)

FRAME_BITS   = 137    # ETSI ACELP input length per 30 ms frame
PCM_SAMPLES  = 240    # 30 ms @ 8 000 Hz
SAMPLE_RATE  = 8_000

_SILENCE = np.zeros(PCM_SAMPLES, dtype=np.int16)

# Library search names without extension (added per platform below).
# Build your own shared library from osmo-tetra + patched ETSI codec and
# place it here or on the system library search path.
_LIB_CANDIDATES = [
    'tetra_codec',
    'libtetra_codec',
    './tetra_codec',
    './libtetra_codec',
]

# Candidate function names from the ETSI reference decoder.
# The patched ETSI source exposes a decode entry point; the exact symbol
# name depends on how it was compiled.  Add your symbol here if it differs.
_FN_CANDIDATES = [
    'tetra_speech_decode',   # osmo-tetra wrapper name (if added)
    'speech_decode_frame',   # common ETSI reference name
    'sp_dec',                # alternative ETSI name
    'decode',                # generic name
]


class TetraCodec:
    """
    Lazy-loading ctypes wrapper for the TETRA ACELP 4.8 kbps decoder.

    Tries every combination of library candidate × function candidate at
    load time; the first working pair is kept.  Returns silence on every
    decode() call when the library is unavailable.
    """

    def __init__(self):
        self._fn  = None
        self._buf = (ctypes.c_int16 * PCM_SAMPLES)()
        self._try_load()

    @property
    def available(self) -> bool:
        return self._fn is not None

    def decode(self, bits: np.ndarray) -> np.ndarray:
        """
        Decode one 137-bit TETRA ACELP frame → 240 int16 PCM @ 8 000 Hz.
        Returns a silent frame when the codec library is not loaded.
        """
        if self._fn is None or len(bits) < FRAME_BITS:
            return _SILENCE.copy()
        try:
            packed = bits[:FRAME_BITS].astype(np.uint8)
            self._fn(packed.ctypes.data_as(ctypes.c_char_p), self._buf)
            return np.frombuffer(self._buf, dtype=np.int16).copy()
        except Exception as exc:
            logger.debug("tetra_codec decode error: %s", exc)
            return _SILENCE.copy()

    # ── private ──────────────────────────────────────────────────────────

    def _try_load(self):
        suffix = '.dll' if sys.platform == 'win32' else '.so'
        for lib_name in _LIB_CANDIDATES:
            path = lib_name if lib_name.endswith(suffix) else lib_name + suffix
            try:
                lib = ctypes.CDLL(path)
            except OSError:
                continue
            for fn_name in _FN_CANDIDATES:
                try:
                    fn = getattr(lib, fn_name)
                    fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int16)]
                    fn.restype  = ctypes.c_int
                    self._fn = fn
                    logger.info("TETRA codec: loaded '%s' → '%s'", path, fn_name)
                    return
                except AttributeError:
                    continue

        logger.info(
            "TETRA ACELP codec library not found — voice output will be silent.\n"
            "  1. cd etsi_codec-patches && ./download_and_patch.sh\n"
            "  2. Build a shared library from codec/ + src/lower_mac/ sources\n"
            "  3. Place tetra_codec%s in the working directory.",
            suffix,
        )


_instance: TetraCodec | None = None


def get_codec() -> TetraCodec:
    global _instance
    if _instance is None:
        _instance = TetraCodec()
    return _instance
