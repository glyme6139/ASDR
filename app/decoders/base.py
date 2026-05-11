"""
Modular decoder base classes and framework
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Callable
import numpy as np
import logging

logger = logging.getLogger(__name__)


@dataclass
class DecoderResult:
    """Result from a decoder"""
    decoder_name: str
    timestamp: float
    data: Any
    confidence: float = 1.0
    metadata: Dict[str, Any] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            'decoder': self.decoder_name,
            'timestamp': self.timestamp,
            'data': str(self.data),
            'confidence': self.confidence,
            'metadata': self.metadata or {},
        }


class BaseDecoder(ABC):
    """Base class for all signal decoders"""
    
    def __init__(self, name: str, sample_rate: int = 48000):
        self.name = name
        self.sample_rate = sample_rate
        self.is_enabled = True
        self.buffer = np.zeros(0, dtype=np.complex64)
        self.on_decode: Optional[Callable[[DecoderResult], None]] = None
        
    @abstractmethod
    def process(self, data: np.ndarray, audio: Optional[np.ndarray] = None) -> Optional[DecoderResult]:
        """
        Process IQ or audio data
        
        Args:
            data: IQ samples (complex64) or audio samples (float32)
            audio: Optional demodulated audio
            
        Returns:
            DecoderResult if decode successful, None otherwise
        """
        pass
    
    @abstractmethod
    def reset(self):
        """Reset decoder state"""
        pass
    
    def set_sample_rate(self, rate: int):
        """Update sample rate"""
        self.sample_rate = rate
    
    def get_info(self) -> dict:
        """Get decoder information"""
        return {
            'name': self.name,
            'enabled': self.is_enabled,
            'sample_rate': self.sample_rate,
        }

    def format_result(self, result: DecoderResult) -> str:
        """Format a decode result for display."""
        if result is None:
            return ""
        return str(result.data)


class BaseAudioDecoder(BaseDecoder):
    """Base class for audio-based decoders"""
    
    def __init__(self, name: str, sample_rate: int = 48000):
        super().__init__(name, sample_rate)
        self.audio_buffer = np.zeros(0, dtype=np.float32)
        self.min_buffer_size = sample_rate // 10  # 100ms
    
    def process(self, data: np.ndarray, audio: Optional[np.ndarray] = None) -> Optional[DecoderResult]:
        """Process audio data"""
        if audio is not None:
            self.audio_buffer = np.concatenate([self.audio_buffer, audio])
        
        if len(self.audio_buffer) >= self.min_buffer_size:
            chunk = self.audio_buffer[:self.min_buffer_size]
            self.audio_buffer = self.audio_buffer[self.min_buffer_size:]
            return self.decode_audio(chunk)
        
        return None
    
    @abstractmethod
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """Decode audio chunk"""
        pass


class BaseIQDecoder(BaseDecoder):
    """Base class for IQ-based decoders"""
    
    def __init__(self, name: str, sample_rate: int = 20_000_000):
        super().__init__(name, sample_rate)
        self.iq_buffer = np.zeros(0, dtype=np.complex64)
        self.min_buffer_size = sample_rate // 100  # 10ms
    
    def process(self, data: np.ndarray, audio: Optional[np.ndarray] = None) -> Optional[DecoderResult]:
        """Process IQ data"""
        if data.dtype == np.complex64 or data.dtype == np.complex128:
            self.iq_buffer = np.concatenate([self.iq_buffer, data.astype(np.complex64)])
        
        if len(self.iq_buffer) >= self.min_buffer_size:
            chunk = self.iq_buffer[:self.min_buffer_size]
            self.iq_buffer = self.iq_buffer[self.min_buffer_size:]
            return self.decode_iq(chunk)
        
        return None
    
    @abstractmethod
    def decode_iq(self, iq_data: np.ndarray) -> Optional[DecoderResult]:
        """Decode IQ chunk"""
        pass


class DecoderRegistry:
    """Registry for managing decoders"""
    
    def __init__(self):
        self.decoders: Dict[str, BaseDecoder] = {}
        self._lock = __import__('threading').RLock()
    
    def register(self, decoder: BaseDecoder) -> bool:
        """Register a decoder"""
        with self._lock:
            if decoder.name in self.decoders:
                logger.warning(f"Decoder {decoder.name} already registered")
                return False
            
            self.decoders[decoder.name] = decoder
            logger.info(f"Decoder registered: {decoder.name}")
            return True
    
    def unregister(self, name: str) -> bool:
        """Unregister a decoder"""
        with self._lock:
            if name in self.decoders:
                del self.decoders[name]
                logger.info(f"Decoder unregistered: {name}")
                return True
            return False
    
    def get_decoder(self, name: str) -> Optional[BaseDecoder]:
        """Get decoder by name"""
        return self.decoders.get(name)
    
    def get_all_decoders(self) -> List[BaseDecoder]:
        """Get all registered decoders"""
        with self._lock:
            return list(self.decoders.values())
    
    def enable_decoder(self, name: str, enabled: bool = True):
        """Enable or disable a decoder"""
        decoder = self.get_decoder(name)
        if decoder:
            decoder.is_enabled = enabled
            logger.info(f"Decoder {name} {'enabled' if enabled else 'disabled'}")
    
    def process_all(self, data: np.ndarray, audio: Optional[np.ndarray] = None) -> List[DecoderResult]:
        """Process data through all enabled decoders"""
        results = []
        with self._lock:
            for decoder in self.decoders.values():
                if not decoder.is_enabled:
                    continue
                
                try:
                    result = decoder.process(data, audio)
                    if result:
                        results.append(result)
                        if decoder.on_decode:
                            decoder.on_decode(result)
                except Exception as e:
                    logger.error(f"Error in {decoder.name}: {e}")
        
        return results
    
    def get_stats(self) -> dict:
        """Get statistics about registered decoders"""
        with self._lock:
            return {
                'total': len(self.decoders),
                'decoders': [
                    {
                        'name': d.name,
                        'enabled': d.is_enabled,
                    }
                    for d in self.decoders.values()
                ]
            }


# Global decoder registry
_decoder_registry = None


def get_decoder_registry() -> DecoderRegistry:
    """Get global decoder registry"""
    global _decoder_registry
    if _decoder_registry is None:
        _decoder_registry = DecoderRegistry()
    return _decoder_registry
