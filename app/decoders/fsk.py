"""
FSK / AFSK decoder with clock recovery and character framing.

Two demodulation modes:
  fsk  — for FM-discriminated audio (NBFM VFO): smooth + threshold on instantaneous frequency
  afsk — for audio-tone-pair signals (USB/LSB VFO): coherent matched-filter detection

AFSK detection uses a per-symbol integrate-and-dump (I&D) matched filter for each tone:
  pm = |Σ x[n] · e^{-j2πf_mark·n/fs}|²   (over one symbol period)
  ps = |Σ x[n] · e^{-j2πf_space·n/fs}|²
  bit = 1 (mark) if pm > ps else 0 (space)

This is the optimal incoherent FSK detector: exact power comparison, zero group delay,
and ~3 dB from theoretical SNR limit for tone separation ≥ baud_rate.

Clock recovery (TLL) is used only for direct FSK; AFSK detection produces one symbol per
symbol period inherently so no separate TLL is needed.

Character framing: standard async serial — idle=mark(1), start=space(0), N data bits
LSB-first, 1–1.5 stop bits mark(1).

Supported encodings:
  ascii   — 7/8-bit ASCII (Bell 202, Bell 103, generic FSK)
  baudot  — 5-bit ITA2/Baudot (RTTY)
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

import numpy as np

from .base import BaseAudioDecoder, DecoderResult

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ITA2 / Baudot lookup tables
# ---------------------------------------------------------------------------

_BAUDOT_LETTERS = (
    '\x00', 'E',  '\n', 'A',  ' ',  'S',  'I',  'U',
    '\r',   'D',  'R',  'J',  'N',  'F',  'C',  'K',
    'T',    'Z',  'L',  'W',  'H',  'Y',  'P',  'Q',
    'O',    'B',  'G',  '\x00', 'M', 'X', 'V',  '\x00',
)
_BAUDOT_FIGURES = (
    '\x00', '3',  '\n', '-',  ' ',  "'",  '8',  '7',
    '\r',   '\x00', '4', '\x07', ',', '!', ':', '(',
    '5',    '"',  ')',  '2',  '#',  '6',  '0',  '1',
    '9',    '?',  '&',  '\x00', '.', '/', '=',  '\x00',
)
_FIGS_CODE = 27   # shift to figures
_LTRS_CODE = 31   # shift to letters


def _decode_baudot_char(code: int, in_figures: bool) -> Tuple[str, bool]:
    code &= 0x1F
    if code == _FIGS_CODE:
        return '', True
    if code == _LTRS_CODE:
        return '', False
    tbl = _BAUDOT_FIGURES if in_figures else _BAUDOT_LETTERS
    ch = tbl[code]
    if ord(ch) < 32 and ch not in ('\n', '\r', ' '):
        ch = ''
    return ch, in_figures


# ---------------------------------------------------------------------------
# Transition-locked loop (TLL) symbol clock recovery — used only for FSK mode
# ---------------------------------------------------------------------------

class _TLL:
    """
    Transition-locked loop: tracks bit-edge transitions to maintain symbol
    phase, then decimates the per-sample bit stream to one bit per symbol.

    Same algorithm as _POCSAGSingleBaudTS — transitions should occur when
    clockPhase ≈ sps/2; correction is applied on each edge.
    """

    def __init__(self, sps: float, alpha: float = 0.10):
        self.sps   = sps
        self.alpha = alpha
        self._phase = 0.0
        self._last  = 0

    def push(self, decisions: np.ndarray) -> List[int]:
        """Feed per-sample bit decisions (0/1). Returns recovered symbols."""
        out  = []
        sps  = self.sps
        half = sps * 0.5
        for d in decisions:
            d = int(d)
            if d != self._last:
                err = self._phase - half
                if err >  half: err -= sps
                if err < -half: err += sps
                if abs(err) < sps * 0.40:
                    self._phase -= err * self.alpha
            self._last = d
            self._phase += 1.0
            if self._phase >= sps:
                self._phase -= sps
                out.append(d)
        return out

    def reset(self):
        self._phase = 0.0
        self._last  = 0


# ---------------------------------------------------------------------------
# Async serial character framer
# ---------------------------------------------------------------------------

class _AsyncFramer:
    """
    Recovers async serial characters from a recovered-symbol stream.

    Frame: idle(1*) | start(0) | data[0..N-1] LSB-first | stop(1)
    """

    def __init__(self, char_bits: int = 8, stop_bits: float = 1.0):
        self.char_bits = char_bits
        self.stop_bits = stop_bits
        self._state  = 'idle'
        self._count  = 0
        self._accum: List[int] = []

    def push(self, symbols: List[int]) -> List[int]:
        chars = []
        for s in symbols:
            if self._state == 'idle':
                if s == 0:                   # start bit
                    self._state = 'data'
                    self._count = 0
                    self._accum = []
            elif self._state == 'data':
                if self._count < self.char_bits:
                    self._accum.append(s)
                    self._count += 1
                else:
                    if s == 1:               # valid stop bit → emit character
                        val = sum(b << i for i, b in enumerate(self._accum))
                        chars.append(val)
                    self._state = 'idle'
        return chars

    def reset(self):
        self._state = 'idle'
        self._count = 0
        self._accum = []


# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------

PRESETS: dict = {
    'RTTY-45':  {'baud': 45.45,  'mark': 2125.0, 'space': 2295.0, 'mode': 'afsk', 'char_bits': 5, 'encoding': 'baudot', 'stop_bits': 1.5},
    'RTTY-75':  {'baud': 75.0,   'mark': 2125.0, 'space': 2295.0, 'mode': 'afsk', 'char_bits': 5, 'encoding': 'baudot', 'stop_bits': 1.5},
    'Bell202':  {'baud': 1200.0, 'mark': 1200.0, 'space': 2200.0, 'mode': 'afsk', 'char_bits': 8, 'encoding': 'ascii',  'stop_bits': 1.0},
    'Bell103':  {'baud': 300.0,  'mark': 1270.0, 'space': 1070.0, 'mode': 'afsk', 'char_bits': 8, 'encoding': 'ascii',  'stop_bits': 1.0},
    'FSK-300':  {'baud': 300.0,  'mark': 1800.0, 'space': 1600.0, 'mode': 'fsk',  'char_bits': 8, 'encoding': 'ascii',  'stop_bits': 1.0},
    'FSK-1200': {'baud': 1200.0, 'mark': 1800.0, 'space': 1200.0, 'mode': 'fsk',  'char_bits': 8, 'encoding': 'ascii',  'stop_bits': 1.0},
    'FSK-2400': {'baud': 2400.0, 'mark': 1800.0, 'space': 1200.0, 'mode': 'fsk',  'char_bits': 8, 'encoding': 'ascii',  'stop_bits': 1.0},
}

FSK_PRESET_NAMES = tuple(PRESETS.keys())


# ---------------------------------------------------------------------------
# Main decoder
# ---------------------------------------------------------------------------

class FSKDecoder(BaseAudioDecoder):
    """
    FSK / AFSK decoder.

    NBFM VFO → fsk mode  : FM-discriminated audio, positive = mark, negative = space
    USB/LSB VFO → afsk   : audio tone pair; coherent matched-filter per symbol period
    """

    def __init__(
        self,
        sample_rate: int  = 48_000,
        preset:      str  = 'Bell202',
        invert:      bool = False,
    ):
        super().__init__('FSK', sample_rate)
        self.invert   = invert
        self._figures = False
        self._raw_bits: List[int] = []
        self._MAX_BITS = 256

        # placeholders — filled by configure()
        self.baud       = 1200.0
        self.mark_freq  = 1200.0
        self.space_freq = 2200.0
        self.mode       = 'afsk'
        self.char_bits  = 8
        self.encoding   = 'ascii'
        self.stop_bits  = 1.0

        self._tll     = _TLL(self.sample_rate / self.baud)
        self._framer  = _AsyncFramer(self.char_bits, self.stop_bits)

        # AFSK coherent-detection buffer (samples from previous chunk that didn't
        # fill a complete symbol period)
        self._afsk_buf = np.zeros(0, dtype=np.float64)

        # Measurement: accumulate audio and run analysis every ~1s
        self._meas_buf     = np.zeros(0, dtype=np.float32)
        self._meas_buf_max = sample_rate          # 1 second of audio
        self._last_meas_t  = 0.0
        self._MEAS_INTERVAL = 1.0                 # seconds between analysis runs
        self._pending_meas: dict = {}             # cached result waiting to be emitted

        self.configure({'preset': preset})

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, params: dict) -> None:
        """Apply preset and/or per-field overrides at runtime."""
        cfg: dict = {}
        preset_name = params.get('preset')
        if preset_name and preset_name in PRESETS:
            cfg.update(PRESETS[preset_name])

        for key in ('baud', 'mark', 'space', 'mode', 'char_bits', 'encoding', 'stop_bits', 'invert'):
            if key in params:
                cfg[key] = params[key]

        if 'baud'      in cfg: self.baud       = float(cfg['baud'])
        if 'mark'      in cfg: self.mark_freq  = float(cfg['mark'])
        if 'space'     in cfg: self.space_freq = float(cfg['space'])
        if 'mode'      in cfg: self.mode       = str(cfg['mode'])
        if 'char_bits' in cfg: self.char_bits  = int(cfg['char_bits'])
        if 'encoding'  in cfg: self.encoding   = str(cfg['encoding'])
        if 'stop_bits' in cfg: self.stop_bits  = float(cfg['stop_bits'])
        if 'invert'    in cfg: self.invert     = bool(cfg['invert'])

        sps = self.sample_rate / self.baud
        self._tll    = _TLL(sps)
        self._framer = _AsyncFramer(self.char_bits, self.stop_bits)
        self._figures = False
        self._afsk_buf  = np.zeros(0, dtype=np.float64)
        self._meas_buf  = np.zeros(0, dtype=np.float32)
        self._pending_meas = {}

        # Pre-compute per-symbol reference waveforms for AFSK detection
        if self.mode == 'afsk':
            self._build_afsk_refs()

    def _build_afsk_refs(self) -> None:
        """Pre-compute one-symbol-period complex reference vectors for I&D detection."""
        sps = int(self.sample_rate / self.baud)
        t   = np.arange(sps) / float(self.sample_rate)
        self._ref_mark  = np.exp(-2j * np.pi * self.mark_freq  * t)
        self._ref_space = np.exp(-2j * np.pi * self.space_freq * t)

    # ------------------------------------------------------------------
    # BaseAudioDecoder interface
    # ------------------------------------------------------------------

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        try:
            audio = np.asarray(audio, dtype=np.float32)
            if audio.size == 0:
                return None

            # Accumulate audio for the measurement engine
            self._meas_buf = np.concatenate([self._meas_buf, audio])
            if len(self._meas_buf) > self._meas_buf_max:
                self._meas_buf = self._meas_buf[-self._meas_buf_max:]

            # Run measurement periodically once we have enough audio
            now = time.time()
            if (now - self._last_meas_t >= self._MEAS_INTERVAL
                    and len(self._meas_buf) >= self.sample_rate // 4):
                self._last_meas_t = now
                self._pending_meas = self._run_measurement()

            if self.mode == 'afsk':
                symbols = self._decode_afsk_symbols(audio)
                if self.invert:
                    symbols = [1 - s for s in symbols]
            else:
                decisions = self._decide_fsk(audio)
                if self.invert:
                    decisions = 1 - decisions
                symbols = self._tll.push(decisions)

            self._raw_bits.extend(symbols)
            if len(self._raw_bits) > self._MAX_BITS:
                self._raw_bits = self._raw_bits[-self._MAX_BITS:]

            chars = self._framer.push(symbols) if symbols else []
            text, hex_str = self._decode_chars(chars) if chars else ('', '')

            meas = self._pending_meas
            self._pending_meas = {}

            # Always emit a result when measurement data is ready (even with no chars)
            if not chars and not meas:
                return None

            return DecoderResult(
                decoder_name='FSK',
                timestamp=now,
                data={
                    'text':       text,
                    'hex':        hex_str,
                    'baud':       self.baud,
                    'mode':       self.mode,
                    'encoding':   self.encoding,
                    'mark_freq':  self.mark_freq,
                    'space_freq': self.space_freq,
                    'bits':       ''.join(str(b) for b in self._raw_bits),
                    'char_count': len(chars),
                    'meas':       meas,
                },
                confidence=0.7 if chars else 0.0,
                metadata={'type': 'fsk', 'mode': self.mode, 'baud': self.baud},
            )
        except Exception as exc:
            logger.debug('FSK decode error: %s', exc)
            return None

    def reset(self) -> None:
        self._tll.reset()
        self._framer.reset()
        self._figures    = False
        self._raw_bits   = []
        self._afsk_buf   = np.zeros(0, dtype=np.float64)
        self._meas_buf   = np.zeros(0, dtype=np.float32)
        self._pending_meas = {}
        if self.mode == 'afsk':
            self._build_afsk_refs()

    # ------------------------------------------------------------------
    # Demodulation
    # ------------------------------------------------------------------

    def _decode_afsk_symbols(self, audio: np.ndarray) -> List[int]:
        """
        Coherent integrate-and-dump AFSK detector.

        For each symbol window: correlate the audio with a complex sinusoid at
        the mark and space frequencies.  |correlation|² gives the incoherent
        power estimate; the larger one determines the bit.

        Because the correlation magnitude is independent of the carrier phase
        within each window (only the frequency matters), this works correctly
        even for continuous-phase signals and across chunk boundaries.
        """
        sps   = int(self.sample_rate / self.baud)
        buf   = np.concatenate([self._afsk_buf, audio.astype(np.float64)])
        n_sym = len(buf) // sps

        symbols: List[int] = []
        ref_m = self._ref_mark
        ref_s = self._ref_space

        for i in range(n_sym):
            chunk = buf[i * sps : (i + 1) * sps]
            pm    = float(abs(np.dot(chunk, ref_m)) ** 2)
            ps    = float(abs(np.dot(chunk, ref_s)) ** 2)
            symbols.append(1 if pm > ps else 0)

        self._afsk_buf = buf[n_sym * sps:]
        return symbols

    def _decide_fsk(self, audio: np.ndarray) -> np.ndarray:
        """
        Direct FSK: FM-discriminated audio, positive = mark (1), negative = space (0).
        Smooth over ~1/4 symbol then threshold at zero.
        """
        win      = max(2, int(self.sample_rate / self.baud / 4))
        k        = np.ones(win, dtype=np.float32) / win
        smoothed = np.convolve(audio.astype(np.float32), k, mode='same')
        return (smoothed > 0.0).astype(np.uint8)

    # ------------------------------------------------------------------
    # Character decoding
    # ------------------------------------------------------------------

    def _decode_chars(self, chars: List[int]) -> Tuple[str, str]:
        text_parts: List[str] = []
        if self.encoding == 'baudot':
            for code in chars:
                ch, self._figures = _decode_baudot_char(code, self._figures)
                if ch and ch != '\r':
                    text_parts.append(ch)
        else:
            for code in chars:
                b = code & 0x7F
                if 32 <= b <= 126:
                    text_parts.append(chr(b))
                elif b in (10, 13):
                    text_parts.append('\n')

        text    = ''.join(text_parts)
        hex_str = ' '.join(f'{c:02X}' for c in chars)
        return text, hex_str

    # ------------------------------------------------------------------
    # Symbol-rate measurement
    # ------------------------------------------------------------------

    # Common baud rates to try during the sweep, coarse then fine
    _BAUD_CANDIDATES = [
        45.45, 50, 57.6, 75, 100, 110, 134.5, 150, 200, 300,
        600, 1200, 2400, 4800, 9600,
    ]

    def _run_measurement(self) -> dict:
        """
        Estimate baud rate and tone frequencies from the audio buffer.

        Method:
          1. Raw audio PSD (100–4 500 Hz) for display and tone-frequency
             estimation (find the two largest spectral peaks).
          2. Coherent I&D baud-rate sweep: for each candidate baud rate,
             reshape the audio into symbol-sized windows, correlate each
             window with mark/space complex exponentials, and compute the
             mean |pm − ps|.  The correct baud rate produces the clearest
             symbol decisions → highest score.
          3. Fine search ±20% around the coarse winner (30 steps).
        """
        try:
            audio = self._meas_buf
            if len(audio) < self.sample_rate // 4:   # need at least 0.25 s
                return {}

            fs  = float(self.sample_rate)
            aud = audio.astype(np.float64)

            # ── 1. Raw audio PSD for display ─────────────────────────────
            n_fft  = min(16384, len(aud))
            win    = np.hanning(n_fft)
            spec   = np.abs(np.fft.rfft(aud[:n_fft] * win)) ** 2
            freqs  = np.fft.rfftfreq(n_fft, 1.0 / fs)

            mask   = (freqs >= 100) & (freqs <= 4500)
            psd_db = 10.0 * np.log10(spec[mask] + 1e-12)
            f_hz   = freqs[mask]
            if len(f_hz) > 512:
                idx    = np.round(np.linspace(0, len(f_hz) - 1, 512)).astype(int)
                psd_db = psd_db[idx];  f_hz = f_hz[idx]

            # ── 2. Tone frequency estimates (two dominant peaks) ─────────
            # Exclusion zone: small enough to keep close tones (e.g. RTTY 170 Hz)
            # but large enough to suppress the skirt of the first peak.
            # Use fixed 10 bins (~86 Hz at our compressed resolution).
            excl   = max(3, min(len(psd_db) // 30, 10))
            p_copy = psd_db.copy()
            p1     = int(np.argmax(p_copy))
            p_copy[max(0, p1 - excl): p1 + excl + 1] = -np.inf
            p2     = int(np.argmax(p_copy))
            if p2 < p1:
                p1, p2 = p2, p1
            f_mark_est  = float(f_hz[p1])
            f_space_est = float(f_hz[p2])

            # ── 3. Coherent I&D baud-rate sweep ──────────────────────────
            def _id_score(baud_hz: float) -> float:
                sps = int(round(fs / baud_hz))
                if sps < 4:
                    return 0.0
                n_win = len(aud) // sps
                if n_win < 4:
                    return 0.0
                mat   = aud[: n_win * sps].reshape(n_win, sps)
                t     = np.arange(sps) / fs
                ref_m = np.exp(-2j * np.pi * f_mark_est  * t)
                ref_s = np.exp(-2j * np.pi * f_space_est * t)
                pm    = np.abs(mat @ ref_m) ** 2
                ps    = np.abs(mat @ ref_s) ** 2
                # Normalise by sps² so the score peaks at the correct baud rate
                # rather than growing monotonically for slower rates.
                return float(np.mean(np.abs(pm - ps))) / (sps ** 2)

            # Coarse pass
            coarse   = [(b, _id_score(b)) for b in self._BAUD_CANDIDATES
                        if fs / b >= 4]
            if not coarse:
                return {}
            best_b, best_s = max(coarse, key=lambda x: x[1])

            # Fine pass: ±20 % around coarse winner (30 log-spaced steps)
            lo, hi  = best_b * 0.80, best_b * 1.20
            fine_bs = np.geomspace(lo, hi, 30)
            fine    = [(b, _id_score(b)) for b in fine_bs]
            best_b, best_s = max(fine, key=lambda x: x[1])

            # Halve-down: the I&D score plateaus for baud ≥ baud_true because
            # sub-symbol windows always fit inside one symbol and score the same.
            # Walk down by ×2 while the score stays ≥ 90 % of the current best.
            while best_b / 2 >= 10:
                half_s = _id_score(best_b / 2)
                if half_s >= 0.90 * best_s:
                    best_b /= 2
                    best_s  = half_s
                else:
                    break

            baud_est = round(best_b, 1)

            # ── 4. I&D score curve for the bottom plot ───────────────────
            # Use 60 log-spaced candidates across 40–12 000 Hz for the plot
            plot_bauds = np.geomspace(40, 12000, 60)
            plot_scores = [_id_score(b) for b in plot_bauds
                           if int(round(fs / b)) >= 4]
            plot_bauds_out = [b for b in plot_bauds
                              if int(round(fs / b)) >= 4]

            return {
                'baud_est':    baud_est,
                'mark_est':    round(f_mark_est,  1),
                'space_est':   round(f_space_est, 1),
                'dev_est':     round(abs(f_space_est - f_mark_est) / 2, 1),
                'psd_db':      psd_db.tolist(),
                'psd_freqs':   f_hz.tolist(),
                'score_curve': plot_scores,
                'score_bauds': plot_bauds_out,
                'null_freq':   baud_est,
            }

        except Exception as exc:
            logger.debug('FSK measurement error: %s', exc)
            return {}

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def format_result(self, result: DecoderResult) -> str:
        data    = result.data if isinstance(result.data, dict) else {}
        text    = str(data.get('text', '')).strip()
        baud    = data.get('baud', '?')
        mode    = data.get('mode', '?')
        enc     = data.get('encoding', '')
        tag     = f'FSK {mode}@{baud}' + (f' {enc}' if enc else '')
        return f'[{tag}] {text}' if text else f'[{tag}] {data.get("hex", "")}'
