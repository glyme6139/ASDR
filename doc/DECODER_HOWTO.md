# Adding a new decoder

## 1. Implement the decoder — `app/decoders/<name>.py`

Pick the right base class:

| Base class | When to use |
|---|---|
| `BaseAudioDecoder` | Works on 48 kHz demodulated audio (override `decode_audio`) |
| `BaseIQDecoder` | Works on raw IQ samples (override `decode_iq`) |
| `BaseDecoder` | Custom buffering — override `process` directly |

```python
from .base import BaseAudioDecoder, DecoderResult
import numpy as np, time
from typing import Optional

class FooDecoder(BaseAudioDecoder):
    def __init__(self, sample_rate: int = 48_000):
        super().__init__('FOO', sample_rate)
        # ... init state

    def decode_audio(self, audio: np.ndarray) -> Optional[DecoderResult]:
        # accumulate audio, run demod, return one result or None
        return DecoderResult(
            decoder_name='FOO',
            timestamp=time.time(),
            data={'key': 'value'},       # always a plain dict
            confidence=1.0,
            metadata={'type': 'foo'},
        )

    def reset(self):
        pass   # clear all internal state

    def format_result(self, result: DecoderResult) -> str:
        return str(result.data)          # one-line string for the VFO log
```

`data` must be a plain-Python dict (no numpy arrays) — it crosses the IPC queue.

### Reserved keys in `data`

| Key | Type | Purpose |
|---|---|---|
| `new_bits` | `list[int]` | Raw recovered symbols (0/1) produced **in this call only**. When present the main window forwards them to the Bitstream Analyzer window automatically. |

Include `new_bits` whenever your decoder produces binary symbols, even if no higher-level framing has completed yet — this is what lets the Bitstream Analyzer give a live entropy and run-length view independent of character decoding.

**Flushing pattern** — accumulate symbols between result emissions so the window gets a steady feed even with no decoded characters:

```python
class FooDecoder(BaseAudioDecoder):
    def __init__(self, sample_rate=48_000):
        super().__init__('FOO', sample_rate)
        self._pending_bits: list[int] = []
        self._last_bits_t = 0.0

    def decode_audio(self, audio):
        now = time.time()
        symbols = self._demodulate(audio)          # your demod returns List[int]
        self._pending_bits.extend(symbols)

        # flush accumulated bits at most every 0.5 s
        if self._pending_bits and now - self._last_bits_t >= 0.5:
            self._last_bits_t = now
            new_bits = self._pending_bits
            self._pending_bits = []
        else:
            new_bits = []

        chars = self._frame(symbols)               # higher-level framing
        if not chars and not new_bits:
            return None

        return DecoderResult(
            decoder_name='FOO',
            timestamp=now,
            data={'text': ''.join(chars), 'new_bits': new_bits},
            confidence=0.8 if chars else 0.0,
            metadata={'type': 'foo'},
        )

    def reset(self):
        self._pending_bits = []
```

## 2. Export it — `app/decoders/__init__.py`

```python
from .foo import FooDecoder
# add 'FooDecoder' to __all__
```

## 3. Register it in the UI — `app/desktop/control_panels.py`

```python
DECODER_NAMES = ['POCSAG', 'ADSB', 'TETRA', 'ACARS', 'FOO', ...]
```

## 4. Instantiate it in the DSP worker — `app/desktop/sdr_worker.py`

Inside `_make_decoder`:
```python
elif name == 'FOO':
    from app.decoders.foo import FooDecoder
    return FooDecoder()
```

---

## Optional: add a visualization window

### 5. Create the window — `app/desktop/decoder_windows/foo_view.py`

```python
from .base import BaseDecoderWindow
from PySide6.QtWidgets import QWidget, QVBoxLayout

class FooWindow(BaseDecoderWindow):
    def __init__(self, vfo_id: int):
        super().__init__(f'FOO — VFO {vfo_id}', vfo_id, 'FOO')
        # build Qt UI here
        self.resize(800, 400)

    def push_result(self, data: dict) -> None:
        # called on the UI thread whenever the decoder emits a result
        pass
```

`push_result` is called with `DecoderResult.data` (the plain dict).  
The window is created lazily when the user clicks "Open View" and stays alive (hidden) until the VFO is removed.

### 6. Register the window — `app/desktop/decoder_windows/__init__.py`

```python
from .foo_view import FooWindow

WINDOW_REGISTRY: dict = {
    ...,
    'FOO': FooWindow,
}
# add 'FooWindow' to __all__
```

---

## Checklist

- [ ] `app/decoders/foo.py` — decoder class
- [ ] `app/decoders/__init__.py` — import + `__all__`
- [ ] `app/desktop/control_panels.py` — add name to `DECODER_NAMES`
- [ ] `app/desktop/sdr_worker.py` — add branch in `_make_decoder`
- [ ] *(optional)* `app/desktop/decoder_windows/foo_view.py` — window class
- [ ] *(optional)* `app/desktop/decoder_windows/__init__.py` — register window
- [ ] *(optional)* emit `new_bits` in `data` to feed the Bitstream Analyzer
