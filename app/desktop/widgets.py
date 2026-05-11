from PySide6.QtWidgets import QDoubleSpinBox
from PySide6.QtGui import QValidator


class AcceptCommaDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that accepts comma or dot as decimal separator."""

    def valueFromText(self, text: str) -> float:
        t = text.replace(',', '.')
        try:
            return float(t)
        except Exception:
            return super().valueFromText(t)

    def validate(self, text: str, pos: int):
        if text == '' or text == '-' or text == ',':
            return QValidator.Intermediate, text, pos
        t = text.replace(',', '.')
        try:
            if t.endswith('.'):
                float(t[:-1])
                return QValidator.Intermediate, text, pos
            val = float(t)
        except Exception:
            return QValidator.Invalid, text, pos
        if self.minimum() <= val <= self.maximum():
            return QValidator.Acceptable, text, pos
        return QValidator.Intermediate, text, pos


__all__ = ["AcceptCommaDoubleSpinBox"]
