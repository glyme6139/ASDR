"""
POCSAG (Post Office Code Standardisation Advisory Group) decoder
Decodes paging networks for POCSAG messages
"""
import numpy as np
from typing import Optional
from scipy import signal
import time
import logging
from .base import BaseAudioDecoder, DecoderResult

logger = logging.getLogger(__name__)


class POCSAGDecoder(BaseAudioDecoder):
    """POCSAG paging network decoder"""
    
    # POCSAG constants
    BAUD_RATE = 1200
    FRAME_LENGTH = 576  # bits
    FRAME_TIME = FRAME_LENGTH / BAUD_RATE
    
    def __init__(self, sample_rate: int = 48000):
        super().__init__("POCSAG", sample_rate)
        self.symbol_buffer = np.zeros(0, dtype=np.int8)
        self.frame_count = 0
        self.last_frame_time = time.time()
        self.samples_per_symbol = sample_rate // self.BAUD_RATE
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode POCSAG from audio"""
        try:
            # Bandpass filter around 1200 Hz for POCSAG
            nyquist = self.sample_rate / 2
            low_freq = 1200 - 100
            high_freq = 1200 + 100
            
            b, a = signal.butter(4, [low_freq / nyquist, high_freq / nyquist], btype='band')
            filtered = signal.filtfilt(b, a, audio)
            
            # Threshold detect and convert to symbols
            threshold = np.mean(np.abs(filtered))
            symbols = (np.abs(filtered) > threshold).astype(np.int8)
            
            self.symbol_buffer = np.concatenate([self.symbol_buffer, symbols])
            
            # Try to find frame sync and decode
            while len(self.symbol_buffer) >= self.FRAME_LENGTH:
                frame = self.symbol_buffer[:self.FRAME_LENGTH]
                self.symbol_buffer = self.symbol_buffer[1:]  # Shift by 1 bit for next attempt
                
                result = self._decode_frame(frame)
                if result:
                    return result
            
            return None
            
        except Exception as e:
            logger.error(f"POCSAG decode error: {e}")
            return None
    
    def _decode_frame(self, frame_bits: np.ndarray) -> Optional[DecoderResult]:
        """Decode a POCSAG frame"""
        try:
            # Frame sync: 0xAAAAAAAA (32 bits of 10101010 pattern)
            # Followed by frame info and 8 codewords
            logger.debug(f"Attempting to decode frame with {len(frame_bits)} bits")
            # Look for frame sync
            frame_sync = 0xAAAAAAAA
            first_32 = 0
            for i in range(32):
                first_32 = (first_32 << 1) | frame_bits[i]
            
            if first_32 != frame_sync:
                return None  # Not a valid frame
            
            self.frame_count += 1
            
            # Extract frame code (5 bits after sync)
            frame_code = 0
            for i in range(32, 37):
                frame_code = (frame_code << 1) | frame_bits[i]
            
            # Extract 8 codewords
            messages = []
            for cw_idx in range(8):
                start = 37 + cw_idx * 32
                if start + 32 > len(frame_bits):
                    break
                
                codeword = 0
                for i in range(32):
                    codeword = (codeword << 1) | frame_bits[start + i]
                
                # Decode codeword
                addr, msg = self._decode_codeword(codeword)
                if addr is not None:
                    messages.append({
                        'address': addr,
                        'message': msg,
                        'codeword': cw_idx,
                    })
            
            if messages:
                logger.debug(f"Decoded POCSAG frame {self.frame_count}: code={frame_code}, messages={messages}")
                return DecoderResult(
                    decoder_name="POCSAG",
                    timestamp=time.time(),
                    data={
                        'frame': self.frame_count,
                        'frame_code': frame_code,
                        'messages': messages,
                    },
                    confidence=0.95,
                    metadata={'type': 'paging'},
                )
        
        except Exception as e:
            logger.debug(f"Frame decode error: {e}")
        
        return None
    
    def _decode_codeword(self, codeword: int) -> tuple:
        """
        Decode a 32-bit POCSAG codeword
        Returns: (address, message) tuple
        """
        try:
            # Check parity (last bit)
            parity = bin(codeword).count('1') & 1
            if parity != 0:
                return None, None  # Invalid parity
            
            # Remove parity bit
            codeword = codeword >> 1
            
            # Bits 0-3: function code (0-3)
            func = codeword & 0x3
            
            # Bits 4-20: address offset (0-131071)
            addr_offset = (codeword >> 2) & 0x1FFFF
            
            # Bits 21-31: message bits
            msg_bits = (codeword >> 19) & 0x7FF
            
            # Calculate full address
            address = (addr_offset * 4) + func
            
            # Format message
            if func == 0 or func == 1:  # Numeric/Tone only
                message = f"Tone {func}"
            else:  # func == 2 or 3 (Alphanumeric)
                message = f"Msg: {msg_bits:011b}"
            
            return address, message
        
        except:
            return None, None
    
    def reset(self):
        """Reset decoder state"""
        self.symbol_buffer = np.zeros(0, dtype=np.int8)
        self.frame_count = 0


class POCSAGIQDecoder(BaseAudioDecoder):
    """Alternative POCSAG decoder using IQ data"""
    
    def __init__(self, sample_rate: int = 48000):
        super().__init__("POCSAG-IQ", sample_rate)
        self.pocsag_decoder = POCSAGDecoder(sample_rate)
    
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode from audio"""
        return self.pocsag_decoder.decode_audio(audio)
    
    def reset(self):
        """Reset decoder"""
        self.pocsag_decoder.reset()
