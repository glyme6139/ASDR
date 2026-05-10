# ASDR Desktop Application

**Advanced Software Defined Radio** - A native desktop application built with PySide6 and pyqtgraph.

## Features

- **Real-time Spectrum Analyzer** - Fast FFT-based spectrum display powered by pyqtgraph
- **Waterfall Display** - Time-frequency waterfall visualization for signal monitoring
- **VFO Control** - Virtual frequency oscillator with adjustable center frequency and bandwidth
- **Decoders** - Support for POCSAG, RDS, AIS, and ADS-B packet decoding
- **Device Settings** - LNA/VGA gain control, sample rate adjustment
- **Dark Theme** - Modern, optimized dark UI for extended viewing

## System Requirements

- **OS**: Windows 10+ (Linux/macOS support possible with minimal changes)
- **Python**: 3.10+
- **HackRF Hardware**: USB HackRF One SDR device
- **RAM**: 2GB minimum, 4GB recommended
- **CPU**: Dual-core 2GHz or faster

## Installation

### 1. Clone or Download the Repository

```bash
cd C:\Users\<YourUsername>\Desktop\ASDR
```

### 2. Create Virtual Environment (if not already done)

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- **PySide6** - Qt framework for native UI
- **pyqtgraph** - High-performance plotting library
- **numpy/scipy** - Signal processing
- **python-hackrf** - HackRF hardware interface

## Running the Application

### Method 1: Direct Launch
```bash
python desktop_app.py
```

### Method 2: Using Launcher Script
```bash
python launch_desktop.py
```

### Method 3: From Command Line (Windows)
```cmd
.venv\Scripts\python.exe desktop_app.py
```

## Usage

1. **Connect Hardware**
   - Plug in HackRF One USB device
   - App will auto-detect and connect on startup

2. **Set Frequency** (VFO Panel)
   - Enter center frequency in MHz
   - Adjust bandwidth as needed
   - Select demodulation mode

3. **Monitor Spectrum**
   - Spectrum pane shows real-time FFT
   - Waterfall pane shows time-frequency history
   - Both update automatically at ~30 Hz

4. **Enable Decoders**
   - Check decoders in right panel to enable
   - Decoder output appears below decoder list

5. **Adjust Gains**
   - Use LNA and VGA sliders to optimize signal level
   - Range: 0-40 dB (LNA), 0-62 dB (VGA)

## Architecture

### Main Components

- **`desktop_app.py`** - Application entry point
- **`app/desktop/main_window.py`** - Main UI window
- **`app/desktop/visualizations.py`** - Spectrum/waterfall rendering
- **`app/desktop/control_panels.py`** - VFO, decoders, device settings UI
- **`app/desktop/sdr_worker.py`** - Background SDR processing thread

### Signal Flow

```
HackRF Hardware
      |
      v
SDRWorkerThread (background)
      |
      +-> IQ Samples -> FFT -> Spectrum Data
      |                          |
      |                          v
      |                   Spectrum Visualizer
      |
      +-> Spectrum Row -> Waterfall Row
                              |
                              v
                        Waterfall Visualizer
```

## Performance Notes

- Spectrum updates at ~30 Hz (configurable via CONFIG)
- Waterfall maintains ~100 rows of history
- Decimation factor adjustable (default 4x) to reduce render load
- Efficient use of typed arrays and direct canvas blitting

## Development

### Adding New Decoders

Edit `app/desktop/control_panels.py`:
```python
decoders = ['POCSAG', 'RDS', 'AIS', 'ADS-B', 'YOUR_DECODER']
```

Then implement decoding logic in backend and wire output to UI.

### Adjusting Performance

Edit `app/desktop/main_window.py`:
```python
SPECTRUM_UPDATE_RATE = 30  # Hz
WATERFALL_HISTORY = 100    # rows
SPECTRUM_RENDER_DECIMATION = 4  # bin grouping
```

### Adding New UI Controls

1. Create control in `app/desktop/control_panels.py`
2. Wire signal to handler in `app/desktop/main_window.py`
3. Implement backend method in `app/desktop/sdr_worker.py`

## Troubleshooting

### HackRF Not Detected
- Check USB cable connection
- Install libusb drivers (Windows: Zadig utility)
- Verify device in Device Manager

### Slow Performance
- Reduce window sizes (spectrum pane takes most CPU)
- Increase SPECTRUM_RENDER_DECIMATION
- Lower SPECTRUM_UPDATE_RATE
- Close other applications

### Import Errors
- Verify all packages installed: `pip list`
- Reinstall if needed: `pip install --force-reinstall PySide6`

## Future Enhancements

- [ ] Save/load frequency bookmarks
- [ ] Recording IQ data to file
- [ ] Real-time signal strength indicator
- [ ] Multi-VFO support
- [ ] Plugin system for custom decoders
- [ ] GQRX-style frequency dialog
- [ ] Macro recording for automation

## License

Open source - modify and distribute freely.

## Contact & Support

For issues or feature requests, refer to project documentation.
