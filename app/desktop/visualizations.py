"""
Real-time spectrum and waterfall visualization using pyqtgraph.

Both views live in one GraphicsLayoutWidget:
  Row 0 (10%): spectrum — auto Y-scale, VFO overlays, click-to-tune
  Row 1 (90%): waterfall — X axis linked to spectrum (same MHz range)
"""

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout
import pyqtgraph as pg
import logging
import time

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


@dataclass
class VFOMarker:
    region:     pg.LinearRegionItem
    center:     pg.InfiniteLine
    label:      pg.TextItem
    color:      str
    freq_hz:    float = 0.0
    bandwidth_hz: float = 12_500.0


class SpectrumViewer(QObject):
    """
    Spectrum analyzer bound to a PlotItem.
    Y-axis auto-ranges to fit peaks on every update.
    Emits frequency_clicked (Hz) when the plot is left-clicked.
    """

    frequency_clicked = Signal(float)   # Hz

    def __init__(self, plot: pg.PlotItem):
        super().__init__()
        self._plot = plot

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

        # Broad Y limits so the user can't pan into nonsense territory
        # plot.getViewBox().setLimits(yMin=-150, yMax=30)

        self._vfo_markers:   Dict[int, VFOMarker] = {}
        self._active_vfo_id: Optional[int]        = None

        plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

    # ------------------------------------------------------------------
    # Frequency range
    # ------------------------------------------------------------------

    def set_freq_range(self, center_hz: float, sample_rate: float):
        self._center_hz    = center_hz
        self._sample_rate  = sample_rate
        lo = (center_hz - sample_rate / 2) / 1e6
        hi = (center_hz + sample_rate / 2) / 1e6
        span = hi - lo
        self._plot.getViewBox().setLimits(
            xMin=lo, xMax=hi,
            minXRange=span * 0.001,   # allow zooming down to 0.1 % of bandwidth
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
        else:
            x = np.linspace(
                (self._center_hz - self._sample_rate / 2) / 1e6,
                (self._center_hz + self._sample_rate / 2) / 1e6,
                n,
            )

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

        region = pg.LinearRegionItem(
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
            label=label,
            labelOpts={'color': color, 'position': 0.95, 'movable': True},
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
        region.sigRegionChangeFinished.connect(
            lambda reg, vid=vfo_id: self._on_region_dragged(vid, reg)
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
        self.frequency_clicked.emit(freq_hz)

    def _on_region_dragged(self, vfo_id: int, region: pg.LinearRegionItem):
        lo, hi     = region.getRegion()
        center_mhz = (lo + hi) / 2
        freq_hz    = center_mhz * 1e6
        marker     = self._vfo_markers.get(vfo_id)
        if marker:
            marker.freq_hz     = freq_hz
            marker.bandwidth_hz = (hi - lo) * 1e6
            marker.center.blockSignals(True)
            marker.center.setValue(center_mhz)
            yr = self._plot.getViewBox().viewRange()[1]
            marker.label.setPos(center_mhz, yr[1])
            marker.center.blockSignals(False)
        self.frequency_clicked.emit(freq_hz)


class WaterfallViewer:
    """
    Waterfall display bound to a PlotItem.
    X axis = frequency (MHz), same range as spectrum (linked).
    Y axis = time index (0 = oldest, history_size-1 = newest at top).
    VFO markers are vertical InfiniteLines at frequency positions.
    """

    def __init__(self, plot: pg.PlotItem,
                 history_size: int = 300, freq_bins: int = 1024):
        self._plot        = plot
        self.history_size = history_size
        self.freq_bins    = freq_bins

        plot.setLabel('bottom', 'Frequency (MHz)')
        plot.showGrid(x=True, y=False)
        plot.setMenuEnabled(False)
        plot.hideAxis('left')

        vb = plot.getViewBox()
        vb.setMouseEnabled(x=True, y=False)
        vb.setYRange(0, history_size, padding=0)
        vb.setLimits(yMin=0, yMax=history_size, minYRange=history_size, maxYRange=history_size)

        # shape: (freq_bins, history_size) → dim0=X(freq), dim1=Y(time)
        self.waterfall_data = np.zeros((freq_bins, history_size), dtype=np.uint8)
        self.image_item = pg.ImageItem(self.waterfall_data)
        self.image_item.setColorMap(self._create_colormap())
        self.image_item.setLevels([0, 255])
        plot.addItem(self.image_item)

        self._center_hz:   float = 100e6
        self._sample_rate: float = 20e6
        self._update_image_rect()

        self._wf_markers: Dict[int, pg.InfiniteLine] = {}
        self._wf_labels:  Dict[int, pg.TextItem]     = {}

    def _update_image_rect(self):
        lo_mhz = (self._center_hz - self._sample_rate / 2) / 1e6
        bw_mhz = self._sample_rate / 1e6
        self.image_item.setRect(QRectF(lo_mhz, 0, bw_mhz, self.history_size))

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
        self._plot.addItem(line)
        self._plot.addItem(text)

        self._wf_markers[vfo_id] = line
        self._wf_labels[vfo_id]  = text

    def update_vfo_marker(self, vfo_id: int, freq_hz: float):
        line = self._wf_markers.get(vfo_id)
        text = self._wf_labels.get(vfo_id)
        if line:
            line._vfo_freq_hz = freq_hz
            mhz = freq_hz / 1e6
            line.setValue(mhz)
            if text:
                text.setPos(mhz, self.history_size * 0.97)

    def remove_vfo_marker(self, vfo_id: int):
        line = self._wf_markers.pop(vfo_id, None)
        text = self._wf_labels.pop(vfo_id, None)
        if line: self._plot.removeItem(line)
        if text: self._plot.removeItem(text)

    # ------------------------------------------------------------------
    # Waterfall update — new column at top (highest Y = newest)
    # ------------------------------------------------------------------

    def update_waterfall(self, col_data):
        if col_data is None or len(col_data) == 0:
            return

        col = np.asarray(col_data, dtype=np.float32)

        if len(col) != self.freq_bins:
            idx = np.linspace(0, len(col) - 1, self.freq_bins).astype(int)
            col = col[idx]

        col_max = col.max()
        if col_max > 255:
            col = col / (col_max + 1e-10) * 255
        col = np.clip(col, 0, 255).astype(np.uint8)

        # Roll time axis: newest column goes to index -1 (top of screen)
        self.waterfall_data = np.roll(self.waterfall_data, -1, axis=1)
        self.waterfall_data[:, -1] = col
        self.image_item.setImage(self.waterfall_data, autoLevels=False)

    # ------------------------------------------------------------------
    # Colormap (blue→cyan→green→yellow→red)
    # ------------------------------------------------------------------

    def _create_colormap(self):
        colors = []
        for i in range(256):
            h = (1 - i / 256) * 240
            r, g, b = self._hsv_to_rgb(h / 360, 1.0, 1.0)
            colors.append((r, g, b, 255))
        return pg.ColorMap(pos=np.linspace(0, 1, len(colors)), color=colors)

    @staticmethod
    def _hsv_to_rgb(h, s, v):
        c = v * s
        x = c * (1 - abs((h * 6) % 2 - 1))
        m = v - c
        if   h < 1/6: r, g, b = c, x, 0
        elif h < 2/6: r, g, b = x, c, 0
        elif h < 3/6: r, g, b = 0, c, x
        elif h < 4/6: r, g, b = 0, x, c
        elif h < 5/6: r, g, b = x, 0, c
        else:         r, g, b = c, 0, x
        return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


class VisualizationPanel(QWidget):
    """
    Spectrum (10%) above waterfall (90%) in one GraphicsLayoutWidget.
    The X axes are linked — panning/zooming one tracks the other.
    """

    def __init__(self, parent=None, display_buffers=None):
        super().__init__(parent)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground('#1a1a1a')
        layout.addWidget(self._glw)
        self.setLayout(layout)

        spec_plot = self._glw.addPlot(row=0, col=0)
        # spec_plot.setDecimationMode(pg.ViewBox.DecimationMode.Subsample)
        # spec_plot.setDownsampling(ds=8, auto=False)
        self._glw.ci.layout.setRowStretchFactor(0, 4)   # 40 %


        wf_plot = self._glw.addPlot(row=1, col=0)
        self._glw.ci.layout.setRowStretchFactor(1, 6)   # 60 %

        # Link X so zoom/pan in one view mirrors the other
        wf_plot.setXLink(spec_plot)

        self.spectrum  = SpectrumViewer(spec_plot)
        self.waterfall = WaterfallViewer(wf_plot)

        self._pending_spectrum:  Optional[np.ndarray] = None
        self._pending_waterfall: Optional[np.ndarray] = None
        # Optional shared buffers (dict with 'spectrum' and 'waterfall' SharedLatest)
        self._display_buffers = display_buffers

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)   # 20 fps cap — rendering never blocks signal delivery
        self._render_timer.timeout.connect(self._flush_pending)
        self._render_timer.start()

    # ------------------------------------------------------------------
    # Data ingestion — store only; timer does the actual rendering
    # ------------------------------------------------------------------

    def update_spectrum(self, data: np.ndarray):
        self._pending_spectrum = data

    def update_waterfall(self, data: np.ndarray):
        self._pending_waterfall = data

    def _flush_pending(self):
        t0 = time.monotonic()
        # First, prefer data from shared buffers if available (producer writes latest).
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
                # Fall back to existing pending buffers on error
                pass

        if self._pending_spectrum is not None:
            self.spectrum.update_spectrum(self._pending_spectrum)
            self._pending_spectrum = None
        if self._pending_waterfall is not None:
            self.waterfall.update_waterfall(self._pending_waterfall)
            self._pending_waterfall = None
        dt = (time.monotonic() - t0) * 1000.0
        if dt > 30.0:
            logger.warning(f"[Visualization] render slow: {dt:.1f} ms (spectrum/waterfall update)")

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

    def remove_vfo_marker(self, vfo_id: int):
        self.spectrum.remove_vfo_marker(vfo_id)
        self.waterfall.remove_vfo_marker(vfo_id)

    def set_active_vfo_marker(self, vfo_id: int):
        self.spectrum.set_active_vfo_marker(vfo_id)
