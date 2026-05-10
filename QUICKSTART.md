# 🚀 ASDR Quick Start Guide

## 60-Second Setup

### 1. Prerequisites
- Python 3.8+ installed
- HackRF One (optional - simulator mode available)

### 2. Quick Install
```bash
# Windows
run.bat

# Linux/macOS
bash run.sh
```

### 3. Open Browser
```
http://localhost:5000
```

---

## First Steps

### Connect Hardware
1. Click **"Connect Device"** button
2. Status indicator changes to green
3. HackRF auto-detected or simulator mode activates

### Create Your First VFO
1. Left panel → **"+ Create VFO"**
2. Enter frequency (e.g., `100000000` for 100 MHz)
3. VFO appears in list

### Tune & Listen
1. Select demodulation mode (NFM, WFM, AM, etc.)
2. Adjust **LNA Gain** and **VGA Gain** sliders
3. Watch **Spectrum** and **Waterfall** displays
4. Monitor **Signal Strength**

### Enable Decoders
1. Left panel → **"Decoders"** section
2. Click checkbox (☐) to enable
3. Results appear in right panel

### Save Frequencies
1. Right panel → Enter frequency name
2. Click **"Add Bookmark"**
3. Click bookmark to tune VFO to that frequency

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+R` / `Cmd+R` | Connect Device |
| `Ctrl+N` / `Cmd+N` | Create New VFO |

---

## Common Tasks

### Change Center Frequency
```
Device Settings → Center Frequency (MHz) → Enter value → Click "Set"
```

### Monitor Multiple Frequencies
```
Create multiple VFOs with different frequencies
Each has independent demod and volume
Listen to all simultaneously
```

### Enable Signal Decoding
```
Left panel → Decoders → Click checkbox
Results stream to right panel in real-time
```

### Save Frequency List
```
Right panel → Bookmarks
Add Name + Frequency
Export/Import via browser storage
```

---

## Demodulation Modes

| Mode | Use Case | Bandwidth |
|------|----------|-----------|
| **NFM** | Ham radio, PMR, walkie-talkie | 12.5 kHz |
| **WFM** | Broadcast FM radio | 200 kHz |
| **AM** | AM broadcasts | 10 kHz |
| **USB** | Amateur radio SSB (upper) | 2.7 kHz |
| **LSB** | Amateur radio SSB (lower) | 2.7 kHz |
| **DSB** | Double sideband | 6 kHz |
| **CW** | Morse code | 500 Hz |
| **IQ** | Raw in-phase/quadrature | 1 MHz |

---

## Decoders

### POCSAG
- **Frequency**: 139-161 MHz (varies by country)
- **Baud rate**: 1200
- **Demod**: NFM
- **Use**: Paging networks

### RDS
- **Frequency**: 87.5-108 MHz
- **Attached to**: WFM
- **Decodes**: Station name, program info
- **Use**: FM broadcast data

### AIS
- **Frequency**: 156-162 MHz
- **Baud rate**: 9600
- **Demod**: NFM
- **Use**: Maritime vessel tracking

### ADSB
- **Frequency**: 1090 MHz
- **Baud rate**: Variable
- **Demod**: IQ
- **Use**: Aircraft tracking

---

## Signal Monitoring

### Spectrum Analyzer
- Shows real-time frequency content
- Peak = strongest signal in band
- Cyan line = current activity

### Waterfall Display
- Time-frequency visualization
- Y-axis = frequency, X-axis = time
- Color = signal strength
- Bright = strong signal

### Signal Strength
- Displayed per VFO
- Units: dBm
- Threshold triggers squelch

---

## Hardware Settings

### LNA Gain (0-40 dB)
- **Low** (0-20): Less noise, reduced sensitivity
- **High** (30-40): Maximum sensitivity, more noise

### VGA Gain (0-62 dB)
- **Low** (0-20): Weak signals, clean
- **High** (40-62): Strong signals captured

### Center Frequency
- Main RF tuning point
- All VFOs are offsets from this

---

## Troubleshooting

### HackRF Not Detected
✅ **Solution**: Click "Connect Device" - simulator mode activates automatically

### No Sound/Audio
✅ **Check**:
- VFO volume slider (not muted)
- Squelch level (set too high?)
- Demod mode (correct for signal?)

### High CPU Usage
✅ **Reduce**:
- Waterfall history size
- FFT size
- Number of active VFOs
- Disable unused decoders

### Decoder No Output
✅ **Check**:
- Decoder enabled (checkbox ☑️)
- Correct frequency for signal
- Correct demod mode
- Signal strength adequate

---

## Frequency Ranges

| Band | Frequency | Use |
|------|-----------|-----|
| **VHF** | 30-300 MHz | FM radio, aviation, marine |
| **UHF** | 300-3000 MHz | Cell, WiFi, amateur radio |
| **L-band** | 1-2 GHz | GPS, satellite |
| **S-band** | 2-4 GHz | Weather radar, satellite |

---

## Tips & Tricks

### 1. Signal Hunting
- Start at center frequency near known signals
- Scan frequency band with multiple VFOs
- Watch waterfall for activity
- Increase gain for weak signals

### 2. Noise Reduction
- Use squelch to gate noise
- Select narrower bandwidth
- Apply de-emphasis filter (FM only)
- Increase LNA gain, reduce VGA gain

### 3. Multiple Monitoring
- Create VFO for each frequency of interest
- Set different demod modes per VFO
- All decode in parallel
- Click bookmark to switch focus

### 4. Power Saving
- Reduce spectrum update rate
- Lower waterfall history
- Disable unused decoders
- Close browser tabs

---

## API Reference (Advanced)

### REST Endpoints
```
GET  /api/device/info              - Device status
POST /api/device/frequency         - Set frequency
GET  /api/vfo/list                 - List VFOs
POST /api/vfo/create               - New VFO
POST /api/decoders/<name>/enable   - Enable decoder
```

### WebSocket Events
```javascript
ws.tuneVFO(vfoId, frequency);           // Tune VFO
ws.setVFODemod(vfoId, mode);            // Change demod
ws.enableDecoder(name);                  // Enable decoder
```

---

## External Resources

- 📖 [HackRF Documentation](https://github.com/greatscottgadgets/hackrf)
- 📚 [GNU Radio Wiki](https://wiki.gnuradio.org/)
- 🛰️ [POCSAG Info](https://en.wikipedia.org/wiki/POCSAG)
- 🚢 [AIS Standard](https://en.wikipedia.org/wiki/Automatic_Identification_System)
- ✈️ [ADSB Format](https://en.wikipedia.org/wiki/Automatic_dependent_surveillance%E2%80%93broadcast)

---

## Getting Help

### Check Logs
```bash
# Application logs appear in terminal
# Look for error messages
```

### Debug Mode
```bash
python run.py --debug
```

### Documentation
- See **README.md** for detailed guide
- See **DEVELOPER_GUIDE.md** for extending
- Check **config/settings.py** for configuration

---

## Next Steps

- 📖 Read **README.md** for full feature list
- 🔧 Read **DEVELOPER_GUIDE.md** to create custom decoders
- 🎛️ Explore different frequencies and decoders
- 📡 Create bookmarks for your favorite frequencies
- 💾 Record interesting signals for offline analysis

---

**Enjoy SDR receiving! 🛰️**

*Questions? Check the documentation files or read the source code!*
