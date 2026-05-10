"""
ADSB (Automatic Dependent Surveillance-Broadcast) decoder for aircraft tracking
Decodes aircraft position and identification information
"""
import numpy as np
from typing import Optional
import time
import logging
from .base import BaseIQDecoder, DecoderResult
from scipy import signal

logger = logging.getLogger(__name__)


class ADSBDecoder(BaseIQDecoder):
    """ADSB aircraft tracking decoder"""
    
    # ADSB constants
    ADSB_FREQ = 1090e6  # MHz
    PREAMBLE = [1, 0, 1, 0, 0, 0, 0, 1]  # ADSB preamble
    FRAME_BITS = 112
    SAMPLE_RATE = 2e6  # 2 MHz typical for ADSB
    
    def __init__(self, sample_rate: int = 2000000):
        super().__init__("ADSB", sample_rate)
        self.last_icao = None
        self.aircraft_cache = {}
        
    def decode_iq(self, iq_data: np.ndarray) -> Optional[DecoderResult]:
        """Decode ADSB from IQ data"""
        try:
            # Compute magnitude (envelope)
            magnitude = np.abs(iq_data)
            
            # Threshold detect to get bits
            threshold = np.mean(magnitude) * 1.5
            bits = (magnitude > threshold).astype(int)
            
            # Look for preamble
            preamble_detected = self._find_preamble(bits)
            
            if preamble_detected:
                frame_start = preamble_detected
                frame_bits = bits[frame_start:frame_start + self.FRAME_BITS]
                
                if len(frame_bits) == self.FRAME_BITS:
                    result = self._decode_message(frame_bits)
                    if result:
                        return result
            
            return None
            
        except Exception as e:
            logger.debug(f"ADSB decode error: {e}")
            return None
    
    def _find_preamble(self, bits: np.ndarray) -> Optional[int]:
        """Find ADSB preamble in bit stream"""
        for i in range(len(bits) - len(self.PREAMBLE)):
            if all(bits[i:i+len(self.PREAMBLE)] == self.PREAMBLE):
                return i + len(self.PREAMBLE)
        return None
    
    def _decode_message(self, frame_bits: np.ndarray) -> Optional[DecoderResult]:
        """Decode ADSB message"""
        try:
            if len(frame_bits) < 32:
                return None
            
            # Extract ICAO address (first 32 bits after preamble)
            icao_bits = frame_bits[0:24]
            icao = int(''.join(map(str, icao_bits)), 2)
            
            # Extract message type (bits 32-37)
            msg_type_bits = frame_bits[32:38] if len(frame_bits) > 37 else []
            msg_type = int(''.join(map(str, msg_type_bits)), 2) if msg_type_bits else 0
            
            if self.last_icao != icao:
                self.last_icao = icao
                
                return DecoderResult(
                    decoder_name="ADSB",
                    timestamp=time.time(),
                    data={
                        'type': f'Type {msg_type}',
                        'icao': f'{icao:06X}',
                        'description': 'Aircraft mode S reply',
                    },
                    confidence=0.85,
                    metadata={
                        'type': 'aviation',
                        'icao_address': icao,
                    },
                )
        
        except Exception as e:
            logger.debug(f"Message decode error: {e}")
        
        return None
    
    def reset(self):
        """Reset decoder state"""
        self.last_icao = None
        self.aircraft_cache = {}
