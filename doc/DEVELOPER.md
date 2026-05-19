# ASDR Developer Guide

**Auto SDR (ASDR)** is a Python desktop application for real-time radio signal reception and decoding using a HackRF One SDR. This guide covers architecture, setup, and how to extend the project.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Repository Layout](#repository-layout)
3. [Architecture Overview](#architecture-overview)
4. [Data Flow](#data-flow)
5. [Component Reference](#component-reference)
   - [Entry Points](#entry-points)
   - [SDR Layer](#sdr-layer)
   - [DSP Process](#dsp-process)
   - [VFO System](#vfo-system)
   - [Decoder Framework](#decoder-framework)
   - [Desktop UI](#desktop-ui)
   - [IPC Protocol](#ipc-protocol)
6. [Session Files](#session-files)
7. [Adding a Decoder](#adding-a-decoder)
8. [Logging & Debugging](#logging--debugging)
9. [Key Constants](#key-constants)
10. [Roadmap](#roadmap)

---

## Quick Start

### Prerequisites

- Python 3.10+
- HackRF One (optional — simulation mode is available without hardware)
- Windows 10/11 (primary target; Linux/macOS untested)
- [TETRA voice codec DLL](#tetra-codec) if TETRA voice decoding is needed

### Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Running

```powershell
python desktop_app.py
```

`launch_desktop.py` is a thin wrapper that does the same thing.

### Simulation mode

If no HackRF is connected, the application starts automatically in simulation mode with a synthetic noise source. All UI features work; decoders receive simulated input.

---

## Repository Layout

```
ASDR/
├── desktop_app.py              # Main entry point
├── launch_desktop.py           # Alias for desktop_app.py
├── requirements.txt
├── DEVELOPER.md                # This file
├── DECODER_HOWTO.md            # Step-by-step guide for adding decoders
├── ROADMAP.md                  # Planned features by phase
│
├── app/
│   ├── sdr/
│   │   ├── hackrf_receiver.py  # HackRF USB interface + sim fallback
│   │   ├── vfo.py              # VFO and VFOManager
│   │   └── fm_demod.py         # FM discriminator (atan2-based)
│   │
│   ├── decoders/
│   │   ├── base.py             # BaseDecoder / BaseAudioDecoder / BaseIQDecoder / DecoderResult
│   │   ├── pocsag.py
│   │   ├── rds.py
│   │   ├── ais.py
│   │   ├── adsb.py
│   │   ├── acars.py
│   │   ├── dmr.py
│   │   ├── modulation.py       # Generic modulation feature extractor
│   │   ├── tetra/              # TETRA package (PHY + MAC + voice)
│   │   │   └── __init__.py
│   │   └── __init__.py
│   │
│   └── desktop/
│       ├── desktop_app.py      # (legacy stub — use root desktop_app.py)
│       ├── main_window.py      # ASURMainWindow (QMainWindow)
│       ├── control_panels.py   # VFOPanel, DevicePanel, DecoderPanel, ControlPanel
│       ├── visualizations.py   # SpectrumViewer, WaterfallViewer, VisualizationPanel
│       ├── audio_output.py     # AudioMixer — per-VFO ring buffers + sounddevice
│       ├── dsp_process.py      # DSPProcess — subprocess manager + command proxies
│       ├── sdr_worker.py       # DSPWorker — the actual DSP subprocess (no Qt)
│       ├── ipc_adapter.py      # IPCAdapterThread — bridges result_q to Qt signals
│       ├── signal_id_panel.py  # Signal strength bar chart
│       ├── timing_window.py    # Performance profiler UI
│       ├── timing.py           # TimingConfig + profiler
│       ├── bookmarks.py        # Bookmark load/save
│       └── decoder_windows/
│           ├── base.py         # BaseDecoderWindow
│           ├── tetra_view.py
│           ├── adsb_view.py
│           ├── acars_view.py
│           ├── dmr_view.py
│           └── __init__.py     # WINDOW_REGISTRY
│
├── osmo-tetra/                 # C source for TETRA ACELP voice codec
├── ARTEMIS/                    # SQLite DB + media for signal reference
└── app/session_default.json    # Default device/VFO config
```

---

## Architecture Overview

ASDR uses a **two-process architecture** to keep the Qt UI responsive regardless of DSP load:

```
┌──────────────────────────────────────────────────┐
│  UI Process (Qt)                                 │
│                                                  │
│  ASURMainWindow                                  │
│    ├── VisualizationPanel  ← SharedMemory reads  │
│    ├── ControlPanel                              │
│    ├── DecoderWindows                            │
│    └── IPCAdapterThread (QThread)                │
│           └── polls result_q → Qt signals        │
│                                                  │
│  DSPProcess (manages subprocess lifecycle)       │
│    └── exposes command proxy methods → cmd_q     │
└────────────┬────────────────────────────┬────────┘
             │ cmd_q (UI → DSP)           │ SharedMemory
             │ result_q (DSP → UI)        │ spectrum_buf / waterfall_buf
             ▼                            ▼
┌──────────────────────────────────────────────────┐
│  DSP Subprocess (no Qt)                          │
│                                                  │
│  DSPWorker                                       │
│    ├── HackRFReceiver  (IQ @ 20 MHz)             │
│    ├── VFOManager  (channelization)              │
│    │     └── per-VFO: decimate → demod → audio  │
│    │           └── per-VFO decoders              │
│    ├── AudioMixer  (sounddevice callback)        │
│    └── Display FFT  (32 768 pts → SharedMemory)  │
└──────────────────────────────────────────────────┘
```

**Why two processes?**
Heavy SciPy resampling and FFT would cause dropped Qt paint events. The DSP subprocess can be CPU-bound without affecting UI responsiveness. Crashes in the DSP subprocess do not crash the UI.

**Why `spawn` context on Windows?**
`fork` is unavailable on Windows. Using the explicit `'spawn'` multiprocessing context is required for portability and avoids hidden state leakage.

---

## Data Flow

```
HackRF USB IQ stream (20 MHz, complex64)
        │
        ▼
DSPWorker.run()  ──────────────────────────────────────────────────
        │                                                          │
        │  [For each 4096-sample block]                           │  [Rolling 32768-sample window]
        │                                                          │
        ▼                                                          ▼
VFOManager.process(iq_block)                               Display FFT (Hann window)
   │                                                               │
   │  [Per VFO]                                             Spectrum (float32) ──→ spec_shm
   │  1. Extract frequency bin from full-rate FFT            Waterfall row (uint8) → wf_shm
   │  2. IFFT → narrowband IQ at ~200 kHz                          │
   │  3. resample_poly → 48 kHz                              disp_gen incremented
   │  4. Demodulate (NFM / WFM / AM / SSB / IQ)
   │  5. Run enabled decoders (BaseAudioDecoder or BaseIQDecoder)
   │  6. Enqueue audio into AudioMixer ring buffer
   │
   └─→ DecoderResult dict → result_q
                                    │
                              IPCAdapterThread (UI process)
                                    │
                              Qt signals → DecoderWindows / ControlPanel log
```

**Display refresh:** The UI render timer checks `display_gen` every ~33 ms. If the generation counter has advanced since last frame, it reads `spectrum_buf` and `waterfall_buf` from shared memory without copying.

---

## Component Reference

### Entry Points

| File | Purpose |
|------|---------|
| [desktop_app.py](desktop_app.py) | Creates `QApplication`, instantiates `ASURMainWindow`, calls `app.exec()` |
| [launch_desktop.py](launch_desktop.py) | Convenience wrapper — identical behaviour |

### SDR Layer

**[app/sdr/hackrf_receiver.py](app/sdr/hackrf_receiver.py)**

`HackRFReceiver` wraps `python-hackrf`. On `start()`, it opens the USB device, applies the stored `HackRFConfig`, and launches a background thread that feeds raw IQ samples into a callback. When no hardware is found, `SimulatedHackRF` is substituted automatically.

Key config fields (set via `DSPProcess` command proxies):

| Field | Default | Valid range |
|-------|---------|-------------|
| `center_freq` | 100 MHz | 1 MHz – 6 GHz |
| `sample_rate` | 20 MHz | 2 – 20 MHz |
| `lna_gain` | 24 dB | 0, 8, 16, 24, 32, 40 |
| `vga_gain` | 20 dB | 0 – 62 (2 dB steps) |
| `amp_enabled` | False | bool |

### DSP Process

**[app/desktop/dsp_process.py](app/desktop/dsp_process.py)** — lives in the UI process.

`DSPProcess` owns the subprocess lifecycle and exposes typed proxy methods. Every proxy method calls `_send(msg_dict)` which puts a dict onto `cmd_q`.

All proxy methods are thread-safe (Queue.put_nowait is GIL-protected).

```python
dsp = DSPProcess()
dsp.start()

dsp.set_center_frequency(433_920_000)
dsp.add_vfo(vfo_id=0, freq_hz=433_920_000)
dsp.set_vfo_demod(vfo_id=0, mode='NFM')
dsp.toggle_decoder(vfo_id=0, decoder_name='POCSAG', enabled=True)

dsp.stop()
```

**Shared memory buffers** (read-only from the UI side):

| Attribute | Shape | dtype | Content |
|-----------|-------|-------|---------|
| `dsp.spectrum_buf` | `(32768,)` | float32 | FFT magnitude in dB |
| `dsp.waterfall_buf` | `(32768,)` | uint8 | Colormap-mapped row |
| `dsp.display_gen` | scalar | uint64 | Incremented each new frame |

**[app/desktop/sdr_worker.py](app/desktop/sdr_worker.py)** — runs inside the subprocess.

`DSPWorker` owns the main loop. It drains `cmd_q` at the start of each iteration, then processes one IQ block. It never imports Qt.

### VFO System

**[app/sdr/vfo.py](app/sdr/vfo.py)**

A `VFO` represents one independent receive channel. It stores its own:
- Centre frequency (offset from SDR centre)
- Bandwidth
- Demodulation mode (`NFM` / `WFM` / `AM` / `USB` / `LSB` / `DSB` / `CW` / `IQ`)
- FIR filter state and resampler state (continuous across calls)
- Per-VFO `DecoderRegistry`
- Audio ring buffer

`VFOManager` holds up to 10 `VFO` instances. On each call to `process(iq_block)`, it extracts the narrowband channel for each VFO via bin extraction + IFFT, then calls `vfo.process()`.

**Pre-decimation:** All IQ is pre-decimated to `TARGET_PROC_RATE = 200 kHz` before VFO channelization. This keeps `resample_poly` numerator/denominator small across all valid HackRF sample rates.

### Decoder Framework

**[app/decoders/base.py](app/decoders/base.py)**

```
BaseDecoder (ABC)
├── process(data, audio) → Optional[DecoderResult]   # abstract
├── reset()                                           # abstract
└── format_result(result) → str

BaseAudioDecoder(BaseDecoder)
└── decode_audio(audio: np.ndarray) → Optional[DecoderResult]   # abstract
    Buffer: 100 ms chunks at 48 kHz (min_buffer_size = sample_rate // 10)

BaseIQDecoder(BaseDecoder)
└── decode_iq(iq_data: np.ndarray) → Optional[DecoderResult]    # abstract
    Buffer: 10 ms chunks at 20 MHz (min_buffer_size = sample_rate // 100)
```

`DecoderResult` fields:

| Field | Type | Notes |
|-------|------|-------|
| `decoder_name` | str | e.g. `'POCSAG'` |
| `timestamp` | float | `time.time()` |
| `data` | dict | Must be plain Python — no numpy arrays (crosses IPC queue) |
| `confidence` | float | 0.0 – 1.0 |
| `metadata` | dict | Optional extra fields |

**Implemented decoders:**

| Name | File | Base class | Frequency |
|------|------|-----------|-----------|
| POCSAG | `pocsag.py` | AudioDecoder | ~466 MHz |
| RDS | `rds.py` | AudioDecoder | FM broadcast subcarrier |
| AIS | `ais.py` | AudioDecoder | 161.975 / 162.025 MHz |
| ADSB | `adsb.py` | IQDecoder | 1090 MHz |
| ACARS | `acars.py` | AudioDecoder | 129–136 MHz |
| DMR | `dmr.py` | AudioDecoder | 430–470 MHz |
| TETRA | `tetra/__init__.py` | IQDecoder | 380–400 MHz |
| Modulation | `modulation.py` | IQDecoder | Generic |

See **[DECODER_HOWTO.md](DECODER_HOWTO.md)** for step-by-step instructions to add a new decoder.

### Desktop UI

**[app/desktop/main_window.py](app/desktop/main_window.py)** — `ASURMainWindow`

Orchestrates all subsystems:
- Instantiates `DSPProcess` and starts it
- Creates `VisualizationPanel`, `ControlPanel`, and `DecoderWindows`
- Starts `IPCAdapterThread`, wires its Qt signals to UI slots
- Manages session load/save on open/close

**[app/desktop/visualizations.py](app/desktop/visualizations.py)** — `VisualizationPanel`

`SpectrumViewer` and `WaterfallViewer` are both `pyqtgraph.PlotWidget` subclasses. A 30 Hz `QTimer` reads `spectrum_buf` / `waterfall_buf` from shared memory and redraws if `display_gen` has advanced. Click on the spectrum to tune the active VFO.

**[app/desktop/control_panels.py](app/desktop/control_panels.py)** — `ControlPanel`

Contains:
- `DevicePanel` — centre frequency, sample rate, gain sliders, amp toggle
- `VFOPanel` — per-VFO tab: frequency, mode, bandwidth, squelch, volume, decoder toggles
- `DecoderPanel` — decoder selector + "Open View" button

**[app/desktop/ipc_adapter.py](app/desktop/ipc_adapter.py)** — `IPCAdapterThread`

A `QThread` that polls `result_q` in a tight loop and re-emits results as typed Qt signals. This decouples the DSP subprocess queue from Qt slot invocations, which must happen on the UI thread.

Signals emitted:

| Signal | Payload | Source message type |
|--------|---------|---------------------|
| `decoder_result` | `(vfo_id, decoder_name, data_dict)` | `'decoder_result'` |
| `device_status` | `(status_str)` | `'device_status'` |
| `signal_strength` | `(vfo_id, dbfs)` | `'signal_strength'` |
| `timing_report` | `(report_dict)` | `'timing_report'` |

**[app/desktop/audio_output.py](app/desktop/audio_output.py)** — `AudioMixer`

One `sounddevice` output stream, one ring buffer per VFO. The `sounddevice` callback pulls from all active VFO buffers and mixes them additively (with volume scaling). Ring buffers use a two-slice copy to avoid index-array allocation in the audio hot path.

### IPC Protocol

All messages are plain Python dicts. They must be picklable (no numpy arrays, no Qt objects).

**UI → DSP (`cmd_q`):**

| `cmd` | Additional keys |
|-------|----------------|
| `stop` | — |
| `set_center_frequency` | `freq_hz` |
| `set_sample_rate` | `rate` |
| `set_lna_gain` | `value` |
| `set_vga_gain` | `value` |
| `set_amp_enable` | `enabled` |
| `add_vfo` | `vfo_id`, `freq_hz` |
| `remove_vfo` | `vfo_id` |
| `set_vfo_frequency` | `vfo_id`, `freq_hz` |
| `set_vfo_demod` | `vfo_id`, `mode` |
| `set_vfo_bandwidth` | `vfo_id`, `bandwidth_hz` |
| `set_vfo_squelch` | `vfo_id`, `level_db` |
| `set_vfo_squelch_enabled` | `vfo_id`, `enabled` |
| `set_vfo_muted` | `vfo_id`, `muted` |
| `set_volume` | `vfo_id`, `volume` |
| `toggle_decoder` | `vfo_id`, `decoder_name`, `enabled` |
| `set_timing_sample_count` | `n` |

**DSP → UI (`result_q`):**

| `type` | Additional keys |
|--------|----------------|
| `device_status` | `status` (str) |
| `decoder_result` | `vfo_id`, `decoder`, `data` (dict), `confidence`, `timestamp` |
| `signal_strength` | `vfo_id`, `dbfs` |
| `timing_report` | `data` (dict) |

---

## Session Files

Session state is saved to `~/.asdr_session.json` on close and loaded on startup. If the file is missing, [app/session_default.json](app/session_default.json) is used.

Schema:

```jsonc
{
  "device": {
    "center_freq": 100000000.0,   // Hz
    "sample_rate": 20000000,      // Hz
    "lna": 24,                    // dB
    "vga": 20,                    // dB
    "amp_enabled": false
  },
  "vfos": [
    {
      "name": "VFO 1",
      "frequency": 100000000.0,
      "demod_mode": "NFM",        // NFM | WFM | AM | USB | LSB | DSB | CW | IQ
      "bandwidth": 12500.0,       // Hz
      "volume": 0.8,              // 0.0 – 1.0
      "squelch_level": -100.0,    // dBFS
      "squelch_enabled": true,
      "enabled": true,
      "decoders": []              // list of decoder name strings
    }
  ]
}
```

Frequency bookmarks are stored separately in [bookmarks.json](bookmarks.json) at the project root.

---

## Adding a Decoder

See **[DECODER_HOWTO.md](DECODER_HOWTO.md)** for the complete walkthrough with code templates.

Short checklist:

- [ ] `app/decoders/<name>.py` — decoder class (subclass `BaseAudioDecoder` or `BaseIQDecoder`)
- [ ] `app/decoders/__init__.py` — import + add to `__all__`
- [ ] `app/desktop/control_panels.py` — add name to `DECODER_NAMES`
- [ ] `app/desktop/sdr_worker.py` — add branch in `_make_decoder`
- [ ] *(optional)* `app/desktop/decoder_windows/<name>_view.py` — `BaseDecoderWindow` subclass
- [ ] *(optional)* `app/desktop/decoder_windows/__init__.py` — add to `WINDOW_REGISTRY`

**Critical constraint:** `DecoderResult.data` must be a plain Python dict. No numpy arrays — it crosses `result_q` via pickle.

---

## TETRA Codec

TETRA voice decoding requires a compiled DLL from the C sources in [osmo-tetra/](osmo-tetra/).

Build on Windows (MSYS2/MinGW):

```bash
cd osmo-tetra
gcc -shared -o tetra_codec.dll -O2 src/*.c
```

Place `tetra_codec.dll` in the project root. See `compile tetra.md` for details.

---

## Logging & Debugging

`desktop_app.py` configures root logging at `INFO`. The DSP subprocess uses its own `basicConfig` at `INFO` with a `[DSP <pid>]` prefix.

To enable verbose TETRA lower-MAC tracing:

```python
# already set in desktop_app.py
logging.getLogger('app.decoders.tetra.lower_mac').setLevel(logging.DEBUG)
```

To enable the performance profiler, open **Timing** from the menu. The `TimingConfig` object is passed to the DSP subprocess at startup; `set_timing_sample_count(n)` can adjust it at runtime. Reports arrive as `timing_report` messages on `result_q`.

---

## Key Constants

| Constant | Value | Location |
|----------|-------|----------|
| Default centre freq | 100 MHz | `session_default.json` |
| Default sample rate | 20 MHz | `session_default.json` |
| Default LNA gain | 24 dB | `session_default.json` |
| Default VGA gain | 20 dB | `session_default.json` |
| Max VFOs | 10 | `VFOManager` |
| Display FFT size | 32 768 pts | `dsp_process.py` / `sdr_worker.py` |
| Audio sample rate | 48 kHz | `BaseAudioDecoder` |
| VFO intermediate rate | ~200 kHz | `VFOManager.TARGET_PROC_RATE` |
| Spectrum update rate | 30 Hz | `VisualizationPanel` QTimer |

---

## Roadmap

See **[ROADMAP.md](ROADMAP.md)** for the full feature roadmap. Major upcoming phases:

| Phase | Focus |
|-------|-------|
| 2 | IQ recording & playback |
| 3 | ML signal classification & frequency scanning |
| 4 | Additional decoders (P25, D-Star, NOAA APT, YSF, Morse, SSTV…) |
| 5 | UI enhancements (heatmap, constellation, eye diagram) |
| 8 | GPU/SIMD acceleration, lock-free data structures |
| 9 | Signal analysis tools (SNR, phase noise, intermodulation) |
| 10 | REST API, Python SDK, plugin architecture |
