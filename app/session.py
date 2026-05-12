import json
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_SESSION_FILENAME = Path.home() / '.asdr_session.json'
DEFAULT_BUNDLED = Path(__file__).parent / 'session_default.json'


def load_session(path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_SESSION_FILENAME
    if target.exists():
        try:
            with open(target, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # Fallback to bundled default
    if DEFAULT_BUNDLED.exists():
        try:
            with open(DEFAULT_BUNDLED, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # If all else fails, return a minimal default
    return {
        'device': {
            'center_freq': 100e6,
            'sample_rate': 20_000_000,
            'lna': 24,
            'vga': 20,
            'amp_enabled': False,
        },
        'vfos': [
            {
                'name': 'VFO 1',
                'frequency': 100e6,
                'demod_mode': 'NFM',
                'bandwidth': 12_500,
                'volume': 0.8,
                'squelch_level': -100.0,
                'squelch_enabled': True,
                'enabled': True,
                'decoders': [],
            }
        ],
    }


def save_session(session: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = Path(path) if path is not None else DEFAULT_SESSION_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
