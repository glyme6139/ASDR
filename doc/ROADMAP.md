# 🗺️ ASDR Roadmap & TODO

Future enhancements and features for ASDR development.


"ADS-B", freq:"1090 MHz", cat:"aviation", diff:"easy", color:"#185FA5", tbg:"#E6F1FB", desc:"Aircraft broadcast position, speed and altitude in real time. Instant live radar with dump1090. Best starter project.", tool:"dump1090 / tar1090" },
"ACARS", freq:"129.125 MHz", cat:"aviation", diff:"easy", color:"#185FA5", tbg:"#E6F1FB", desc:"Short text messages between aircraft and ground stations (maintenance, fuel, clearances). Readable and interesting.", tool:"acarsdec" },
"VOR", freq:"108–118 MHz", cat:"aviation", diff:"medium", color:"#185FA5", tbg:"#E6F1FB", desc:"Navigation beacons for aircraft. You can decode the radial bearing signal from nearby airports.", tool:"SDR++ / custom" },
"APRS", freq:"144.800 MHz", cat:"amateur", diff:"easy", color:"#3B6D11", tbg:"#EAF3DE", desc:"Amateur packet radio — position reports, weather, messages. Very active across France and especially in the Alps.", tool:"Direwolf / APRSIS32" },
"FT8", freq:"HF (e.g. 14.074 MHz)", cat:"amateur", diff:"medium", color:"#3B6D11", tbg:"#EAF3DE", desc:"Weak-signal digital ham mode. Needs an upconverter with HackRF. Incredible DX — worldwide contacts on milliwatts.", tool:"WSJT-X + upconverter" },
"WSPR", freq:"HF (e.g. 14.0956 MHz)", cat:"amateur", diff:"medium", color:"#3B6D11", tbg:"#EAF3DE", desc:"Whisper mode — ultra-weak beacons used to map radio propagation. Hundreds of stations visible at once.", tool:"WSJT-X + upconverter" },
"DMR", freq:"430–470 MHz", cat:"amateur", diff:"medium", color:"#3B6D11", tbg:"#EAF3DE", desc:"Digital Mobile Radio used by licensed hams and some emergency services. Voice + data decoded in real time.", tool:"DSD+ / SDRTrunk" },
"Meteor-M LRPT", freq:"137.1 / 137.9 MHz", cat:"weather", diff:"medium", color:"#533AB7", tbg:"#EEEDFE", desc:"Russian weather satellite — digital replacement for old NOAA APT. Good image quality, passes every ~100 min.", tool:"SatDump" },
"GOES HRIT", freq:"1694.1 MHz", cat:"satellite", diff:"hard", color:"#854F0B", tbg:"#FAEEDA", desc:"Geostationary weather satellite (visible from Europe via MSG). Full-disc Earth imagery. Requires dish + LNA.", tool:"SatDump" },
"Metop HRPT", freq:"1701.3 MHz", cat:"satellite", diff:"hard", color:"#854F0B", tbg:"#FAEEDA", desc:"European polar weather satellite — very high resolution imagery. Requires tracking dish and L-band LNA.", tool:"SatDump" },
"POCSAG", freq:"~466 MHz", cat:"emergency", diff:"easy", color:"#993C1D", tbg:"#FAECE7", desc:"Pager protocol still heavily used in France by SAMU (15), firefighters and hospitals. Plaintext messages visible.", tool:"PDW / GQRX+multimon" },
"TETRA", freq:"380–400 MHz", cat:"emergency", diff:"hard", color:"#993C1D", tbg:"#FAECE7", desc:"Digital trunked radio for French police (INPT network). Metadata decodable; voice is encrypted.", tool:"OSP-TETRA / SDRTrunk" },
"PMR446", freq:"446 MHz", cat:"emergency", diff:"easy", color:"#993C1D", tbg:"#FAECE7", desc:"Licence-free walkie-talkie band. Mountain rescue teams, ski patrol, and hikers all use this. Analogue FM.", tool:"GQRX / SDR++" },
"rtl_433 devices", freq:"433.92 MHz", cat:"iot", diff:"easy", color:"#0F6E56", tbg:"#E1F5EE", desc:"Dozens of sensors: weather stations, doorbells, door sensors, smoke detectors, power meters. One tool catches all.", tool:"rtl_433" },
"LoRaWAN TTN", freq:"868 MHz", cat:"iot", diff:"medium", color:"#0F6E56", tbg:"#E1F5EE", desc:"IoT long-range network. Smart city sensors, animal trackers, agriculture monitors. Dense in French towns.", tool:"gr-lora / CyberEther" },
"Zigbee", freq:"2.4 GHz", cat:"iot", diff:"hard", color:"#0F6E56", tbg:"#E1F5EE", desc:"Smart home protocol (Philips Hue, IKEA, etc). Requires HackRF at 2.4 GHz — borderline but doable.", tool:"gr-zigbee / Wireshark" },
"TPMS", freq:"433 MHz", cat:"iot", diff:"easy", color:"#0F6E56", tbg:"#E1F5EE", desc:"Tyre pressure sensors from every passing car — unique IDs, pressure and temperature data. Surprisingly fun.", tool:"rtl_433" },
"DAB+", freq:"174–240 MHz", cat:"digital", diff:"easy", color:"#533AB7", tbg:"#EEEDFE", desc:"Digital radio used across France. Multiple stations per multiplex, with metadata, traffic info and slideshows.", tool:"welle.io" },
"RDS", freq:"87.5–108 MHz", cat:"digital", diff:"easy", color:"#533AB7", tbg:"#EEEDFE", desc:"Subcarrier on FM radio. Station name, song title, traffic messages (TMC), clock sync. Classic beginner decode.", tool:"redsea / SDR++" },
"FLEXFM / FLEX pager", freq:"169 MHz", cat:"infra", diff:"medium", color:"#888780", tbg:"#F1EFE8", desc:"Higher-speed pager protocol, used alongside POCSAG in French infrastructure.", tool:"multimon-ng" },
"EDF Linky RF", freq:"~169 MHz (FSK)", cat:"infra", diff:"hard", color:"#888780", tbg:"#F1EFE8", desc:"French smart electricity meters communicate on the ERDF radio mesh. Decode your own meter's transmissions.", tool:"custom GNU Radio" },
"GSM", freq:"900 / 1800 MHz", cat:"infra", diff:"hard", color:"#888780", tbg:"#F1EFE8", desc:"2G mobile. Downlink decodable (control channels, IMSI catcher detection). Legal to receive, not to intercept calls.", tool:"gr-gsm / Wireshark" },
"DECT", freq:"1880–1900 MHz", cat:"infra", diff:"hard", color:"#888780", tbg:"#F1EFE8", desc:"Cordless phones. Frame structure decodable; audio may be encrypted depending on handset.", tool:"deDECTed / gr-dect2" },
"ISS APRS", freq:"145.825 MHz", cat:"satellite", diff:"easy", color:"#854F0B", tbg:"#FAEEDA", desc:"The International Space Station relays APRS packets. Listen during a pass (~10 min) and decode callsigns worldwide.", tool:"Direwolf + Gpredict" },
"Radiosondes RS41", freq:"400–406 MHz", cat:"weather", diff:"easy", color:"#533AB7", tbg:"#EEEDFE", desc:"Weather balloon telemetry — GPS position, altitude, temperature, humidity. Mountains = perfect reception. Can recover them!", tool:"radiosonde_auto_rx" },
"SAME / EAS", freq:"162.4–162.55 MHz", cat:"weather", diff:"easy", color:"#533AB7", tbg:"#EEEDFE", desc:"NOAA-style weather alert tones (also used by Météo-France on some VHF channels). Digital header + audio.", tool:"multimon-ng" },
"Inmarsat STD-C", freq:"1.5 GHz L-band", cat:"satellite", diff:"medium", color:"#854F0B", tbg:"#FAEEDA", desc:"Maritime/aviation safety texts from geostationary satellites. Decodable with patch antenna — no tracking needed.", tool:"SatDump / iridium-toolkit" },
"SNCF GSM-R", freq:"876–880 / 921–925 MHz", cat:"transport", diff:"hard", color:"#5F5E5A", tbg:"#F1EFE8", desc:"Railway communication network used by SNCF trains. GSM-based, control channels visible. Voice encrypted.", tool:"gr-gsm" },
"Iridium", freq:"1616–1626 MHz", cat:"satellite", diff:"medium", color:"#854F0B", tbg:"#FAEEDA", desc:"LEO satellite phone network. Metadata, paging and some message types decodable. Global coverage overhead.", tool:"iridium-toolkit" },
"P25 Phase 1", freq:"varies (150/400/800 MHz)", cat:"emergency", diff:"medium", color:"#993C1D", tbg:"#FAECE7", desc:"Digital trunked voice (US standard, some EU use). Unencrypted channels fully decodable including voice.", tool:"SDRTrunk / DSD+" },


You can filter by category — a few highlights worth noting for your mountain context specifically:

**Best starting points** (easy wins, no extra hardware): ADS-B (#1), APRS (#4), radiosondes (#25), TPMS (#17), RDS (#19), POCSAG (#11), rtl_433 sensors (#14).

**Mountain-specific gems:**
- **Radiosondes** (#25) are arguably the best mountain activity — you get excellent line of sight at altitude, can track balloons in real time, and some people actually hike to recover them. Météo-France launches from multiple sites.
- **PMR446** (#13) is everywhere in the Alps — ski patrol, mountain rescue (PGHM), hikers. Pure FM, trivial to decode.
- **APRS** (#4) is very active in the Alps with digipeaters on peaks relaying packets from hikers and climbers.

**Things needing extra hardware:**
- FT8/WSPR (#5, #6) need an **upconverter** to reach HF with the HackRF.
- Metop/GOES satellites (#9, #10) need a **dish + LNA**.
- Zigbee (#16) pushes the HackRF to its upper frequency limit (2.4 GHz) — works but marginal.



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
