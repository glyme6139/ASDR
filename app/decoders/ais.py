"""
AIS (Automatic Identification System) decoder for maritime tracking
Decodes vessel information from AIS signals
"""
import numpy as np
from typing import Optional
import time
import logging
from .base import BaseAudioDecoder, DecoderResult
from scipy import signal

logger = logging.getLogger(__name__)


class AISDecoder(BaseAudioDecoder):
    """AIS maritime signal decoder"""
    
    # AIS constants
    BAUD_RATE = 9600
    FRAME_START_BITS = 0x7E  # 01111110
    
    def __init__(self, sample_rate: int = 48000):
        super().__init__("AIS", sample_rate)
        self.bit_buffer = []
        self.frame_buffer = []
        self.last_mmsi = None
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode AIS from audio"""
        try:
            # Filter for AIS frequency (9.6 kHz approximately)
            # AIS uses GMSK modulation at 9600 baud
            nyquist = self.sample_rate / 2
            low_freq = 8000 / nyquist
            high_freq = 12000 / nyquist
            
            b, a = signal.butter(4, [low_freq, high_freq], btype='band')
            filtered = signal.filtfilt(b, a, audio)
            
            # Threshold detect to get bits
            threshold = np.mean(np.abs(filtered))
            bits = (filtered > threshold).astype(int)
            
            # Process bits
            for bit in bits:
                self.bit_buffer.append(bit)
                
                # Look for frame sync (01111110)
                if len(self.bit_buffer) >= 8:
                    recent = self.bit_buffer[-8:]
                    frame_byte = int(''.join(map(str, recent)), 2)
                    
                    if frame_byte == self.FRAME_START_BITS:
                        # Found frame sync, try to decode
                        result = self._decode_frame()
                        if result:
                            return result
            
            return None
            
        except Exception as e:
            logger.debug(f"AIS decode error: {e}")
            return None
    
    def _decode_frame(self) -> Optional[DecoderResult]:
        """Decode an AIS frame"""
        try:
            # Minimum AIS frame size
            if len(self.bit_buffer) < 168:  # 21 bytes * 8 bits
                return None
            
            # Extract frame data (simplified)
            frame_bits = self.bit_buffer[:168]
            self.bit_buffer = self.bit_buffer[1:]  # Shift for next search
            
            # Parse AIS message type and data
            message_type = int(''.join(map(str, frame_bits[0:6])), 2)
            
            if message_type == 1 or message_type == 2 or message_type == 3:
                # Position Report Class A
                mmsi = int(''.join(map(str, frame_bits[8:38])), 2)
                
                if self.last_mmsi != mmsi:
                    self.last_mmsi = mmsi
                    
                    return DecoderResult(
                        decoder_name="AIS",
                        timestamp=time.time(),
                        data={
                            'type': f'Type {message_type}',
                            'mmsi': mmsi,
                            'description': 'Vessel position report',
                        },
                        confidence=0.80,
                        metadata={'type': 'maritime'},
                    )
        
        except Exception as e:
            logger.debug(f"Frame decode error: {e}")
        
        return None
    
    def reset(self):
        """Reset decoder state"""
        self.bit_buffer = []
        self.frame_buffer = []
        self.last_mmsi = None
