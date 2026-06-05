"""Base class for optional per-decoder visualization windows."""

from PySide6.QtWidgets import QMainWindow
from PySide6.QtCore import Qt


class BaseDecoderWindow(QMainWindow):
    """
    Optional floating window that displays structured decoder results.

    Lifecycle
    ─────────
    Created lazily when the user clicks "Open View" on a VFO tab.
    Receives push_result() calls whenever the corresponding decoder emits
    a result *and* the window is currently visible.
    Stays alive (hidden) when the user closes it; re-shown on next open click.
    Destroyed when the parent VFO is removed.
    """

    def __init__(self, title: str, vfo_id: int, decoder_name: str):
        super().__init__()
        self.vfo_id       = vfo_id
        self.decoder_name = decoder_name
        self.setWindowTitle(title)
        self.setMinimumSize(640, 480)
        # Closing hides the window; it is not destroyed until the VFO is removed.
        self.setAttribute(Qt.WA_QuitOnClose, False)

    def set_configure_fn(self, fn) -> None:
        """
        Provide a callable that subclasses can use to push runtime config
        changes back to the running decoder.  Signature: fn(params: dict).
        Called by the main window after window creation; ignored by default.
        """

    def push_result(self, data: dict) -> None:
        """
        Receive one structured decoder result from the UI thread.

        *data* is the raw ``DecoderResult.data`` dict emitted by the DSP
        worker — always a plain-Python-type dict (no NumPy arrays).

        Subclasses override this to update their display.
        """
