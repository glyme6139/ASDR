"""
ACARS (Aircraft Communications Addressing and Reporting System) decoder.

VHF frequencies: 129.125, 130.025, 131.550 MHz (US), 136.900 MHz (EU primary)
Modulation: AM — select AM demod mode on the VFO.
Data: 2400 bps NRZ-I (NRZI) FSK subcarrier
  Mark  = 2400 Hz  → decoded bit 1 (no tone transition)
  Space = 1200 Hz  → decoded bit 0 (tone transition)
Character encoding: 7-bit ASCII, LSB-first, odd-parity 8th bit.
"""
import time
import logging
import numpy as np
from typing import Optional, Tuple
from collections import deque

from .base import BaseAudioDecoder, DecoderResult

logger = logging.getLogger(__name__)

_BAUD        = 2400
_MARK_HZ     = 2400
_SPACE_HZ    = 1200
_AUDIO_RATE  = 48_000
_SPB         = _AUDIO_RATE // _BAUD   # 20 samples per bit

# ACARS control codes
_SOH = 0x01
_STX = 0x02
_ETX = 0x03
_DEL = 0x7F

# Wire encoding of 0x2B ('+', used as ACARS sync char):
#   0x2B = 0b00101011 → bit0..6 = [1,1,0,1,0,1,0] → 4 ones (even) → parity=1
#   Wire (LSB-first + parity): [1,1,0,1,0,1,0,1]
_SYN_WIRE = np.array([1, 1, 0, 1, 0, 1, 0, 1], dtype=np.uint8)


def _decode_char(bits: np.ndarray, offset: int) -> Tuple[Optional[int], bool]:
    """Decode one 8-bit wire character (LSB-first + odd parity) at *offset*."""
    if offset + 8 > len(bits):
        return None, False
    b = bits[offset:offset + 8]
    val = int(sum(int(b[i]) << i for i in range(7)))
    parity_ok = (int(b[7]) == (1 - bin(val).count('1') % 2))
    return val, parity_ok


def _crc16_acars(data: bytes) -> int:
    """CRC-16 CCITT (poly 0x1021, init 0x0000) over *data*."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
    return crc & 0xFFFF


class ACARSDecoder(BaseAudioDecoder):
    """
    Decode ACARS from 48 kHz AM-demodulated audio.

    Tries all 20 possible clock-phase offsets and both NRZI initial states on
    each audio block, so it locks on automatically within one frame period.
    """

    def __init__(self, sample_rate: int = _AUDIO_RATE):
        super().__init__('ACARS', sample_rate)
        n = round(sample_rate / _BAUD)
        self._n = n
        t = np.arange(n) / sample_rate
        self._mark  = np.cos(2 * np.pi * _MARK_HZ  * t).astype(np.float32)
        self._space = np.cos(2 * np.pi * _SPACE_HZ * t).astype(np.float32)
        self._samp: np.ndarray = np.zeros(0, dtype=np.float32)
        self._pending: deque = deque()

    # ------------------------------------------------------------------ #
    #  BaseAudioDecoder interface                                          #
    # ------------------------------------------------------------------ #

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        self._samp = np.concatenate([self._samp, audio.astype(np.float32)])
        result = self._process()
        # Prevent unbounded growth when no signal is present (~2 s of audio)
        max_samp = self._n * 3000
        if len(self._samp) > max_samp:
            self._samp = self._samp[-max_samp // 2:]
        return result

    def reset(self):
        self._samp = np.zeros(0, dtype=np.float32)
        self._pending.clear()

    def format_result(self, result: DecoderResult) -> str:
        d = result.data if isinstance(result.data, dict) else {}
        parts = []
        reg = d.get('registration', '').strip()
        if reg:
            parts.append(reg)
        label = d.get('label', '').strip()
        if label:
            parts.append(f'[{label}]')
        flight = d.get('flight', '').strip()
        if flight and flight != reg:
            parts.append(flight)
        text = str(d.get('text', '')).strip()
        if text:
            parts.append(text)
        return ' '.join(parts) if parts else str(d)

    # ------------------------------------------------------------------ #
    #  Core demodulation                                                   #
    # ------------------------------------------------------------------ #

    def _process(self) -> Optional[DecoderResult]:
        n    = self._n
        samp = self._samp
        N    = len(samp)

        if N < n * 60:   # need at least 60 bits buffered
            return None

        for phase in range(n):
            K = (N - phase) // n
            if K < 50:
                continue

            # Reshape into K × n windows at this clock phase (view, no copy)
            chunk  = samp[phase: phase + K * n].reshape(K, n)
            mark_e = chunk @ self._mark    # K
            spc_e  = chunk @ self._space   # K
            raw    = (mark_e > spc_e).astype(np.uint8)

            # Try both NRZI initial states (handles signal polarity ambiguity)
            for init in (0, 1):
                prev    = np.empty(K, dtype=np.uint8)
                prev[0] = init
                prev[1:] = raw[:-1]
                decoded = (raw == prev).astype(np.uint8)

                frame, consumed_bits = self._search(decoded)
                if frame is not None:
                    # Advance sample buffer past the decoded frame
                    self._samp = samp[phase + consumed_bits * n:]
                    return DecoderResult(
                        decoder_name='ACARS',
                        timestamp=time.time(),
                        data=frame,
                        confidence=1.0,
                        metadata={'type': 'acars'},
                    )
        return None

    # ------------------------------------------------------------------ #
    #  Frame search & parsing                                              #
    # ------------------------------------------------------------------ #

    def _search(self, bits: np.ndarray) -> Tuple[Optional[dict], int]:
        """Scan *bits* for SYN+SYN+SOH, parse frame. Returns (frame, bits_consumed)."""
        N = len(bits)
        syn = _SYN_WIRE
        i   = 0
        while i <= N - 24:
            # Quick scalar reject on first bit before the expensive array_equal
            if bits[i] == syn[0] and np.array_equal(bits[i:i + 8], syn):
                if np.array_equal(bits[i + 8:i + 16], syn):
                    frame, advance = self._parse(bits, i + 16)
                    if frame is not None:
                        return frame, i + 16 + advance
            i += 1
        return None, 0

    def _parse(self, bits: np.ndarray, pos0: int) -> Tuple[Optional[dict], int]:
        """
        Parse ACARS frame from *bits* starting at *pos0* (just after SYN+SYN).
        Returns (frame_dict, bits_consumed_from_pos0) or (None, 0).
        """
        pos = pos0

        def rd() -> Tuple[Optional[int], bool]:
            nonlocal pos
            v, ok = _decode_char(bits, pos)
            if v is not None:
                pos += 8
            return v, ok

        # SOH
        soh, _ = rd()
        if soh != _SOH:
            return None, 0

        # Mode character
        mode, _ = rd()
        if mode is None:
            return None, 0

        # Aircraft registration: 7 characters (dot-padded tail number)
        reg_bytes: list = []
        for _ in range(7):
            c, _ = rd()
            if c is None:
                return None, 0
            reg_bytes.append(c)
        registration = ''.join(
            chr(c) if 0x20 <= c <= 0x7E else '' for c in reg_bytes
        ).strip('.').strip()

        # Technical ACK / NAK
        ack, _ = rd()
        if ack is None:
            return None, 0

        # Label: 2 characters (ARINC message type)
        label_bytes: list = []
        for _ in range(2):
            c, _ = rd()
            if c is None:
                return None, 0
            label_bytes.append(c)
        label = ''.join(chr(c) if 0x20 <= c <= 0x7E else '?' for c in label_bytes)

        # Block ID: 1 character ('0'-'9', 'A'-'Z')
        bid, _ = rd()
        if bid is None:
            return None, 0

        # STX
        stx, _ = rd()
        if stx != _STX:
            return None, 0

        # Collect CRC-scoped bytes: Mode … STX
        crc_scope = bytearray([mode] + reg_bytes + [ack] + label_bytes + [bid, _STX])

        # Message text: until ETX or DEL (max 220 chars per ARINC 618)
        text_chars: list = []
        end_ctrl: Optional[int] = None
        for _ in range(220):
            c, _ = rd()
            if c is None:
                return None, 0
            crc_scope.append(c)
            if c in (_ETX, _DEL):
                end_ctrl = c
                break
            if 0x20 <= c <= 0x7E:
                text_chars.append(chr(c))
            elif c in (0x0A, 0x0D):
                text_chars.append('\n')

        if end_ctrl is None:
            return None, 0   # frame truncated

        # CRC bytes (2 × 7-bit characters)
        crc1, _ = rd()
        crc2, _ = rd()

        # Optional CRC check — accept frame regardless (structural match is primary)
        if crc1 is not None and crc2 is not None:
            rx_crc = (crc1 << 7) | crc2
            calc_crc = _crc16_acars(bytes(crc_scope))
            # Low 14 bits comparison (ACARS packs CRC into two 7-bit chars)
            crc_ok = (rx_crc & 0x3FFF) == (calc_crc & 0x3FFF)
        else:
            crc_ok = False

        text = ''.join(text_chars).strip()

        frame: dict = {
            'mode':         chr(mode) if 0x20 <= mode <= 0x7E else '?',
            'registration': registration,
            'label':        label,
            'block_id':     chr(bid) if 0x20 <= bid <= 0x7E else '?',
            'text':         text,
            'crc_ok':       crc_ok,
            'ack':          ack,
        }

        # Flight ID sometimes embedded as "REG-FLIGHT" in the registration field
        if '-' in registration:
            parts = registration.split('-', 1)
            frame['registration'] = parts[0]
            frame['flight']       = parts[1]

        return frame, pos - pos0
