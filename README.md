# 🛰️ ASDR - Advanced Software Defined Radio Receiver

A powerful, feature-rich web-based SDR (Software Defined Radio) receiver application built with Flask and Python. Designed for HackRF One but adaptable to other SDR hardware.

## ✨ Features

### 🎯 Multi-VFO Mastery
- Create unlimited Virtual Frequency Oscillators (VFOs)
- Each VFO has independent:
  - Frequency tuning
  - Demodulation modes
  - Volume control
  - Squelch settings
  - DSP parameters
- Listen to multiple frequencies simultaneously

### 📊 Visualization
- **Real-time Spectrum Analyzer**: GPU-accelerated WebGL display
- **Waterfall Display**: Dynamic frequency activity visualization
- **Frequency Activity Scanner**: Spot active signals instantly
- **Interactive Controls**: Click-to-tune interface

### 📻 Demodulation Support
- **NFM** (Narrow FM) - Ham radio, PMR
- **WFM** (Wide FM) - Broadcast FM
- **AM** - AM broadcasts
- **USB/LSB** - Amateur radio SSB
- **DSB** - Double sideband
- **CW** - Morse code
- **IQ** - Raw IQ data

### 📡 Decoder System
Modular, extensible decoder framework:
- **POCSAG**: Paging network decoding
- **RDS**: FM RadioData System (station name, programs, text)
- **Easily add more**: Framework supports custom decoders

### 🔖 Advanced Bookmarking
- Save favorite frequencies with custom names
- Organize bookmarks into categories
- One-click frequency recall
- Export/import bookmark collections

### 🎛️ Full DSP Toolset
- Squelch (noise gate) control
- Noise reduction
- De-emphasis filters (75µs for FM)
- Per-VFO volume control
- Real-time signal strength monitoring

### 🔊 Audio Management
- Multi-output support
- Independent audio routing
- Volume and gain control

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- HackRF One (or simulator mode)
- Git

### Installation

1. **Clone or extract the project**
   ```bash
   cd ASDR
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure settings (optional)**
   ```bash
   cp .env.example .env
   # Edit .env with your preferences
   ```

5. **Run the application**
   ```bash
   python run.py
   ```

6. **Open in browser**
   ```
   http://localhost:5000
   ```

## 📋 Usage

### Getting Started
1. Click "Connect Device" to initialize HackRF
2. Set center frequency (MHz)
3. Create a VFO by clicking "+ Create VFO"
4. Tune to desired frequency
5. Select demodulation mode
6. Enable desired decoders
7. Monitor spectrum and waterfall in real-time

### Creating a VFO
1. In left panel, click "+ Create VFO"
2. Enter frequency in Hz
3. VFO will appear with current signal strength
4. Adjust:
   - **Frequency**: Click "Edit Freq" button
   - **Demod Mode**: Change in VFO settings
   - **Volume**: Adjust slider
   - **Squelch**: Set noise gate level

### Using Decoders
1. Select decoders in the "🔍 Decoders" section
2. Click checkbox (☐/☑️) to enable/disable
3. Active decoders will display results in "📡 Decoder Output" panel
4. Each result shows:
   - Decoder name
   - Decoded data
   - Timestamp
   - Confidence level

### Bookmarking Frequencies
1. In right panel, enter frequency details:
   - Name: e.g., "Local Repeater"
   - Frequency: in Hz
2. Click "Add Bookmark"
3. Click bookmark to tune VFO to that frequency

### Adjusting Hardware Gains
1. Use sliders in Device Settings:
   - **LNA Gain**: Receive amplification (0-40 dB)
   - **VGA Gain**: Variable gain (0-62 dB)
2. Sliders update in real-time

## 🏗️ Architecture

```
ASDR/
├── app/
│   ├── sdr/
│   │   ├── vfo.py              # VFO management system
│   │   └── hackrf_receiver.py  # HackRF interface
│   ├── decoders/
│   │   ├── base.py             # Decoder base classes
│   │   ├── pocsag.py           # POCSAG decoder
│   │   └── rds.py              # RDS decoder
│   ├── api/
│   │   ├── __init__.py         # REST API routes
│   │   └── websocket_handlers.py  # Real-time WebSocket
│   └── __init__.py             # Flask app factory
├── static/
│   ├── js/
│   │   ├── websocket.js        # WebSocket client
│   │   ├── visualization.js    # Spectrum/waterfall
│   │   ├── ui-controls.js      # UI event handlers
│   │   ├── config.js           # Configuration
│   │   └── app.js              # Main app logic
│   └── css/
│       └── style.css           # Styling
├── templates/
│   └── index.html              # Main UI
├── config/
│   └── settings.py             # Configuration
├── data/
│   ├── bookmarks.json          # Saved bookmarks
│   └── settings.json           # User settings
├── run.py                       # Entry point
└── requirements.txt            # Dependencies
```

## 🔧 Configuration

Edit `config/settings.py` to customize:

```python
# HackRF settings
HACKRF_SAMPLE_RATE = 20_000_000  # 20 MHz
HACKRF_CENTER_FREQ = 100_000_000  # 100 MHz default

# VFO settings
MAX_VFOS = 10
DEFAULT_BANDWIDTH = 200_000

# Demodulation modes
DEMOD_MODES = {
    'NFM': {'bandwidth': 12_500},
    'WFM': {'bandwidth': 200_000},
    # ...
}

# Enabled decoders
ENABLED_DECODERS = ['POCSAG', 'RDS']
```

## 📡 Creating Custom Decoders

The modular decoder system makes it easy to add new decoders:

```python
from app.decoders.base import BaseAudioDecoder, DecoderResult
import time

class MyDecoder(BaseAudioDecoder):
    def __init__(self):
        super().__init__("MyDecoder", sample_rate=48000)
    
    def decode_audio(self, audio):
        # Your decoding logic here
        if detected_something:
            return DecoderResult(
                decoder_name="MyDecoder",
                timestamp=time.time(),
                data="Decoded message",
                confidence=0.95,
            )
        return None
    
    def reset(self):
        # Reset any internal state
        pass

# Register your decoder
from app.decoders.base import get_decoder_registry
registry = get_decoder_registry()
registry.register(MyDecoder())
```

## 🌊 API Endpoints

### Device Control
- `POST /api/device/connect` - Connect to HackRF
- `POST /api/device/disconnect` - Disconnect
- `POST /api/device/frequency` - Set center frequency
- `POST /api/device/gain/lna` - Set LNA gain
- `POST /api/device/gain/vga` - Set VGA gain
- `GET /api/device/info` - Get device information

### VFO Management
- `GET /api/vfo/list` - List all VFOs
- `POST /api/vfo/create` - Create new VFO
- `POST /api/vfo/<id>/delete` - Delete VFO
- `POST /api/vfo/<id>/frequency` - Set VFO frequency
- `POST /api/vfo/<id>/demod` - Set demodulation mode
- `POST /api/vfo/<id>/volume` - Set volume
- `POST /api/vfo/<id>/squelch` - Set squelch

### Decoders
- `GET /api/decoders/list` - List all decoders
- `POST /api/decoders/<name>/enable` - Enable decoder
- `POST /api/decoders/<name>/disable` - Disable decoder

## 🔌 WebSocket Events

Real-time updates via WebSocket:

```javascript
// Subscribe to updates
ws.subscribeSpectrum();      // Spectrum data
ws.subscribeWaterfall();     // Waterfall rows
ws.subscribeDecoders();      // Decoder output
ws.subscribeVFOStatus();     // VFO status updates

// Control via WebSocket
ws.tuneVFO(vfoId, frequency);
ws.setVFODemod(vfoId, mode);
```

## 🎮 Keyboard Shortcuts

- `Ctrl/Cmd + R` - Connect/reconnect device
- `Ctrl/Cmd + N` - Create new VFO

## 📊 Performance Tips

1. **Limit VFOs**: More VFOs = more CPU usage
2. **Reduce Waterfall History**: Lower `WATERFALL_HISTORY` for better performance
3. **Disable Unused Decoders**: Each decoder consumes CPU
4. **Adjust FFT Size**: Smaller FFT = faster but less detail

## 🐛 Troubleshooting

### HackRF Not Detected
- Install libusb drivers
- Check USB connection
- Use simulator mode (automatic fallback)

### High CPU Usage
- Reduce waterfall history size
- Disable unused decoders
- Lower spectrum update rate
- Close other applications

### No Audio Output
- Check volume levels
- Verify squelch settings
- Enable at least one VFO
- Check demodulation mode

## 📚 References

- [HackRF Documentation](https://github.com/greatscottgadgets/hackrf)
- [GNU Radio](https://www.gnuradio.org/)
- [POCSAG Standard](https://en.wikipedia.org/wiki/POCSAG)
- [RDS Standard](https://en.wikipedia.org/wiki/Radio_Data_System)

## 🤝 Contributing

Contributions welcome! Areas for enhancement:

- [ ] Additional decoders (AIS, ADSB, DMR, P25)
- [ ] Recording and playback
- [ ] Signal classification
- [ ] Frequency scan automation
- [ ] Mobile app support
- [ ] Database backend for results
- [ ] Plugin system

## 📄 License

MIT License - See LICENSE file

## 🎯 Roadmap

- [ ] Multi-device support
- [ ] Recording and replay
- [ ] Advanced DSP filters
- [ ] IQ file import/export
- [ ] Automated frequency scanning
- [ ] Remote operation
- [ ] Frequency analysis database
- [ ] Built-in tutorials

## 📞 Support

For issues, questions, or suggestions:
1. Check troubleshooting section
2. Review existing issues
3. Open new issue with:
   - Python version
   - OS
   - Error messages
   - Steps to reproduce

---

**Happy SDR receiving! 🛰️**
