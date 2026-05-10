# 🔧 ASDR Developer Guide

Advanced guide for extending ASDR with custom decoders, DSP algorithms, and features.

## 📚 Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Creating Custom Decoders](#creating-custom-decoders)
3. [VFO System](#vfo-system)
4. [DSP Techniques](#dsp-techniques)
5. [Frontend Integration](#frontend-integration)
6. [Testing](#testing)

## Architecture Overview

### Core Components

```
┌─────────────────────────────────────────────────────────┐
│                     Web Frontend                        │
│              (HTML/JS + WebSocket + Canvas)             │
└──────────────────────┬──────────────────────────────────┘
                       │
                ┌──────▼────────────┐
                │  Flask + Socket.io│
                │   (REST + WS)     │
                └──────┬────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
    ┌───▼───┐    ┌────▼───┐    ┌────▼────┐
    │HackRF │    │ VFO    │    │Decoders │
    │Module │    │Manager │    │Registry │
    └───┬───┘    └────┬───┘    └────┬────┘
        │             │             │
        └─────────────┼─────────────┘
                      │
              ┌───────▼───────┐
              │ Signal Chain  │
              │ DSP Pipeline  │
              └───────────────┘
```

### Data Flow

```
HackRF (IQ) → VFO Manager → Per-VFO Processing → Decoders → WebSocket → UI
                            ├─ Frequency shift
                            ├─ Demodulation
                            ├─ Squelch
                            └─ Audio output
```

## Creating Custom Decoders

### Decoder Types

ASDR supports three types of decoders:

#### 1. Audio Decoders (BaseAudioDecoder)

Process demodulated audio samples:

```python
from app.decoders.base import BaseAudioDecoder, DecoderResult
import numpy as np
import time

class MyAudioDecoder(BaseAudioDecoder):
    """Example audio decoder"""
    
    def __init__(self, sample_rate=48000):
        super().__init__("MyDecoder", sample_rate)
        # Initialize state here
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        """
        Decode audio chunk
        
        Args:
            audio: 16-bit PCM or float32 audio samples
            
        Returns:
            DecoderResult if decode successful, None otherwise
        """
        try:
            # Process audio
            result = self.process_signal(audio)
            
            if result:
                return DecoderResult(
                    decoder_name=self.name,
                    timestamp=time.time(),
                    data=result,
                    confidence=0.95,
                    metadata={'type': 'your_type'},
                )
        except Exception as e:
            logger.error(f"Decode error: {e}")
        
        return None
    
    def reset(self):
        """Reset internal state"""
        pass
```

#### 2. IQ Decoders (BaseIQDecoder)

Process raw IQ (complex) samples at RF:

```python
from app.decoders.base import BaseIQDecoder, DecoderResult
import numpy as np

class MyIQDecoder(BaseIQDecoder):
    """Example IQ decoder"""
    
    def __init__(self, sample_rate=20_000_000):
        super().__init__("MyIQDecoder", sample_rate)
        
    def decode_iq(self, iq_data: np.ndarray) -> Optional[DecoderResult]:
        """
        Decode IQ data
        
        Args:
            iq_data: Complex64 numpy array of IQ samples
            
        Returns:
            DecoderResult or None
        """
        # Analyze IQ data
        power = np.abs(iq_data) ** 2
        mean_power = np.mean(power)
        
        # Your decoding logic
        
        return None
```

#### 3. Custom Decoders (BaseDecoder)

For specialized signal processing:

```python
from app.decoders.base import BaseDecoder, DecoderResult
import numpy as np

class MyCustomDecoder(BaseDecoder):
    """Custom decoder for special signals"""
    
    def __init__(self):
        super().__init__("CustomDecoder")
        
    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        """
        Process either IQ or audio data
        
        Args:
            data: IQ or audio samples
            audio: Optional demodulated audio
            
        Returns:
            DecoderResult or None
        """
        # Your custom processing
        pass
    
    def reset(self):
        pass
```

### Registration and Usage

Register your decoder:

```python
from app.decoders.base import get_decoder_registry

# In your initialization code
decoder_registry = get_decoder_registry()
my_decoder = MyAudioDecoder()
decoder_registry.register(my_decoder)
```

### Example: Simple Morse Code Decoder

```python
class CWDecoder(BaseAudioDecoder):
    """Morse code (CW) detector"""
    
    def __init__(self):
        super().__init__("CW Detector", sample_rate=48000)
        self.dot_time = 0.1  # 100ms dot
        self.tone_freq = 800  # Hz
        
    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        from scipy import signal
        
        # Bandpass filter around 800 Hz
        nyquist = self.sample_rate / 2
        low = 700 / nyquist
        high = 900 / nyquist
        b, a = signal.butter(4, [low, high], btype='band')
        filtered = signal.filtfilt(b, a, audio)
        
        # Detect tone presence
        threshold = np.mean(np.abs(filtered)) * 1.5
        tone_detected = np.sum(np.abs(filtered) > threshold) > len(filtered) * 0.3
        
        if tone_detected:
            return DecoderResult(
                decoder_name="CW",
                timestamp=time.time(),
                data="Morse tone detected",
                confidence=0.8,
            )
        
        return None
    
    def reset(self):
        pass
```

## VFO System

### Understanding VFOs

Each Virtual Frequency Oscillator provides:

- **Frequency offset**: Relative to center frequency
- **Demodulation**: Independent per-VFO
- **Audio processing**: Volume, squelch, filters
- **Signal metrics**: Strength, activity status

### Working with VFOs

```python
from app.sdr.vfo import VFOManager, VFO

# Create manager
vfo_manager = VFOManager(
    center_freq=100_000_000,  # 100 MHz
    sample_rate=20_000_000,    # 20 MHz
    max_vfos=10
)

# Create VFO
vfo = vfo_manager.create_vfo(101_000_000)  # 101 MHz

# Configure
vfo.set_demod_mode('NFM')
vfo.set_volume(0.8)
vfo.set_squelch(-80.0, enabled=True)  # -80 dBm squelch

# Process IQ data
audio_output = vfo.process_iq(iq_samples)

# Get status
status = vfo.get_status()
print(f"Signal: {status['signal_strength']} dBm")
```

### Extending Demodulation

Add custom demodulation to VFO:

```python
class VFO:
    def _demodulate(self, iq_data):
        mode = self.settings.demod_mode
        
        if mode == 'CUSTOM':
            return self._demod_custom(iq_data)
        else:
            return super()._demodulate(iq_data)
    
    def _demod_custom(self, iq_data):
        """Your custom demodulation algorithm"""
        # Example: Simple product detector
        demod = np.real(iq_data) * np.cos(2*np.pi*1000*t)
        return demod.astype(np.float32)
```

## DSP Techniques

### Filtering

```python
from scipy import signal

# Design lowpass filter
fs = 48000  # Sample rate
fc = 1000   # Cutoff frequency
nyquist = fs / 2
normalized_cutoff = fc / nyquist
b, a = signal.butter(4, normalized_cutoff, btype='low')

# Apply filter
filtered = signal.filtfilt(b, a, data)

# FIR filter (better for real-time)
taps = signal.firwin(64, normalized_cutoff)
filtered = signal.lfilter(taps, 1.0, data)
```

### Frequency Shift

```python
# Shift signal by frequency offset
offset_hz = 5000
t = np.arange(len(signal)) / sample_rate
shifted = signal * np.exp(2j * np.pi * offset_hz * t)
```

### Envelope Detection

```python
# Extract amplitude modulation
analytic = signal.hilbert(real_signal)
envelope = np.abs(analytic)
```

### FFT and Spectrum

```python
# Compute power spectrum
fft = np.fft.fft(audio)
power_spectrum = np.abs(fft) ** 2

# Log scale (dB)
power_db = 10 * np.log10(power_spectrum + 1e-10)
```

## Frontend Integration

### Communicating with JavaScript

WebSocket events from JavaScript:

```javascript
// Tune a VFO
ws.tuneVFO(vfoId, frequency);

// Change demodulation
ws.setVFODemod(vfoId, 'NFM');

// Enable decoder
ws.enableDecoder('POCSAG');
```

Backend handling:

```python
@socketio.on('vfo_tune')
def handle_vfo_tune(data):
    vfo_id = data['vfo_id']
    freq = data['frequency']
    vfo = app.vfo_manager.get_vfo(vfo_id)
    if vfo:
        vfo.set_frequency(freq)
```

### Emitting Real-time Data

```python
from app.api.websocket_handlers import emit_spectrum_data, emit_decoder_result

# Send spectrum
emit_spectrum_data(socketio, vfo_id, spectrum_array)

# Send decoder result
emit_decoder_result(socketio, decoder_result)
```

### Visualization

Custom canvas visualizations in JavaScript:

```javascript
class CustomVisualizer {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
    }
    
    draw(data) {
        const {width, height} = this.canvas;
        
        // Clear
        this.ctx.fillStyle = '#1a1a1a';
        this.ctx.fillRect(0, 0, width, height);
        
        // Draw your visualization
        this.ctx.strokeStyle = '#00ffff';
        this.ctx.beginPath();
        // ...
    }
}
```

## Testing

### Unit Tests

```python
import unittest
from app.decoders.pocsag import POCSAGDecoder
import numpy as np

class TestPOCSAG(unittest.TestCase):
    def setUp(self):
        self.decoder = POCSAGDecoder()
    
    def test_frame_detection(self):
        # Create test signal
        test_data = np.ones(48000, dtype=np.float32)
        result = self.decoder.decode_audio(test_data)
        # Assert
        self.assertIsNone(result)  # No actual frame
    
    def test_reset(self):
        self.decoder.reset()
        self.assertEqual(len(self.decoder.symbol_buffer), 0)
```

### Integration Tests

```python
def test_vfo_pipeline():
    """Test full signal processing pipeline"""
    # Create components
    vfo = VFO(0, 100e6, 20e6)
    decoder = POCSAGDecoder()
    
    # Simulate IQ data
    t = np.arange(20000) / 20e6
    iq_data = np.exp(2j * np.pi * 1e6 * t).astype(np.complex64)
    
    # Process
    audio = vfo.process_iq(iq_data)
    result = decoder.decode_audio(audio)
    
    # Verify
    assert audio is not None
    assert result is None  # No actual POCSAG signal
```

## Performance Optimization

### Vectorization

```python
# Slow
for i in range(len(data)):
    output[i] = data[i] * 0.5

# Fast
output = data * 0.5
```

### Memory Efficiency

```python
# Pre-allocate buffers
buffer = np.zeros(chunk_size, dtype=np.complex64)

# Reuse buffers
fft_buffer = np.fft.fft(buffer, n=4096)
```

### Threading

```python
import threading

def process_iq(self, iq_data):
    def process_thread():
        for vfo in self.vfos:
            vfo.process_iq(iq_data)
    
    thread = threading.Thread(target=process_thread)
    thread.start()
```

## Debugging

### Logging

```python
import logging

logger = logging.getLogger(__name__)

logger.debug("Debug message")
logger.info("Info message")
logger.warning("Warning message")
logger.error("Error message")
```

### Signal Inspection

```python
# Save IQ data for offline analysis
iq_file = open('iq_data.bin', 'wb')
iq_file.write(iq_data.tobytes())
iq_file.close()

# Analyze with external tools:
# - GQRX
# - GNU Radio
# - CubicSDR
```

## Contributing Decoders

To contribute a decoder:

1. Follow the base class interface
2. Add docstrings and comments
3. Include unit tests
4. Update `__init__.py` to export
5. Register in Flask app initialization
6. Document usage in README

Example pull request checklist:

- [ ] Decoder class implementation
- [ ] Unit tests with >80% coverage
- [ ] Integration with VFO system
- [ ] WebSocket event handlers
- [ ] Frontend UI controls (optional)
- [ ] Documentation and examples
- [ ] Performance tested

---

**Happy decoding! 🛰️**
