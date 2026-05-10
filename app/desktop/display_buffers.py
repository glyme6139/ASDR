import threading
from typing import Any, Optional


class SharedLatest:
    """Thread-safe holder that keeps only the latest value.

    Writer threads call `set(value)`; reader threads call
    `get_and_clear()` to atomically retrieve and clear the stored value.
    This avoids filling Qt event queues with frequent arrays; the GUI
    polls the latest available data at its own rate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._val: Optional[Any] = None

    def set(self, val: Any) -> None:
        with self._lock:
            self._val = val

    def get_and_clear(self) -> Optional[Any]:
        with self._lock:
            v = self._val
            self._val = None
            return v
