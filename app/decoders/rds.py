"""
RDS (Radio Data System) decoder for FM broadcast
Decodes station name, program type, and radiotext from FM signals
"""
import numpy as np
from typing import Optional, Dict
import time
import logging
from .base import BaseAudioDecoder, DecoderResult
from scipy import signal

logger = logging.getLogger(__name__)


class RDSDecoder(BaseAudioDecoder):
    """RDS decoder for FM broadcasting"""
    
    # RDS constants
    RDS_CARRIER = 57000  # Hz (19 kHz pilot * 3)
    
    def __init__(self, sample_rate: int = 48000):
        super().__init__("RDS", sample_rate)
        self.rds_data = {}
        self.group_buffer = []
        self.last_decode_time = time.time()
        self.min_confidence = 0.7
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode RDS from audio"""
        try:
            # Extract RDS subcarrier (57 kHz)
            rds_signal = self._extract_rds_carrier(audio)
            
            # Look for Manchester coded bits
            bits = self._manchester_decode(rds_signal)
            
            if len(bits) < 104:  # RBDS block size
                return None
            
            # Try to decode blocks
            blocks = self._decode_blocks(bits)
            
            if blocks:
                # Process RDS data
                rds_info = self._process_rds_data(blocks)
                if rds_info:
                    return DecoderResult(
                        decoder_name="RDS",
                        timestamp=time.time(),
                        data=rds_info,
                        confidence=0.85,
                        metadata={'type': 'rds', 'standard': 'RBDS'},
                    )
        
        except Exception as e:
            logger.debug(f"RDS decode error: {e}")
        
        return None
    
    def _extract_rds_carrier(self, audio: np.ndarray) -> np.ndarray:
        """Extract the 57 kHz RDS carrier"""
        try:
            # Bandpass filter around 57 kHz
            nyquist = self.sample_rate / 2
            low_freq = 55000 / nyquist
            high_freq = 59000 / nyquist
            
            low_freq = max(0.001, low_freq)
            high_freq = min(0.999, high_freq)
            
            b, a = signal.butter(4, [low_freq, high_freq], btype='band')
            rds_signal = signal.filtfilt(b, a, audio)
            
            return rds_signal
        except:
            return audio
    
    def _manchester_decode(self, signal_data: np.ndarray) -> np.ndarray:
        """Decode Manchester encoded data"""
        try:
            # Threshold detect
            threshold = np.mean(np.abs(signal_data))
            bits = (signal_data > threshold).astype(int)
            
            # Look for Manchester bit patterns (10 or 01)
            decoded_bits = []
            i = 0
            while i < len(bits) - 1:
                if bits[i] == 1 and bits[i + 1] == 0:
                    decoded_bits.append(1)
                    i += 2
                elif bits[i] == 0 and bits[i + 1] == 1:
                    decoded_bits.append(0)
                    i += 2
                else:
                    i += 1
            
            return np.array(decoded_bits, dtype=np.int8)
        except:
            return np.array([], dtype=np.int8)
    
    def _decode_blocks(self, bits: np.ndarray) -> Optional[Dict]:
        """Decode RDS blocks"""
        try:
            if len(bits) < 104:
                return None
            
            # Extract 4 blocks of 26 bits (104 total)
            blocks = {}
            for i in range(4):
                start = i * 26
                block_bits = bits[start:start + 26]
                if len(block_bits) == 26:
                    # First 25 bits are data, last bit is parity
                    block_data = 0
                    for j in range(25):
                        block_data = (block_data << 1) | block_bits[j]
                    blocks[f'block_{i}'] = block_data
            
            return blocks if len(blocks) == 4 else None
        
        except Exception as e:
            logger.debug(f"Block decode error: {e}")
            return None
    
    def _process_rds_data(self, blocks: Dict) -> Optional[Dict]:
        """Process RDS block data"""
        try:
            result = {}
            
            # Extract data from blocks
            if 'block_1' in blocks:
                block1 = blocks['block_1']
                
                # Group type (5 bits)
                group_type = (block1 >> 20) & 0x1F
                
                # Traffic Program (1 bit)
                tp = (block1 >> 19) & 1
                
                # Program Type (5 bits)
                pty = (block1 >> 14) & 0x1F
                
                result['group_type'] = group_type
                result['traffic_program'] = bool(tp)
                result['program_type'] = self._decode_program_type(pty)
                
                # Process group-specific data
                if group_type == 0:  # 0A: PS/AF
                    result['group'] = '0A - Station Name'
                    if 'block_3' in blocks and 'block_4' in blocks:
                        ps_chars = self._extract_ps_characters(blocks)
                        if ps_chars:
                            result['station_name'] = ps_chars
                
                elif group_type == 1:  # 0B: PIN/SL
                    result['group'] = '0B - Program Service'
                
                elif group_type == 2:  # 1A: Radiotext
                    result['group'] = '1A - Radiotext'
                    if 'block_3' in blocks and 'block_4' in blocks:
                        radiotext = self._extract_radiotext(blocks)
                        if radiotext:
                            result['radiotext'] = radiotext
                
                elif group_type == 3:  # 1B: Radiotext
                    result['group'] = '1B - Radiotext'
                
                return result
        
        except Exception as e:
            logger.debug(f"RDS process error: {e}")
        
        return None
    
    def _extract_ps_characters(self, blocks: Dict) -> Optional[str]:
        """Extract station name (PS) from RDS blocks"""
        try:
            block3 = blocks.get('block_3', 0)
            block4 = blocks.get('block_4', 0)
            
            # Each block contains 2 characters
            chars = []
            
            # From block 3
            chars.append(chr((block3 >> 8) & 0xFF))
            chars.append(chr(block3 & 0xFF))
            
            # From block 4
            chars.append(chr((block4 >> 8) & 0xFF))
            chars.append(chr(block4 & 0xFF))
            
            return ''.join(c for c in chars if 32 <= ord(c) <= 126)
        
        except:
            return None
    
    def _extract_radiotext(self, blocks: Dict) -> Optional[str]:
        """Extract radiotext from RDS blocks"""
        try:
            block3 = blocks.get('block_3', 0)
            block4 = blocks.get('block_4', 0)
            
            chars = []
            for block in [block3, block4]:
                chars.append(chr((block >> 8) & 0xFF))
                chars.append(chr(block & 0xFF))
            
            return ''.join(c for c in chars if 32 <= ord(c) <= 126)
        
        except:
            return None
    
    @staticmethod
    def _decode_program_type(pty: int) -> str:
        """Decode program type code"""
        pty_names = [
            'No Program Type', 'News', 'Current Affairs', 'Information',
            'Sport', 'Education', 'Drama', 'Culture', 'Science',
            'Varied', 'Pop Music', 'Rock Music', 'Easy Listening Music',
            'Light Classical', 'Serious Classical', 'Other Music', 'Weather',
            'Finance', 'Children\'s Programs', 'Social Affairs', 'Religion',
            'Phone-In', 'Travel', 'Leisure', 'Jazz Music', 'Country Music',
            'National Music', 'Oldies Music', 'Folk Music', 'Documentary',
            'Alarm Test', 'Alarm'
        ]
        return pty_names[min(pty, len(pty_names) - 1)]
    
    def reset(self):
        """Reset decoder state"""
        self.rds_data = {}
        self.group_buffer = []
