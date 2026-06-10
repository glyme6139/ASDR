import sys
from PySide6.QtWidgets import QApplication
from app.desktop.signal_id_panel import SignalIDWindow

app = QApplication(sys.argv)
win = SignalIDWindow(db_path=None, freq_hz=100e6)
win.show()
win.raise_()
win.activateWindow()
sys.exit(app.exec())
