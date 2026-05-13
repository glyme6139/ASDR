"""
Decoder window registry.

Maps decoder names to their optional custom visualization window classes.
Only decoders listed here will show an "Open View" button in the VFO tab.
"""

from .adsb_map import ADSBMapWindow
from .tetra_view import TETRAWindow
from .base import BaseDecoderWindow

# Map decoder_name → window class (constructor receives vfo_id: int)
WINDOW_REGISTRY: dict = {
    "ADSB": ADSBMapWindow,
    "TETRA": TETRAWindow,
}


def has_window(decoder_name: str) -> bool:
    """Return True if *decoder_name* has a registered visualization window."""
    return decoder_name in WINDOW_REGISTRY


def create_window(decoder_name: str, vfo_id: int) -> "BaseDecoderWindow | None":
    """Instantiate and return the window for *decoder_name*, or None."""
    cls = WINDOW_REGISTRY.get(decoder_name)
    return cls(vfo_id) if cls is not None else None


__all__ = [
    "BaseDecoderWindow",
    "ADSBMapWindow",
    "TETRAWindow",
    "WINDOW_REGISTRY",
    "has_window",
    "create_window",
]
