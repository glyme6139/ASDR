"""
POCSAG (Post Office Code Standardisation Advisory Group) decoder
Decodes paging networks for POCSAG messages
"""
import numpy as np
from typing import Optional, Callable, Dict, List, Any
from scipy import signal
import time
import logging
from .base import BaseAudioDecoder, DecoderResult

logger = logging.getLogger(__name__)


class POCSAGDecoder(BaseAudioDecoder):
    """POCSAG paging network decoder"""
    
    FRAME_LENGTH = 544  # bits: sync word + 16 payload codewords
    SYNC_WORD = 0x7CD215D8
    IDLE_WORD = 0x7A89C197
    
    def __init__(self, sample_rate: int = 48000, baud_rate: int = 1200, debug: bool = False):
        super().__init__("POCSAG", sample_rate)
        self.baud_rate = baud_rate
        self.frame_time = self.FRAME_LENGTH / float(self.baud_rate)
        self.sample_buffer = np.zeros(0, dtype=np.float32)
        self.symbol_buffer = np.zeros(0, dtype=np.int8)
        self.frame_count = 0
        self.last_frame_time = time.time()
        # use rounded integer samples per symbol for timing
        self.samples_per_symbol = int(round(sample_rate / self.baud_rate))
        self.debug = debug
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode POCSAG from audio"""
        try:
            if audio.size == 0:
                return None

            # Keep sample continuity across chunks so symbol slicing does not
            # restart at every callback boundary.
            audio = audio.astype(np.float32, copy=False)
            self.sample_buffer = np.concatenate([self.sample_buffer, audio])

            if len(self.sample_buffer) < self.samples_per_symbol:
                return None

            # Simple smoothing on the sample stream helps suppress discriminator noise.
            win = max(1, self.samples_per_symbol // 6)
            if win > 1 and len(self.sample_buffer) >= win:
                kernel = np.ones(win, dtype=np.float32) / win
                smoothed = np.convolve(self.sample_buffer, kernel, mode='same').astype(np.float32)
            else:
                smoothed = self.sample_buffer

            symbol_count = len(smoothed) // self.samples_per_symbol
            usable = symbol_count * self.samples_per_symbol
            if symbol_count == 0:
                return None

            trimmed = smoothed[:usable]
            remainder = smoothed[usable:]
            self.sample_buffer = remainder.copy() if remainder.size else np.zeros(0, dtype=np.float32)

            symbol_means_arr = trimmed.reshape(symbol_count, self.samples_per_symbol).mean(axis=1)
            threshold = 0.0
            symbols = (symbol_means_arr > threshold).astype(np.int8)

            if self.debug:
                density = float(np.mean(symbols)) if symbols.size else 0.0
                logger.debug(
                    "POCSAG: baud=%d audio_samples=%d sym_blocks=%d sym_thresh=%.6f density=%.3f sample_remainder=%d",
                    self.baud_rate,
                    len(audio),
                    len(symbols),
                    threshold,
                    density,
                    len(self.sample_buffer),
                )

            if symbols.size:
                self.symbol_buffer = np.concatenate([self.symbol_buffer, symbols])

            # Search for sync and decode complete batches.
            while len(self.symbol_buffer) >= self.FRAME_LENGTH:
                sync_pos = self._find_sync_word(self.symbol_buffer)
                if sync_pos is None:
                    # keep a small tail in case sync spans chunks
                    self.symbol_buffer = self.symbol_buffer[-31:]
                    return None

                if sync_pos > 0:
                    self.symbol_buffer = self.symbol_buffer[sync_pos:]
                    if len(self.symbol_buffer) < self.FRAME_LENGTH:
                        return None

                batch = self.symbol_buffer[:self.FRAME_LENGTH]
                result = self._decode_batch(batch)
                self.symbol_buffer = self.symbol_buffer[self.FRAME_LENGTH:]
                if result:
                    return result
            
            return None
            
        except Exception as e:
            logger.error(f"POCSAG decode error: {e}")
            return None
    
    def _find_sync_word(self, bits: np.ndarray) -> Optional[int]:
        """Return the first bit offset where the POCSAG sync word appears."""
        if len(bits) < 32:
            return None

        target = self.SYNC_WORD
        limit = len(bits) - 32 + 1
        for offset in range(limit):
            word = 0
            for i in range(32):
                word = (word << 1) | int(bits[offset + i] & 1)
            if word == target:
                return offset
        return None

    def _decode_batch(self, batch_bits: np.ndarray) -> Optional[DecoderResult]:
        """Decode one 576-bit batch consisting of sync + 16 codewords."""
        try:
            if len(batch_bits) < self.FRAME_LENGTH:
                return None

            first_32 = 0
            for i in range(32):
                first_32 = (first_32 << 1) | int(batch_bits[i] & 1)

            if first_32 != self.SYNC_WORD:
                if self.debug:
                    logger.debug(
                        "POCSAG: sync mismatch first32=0x%08X expected=0x%08X",
                        first_32,
                        self.SYNC_WORD,
                    )
                return None
            
            self.frame_count += 1

            current_address = None
            current_func = None
            message_parts = []
            decoded_messages = []

            for cw_idx in range(16):
                start = 32 + cw_idx * 32
                codeword = 0
                for i in range(32):
                    codeword = (codeword << 1) | int(batch_bits[start + i] & 1)

                if codeword == self.IDLE_WORD:
                    if current_address is not None and message_parts:
                        decoded_messages.append(self._finalize_message(current_address, current_func, message_parts))
                        current_address = None
                        current_func = None
                        message_parts = []
                    continue

                if not self._check_even_parity(codeword):
                    continue

                is_data = (codeword >> 31) & 0x1
                if is_data == 0:
                    if current_address is not None and message_parts:
                        decoded_messages.append(self._finalize_message(current_address, current_func, message_parts))

                    current_address, current_func = self._decode_address_codeword(codeword, cw_idx)
                    message_parts = []
                else:
                    part = self._decode_data_codeword(codeword, current_func)
                    if part:
                        message_parts.append(part)

            if current_address is not None and message_parts:
                decoded_messages.append(self._finalize_message(current_address, current_func, message_parts))

            if decoded_messages:
                logger.debug(
                    "Decoded POCSAG frame %d: messages=%s",
                    self.frame_count,
                    decoded_messages,
                )
                return DecoderResult(
                    decoder_name="POCSAG",
                    timestamp=time.time(),
                    data={
                        'frame': self.frame_count,
                        'sync_word': f"0x{self.SYNC_WORD:08X}",
                        'messages': decoded_messages,
                    },
                    confidence=0.95,
                    metadata={'type': 'paging'},
                )
        
        except Exception as e:
            logger.debug(f"Frame decode error: {e}")
        
        return None
    
    def _check_even_parity(self, codeword: int) -> bool:
        """POCSAG uses even parity across all 32 bits."""
        return (bin(codeword).count('1') & 1) == 0

    def _decode_address_codeword(self, codeword: int, codeword_index: int) -> tuple:
        """Decode a POCSAG address codeword."""
        try:
            address_bits = (codeword >> 13) & 0x3FFFF
            func = (codeword >> 11) & 0x3
            frame_slot = (codeword_index // 2) & 0x7
            address = (address_bits << 3) | frame_slot
            return address, func
        except Exception:
            return None, None

    def _decode_data_codeword(self, codeword: int, current_func: Optional[int]) -> Optional[str]:
        """Decode a POCSAG data codeword into a best-effort text fragment."""
        try:
            data_bits = (codeword >> 11) & 0xFFFFF

            if current_func in (0, 1):
                return self._decode_numeric_fragment(data_bits)
            return self._decode_ascii_fragment(data_bits)
        except Exception:
            return None

    def _decode_numeric_fragment(self, data_bits: int) -> Optional[str]:
        bits = f"{data_bits:020b}"
        digits = []
        for i in range(0, 20, 4):
            nibble = bits[i:i + 4]
            if len(nibble) < 4:
                break
            value = int(nibble[::-1], 2)
            if value <= 9:
                digits.append(str(value))
            elif value == 0xC:
                digits.append(" ")
            elif value == 0xD:
                digits.append("-")
            elif value == 0xE:
                digits.append(")")
            elif value == 0xF:
                digits.append("(")

        text = "".join(digits).strip()
        return text or None

    def _decode_ascii_fragment(self, data_bits: int) -> Optional[str]:
        bits = f"{data_bits:020b}"
        chars = []
        for i in range(0, 20, 7):
            chunk = bits[i:i + 7]
            if len(chunk) < 7:
                break
            value = int(chunk[::-1], 2)
            if 32 <= value <= 126:
                chars.append(chr(value))
            elif value in (10, 13):
                chars.append(" ")

        text = "".join(chars).strip()
        return text or None

    def _finalize_message(self, address: int, func: Optional[int], parts: list) -> dict:
        return {
            'address': address,
            'function': func,
            'message': ''.join(parts),
        }
    
    def reset(self):
        """Reset decoder state"""
        self.sample_buffer = np.zeros(0, dtype=np.float32)
        self.symbol_buffer = np.zeros(0, dtype=np.int8)
        self.frame_count = 0


class POCSAGIQDecoder(BaseAudioDecoder):
    """Alternative POCSAG decoder using IQ data"""
    
    def __init__(self, sample_rate: int = 48000, baud_rate: int = 1200, debug: bool = False):
        super().__init__("POCSAG-IQ", sample_rate)
        self.pocsag_decoder = POCSAGDecoder(sample_rate, baud_rate=baud_rate, debug=debug)
    
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode from audio"""
        return self.pocsag_decoder.decode_audio(audio)
    
    def reset(self):
        """Reset decoder"""
        self.pocsag_decoder.reset()


# --- TS-style POCSAG implementation (parallel 512/1200 baud) -----------------

def _pocsag_hamming(a: int, b: int) -> int:
    x = (a ^ b) & 0xFFFFFFFF
    x = x - ((x >> 1) & 0x55555555)
    x = (x & 0x33333333) + ((x >> 2) & 0x33333333)
    x = (x + (x >> 4)) & 0x0F0F0F0F
    return ((x * 0x01010101) >> 24) & 0xFF


class _POCSAGSingleBaudTS:
    SYNC_WORD = 0x7CD215D8
    SYNC_INVERTED = (~0x7CD215D8) & 0xFFFFFFFF
    IDLE_CW = 0x7A89C197
    SYNC_TOLERANCE = 2

    _ecc_tables = None

    def __init__(self, audio_rate: int, baud: int, on_message: Callable[[Dict[str, Any]], None]):
        self.audio_rate = audio_rate
        self.baudRate = baud
        self.spb = float(audio_rate) / float(baud)
        self.onMessage = on_message
        self.reset()

    def reset(self) -> None:
        self.dcLevel = 0.0
        self.dcAlpha = 0.001
        self.clockPhase = 0.0
        self.lastSample = 0.0
        self.shiftReg = 0
        self.inverted = False
        self.state = 'hunt'
        self.cwBitCnt = 0
        self.currentCw = 0
        self.batchCwIdx = 0
        self.pageActive = False
        self.pageCapcode = 0
        self.pageFunc = 0
        self.pageBits: List[int] = []

    def process(self, samples: np.ndarray) -> None:
        spb = self.spb
        spbHalf = spb * 0.5
        samples = np.asarray(samples, dtype=np.float32)

        for raw in samples:
            self.dcLevel += self.dcAlpha * (float(raw) - self.dcLevel)
            if self.dcAlpha > 0.0001:
                self.dcAlpha *= 0.9999

            s = float(raw) - self.dcLevel

            if (self.lastSample < 0) != (s < 0):
                err = self.clockPhase - spbHalf
                if err > spbHalf:
                    err -= spb
                if err < -spbHalf:
                    err += spb
                if abs(err) < spb * 0.40:
                    self.clockPhase -= err * 0.12

            self.lastSample = s
            self.clockPhase += 1.0
            if self.clockPhase >= spb:
                self.clockPhase -= spb
                self._on_bit(1 if s >= 0 else 0)

    def _on_bit(self, bit: int) -> None:
        self.shiftReg = ((self.shiftReg << 1) | (bit & 1)) & 0xFFFFFFFF

        if self.state == 'hunt':
            d_norm = _pocsag_hamming(self.shiftReg, self.SYNC_WORD)
            d_inv = _pocsag_hamming(self.shiftReg, self.SYNC_INVERTED)
            if d_norm <= self.SYNC_TOLERANCE:
                self.inverted = False
                self._on_sync()
            elif d_inv <= self.SYNC_TOLERANCE:
                self.inverted = True
                self._on_sync()
            return

        d_norm = _pocsag_hamming(self.shiftReg, self.SYNC_WORD)
        d_inv = _pocsag_hamming(self.shiftReg, self.SYNC_INVERTED)
        if self.cwBitCnt >= 28 and (d_norm <= self.SYNC_TOLERANCE or d_inv <= self.SYNC_TOLERANCE):
            self.inverted = d_inv < d_norm
            self.cwBitCnt = 0
            self.currentCw = 0
            self.batchCwIdx = 0
            return

        b = 1 - bit if self.inverted else bit
        self.currentCw = ((self.currentCw << 1) | (b & 1)) & 0xFFFFFFFF
        self.cwBitCnt += 1
        if self.cwBitCnt == 32:
            self.cwBitCnt = 0
            self._on_codeword(self.currentCw)
            self.currentCw = 0

    def _on_sync(self) -> None:
        self.state = 'data'
        self.cwBitCnt = 0
        self.currentCw = 0
        self.batchCwIdx = 0

    def _on_codeword(self, cw: int) -> None:
        pass1 = self._ecc_correct(cw)
        pass2 = self._ecc_correct(pass1['cw'])

        if pass2['errors'] >= 3:
            self.batchCwIdx += 1
            if self.batchCwIdx >= 16:
                self.state = 'hunt'
            return

        corrected = pass2['cw']

        if corrected == self.IDLE_CW:
            if self.pageActive and self.pageBits:
                self._emit_page()
            self.pageActive = False
        else:
            self._process_cw(corrected, self.batchCwIdx)

        self.batchCwIdx += 1
        if self.batchCwIdx >= 16:
            self.state = 'hunt'

    def _process_cw(self, cw: int, cw_idx: int) -> None:
        if ((cw >> 31) & 1) == 0:
            if self.pageActive and self.pageBits:
                self._emit_page()

            addr_high = (cw >> 13) & 0x3FFFF
            func = (cw >> 11) & 0x3
            frame = (cw_idx >> 1) & 0x7
            self.pageCapcode = (addr_high << 3) | frame
            self.pageFunc = func
            self.pageBits = []
            self.pageActive = True
        else:
            if self.pageActive:
                data = (cw >> 11) & 0xFFFFF
                for b in range(19, -1, -1):
                    self.pageBits.append((data >> b) & 1)

    @classmethod
    def _build_ecc(cls):
        ecs = [0] * 32
        bch = [0] * 1025

        srr = 0x3B4
        for i in range(0, 21):
            ecs[i] = srr
            if srr & 1:
                srr = (srr >> 1) ^ 0x3B4
            else:
                srr >>= 1

        for n in range(0, 21):
            for i in range(0, 21):
                k = (ecs[n] ^ ecs[i]) & 0x3FF
                bch[k] = (i << 5) + n + 0x2000

        for n in range(0, 21):
            k = ecs[n] & 0x3FF
            bch[k] = n + (0x1F << 5) + 0x1000

        for n in range(0, 21):
            for i in range(0, 10):
                k = (ecs[n] ^ (1 << i)) & 0x3FF
                bch[k] = n + (0x1F << 5) + 0x2000

        for n in range(0, 10):
            bch[1 << n] = 0x3FF + 0x1000

        for n in range(0, 10):
            for i in range(0, 10):
                if i != n:
                    bch[(1 << n) ^ (1 << i)] = 0x3FF + 0x2000

        return {'ecs': ecs, 'bch': bch}

    def _ecc_correct(self, val: int) -> Dict[str, int]:
        if _POCSAGSingleBaudTS._ecc_tables is None:
            _POCSAGSingleBaudTS._ecc_tables = self._build_ecc()

        ecs = _POCSAGSingleBaudTS._ecc_tables['ecs']
        bch = _POCSAGSingleBaudTS._ecc_tables['bch']

        ecc = 0
        for i in range(31, 10, -1):
            if (val >> i) & 1:
                ecc ^= ecs[31 - i]

        acc = 0
        for i in range(10, 0, -1):
            acc = (acc << 1) | ((val >> i) & 1)
        acc &= 0x3FF

        synd = (ecc ^ acc) & 0x3FF
        errl = 0

        if synd != 0:
            entry = bch[synd]
            if entry != 0:
                b1 = entry & 0x1F
                b2 = (entry >> 5) & 0x1F

                if b2 != 0x1F:
                    val ^= 1 << (31 - b2)
                if b1 != 0x1F:
                    val ^= 1 << (31 - b1)

                errl = entry >> 12
            else:
                errl = 3

        if errl == 4:
            errl = 3

        return {'cw': val & 0xFFFFFFFF, 'errors': errl}

    def _emit_page(self) -> None:
        text = ''

        if self.pageFunc == 3:
            c = 0
            cb = 0
            for bit in self.pageBits:
                c |= (bit << cb)
                cb += 1
                if cb == 7:
                    if 32 <= c < 127:
                        text += chr(c)
                    elif c in (10, 13):
                        text += '\n'
                    c = 0
                    cb = 0
        elif self.pageFunc != 0:
            nmap = '0123456789 -.)('
            for i in range(0, len(self.pageBits), 4):
                if i + 3 >= len(self.pageBits):
                    break
                n = (
                    self.pageBits[i]
                    | (self.pageBits[i + 1] << 1)
                    | (self.pageBits[i + 2] << 2)
                    | (self.pageBits[i + 3] << 3)
                )
                if n < len(nmap):
                    text += nmap[n]

        clean = ''.join(' ' if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
        clean = ' '.join(clean.split()).strip()

        if clean or self.pageFunc == 0:
            self.onMessage({
                'capcode': self.pageCapcode,
                'func': self.pageFunc,
                'type': 'alpha' if self.pageFunc == 3 else ('tone' if self.pageFunc == 0 else 'numeric'),
                'text': clean,
                'baud': self.baudRate,
            })

        self.pageBits = []
        self.pageActive = False


class POCSAGDecoder(BaseAudioDecoder):
    """POCSAG paging network decoder"""

    def __init__(self, sample_rate: int = 48000, debug: bool = False):
        super().__init__('POCSAG', sample_rate)
        self.debug = debug
        self._results: List[DecoderResult] = []
        self._d1200 = _POCSAGSingleBaudTS(sample_rate, 1200, self._handle_message)
        self._d512 = _POCSAGSingleBaudTS(sample_rate, 512, self._handle_message)

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        self._results.append(
            DecoderResult(
                decoder_name='POCSAG',
                timestamp=time.time(),
                data=msg,
                confidence=0.98,
                metadata={'type': 'paging', 'baud': msg.get('baud')},
            )
        )

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        if audio.size == 0:
            return None

        self._results = []
        self._d1200.process(audio)
        self._d512.process(audio)
        return self._results[0] if self._results else None

    def reset(self):
        self._d1200.reset()
        self._d512.reset()
        self._results = []


class POCSAGIQDecoder(BaseAudioDecoder):
    """Alternative POCSAG decoder using IQ data"""

    def __init__(self, sample_rate: int = 48000, debug: bool = False):
        super().__init__('POCSAG-IQ', sample_rate)
        self.pocsag_decoder = POCSAGDecoder(sample_rate, debug=debug)

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        return self.pocsag_decoder.decode_audio(audio)

    def reset(self):
        self.pocsag_decoder.reset()
