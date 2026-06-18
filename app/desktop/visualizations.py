"""
Real-time spectrum and waterfall visualization using pyqtgraph.

Both views live in one GraphicsLayoutWidget:
  Row 0 (10%): spectrum — auto Y-scale, VFO overlays, click-to-tune
  Row 1 (90%): waterfall — X axis linked to spectrum (same MHz range)
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
from PySide6.QtCore import QObject, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QPushButton
import pyqtgraph as pg

# OpenGL rendering: GPU takes over the actual draw calls, releasing the Python GIL
# during the heavy work. Must be set before any pg widget is instantiated.
pg.setConfigOptions(useOpenGL=True, antialias=False)

import logging
import time

from .timing import TimingConfig, TimingProfiler, profiler_from_config

logger = logging.getLogger(__name__)

VFO_COLORS = [
    '#00ffff',  # cyan
    '#ffff00',  # yellow
    '#00ff00',  # green
    '#ff00ff',  # magenta
    '#ff8800',  # orange
    '#ff4444',  # red
    '#8888ff',  # purple
    '#00ff88',  # teal
]

ACTIVE_LINE_WIDTH   = 2
INACTIVE_LINE_WIDTH = 1
_FILL_DB            = -140.0   # fillLevel for spectrum curve
_WATERFALL_HZ       = 100      # must match sdr_worker.WATERFALL_HZ; rows = seconds × this


class ClickableRegion(pg.LinearRegionItem):
    """LinearRegionItem that emits sigClicked on mouse click."""
    sigClicked = Signal(object, object)

    def mouseClickEvent(self, ev):
        super().mouseClickEvent(ev)
        self.sigClicked.emit(self, ev)


@dataclass
class VFOMarker:
    region:     pg.LinearRegionItem
    center:     pg.InfiniteLine
    label:      pg.TextItem
    color:      str
    freq_hz:    float = 0.0
    bandwidth_hz: float = 12_500.0


# ---------------------------------------------------------------------------
# Signal auto-detection overlay
# ---------------------------------------------------------------------------

_ANNOTATION_COLOR  = '#a0c8ff'   # pale blue — distinct from VFO cyan/yellow
_ANNOTATION_FILL   = '#a0c8ff18' # ~10 % opacity fill
_ANNOTATION_HOVER  = '#a0c8ff30' # ~19 % opacity on hover
_ANNOTATION_ZVAL   = 5           # below VFO markers (z 10-12)
_ANNOTATION_FONT   = QFont("Monospace", 8)


def _fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:.4f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.1f} kHz"
    return f"{hz:.0f} Hz"


class _AnnotationRegion(pg.LinearRegionItem):
    """Non-movable LinearRegionItem with hover and click signals."""

    sigHovered = Signal(object, bool)  # (self, entering: bool)
    sigClicked = Signal(object)        # (self,)

    def __init__(self, signal_data: dict, *args, **kwargs):
        kwargs['movable'] = False
        super().__init__(*args, **kwargs)
        self.signal_data = signal_data
        self.setAcceptHoverEvents(True)

    def hoverEvent(self, ev):
        # pyqtgraph passes a HoverEvent; ev.isExit() is True when leaving
        self.sigHovered.emit(self, not ev.isExit())

    def mouseClickEvent(self, ev):
        # pyqtgraph MouseClickEvent; accept so it doesn't propagate
        self.sigClicked.emit(self)
        ev.accept()


class SignalOverlay(QObject):
    """
    Manages auto-detected signal annotation overlays on a spectrum PlotItem.

    Each detected signal is rendered as:
      - a non-movable coloured band (_AnnotationRegion)
      - a compact label at the top of the plot showing the modulation hint
      - a shared tooltip TextItem revealed on hover with full details

    annotation_clicked carries the raw signal-data dict so the main window
    can open the correct Artemis entry without any extra lookups.
    """

    annotation_clicked = Signal(object)   # signal_data dict

    def __init__(self, plot: pg.PlotItem, parent: QObject | None = None):
        super().__init__(parent)
        self._plot        = plot
        self._annotations: List[tuple] = []   # (region, label)

        # Shared tooltip — positioned in the upper-left corner on hover
        self._tooltip = pg.TextItem(
            anchor=(0.0, 0.0),
            fill=pg.mkBrush(15, 15, 15, 210),
        )
        self._tooltip.setFont(_ANNOTATION_FONT)
        self._tooltip.setZValue(100)
        self._tooltip.hide()
        plot.addItem(self._tooltip)

        self._hover_locked = False   # True while any annotation is hovered

        # Reposition labels whenever the spectrum Y range auto-scales
        plot.getViewBox().sigYRangeChanged.connect(self._reposition_labels)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, signals: list):
        """Replace all annotations with a new list of signal dicts."""
        if self._hover_locked:
            return
        self._clear()
        if not signals:
            return
        yr = self._plot.getViewBox().viewRange()[1]
        for s in signals:
            self._add(s, yr)

    def clear(self):
        self._clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clear(self):
        for region, label in self._annotations:
            self._plot.removeItem(region)
            self._plot.removeItem(label)
        self._annotations.clear()
        self._tooltip.hide()

    def _reposition_labels(self):
        yr = self._plot.getViewBox().viewRange()[1]
        for _, label in self._annotations:
            x = label.pos().x()
            label.setPos(x, yr[1])

    def _add(self, sig: dict, yr: tuple):
        center_mhz  = sig['center_hz'] / 1e6
        half_bw_mhz = sig['bandwidth_hz'] / 2e6

        region = _AnnotationRegion(
            signal_data=sig,
            values=[center_mhz - half_bw_mhz, center_mhz + half_bw_mhz],
            brush=pg.mkBrush(pg.mkColor(_ANNOTATION_FILL)),
            pen=pg.mkPen(color=_ANNOTATION_COLOR, width=1),
        )
        region.setZValue(_ANNOTATION_ZVAL)

        label = pg.TextItem(
            text=sig['modulation_hint'],
            color=_ANNOTATION_COLOR,
            anchor=(0.5, 0.0),
            fill=pg.mkBrush(10, 12, 20, 180),
        )
        label.setFont(_ANNOTATION_FONT)
        label.setPos(center_mhz, yr[1])
        label.setZValue(_ANNOTATION_ZVAL + 1)
        # Labels should not eat mouse events
        label.setAcceptHoverEvents(False)

        self._plot.addItem(region)
        self._plot.addItem(label)
        self._annotations.append((region, label))

        region.sigHovered.connect(
            lambda _, entering, s=sig, lbl=label: self._on_hover(s, lbl, entering)
        )
        region.sigClicked.connect(
            lambda _, s=sig: self.annotation_clicked.emit(s)
        )

    def _on_hover(self, sig: dict, label: pg.TextItem, entering: bool):
        self._hover_locked = entering
        if entering:
            label.setColor('#ffffff')
            region = self._sender_region(sig)
            if region is not None:
                region.setBrush(pg.mkBrush(pg.mkColor(_ANNOTATION_HOVER)))

            # Build tooltip text
            bw_str  = _fmt_hz(sig['bandwidth_hz'])
            text = (
                f"Center:  {_fmt_hz(sig['center_hz'])}\n"
                f"BW (−6 dB): ≈{bw_str}\n"
                f"Peak:    {sig['peak_db']:.1f} dBFS\n"
                f"SNR:     {sig['snr_db']:.1f} dB\n"
                f"Type:    {sig['modulation_hint']}\n"
                f"\nClick to search Artemis"
            )
            xr = self._plot.getViewBox().viewRange()[0]
            yr = self._plot.getViewBox().viewRange()[1]
            center_mhz = sig['center_hz'] / 1e6

            # Extend left when signal is in the right half so the box stays in-frame
            if center_mhz < (xr[0] + xr[1]) / 2.0:
                self._tooltip.anchor = pg.Point(0.0, 0.0)
            else:
                self._tooltip.anchor = pg.Point(1.0, 0.0)

            # setText triggers an internal reposition that applies the new anchor
            self._tooltip.setText(text)

            y_pos = yr[1] - (yr[1] - yr[0]) * 0.01
            self._tooltip.setPos(center_mhz, y_pos)
            self._tooltip.show()
        else:
            label.setColor(_ANNOTATION_COLOR)
            region = self._sender_region(sig)
            if region is not None:
                region.setBrush(pg.mkBrush(pg.mkColor(_ANNOTATION_FILL)))
            self._tooltip.hide()

    def _sender_region(self, sig: dict) -> '_AnnotationRegion | None':
        for region, _ in self._annotations:
            if region.signal_data is sig:
                return region
        return None


class SpectrumViewer(QObject):
    """
    Spectrum analyzer bound to a PlotItem.
    Y-axis auto-ranges to fit peaks on every update.
    Emits frequency_clicked (Hz) when the plot is left-clicked.
    """

    frequency_clicked   = Signal(float)             # Hz — raw spectrum click
    vfo_marker_changed  = Signal(int, float, float) # vfo_id, freq_hz, bw_hz — drag finished
    vfo_selected        = Signal(int)               # vfo_id — marker clicked/dragged
    vfo_drag_active     = Signal(int, float, float) # vfo_id, freq_hz, bw_hz — live drag
    vfo_drag_done       = Signal(int)               # vfo_id

    def __init__(self, plot: pg.PlotItem, profiler: TimingProfiler | None = None):
        super().__init__()
        self._plot = plot
        self._profiler = profiler

        plot.setLabel('left', 'dB')
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setMenuEnabled(False)
        plot.hideAxis('bottom')          # freq labels shown at waterfall bottom
        plot.hideAxis('left')          # freq labels shown at waterfall bottom
        plot.getAxis('left').setWidth(48)

        self.plot_curve = plot.plot(
            pen=pg.mkPen(color='#00ffff', width=1),
            fillLevel=_FILL_DB,
            brush=pg.mkBrush(color=(0, 255, 255, 40)),
        )
        self.plot_curve.setDownsampling(ds=2, auto=False, method='subsample')
        self.plot_curve.setClipToView(True)
        self.peak_curve = plot.plot(pen=pg.mkPen(color='#005555', width=1))
        self.peak_curve.setDownsampling(ds=4, auto=False, method='subsample')
        self.peak_curve.setClipToView(True)
        self._peak_data: Optional[np.ndarray] = None

        self._center_hz:   float = 100e6
        self._sample_rate: float = 20e6

        self._vfo_markers:   Dict[int, VFOMarker] = {}
        self._active_vfo_id: Optional[int]        = None

        # Cached x-axis array — rebuilt only when freq range or bin count changes
        self._x_cache:   Optional[np.ndarray] = None
        self._x_cache_n: int = 0

        plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

    # ------------------------------------------------------------------
    # Frequency range
    # ------------------------------------------------------------------

    def set_freq_range(self, center_hz: float, sample_rate: float):
        self._center_hz    = center_hz
        self._sample_rate  = sample_rate
        self._x_cache      = None   # invalidate cached x-axis
        self._x_cache_n    = 0
        self.reset_peak()
        lo = (center_hz - sample_rate / 2) / 1e6
        hi = (center_hz + sample_rate / 2) / 1e6
        span = hi - lo
        self._plot.getViewBox().setLimits(
            xMin=lo, xMax=hi,
            minXRange=span * 0.001,
            maxXRange=span,
        )
        for marker in self._vfo_markers.values():
            self._reposition_marker(marker)

    # ------------------------------------------------------------------
    # Spectrum update
    # ------------------------------------------------------------------

    def update_spectrum(self, spectrum_data: np.ndarray,
                        freq_start: Optional[float] = None,
                        freq_end:   Optional[float] = None):
        if spectrum_data is None or len(spectrum_data) == 0:
            return

        n = len(spectrum_data)

        if freq_start is not None and freq_end is not None:
            x = np.linspace(freq_start / 1e6, freq_end / 1e6, n)
            self._x_cache = x
            self._x_cache_n = n
        elif n == self._x_cache_n and self._x_cache is not None:
            x = self._x_cache
        else:
            x = np.linspace(
                (self._center_hz - self._sample_rate / 2) / 1e6,
                (self._center_hz + self._sample_rate / 2) / 1e6,
                n,
            )
            self._x_cache   = x
            self._x_cache_n = n

        if self._profiler is not None:
            with self._profiler.measure("visual / spectrum update"):
                self.plot_curve.setData(x, spectrum_data)

                if self._peak_data is None or len(self._peak_data) != n:
                    self._peak_data = spectrum_data.copy()
                else:
                    np.maximum(self._peak_data, spectrum_data, out=self._peak_data)
                self.peak_curve.setData(x, self._peak_data)
        else:
            self.plot_curve.setData(x, spectrum_data)

            if self._peak_data is None or len(self._peak_data) != n:
                self._peak_data = spectrum_data.copy()
            else:
                np.maximum(self._peak_data, spectrum_data, out=self._peak_data)
            self.peak_curve.setData(x, self._peak_data)

        # Auto Y: fit min/max of current data with a small margin
        lo = float(spectrum_data.min()) - 3.0
        hi = float(spectrum_data.max()) + 3.0
        if hi > lo:
            self._plot.setYRange(lo, hi, padding=0)

    def reset_peak(self):
        self._peak_data = None
        self.peak_curve.setData([], [])

    # ------------------------------------------------------------------
    # VFO markers
    # ------------------------------------------------------------------

    def add_vfo_marker(self, vfo_id: int, freq_hz: float, bandwidth_hz: float,
                       color: str, label: str):
        if vfo_id in self._vfo_markers:
            self.remove_vfo_marker(vfo_id)

        center_mhz  = freq_hz / 1e6
        half_bw_mhz = bandwidth_hz / 2e6

        region = ClickableRegion(
            values=[center_mhz - half_bw_mhz, center_mhz + half_bw_mhz],
            movable=True,
            brush=pg.mkBrush(pg.mkColor(color + '33')),
            pen=pg.mkPen(color=color, width=1, style=pg.QtCore.Qt.DashLine),
        )
        region.setZValue(10)

        center_line = pg.InfiniteLine(
            pos=center_mhz,
            angle=90,
            movable=True,
            pen=pg.mkPen(color=color, width=INACTIVE_LINE_WIDTH),
        )
        center_line.setZValue(11)

        text = pg.TextItem(text=label, color=color, anchor=(0.5, 1.0))
        text.setZValue(12)

        self._plot.addItem(region)
        self._plot.addItem(center_line)
        self._plot.addItem(text)

        marker = VFOMarker(
            region=region, center=center_line, label=text,
            color=color, freq_hz=freq_hz, bandwidth_hz=bandwidth_hz,
        )
        self._vfo_markers[vfo_id] = marker
        self._reposition_marker(marker)

        center_line.sigPositionChangeFinished.connect(
            lambda line, vid=vfo_id: self._on_marker_dragged(vid, line)
        )
        center_line.sigPositionChanged.connect(
            lambda line, vid=vfo_id: self._on_marker_drag_live(vid, line)
        )
        center_line.sigClicked.connect(
            lambda line, ev, vid=vfo_id: self.vfo_selected.emit(vid)
        )
        region.sigClicked.connect(
            lambda reg, ev, vid=vfo_id: self.vfo_selected.emit(vid)
        )
        region.sigRegionChangeFinished.connect(
            lambda reg, vid=vfo_id: self._on_region_dragged(vid, reg)
        )
        region.sigRegionChanged.connect(
            lambda reg, vid=vfo_id: self._on_region_drag_live(vid, reg)
        )

    def update_vfo_marker(self, vfo_id: int, freq_hz: float,
                          bandwidth_hz: Optional[float] = None):
        marker = self._vfo_markers.get(vfo_id)
        if marker is None:
            return
        marker.freq_hz = freq_hz
        if bandwidth_hz is not None:
            marker.bandwidth_hz = bandwidth_hz
        self._reposition_marker(marker)

    def set_vfo_label(self, vfo_id: int, label: str):
        marker = self._vfo_markers.get(vfo_id)
        if marker is None:
            return
        marker.label.setText(label)

    def remove_vfo_marker(self, vfo_id: int):
        marker = self._vfo_markers.pop(vfo_id, None)
        if marker is None:
            return
        self._plot.removeItem(marker.region)
        self._plot.removeItem(marker.center)
        self._plot.removeItem(marker.label)

    def set_active_vfo_marker(self, vfo_id: int):
        if self._active_vfo_id is not None and self._active_vfo_id in self._vfo_markers:
            m = self._vfo_markers[self._active_vfo_id]
            m.center.setPen(pg.mkPen(color=m.color, width=INACTIVE_LINE_WIDTH))
        self._active_vfo_id = vfo_id
        if vfo_id in self._vfo_markers:
            m = self._vfo_markers[vfo_id]
            m.center.setPen(pg.mkPen(color=m.color, width=ACTIVE_LINE_WIDTH))

    def _reposition_marker(self, marker: VFOMarker):
        center_mhz  = marker.freq_hz / 1e6
        half_bw_mhz = marker.bandwidth_hz / 2e6
        yr = self._plot.getViewBox().viewRange()[1]
        marker.center.blockSignals(True)
        marker.region.blockSignals(True)
        marker.center.setValue(center_mhz)
        marker.region.setRegion([center_mhz - half_bw_mhz, center_mhz + half_bw_mhz])
        marker.label.setPos(center_mhz, yr[1])
        marker.center.blockSignals(False)
        marker.region.blockSignals(False)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_mouse_clicked(self, event):
        if event.button() != pg.QtCore.Qt.LeftButton:
            return
        vb = self._plot.getViewBox()
        # Ignore clicks that land outside this plot's ViewBox
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        pos = vb.mapSceneToView(event.scenePos())
        self.frequency_clicked.emit(pos.x() * 1e6)

    def _on_marker_dragged(self, vfo_id: int, line: pg.InfiniteLine):
        freq_hz = line.value() * 1e6
        marker  = self._vfo_markers.get(vfo_id)
        if marker:
            marker.freq_hz = freq_hz
            half_bw = marker.bandwidth_hz / 2e6
            marker.region.blockSignals(True)
            marker.region.setRegion([line.value() - half_bw, line.value() + half_bw])
            yr = self._plot.getViewBox().viewRange()[1]
            marker.label.setPos(line.value(), yr[1])
            marker.region.blockSignals(False)
        bw_hz = marker.bandwidth_hz if marker else 0.0
        self.vfo_selected.emit(vfo_id)
        self.vfo_marker_changed.emit(vfo_id, freq_hz, bw_hz)
        self.vfo_drag_done.emit(vfo_id)

    def _on_marker_drag_live(self, vfo_id: int, line: pg.InfiniteLine):
        marker = self._vfo_markers.get(vfo_id)
        if marker:
            freq_hz = line.value() * 1e6
            self.vfo_drag_active.emit(vfo_id, freq_hz, marker.bandwidth_hz)

    def _on_region_dragged(self, vfo_id: int, region: pg.LinearRegionItem):
        lo, hi      = region.getRegion()
        center_mhz  = (lo + hi) / 2
        freq_hz     = center_mhz * 1e6
        bw_hz       = (hi - lo) * 1e6
        marker      = self._vfo_markers.get(vfo_id)
        if marker:
            marker.freq_hz      = freq_hz
            marker.bandwidth_hz = bw_hz
            marker.center.blockSignals(True)
            marker.center.setValue(center_mhz)
            yr = self._plot.getViewBox().viewRange()[1]
            marker.label.setPos(center_mhz, yr[1])
            marker.center.blockSignals(False)
        self.vfo_selected.emit(vfo_id)
        self.vfo_marker_changed.emit(vfo_id, freq_hz, bw_hz)
        self.vfo_drag_done.emit(vfo_id)

    def _on_region_drag_live(self, vfo_id: int, region: pg.LinearRegionItem):
        lo, hi = region.getRegion()
        freq_hz = (lo + hi) / 2 * 1e6
        bw_hz   = (hi - lo) * 1e6
        self.vfo_drag_active.emit(vfo_id, freq_hz, bw_hz)


class WaterfallViewer:
    """
    Waterfall display bound to a PlotItem.
    X axis = frequency (MHz), same range as spectrum (linked).
    Y axis = time index (0 = oldest, history_size-1 = newest at top).
    VFO markers are vertical InfiniteLines at frequency positions.
    """

    def __init__(self, plot: pg.PlotItem,
                 history_size: int = 300, freq_bins: int = 1024,
                 profiler: TimingProfiler | None = None):
        self._plot        = plot
        self.history_size = history_size
        self.freq_bins    = freq_bins
        self._profiler    = profiler

        plot.setLabel('bottom', 'Frequency (MHz)')
        plot.showGrid(x=True, y=False)
        plot.setMenuEnabled(False)
        plot.hideAxis('left')

        vb = plot.getViewBox()
        vb.setMouseEnabled(x=True, y=False)
        vb.setYRange(0, history_size, padding=0)
        vb.setLimits(yMin=0, yMax=history_size, minYRange=history_size, maxYRange=history_size)

        # Ring buffer: (freq_bins, history_size) — written one column at a time.
        # _ordered is a pre-allocated reorder scratch buffer so render_pending
        # never allocates — it just does two in-place slice copies.
        self._ring      = np.zeros((freq_bins, history_size), dtype=np.uint8)
        self._ordered   = np.empty((freq_bins, history_size), dtype=np.uint8)
        self._write_idx = 0     # next column to overwrite
        self._dirty     = False # True when ring has unrendered data
        self.image_item = pg.ImageItem(self._ring)
        # Let pyqtgraph smooth the waterfall against the screen resolution instead
        # of rendering every FFT bin as a hard-edged pixel block.
        self.image_item.setAutoDownsample(True)
        self.image_item.setLookupTable(self._build_lut())
        self.image_item.setLevels([0, 255])
        plot.addItem(self.image_item)

        self._center_hz:   float = 100e6
        self._sample_rate: float = 20e6
        self._update_image_rect()

        self._wf_markers:  Dict[int, pg.InfiniteLine]       = {}
        self._wf_labels:   Dict[int, pg.TextItem]           = {}
        self._wf_regions:  Dict[int, pg.LinearRegionItem]   = {}

    def _update_image_rect(self):
        lo_mhz = (self._center_hz - self._sample_rate / 2) / 1e6
        bw_mhz = self._sample_rate / 1e6
        self.image_item.setRect(QRectF(lo_mhz, 0, bw_mhz, self.history_size))

    def resize_history(self, new_size: int):
        """Reallocate the ring buffer to a new history depth. Clears existing data."""
        if new_size < 1 or new_size == self.history_size:
            return
        self.history_size = new_size
        self._ring      = np.zeros((self.freq_bins, new_size), dtype=np.uint8)
        self._ordered   = np.empty((self.freq_bins, new_size), dtype=np.uint8)
        self._write_idx = 0
        self._dirty     = False
        # Set image to the new empty buffer BEFORE _update_image_rect so that
        # setRect → _recalcTransform uses the correct new shape, not the old one.
        self.image_item.setImage(self._ring, autoLevels=False)
        vb = self._plot.getViewBox()
        vb.setYRange(0, new_size, padding=0)
        vb.setLimits(yMin=0, yMax=new_size, minYRange=new_size, maxYRange=new_size)
        self._update_image_rect()
        for text in self._wf_labels.values():
            text.setPos(text.pos().x(), new_size * 0.97)

    # ------------------------------------------------------------------
    # Frequency range
    # ------------------------------------------------------------------

    def set_freq_range(self, center_hz: float, sample_rate: float):
        self._center_hz   = center_hz
        self._sample_rate = sample_rate
        self._update_image_rect()
        lo   = (center_hz - sample_rate / 2) / 1e6
        hi   = (center_hz + sample_rate / 2) / 1e6
        span = hi - lo
        self._plot.getViewBox().setLimits(
            xMin=lo, xMax=hi,
            minXRange=span * 0.001,
            maxXRange=span,
        )
        for vfo_id, line in self._wf_markers.items():
            freq_hz = getattr(line, '_vfo_freq_hz', center_hz)
            line.setValue(freq_hz / 1e6)
            lbl = self._wf_labels.get(vfo_id)
            if lbl:
                lbl.setPos(freq_hz / 1e6, self.history_size * 0.97)

    # ------------------------------------------------------------------
    # VFO markers (vertical lines at frequency position)
    # ------------------------------------------------------------------

    def add_vfo_marker(self, vfo_id: int, freq_hz: float, color: str, label: str):
        self.remove_vfo_marker(vfo_id)
        mhz = freq_hz / 1e6
        line = pg.InfiniteLine(
            pos=mhz, angle=90, movable=False,
            pen=pg.mkPen(color=color, width=1),
        )
        line._vfo_freq_hz = freq_hz
        line.setZValue(15)
        text = pg.TextItem(text=label, color=color, anchor=(0.0, 1.0))
        text.setPos(mhz, self.history_size * 0.97)
        text.setZValue(16)

        region = pg.LinearRegionItem(
            values=[mhz, mhz],
            movable=False,
            brush=pg.mkBrush(pg.mkColor(color + '44')),
            pen=pg.mkPen(color=color, width=1, style=pg.QtCore.Qt.DashLine),
        )
        region.setZValue(14)
        region.hide()

        self._plot.addItem(region)
        self._plot.addItem(line)
        self._plot.addItem(text)

        self._wf_markers[vfo_id]  = line
        self._wf_labels[vfo_id]   = text
        self._wf_regions[vfo_id]  = region

    def update_vfo_marker(self, vfo_id: int, freq_hz: float):
        line = self._wf_markers.get(vfo_id)
        text = self._wf_labels.get(vfo_id)
        if line:
            line._vfo_freq_hz = freq_hz
            mhz = freq_hz / 1e6
            line.setValue(mhz)
            if text:
                text.setPos(mhz, self.history_size * 0.97)

    def set_vfo_label(self, vfo_id: int, label: str):
        text = self._wf_labels.get(vfo_id)
        if text:
            try:
                text.setText(label)
            except Exception:
                pass

    def show_vfo_region(self, vfo_id: int, freq_hz: float, bw_hz: float):
        region = self._wf_regions.get(vfo_id)
        if region is None:
            return
        center_mhz  = freq_hz / 1e6
        half_bw_mhz = bw_hz / 2e6
        region.blockSignals(True)
        region.setRegion([center_mhz - half_bw_mhz, center_mhz + half_bw_mhz])
        region.blockSignals(False)
        region.show()

    def hide_vfo_region(self, vfo_id: int):
        region = self._wf_regions.get(vfo_id)
        if region is not None:
            region.hide()

    def remove_vfo_marker(self, vfo_id: int):
        line   = self._wf_markers.pop(vfo_id, None)
        text   = self._wf_labels.pop(vfo_id, None)
        region = self._wf_regions.pop(vfo_id, None)
        if line:   self._plot.removeItem(line)
        if text:   self._plot.removeItem(text)
        if region: self._plot.removeItem(region)

    # ------------------------------------------------------------------
    # Waterfall update — writes column into ring buffer; render_pending() displays it
    # ------------------------------------------------------------------

    def update_waterfall(self, col_data):
        if col_data is None or len(col_data) == 0:
            return

        col = np.asarray(col_data, dtype=np.uint8) if col_data.dtype == np.uint8 \
              else np.clip(col_data, 0, 255).astype(np.uint8)

        if len(col) != self.freq_bins:
            # linear interpolation preserves frequency resolution better than integer indexing
            src_x = np.linspace(0, len(col) - 1, self.freq_bins)
            col = np.interp(src_x, np.arange(len(col)), col.astype(np.float32)).astype(np.uint8)

        if self._profiler is not None:
            with self._profiler.measure("visual / waterfall update"):
                # In-place ring write — no allocation
                self._ring[:, self._write_idx] = col
                self._write_idx = (self._write_idx + 1) % self.history_size
                self._dirty = True
        else:
            # In-place ring write — no allocation
            self._ring[:, self._write_idx] = col
            self._write_idx = (self._write_idx + 1) % self.history_size
            self._dirty = True

    def render_pending(self):
        """Reorder ring buffer into pre-allocated scratch and send to GPU."""
        if not self._dirty:
            return
        if self._profiler is not None:
            with self._profiler.measure("visual / waterfall render"):
                self._dirty = False
                idx   = self._write_idx
                first = self.history_size - idx
                # In-place reorder: oldest column → left (Y=0), newest → right (Y=top)
                self._ordered[:, :first] = self._ring[:, idx:]
                self._ordered[:, first:] = self._ring[:, :idx]
                self.image_item.setImage(self._ordered, autoLevels=False)
        else:
            self._dirty = False
            idx   = self._write_idx
            first = self.history_size - idx
            # In-place reorder: oldest column → left (Y=0), newest → right (Y=top)
            self._ordered[:, :first] = self._ring[:, idx:]
            self._ordered[:, first:] = self._ring[:, :idx]
            self.image_item.setImage(self._ordered, autoLevels=False)

    # ------------------------------------------------------------------
    # Colormap (blue→cyan→green→yellow→red)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_lut() -> np.ndarray:
        """Build a 256×3 uint8 LUT matching the BrowSDR color scheme:
        dark-navy → dodger-blue → white → yellow → orange → red → dark-maroon.
        Weak signals appear blue, noise-floor-plus signals appear bright white,
        strong signals go warm orange→red.  Applied via setLookupTable — zero
        render-time cost over a plain grayscale image.
        """
        stops = np.array([
            [0x00, 0x00, 0x20],
            [0x00, 0x00, 0x30],
            [0x00, 0x00, 0x50],
            [0x00, 0x00, 0x91],
            [0x1E, 0x90, 0xFF],
            [0xFF, 0xFF, 0xFF],
            [0xFF, 0xFF, 0x00],
            [0xFE, 0x6D, 0x16],
            [0xFE, 0x6D, 0x16],
            [0xFF, 0x00, 0x00],
            [0xFF, 0x00, 0x00],
            [0xC6, 0x00, 0x00],
            [0x9F, 0x00, 0x00],
            [0x75, 0x00, 0x00],
            [0x4A, 0x00, 0x00],
        ], dtype=np.float32)
        n = len(stops)
        lut = np.empty((256, 3), dtype=np.uint8)
        for i in range(256):
            p = i / 255.0 * n
            lo = min(int(p), n - 1)
            hi = min(lo + 1, n - 1)
            t  = p - lo
            lut[i] = np.clip(stops[lo] * (1 - t) + stops[hi] * t, 0, 255).astype(np.uint8)
        return lut


class MeasurementMarkers:
    """Draggable precision markers for the spectrum/waterfall display.

    Right-click spectrum:          cycle freq markers M1→M2→clear.
    Right-click waterfall:         cycle time markers T1→T2→clear.
    Shift+right-click waterfall:   cycle freq markers M1→M2→clear.
    When two markers of the same type exist a Δ label appears inline and
    the delta text is forwarded to on_delta_changed if provided.
    """

    _FREQ_COLORS = ('#ff4444', '#ff8800')  # M1 red, M2 orange
    _TIME_COLORS = ('#44ff44', '#00ff88')  # T1 green, T2 teal

    def __init__(self, spec_plot: pg.PlotItem, wf_plot: pg.PlotItem,
                 waterfall: 'WaterfallViewer', on_delta_changed=None):
        self._sp  = spec_plot
        self._wp  = wf_plot
        self._wf  = waterfall

        self._fmarkers: list[dict] = []
        self._tmarkers: list[dict] = []
        self._fdelta: Optional[pg.TextItem] = None
        self._tdelta: Optional[pg.TextItem] = None
        self._visible = True
        self._on_delta_changed = on_delta_changed

        spec_plot.getViewBox().sigRangeChanged.connect(self._reposition_freq_labels)
        wf_plot.getViewBox().sigRangeChanged.connect(self._reposition_time_labels)

    # ------------------------------------------------------------------ public

    def handle_click(self, scene_pos, shift: bool, in_waterfall: bool):
        """Dispatch a right-click to freq or time markers."""
        if in_waterfall and shift:
            pos = self._wp.getViewBox().mapSceneToView(scene_pos)
            self._place_freq_marker(pos.x())
        elif in_waterfall:
            pos = self._wp.getViewBox().mapSceneToView(scene_pos)
            self._place_time_marker(pos.y())
        else:
            pos = self._sp.getViewBox().mapSceneToView(scene_pos)
            self._place_freq_marker(pos.x())

    def clear_all(self):
        self._clear_freq()
        self._clear_time()

    def set_visible(self, visible: bool):
        self._visible = visible
        for m in self._fmarkers:
            for key in ('line_s', 'line_w', 'lbl_s', 'lbl_w'):
                m[key].setVisible(visible)
        for m in self._tmarkers:
            m['line'].setVisible(visible)
            m['lbl'].setVisible(visible)
        if self._fdelta:
            self._fdelta.setVisible(visible)
        if self._tdelta:
            self._tdelta.setVisible(visible)

    def _fire_delta_cb(self):
        if not self._on_delta_changed:
            return
        parts = []
        if len(self._fmarkers) == 2:
            f1, f2 = self._fmarkers[0]['freq_mhz'], self._fmarkers[1]['freq_mhz']
            parts.append(f"Δf {_fmt_hz(abs(f1 - f2) * 1e6)}")
        if len(self._tmarkers) == 2:
            t1, t2 = self._tmarkers[0]['secs'], self._tmarkers[1]['secs']
            parts.append(f"Δt {abs(t1 - t2):.3f}s")
        self._on_delta_changed("   ".join(parts) if parts else "")

    # ------------------------------------------------------------------ freq markers

    def _place_freq_marker(self, freq_mhz: float):
        if len(self._fmarkers) >= 2:
            self._clear_freq()
        idx   = len(self._fmarkers)
        color = self._FREQ_COLORS[idx]
        name  = f"M{idx + 1}"
        pen   = pg.mkPen(color=color, width=1, style=Qt.DashLine)

        line_s = pg.InfiniteLine(pos=freq_mhz, angle=90, movable=True, pen=pen)
        line_s.setZValue(20)
        line_w = pg.InfiniteLine(pos=freq_mhz, angle=90, movable=True, pen=pen)
        line_w.setZValue(20)

        yr    = self._sp.getViewBox().viewRange()[1]
        lbl_s = pg.TextItem(text=f"{name} {freq_mhz:.4f} MHz", color=color, anchor=(0.0, 1.0))
        lbl_s.setZValue(21)
        lbl_s.setPos(freq_mhz, yr[1])

        lbl_w = pg.TextItem(text=f"{name} {freq_mhz:.4f} MHz", color=color, anchor=(0.0, 0.0))
        lbl_w.setZValue(21)
        lbl_w.setPos(freq_mhz, self._wf.history_size * 0.97)

        self._sp.addItem(line_s);  self._sp.addItem(lbl_s)
        self._wp.addItem(line_w);  self._wp.addItem(lbl_w)

        m = dict(line_s=line_s, line_w=line_w, lbl_s=lbl_s, lbl_w=lbl_w,
                 freq_mhz=freq_mhz, idx=idx)
        self._fmarkers.append(m)

        if not self._visible:
            for item in (line_s, line_w, lbl_s, lbl_w):
                item.setVisible(False)

        line_s.sigPositionChanged.connect(lambda l, _m=m: self._sync_freq(_m, l.value(), from_wf=False))
        line_w.sigPositionChanged.connect(lambda l, _m=m: self._sync_freq(_m, l.value(), from_wf=True))

        self._update_fdelta()

    def _sync_freq(self, m: dict, freq_mhz: float, from_wf: bool):
        m['freq_mhz'] = freq_mhz
        name  = f"M{m['idx'] + 1}"
        label = f"{name} {freq_mhz:.4f} MHz"
        m['lbl_s'].setText(label)
        m['lbl_w'].setText(label)
        yr = self._sp.getViewBox().viewRange()[1]
        m['lbl_s'].setPos(freq_mhz, yr[1])
        m['lbl_w'].setPos(freq_mhz, self._wf.history_size * 0.97)
        other = m['line_w'] if not from_wf else m['line_s']
        other.blockSignals(True)
        other.setValue(freq_mhz)
        other.blockSignals(False)
        self._update_fdelta()

    def _update_fdelta(self):
        if self._fdelta:
            self._sp.removeItem(self._fdelta)
            self._fdelta = None
        if len(self._fmarkers) != 2:
            self._fire_delta_cb()
            return
        f1, f2 = self._fmarkers[0]['freq_mhz'], self._fmarkers[1]['freq_mhz']
        df_hz  = abs(f1 - f2) * 1e6
        mid    = (f1 + f2) / 2
        yr     = self._sp.getViewBox().viewRange()[1]
        self._fdelta = pg.TextItem(
            text=f"Δf = {_fmt_hz(df_hz)}", color='#ffffff', anchor=(0.5, 1.0)
        )
        self._fdelta.setZValue(22)
        self._fdelta.setPos(mid, yr[1])
        self._sp.addItem(self._fdelta)
        if not self._visible:
            self._fdelta.setVisible(False)
        self._fire_delta_cb()

    def _clear_freq(self):
        for m in self._fmarkers:
            self._sp.removeItem(m['line_s']);  self._sp.removeItem(m['lbl_s'])
            self._wp.removeItem(m['line_w']);  self._wp.removeItem(m['lbl_w'])
        self._fmarkers.clear()
        if self._fdelta:
            self._sp.removeItem(self._fdelta)
            self._fdelta = None
        self._fire_delta_cb()

    def _reposition_freq_labels(self):
        yr = self._sp.getViewBox().viewRange()[1]
        for m in self._fmarkers:
            m['lbl_s'].setPos(m['freq_mhz'], yr[1])
        if self._fdelta and len(self._fmarkers) == 2:
            f1, f2 = self._fmarkers[0]['freq_mhz'], self._fmarkers[1]['freq_mhz']
            self._fdelta.setPos((f1 + f2) / 2, yr[1])

    # ------------------------------------------------------------------ time markers

    def _place_time_marker(self, row_y: float):
        if len(self._tmarkers) >= 2:
            self._clear_time()
        idx   = len(self._tmarkers)
        color = self._TIME_COLORS[idx]
        name  = f"T{idx + 1}"
        secs  = max(0.0, (self._wf.history_size - 1 - row_y) / _WATERFALL_HZ)
        pen   = pg.mkPen(color=color, width=1, style=Qt.DashLine)

        line  = pg.InfiniteLine(pos=row_y, angle=0, movable=True, pen=pen)
        line.setZValue(20)

        xr  = self._wp.getViewBox().viewRange()[0]
        lbl = pg.TextItem(text=f"{name} −{secs:.2f}s", color=color, anchor=(1.0, 0.5))
        lbl.setZValue(21)
        lbl.setPos(xr[1], row_y)

        self._wp.addItem(line);  self._wp.addItem(lbl)

        if not self._visible:
            line.setVisible(False)
            lbl.setVisible(False)

        m = dict(line=line, lbl=lbl, row_y=row_y, secs=secs, idx=idx)
        self._tmarkers.append(m)
        line.sigPositionChanged.connect(lambda l, _m=m: self._sync_time(_m, l.value()))
        self._update_tdelta()

    def _sync_time(self, m: dict, row_y: float):
        m['row_y'] = row_y
        m['secs']  = max(0.0, (self._wf.history_size - 1 - row_y) / _WATERFALL_HZ)
        xr = self._wp.getViewBox().viewRange()[0]
        m['lbl'].setPos(xr[1], row_y)
        m['lbl'].setText(f"T{m['idx'] + 1} −{m['secs']:.2f}s")
        self._update_tdelta()

    def _update_tdelta(self):
        if self._tdelta:
            self._wp.removeItem(self._tdelta)
            self._tdelta = None
        if len(self._tmarkers) != 2:
            self._fire_delta_cb()
            return
        t1, t2 = self._tmarkers[0]['secs'],  self._tmarkers[1]['secs']
        y1, y2 = self._tmarkers[0]['row_y'], self._tmarkers[1]['row_y']
        dt     = abs(t1 - t2)
        mid_y  = (y1 + y2) / 2
        xr     = self._wp.getViewBox().viewRange()[0]
        self._tdelta = pg.TextItem(
            text=f"Δt = {dt:.3f}s", color='#ffffff', anchor=(1.0, 0.5)
        )
        self._tdelta.setZValue(22)
        self._tdelta.setPos(xr[1], mid_y)
        self._wp.addItem(self._tdelta)
        if not self._visible:
            self._tdelta.setVisible(False)
        self._fire_delta_cb()

    def _clear_time(self):
        for m in self._tmarkers:
            self._wp.removeItem(m['line']);  self._wp.removeItem(m['lbl'])
        self._tmarkers.clear()
        if self._tdelta:
            self._wp.removeItem(self._tdelta)
            self._tdelta = None
        self._fire_delta_cb()

    def _reposition_time_labels(self):
        xr = self._wp.getViewBox().viewRange()[0]
        for m in self._tmarkers:
            m['lbl'].setPos(xr[1], m['row_y'])
        if self._tdelta and len(self._tmarkers) == 2:
            y1, y2 = self._tmarkers[0]['row_y'], self._tmarkers[1]['row_y']
            self._tdelta.setPos(xr[1], (y1 + y2) / 2)


class _ClickableGLW(pg.GraphicsLayoutWidget):
    """GraphicsLayoutWidget that exposes right-click via a plain Python signal.

    Overriding QGraphicsView.mousePressEvent is the most reliable interception
    point — it fires before any PyQtGraph item routing or ViewBox handling.
    """
    sigRightClicked = Signal(float, float, bool)  # scene_x, scene_y, shift_held

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        if ev.button() == Qt.MouseButton.RightButton:
            sp = self.mapToScene(ev.pos())
            self.sigRightClicked.emit(sp.x(), sp.y(),
                                      bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))


class VisualizationPanel(QWidget):
    """
    Spectrum (10%) above waterfall (90%) in one GraphicsLayoutWidget.
    The X axes are linked — panning/zooming one tracks the other.
    """

    # Emitted when the user clicks an auto-detected signal annotation.
    # Carries the raw signal-data dict (center_hz, bandwidth_hz, modulation_hint, …).
    signal_annotation_clicked = Signal(object)

    def __init__(self, parent=None, display_buffers=None, timing: TimingConfig | None = None,
                 report_handler=None):
        super().__init__(parent)
        self._timing = timing or TimingConfig()
        self._profiler = profiler_from_config(self._timing, prefix="UI", report_handler=report_handler)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._glw = _ClickableGLW()
        self._glw.setBackground('#1a1a1a')
        layout.addWidget(self._glw, stretch=1)

        # Compact bottom bar — controls
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 2, 6, 2)
        bar.setSpacing(4)

        self._pause_btn = QPushButton("⏸  Pause")
        self._pause_btn.setFixedWidth(80)
        self._pause_btn.setToolTip("Freeze display (Space)")
        self._pause_btn.clicked.connect(self.toggle_pause)
        bar.addWidget(self._pause_btn)

        self._markers_btn = QPushButton("◉ Markers")
        self._markers_btn.setCheckable(True)
        self._markers_btn.setChecked(True)
        self._markers_btn.setFixedWidth(90)
        self._markers_btn.setToolTip(
            "Show/hide measurement markers\n"
            "Right-click spectrum → freq marker (M1/M2)\n"
            "Right-click waterfall → time marker (T1/T2)\n"
            "Shift+right-click waterfall → freq marker"
        )
        bar.addWidget(self._markers_btn)

        self._annot_btn = QPushButton("◈ Sig ID")
        self._annot_btn.setCheckable(True)
        self._annot_btn.setChecked(True)
        self._annot_btn.setFixedWidth(76)
        self._annot_btn.setToolTip("Show/hide auto-detected signal annotations on the spectrum")
        self._annot_btn.toggled.connect(self._on_annotations_toggled)
        bar.addWidget(self._annot_btn)

        self._delta_label = QLabel("")
        self._delta_label.setStyleSheet(
            "color: #cccccc; font-family: monospace; font-size: 11px; padding: 0 4px;"
        )
        bar.addWidget(self._delta_label)

        bar.addStretch()
        bar.addWidget(QLabel("History:"))
        self._history_spin = QSpinBox()
        self._history_spin.setRange(1, 60)
        self._history_spin.setValue(5)
        self._history_spin.setSuffix(" s")
        self._history_spin.setFixedWidth(72)
        self._history_spin.setToolTip("Waterfall history depth (seconds visible)")
        bar.addWidget(self._history_spin)
        bar_widget = QWidget()
        bar_widget.setLayout(bar)
        bar_widget.setFixedHeight(26)
        layout.addWidget(bar_widget)

        self.setLayout(layout)

        spec_plot = self._glw.addPlot(row=0, col=0)
        # spec_plot.setDecimationMode(pg.ViewBox.DecimationMode.Subsample)
        # spec_plot.setDownsampling(ds=8, auto=False)
        self._glw.ci.layout.setRowStretchFactor(0, 4)   # 40 %

        wf_plot = self._glw.addPlot(row=1, col=0)
        self._glw.ci.layout.setRowStretchFactor(1, 6)   # 60 %
        self._spec_plot = spec_plot
        self._wf_plot   = wf_plot

        # Link X so zoom/pan in one view mirrors the other
        wf_plot.setXLink(spec_plot)

        self.spectrum  = SpectrumViewer(spec_plot, profiler=self._profiler)
        _default_history = self._history_spin.value() * _WATERFALL_HZ
        self.waterfall = WaterfallViewer(wf_plot, history_size=_default_history, freq_bins=32768, profiler=self._profiler)
        self._history_spin.valueChanged.connect(
            lambda s: self.waterfall.resize_history(s * _WATERFALL_HZ)
        )

        # Auto-detection overlay (sits below VFO markers in z-order)
        self._signal_overlay = SignalOverlay(spec_plot, parent=self)
        self._signal_overlay.annotation_clicked.connect(self.signal_annotation_clicked)
        self._annotations_enabled = True

        self.spectrum.vfo_drag_active.connect(
            lambda vid, f, bw: self.waterfall.show_vfo_region(vid, f, bw)
        )
        self.spectrum.vfo_drag_done.connect(self.waterfall.hide_vfo_region)

        self._pending_spectrum:  Optional[np.ndarray] = None
        self._pending_waterfall: Optional[np.ndarray] = None
        # Optional shared buffers (dict with 'spectrum' and 'waterfall' SharedLatest)
        self._display_buffers = display_buffers

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)  # 20 fps — matches DISPLAY_HZ, no frames dropped
        self._render_timer.timeout.connect(self._flush_pending)
        self._render_timer.start()

        # Pause state
        self._paused = False
        self._need_gen_sync = False

        # "PAUSED" overlay label (shown on top of the GL widget)
        self._pause_label = QLabel("⏸  PAUSED", self)
        self._pause_label.setStyleSheet(
            "color: #ff4444; font-size: 14px; font-weight: bold;"
            " background: rgba(0,0,0,160); padding: 2px 6px; border-radius: 3px;"
        )
        self._pause_label.adjustSize()
        self._pause_label.move(8, 8)
        self._pause_label.hide()
        self._pause_label.raise_()

        # Measurement markers
        self._markers = MeasurementMarkers(
            spec_plot, wf_plot, self.waterfall,
            on_delta_changed=self._delta_label.setText,
        )
        self._markers_btn.clicked.connect(self._toggle_markers)

        # Right-click → measurement markers
        self._glw.sigRightClicked.connect(self._on_right_click)

        # Space shortcut for pause toggle
        _sc = QShortcut(QKeySequence(Qt.Key_Space), self)
        _sc.setContext(Qt.WidgetWithChildrenShortcut)
        _sc.activated.connect(self.toggle_pause)

    # ------------------------------------------------------------------
    # Data ingestion — store only; timer does the actual rendering
    # ------------------------------------------------------------------

    def update_spectrum(self, data: np.ndarray):
        self._pending_spectrum = data

    def update_waterfall(self, data: np.ndarray):
        self._pending_waterfall = data

    def set_process_display(self, spec_arr: np.ndarray, wf_arr: np.ndarray, disp_gen,
                            wf_queue: np.ndarray = None, wf_gen=None):
        """Wire up shared-memory numpy arrays from the DSP subprocess.

        wf_queue is an (N_SLOTS, ROW_BINS) uint8 array; wf_gen is a
        multiprocessing Value('L') incremented once per queued row.  When
        provided, _flush_pending drains all pending rows each tick instead of
        consuming one row per display-gen tick (gives up to 5× time resolution).
        """
        self._shm_spec         = spec_arr
        self._shm_wf           = wf_arr
        self._shm_gen          = disp_gen
        self._shm_last_gen     = -1
        self._shm_wf_queue     = wf_queue   # (N_SLOTS, ROW_BINS) uint8 or None
        self._shm_wf_gen       = wf_gen     # mp.Value or None
        self._shm_wf_last_gen  = 0

    def _flush_pending(self):
        if self._paused:
            return

        with self._profiler.measure("visual / frame flush"):
            t0 = time.monotonic()

            # Sync gen counters on first frame after unpause to avoid replaying
            # stale queue data that accumulated while the display was frozen.
            if self._need_gen_sync and hasattr(self, '_shm_wf_gen'):
                self._need_gen_sync = False
                if self._shm_wf_gen is not None:
                    self._shm_wf_last_gen = int(self._shm_wf_gen.value)

            # Fast path: shared memory from DSP subprocess
            if hasattr(self, '_shm_spec'):
                # Spectrum: update whenever disp_gen changes (20 Hz)
                gen = self._shm_gen.value
                if gen != self._shm_last_gen:
                    self._shm_last_gen = gen
                    self.spectrum.update_spectrum(self._shm_spec)
                    # Fallback for sweep / legacy path with no wf_queue
                    if self._shm_wf_queue is None:
                        self.waterfall.update_waterfall(self._shm_wf)

                # Waterfall: drain all queued rows (up to N_SLOTS per tick)
                if self._shm_wf_queue is not None:
                    wf_now  = int(self._shm_wf_gen.value)
                    n_slots = self._shm_wf_queue.shape[0]
                    pending = min(wf_now - int(self._shm_wf_last_gen), n_slots)
                    if pending > 0:
                        for i in range(pending):
                            slot = (self._shm_wf_last_gen + i) % n_slots
                            self.waterfall.update_waterfall(self._shm_wf_queue[slot])
                        self._shm_wf_last_gen = wf_now

                self.waterfall.render_pending()
                return

            # Legacy path: SharedLatest buffers or direct push
            if self._display_buffers:
                try:
                    sb = self._display_buffers.get('spectrum')
                    if sb is not None:
                        latest = sb.get_and_clear()
                        if latest is not None:
                            self.spectrum.update_spectrum(latest)
                            self._pending_spectrum = None

                    wb = self._display_buffers.get('waterfall')
                    if wb is not None:
                        latest_w = wb.get_and_clear()
                        if latest_w is not None:
                            self.waterfall.update_waterfall(latest_w)
                            self._pending_waterfall = None
                except Exception:
                    pass

            if self._pending_spectrum is not None:
                self.spectrum.update_spectrum(self._pending_spectrum)
                self._pending_spectrum = None
            if self._pending_waterfall is not None:
                self.waterfall.update_waterfall(self._pending_waterfall)
                self._pending_waterfall = None

            # Render waterfall ring buffer → GPU (no-op if no new data since last tick)
            self.waterfall.render_pending()

            dt = (time.monotonic() - t0) * 1000.0
            if dt > 30.0:
                logger.warning(f"[Visualization] render slow: {dt:.1f} ms")

    # ------------------------------------------------------------------
    # Pause and measurement markers
    # ------------------------------------------------------------------

    def toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self._pause_label.show()
            self._pause_label.raise_()
            self._pause_btn.setText("▶  Resume")
        else:
            self._need_gen_sync = True
            self._pause_label.hide()
            self._pause_btn.setText("⏸  Pause")

    def _toggle_markers(self, checked: bool):
        self._markers.set_visible(checked)
        self._markers_btn.setText("◉ Markers" if checked else "○ Markers")

    def _on_right_click(self, scene_x: float, scene_y: float, shift: bool):
        from PySide6.QtCore import QPointF
        scene_pos = QPointF(scene_x, scene_y)
        sp_vb = self._spec_plot.getViewBox()
        wp_vb = self._wf_plot.getViewBox()

        # Map scene → data coords for each ViewBox, then check against visible range.
        # sceneBoundingRect() is unreliable in PyQtGraph; viewRange() is authoritative.
        wf_pt  = wp_vb.mapSceneToView(scene_pos)
        sp_pt  = sp_vb.mapSceneToView(scene_pos)
        wf_xr, wf_yr = wp_vb.viewRange()
        sp_xr, sp_yr = sp_vb.viewRange()

        in_wf = (wf_xr[0] <= wf_pt.x() <= wf_xr[1] and
                 wf_yr[0] <= wf_pt.y() <= wf_yr[1])
        in_sp = (sp_xr[0] <= sp_pt.x() <= sp_xr[1] and
                 sp_yr[0] <= sp_pt.y() <= sp_yr[1])

        # Waterfall right-click → time marker (time axis); shift → freq marker
        # Spectrum right-click → freq marker
        if in_wf and shift:
            self._markers._place_freq_marker(wf_pt.x())
        elif in_wf:
            self._markers._place_time_marker(wf_pt.y())
        elif in_sp:
            self._markers._place_freq_marker(sp_pt.x())

    # ------------------------------------------------------------------
    # Proxy helpers so callers don't need to reach into .spectrum/.waterfall
    # ------------------------------------------------------------------

    def set_freq_range(self, center_hz: float, sample_rate: float):
        self.spectrum.set_freq_range(center_hz, sample_rate)
        self.waterfall.set_freq_range(center_hz, sample_rate)

    def add_vfo_marker(self, vfo_id: int, freq_hz: float, bandwidth_hz: float,
                       color: str, label: str):
        self.spectrum.add_vfo_marker(vfo_id, freq_hz, bandwidth_hz, color, label)
        self.waterfall.add_vfo_marker(vfo_id, freq_hz, color, label)

    def update_vfo_marker(self, vfo_id: int, freq_hz: float,
                          bandwidth_hz: Optional[float] = None):
        self.spectrum.update_vfo_marker(vfo_id, freq_hz, bandwidth_hz)
        self.waterfall.update_vfo_marker(vfo_id, freq_hz)

    def set_vfo_label(self, vfo_id: int, label: str):
        """Update label for a VFO marker on both spectrum and waterfall."""
        try:
            self.spectrum.set_vfo_label(vfo_id, label)
        except Exception:
            pass
        try:
            self.waterfall.set_vfo_label(vfo_id, label)
        except Exception:
            pass

    def remove_vfo_marker(self, vfo_id: int):
        self.spectrum.remove_vfo_marker(vfo_id)
        self.waterfall.remove_vfo_marker(vfo_id)

    def set_active_vfo_marker(self, vfo_id: int):
        self.spectrum.set_active_vfo_marker(vfo_id)

    # ------------------------------------------------------------------
    # Signal auto-detection overlay
    # ------------------------------------------------------------------

    def _on_annotations_toggled(self, enabled: bool):
        self._annotations_enabled = enabled
        if not enabled:
            self._signal_overlay.clear()

    def update_signal_annotations(self, signals: list):
        """Refresh all auto-detected signal overlays on the spectrum plot."""
        if not self._annotations_enabled:
            return
        with self._profiler.measure("visual / signal annotations"):
            self._signal_overlay.update(signals)

    def clear_signal_annotations(self):
        self._signal_overlay.clear()
