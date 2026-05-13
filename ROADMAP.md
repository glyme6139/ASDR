# 🗺️ ASDR Roadmap & TODO

Future enhancements and features for ASDR development.

## Phase 2: Recording & Replay

### IQ Recording
- [ ] Record raw IQ data to disk
- [ ] Support multiple file formats (WAV, RAW, IQ)
- [ ] Configurable recording length/size limits
- [ ] Metadata logging (frequency, gain, timestamp)
- [ ] File browser in UI

### Playback
- [ ] Playback recorded IQ files
- [ ] Frame-by-frame stepping
- [ ] Speed control
- [ ] Time scrubber

### Use Cases
- [ ] Offline signal analysis
- [ ] Replay for testing decoders
- [ ] Signal archival and sharing

---

## Phase 3: Signal Classification & ML

### Automatic Signal Detection
- [ ] Spectrum-based anomaly detection
- [ ] Signal classification (modulation type)
- [ ] Machine learning model integration
- [ ] Training with known signals

### Frequency Scanning
- [ ] Automated frequency sweep
- [ ] Activity detection and logging
- [ ] Heat map generation
- [ ] Time-of-day analysis

### Database
- [ ] Store frequency history
- [ ] Signal event logging
- [ ] Query and analysis tools
- [ ] Frequency database export

---

## Phase 4: Advanced Decoders

### Priority 1: High Value
- [ ] **DMR** (Digital Mobile Radio)
- [ ] **P25** (Phase 2 emergency radio)
- [ ] **D-Star** (Amateur digital)
- [ ] **NOAA APT** (Weather satellite imagery)
- [ ] **TETRA** (European emergency services)

### Priority 2: Specialized
- [ ] **YSF** (C4FM Amateur)
- [ ] **NXDN** (Digital Land Mobile)
- [ ] **M17** (Open digital standard)
- [ ] **Weather data** (SAME, EAS)
- [ ] **AM/FM RDS+** (Enhanced RDS)

### Priority 3: Fun/Experimental
- [ ] **Morse code** (CW decoder)
- [ ] **RTTY** (Radioteletype)
- [ ] **SSTV** (Slow-scan TV)
- [ ] **Packet radio** (AX.25)
- [ ] **FSK Bell** (Telephone data)

---

## Phase 5: User Interface Enhancements

### Frontend Improvements
- [ ] Custom color schemes
- [ ] Layout customization (drag/drop panels)
- [ ] Fullscreen spectrum/waterfall
- [ ] Keyboard-centric mode

### Data Visualization
- [ ] Heatmap display
- [ ] Signal constellation diagram
- [ ] Eye diagram
- [ ] Spectrogram with zoom

### Controls
- [ ] VFO grouping/linking
- [ ] Multi-VFO sync control

---

## Phase 8: Performance & Scalability

### CPU Optimization
- [ ] GPU acceleration (CUDA/OpenCL)
- [ ] SIMD vectorization
- [ ] Lock-free data structures
- [ ] Memory pooling
- [ ] CPU affinity management

### Streaming
- [ ] IQ streaming to network
- [ ] Real-time sync with multiple instances
- [ ] High-speed USB optimization
- [ ] PCI express interface support

### Testing
- [ ] Benchmark suite
- [ ] Profiling tools
- [ ] Memory leak detection
- [ ] Load testing

---

## Phase 9: Analysis Tools

### Signal Analysis
- [ ] Automatic modulation classification
- [ ] Signal quality metrics (SNR, SINAD)
- [ ] Frequency stability analysis
- [ ] Phase noise measurement
- [ ] Intermodulation analysis

### Tools
- [ ] Spectrum analyzer mode
- [ ] Network analyzer mode
- [ ] Time-domain analysis
- [ ] Frequency counter
- [ ] Phase meter

### Reports
- [ ] Signal strength over time
- [ ] Frequency activity report
- [ ] Decoder statistics
- [ ] Export to CSV/PDF

---

## Phase 10: Automation & Integration

### External Integration
- [ ] REST API client support
- [ ] WebSocket client libraries
- [ ] Python SDK
- [ ] JavaScript library
- [ ] Command-line interface

### Scripting
- [ ] Python automation scripts
- [ ] Frequency sweeping scripts
- [ ] Scheduled decoding
- [ ] Conditional alerting
- [ ] Custom workflows

### Plugins
- [ ] Plugin architecture
- [ ] Custom decoder plugins
- [ ] UI widget plugins
- [ ] Data sink plugins
- [ ] Package manager

---
## Performance Metrics (Target)

| Metric | Current | Target |
|--------|---------|--------|
| Spectrum update rate | 30 Hz | 60 Hz |
| Waterfall latency | 100ms | 50ms |
| Decoder decode time | <100ms | <50ms |
| Web UI responsiveness | Good | Excellent |
| Memory usage | ~200MB | ~150MB |
| CPU usage (idle) | ~5% | ~2% |

---

## Known Limitations
- [ ] No TX support yet
- [ ] Limited decoder count
