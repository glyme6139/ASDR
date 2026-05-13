"""
TETRA ACELP 4.8 kbps speech codec wrapper.

Supports two backend DLLs:
  1. tetraVoiceDec  — from SDRSharp TETRA plugin (tetraVoiceDec.dll)
       API: tetra_decode_init() → void*
            tetra_cdec(int fp, uint8* inp, int16* outp, int hs) → void
            tetra_sdec(int16* inp, int16* outp, void* chStruct) → void
  2. ETSI reference  — from osmo-tetra + ETSI EN 300 395-2 source patches
       API: speech_decode_frame(uint8* bits, int16* pcm) → int

Build tetraVoiceDec.dll:
  See SDRSharp TETRA plugin repo (it ships a pre-built DLL).
  Place tetraVoiceDec.dll next to the executable or on PATH.

Input per frame  : 137 bits (uint8 values 0/1) — post-reordering
Output per frame : 240 int16 samples @ 8 000 Hz (30 ms)
One NDB burst    : 2 ACELP frames → 480 PCM samples
"""

import sys
import ctypes
import numpy as np
import logging

logger = logging.getLogger(__name__)

FRAME_BITS  = 137
PCM_SAMPLES = 240
SAMPLE_RATE = 8_000

_SILENCE = np.zeros(PCM_SAMPLES, dtype=np.int16)

# Library search order (without extension)
_LIB_CANDIDATES = [
    'tetraVoiceDec',
    'libtetraVoiceDec',
    './tetraVoiceDec',
    'tetra_codec',
    'libtetra_codec',
    './tetra_codec',
    './libtetra_codec',
]


class TetraCodec:
    """
    Lazy-loading ctypes wrapper.  Tries tetraVoiceDec API first (SDRSharp
    variant), then falls back to ETSI reference symbol names.
    Returns silence on every decode() call when no library is found.
    """

    def __init__(self):
        self._ctx      = None          # void* from tetra_decode_init
        self._cdec_fn  = None          # tetra_cdec (SDRSharp API)
        self._sdec_fn  = None          # tetra_sdec (SDRSharp API)
        self._etsi_fn  = None          # speech_decode_frame (ETSI API)
        self._buf      = (ctypes.c_int16 * PCM_SAMPLES)()
        self._try_load()

    @property
    def available(self) -> bool:
        return (self._cdec_fn is not None) or (self._etsi_fn is not None)

    def decode(self, bits: np.ndarray) -> np.ndarray:
        """Decode one 137-bit ACELP frame → 240 int16 PCM @ 8 000 Hz."""
        if not self.available or len(bits) < FRAME_BITS:
            return _SILENCE.copy()
        try:
            if self._cdec_fn is not None:
                return self._decode_sdrsharp(bits)
            return self._decode_etsi(bits)
        except Exception as exc:
            logger.debug("tetra_codec decode error: %s", exc)
            return _SILENCE.copy()

    # ── private ──────────────────────────────────────────────────────────────

    def _decode_sdrsharp(self, bits: np.ndarray) -> np.ndarray:
        packed = (ctypes.c_uint8 * FRAME_BITS)(*bits[:FRAME_BITS].astype(np.uint8))
        # tetra_cdec(fp=0, inp, outp, hs=0)
        self._cdec_fn(ctypes.c_int(0), packed, self._buf, ctypes.c_int(0))
        return np.frombuffer(self._buf, dtype=np.int16).copy()

    def _decode_etsi(self, bits: np.ndarray) -> np.ndarray:
        packed = bits[:FRAME_BITS].astype(np.uint8)
        self._etsi_fn(packed.ctypes.data_as(ctypes.c_char_p), self._buf)
        return np.frombuffer(self._buf, dtype=np.int16).copy()

    def _try_load(self):
        suffix = '.dll' if sys.platform == 'win32' else '.so'
        for lib_name in _LIB_CANDIDATES:
            path = lib_name if lib_name.endswith(suffix) else lib_name + suffix
            try:
                lib = ctypes.CDLL(path)
            except OSError:
                continue

            # Try tetraVoiceDec API (SDRSharp)
            if self._try_sdrsharp_api(lib, path):
                return

            # Try ETSI reference API
            for fn_name in ('speech_decode_frame', 'tetra_speech_decode', 'sp_dec', 'decode'):
                try:
                    fn = getattr(lib, fn_name)
                    fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int16)]
                    fn.restype  = ctypes.c_int
                    self._etsi_fn = fn
                    logger.info("TETRA codec: ETSI API '%s' → '%s'", path, fn_name)
                    return
                except AttributeError:
                    continue

        logger.info(
            "TETRA ACELP codec not found — voice output will be silent.\n"
            "  Place tetraVoiceDec%s (SDRSharp plugin) in the working directory.", suffix
        )

    def _try_sdrsharp_api(self, lib, path: str) -> bool:
        """Try to bind the SDRSharp tetraVoiceDec API."""
        try:
            init_fn = lib.tetra_decode_init
            init_fn.argtypes = []
            init_fn.restype  = ctypes.c_void_p
            ctx = init_fn()

            cdec = lib.tetra_cdec
            cdec.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_int16),
                ctypes.c_int,
            ]
            cdec.restype = None

            sdec = lib.tetra_sdec
            sdec.argtypes = [
                ctypes.POINTER(ctypes.c_int16),
                ctypes.POINTER(ctypes.c_int16),
                ctypes.c_void_p,
            ]
            sdec.restype = None

            self._ctx     = ctx
            self._cdec_fn = cdec
            self._sdec_fn = sdec
            logger.info("TETRA codec: SDRSharp API loaded from '%s'", path)
            return True
        except AttributeError:
            return False


_instance: TetraCodec | None = None


def get_codec() -> TetraCodec:
    global _instance
    if _instance is None:
        _instance = TetraCodec()
    return _instance
