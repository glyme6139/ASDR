"""
ADS-B aircraft map window.

Displays live aircraft positions on a dark OpenStreetMap tile layer via
QWebEngineView + Leaflet.js.  Plane markers rotate with their reported
track and are coloured by altitude.  Stale aircraft (silent > 90 s) are
removed automatically.

Requires: PySide6-WebEngine  (pip install PySide6-WebEngine)
"""

import json
import time

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QGroupBox, QSplitter
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWebEngineWidgets import QWebEngineView

from .base import BaseDecoderWindow

_AIRCRAFT_TIMEOUT = 90    # seconds before removing a silent aircraft
_REFRESH_MS       = 1_000

_MAP_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<link rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  body, html, #map { margin:0; padding:0; width:100%; height:100%; }
  .leaflet-popup-content-wrapper,
  .leaflet-popup-tip {
    background:#2a2a2a; color:#e0e0e0; border:1px solid #444;
  }
  .leaflet-popup-close-button { color:#aaa !important; }
  .leaflet-tooltip {
    background:#2a2a2a; color:#e0e0e0; border:1px solid #555;
    font-size:12px; font-weight:600;
  }
</style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var map = L.map('map', { zoomControl: true }).setView([48, 8], 5);
L.tileLayer(
  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  { attribution: '&copy; OpenStreetMap contributors &copy; CartoDB', maxZoom: 19 }
).addTo(map);

var markers = {};

function planeIcon(color, track) {
  var rot = (track != null) ? track : 0;
  return L.divIcon({
    className: '',
    html: '<div style="color:' + color + '; font-size:20px;'
        + 'transform:rotate(' + rot + 'deg);'
        + 'line-height:1; text-shadow:0 0 4px #000;">&#9992;</div>',
    iconSize:    [24, 24],
    iconAnchor:  [12, 12],
    popupAnchor: [0, -14]
  });
}

function updateAircraft(icao, lat, lon, color, callsign, airline, country, originAirport, destination, destinationCountry, alt, speed, track) {
  var label = (callsign && callsign !== icao) ? callsign + ' (' + icao + ')' : icao;
  var popup = '<b>' + label + '</b>';
  if (airline) popup += '<br>Airline: ' + airline;
  if (country) popup += '<br>Country: ' + country;
  if (originAirport) popup += '<br>Origin: ' + originAirport;
  if (destination) popup += '<br>Destination: ' + destination;
  if (destinationCountry) popup += '<br>Destination country: ' + destinationCountry;
  if (alt   != null) popup += '<br>' + alt.toLocaleString() + ' ft';
  if (speed != null) popup += '<br>' + speed + ' kt';
  if (track != null) popup += '<br>&#8599; ' + Math.round(track) + '&deg;';

  var icon = planeIcon(color, track);
  if (markers[icao]) {
    markers[icao].setLatLng([lat, lon]);
    markers[icao].setIcon(icon);
    markers[icao].getPopup().setContent(popup);
    markers[icao].getTooltip().setContent(label);
  } else {
    var m = L.marker([lat, lon], { icon: icon })
             .bindPopup(popup, { className: 'adsb-popup' })
             .bindTooltip(label, { direction: 'top', opacity: 0.9 });
    m.addTo(map);
    markers[icao] = m;
  }
}

function removeAircraft(icao) {
  if (markers[icao]) {
    map.removeLayer(markers[icao]);
    delete markers[icao];
  }
}

function fitBounds(s, w, n, e) {
  map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
}
</script>
</body>
</html>
"""


class ADSBMapWindow(BaseDecoderWindow):
    """Real-time ADS-B aircraft map using Leaflet + CartoDB dark tiles."""

    def __init__(self, vfo_id: int):
        super().__init__(f"ADS-B Map — VFO {vfo_id + 1}", vfo_id, "ADSB")
        self._aircraft:    dict = {}
        self._auto_ranged: bool = False
        self._page_ready:  bool = False
        self._list_widget: QListWidget = None

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_REFRESH_MS)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self):
        self._web = QWebEngineView()
        self._web.setHtml(_MAP_HTML)
        self._web.loadFinished.connect(self._on_load_finished)

        self._status_lbl = QLabel("Waiting for ADS-B data…")
        self._status_lbl.setStyleSheet("color:#aaaaaa; padding:2px 4px;")

        fit_btn = QPushButton("Fit view")
        fit_btn.setToolTip("Zoom to show all tracked aircraft")
        fit_btn.setFixedWidth(80)
        fit_btn.clicked.connect(self._fit_view)

        legend_lbl = QLabel(
            '<span style="color:#00c8ff">✈</span> &lt;10k ft &nbsp;'
            '<span style="color:#ffdd00">✈</span> 10–30k ft &nbsp;'
            '<span style="color:#ff4400">✈</span> &gt;30k ft'
        )
        legend_lbl.setStyleSheet("color:#888888; padding:2px 4px;")

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self._status_lbl)
        bar.addStretch()
        bar.addWidget(legend_lbl)
        bar.addWidget(fit_btn)

        # Aircraft list panel (collapsible)
        self._list_widget = QListWidget()
        self._list_widget.setMaximumHeight(200)
        self._list_widget.itemClicked.connect(self._on_aircraft_selected)
        self._list_widget.setStyleSheet(
            "QListWidget { background:#1a1a1a; color:#e0e0e0; border:1px solid #444; }"
            "QListWidget::item { padding:2px; }"
            "QListWidget::item:selected { background:#404080; }"
        )

        list_group = QGroupBox("Tracked Aircraft")
        list_group.setStyleSheet("QGroupBox { color:#e0e0e0; border:1px solid #555; padding-top:8px; margin-top:0; }")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(4, 8, 4, 4)
        list_layout.addWidget(self._list_widget)

        central = QWidget()
        layout  = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._web)
        splitter.addWidget(list_group)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)
        self.resize(960, 680)

    def _on_load_finished(self, ok: bool):
        self._page_ready = ok

    # ── BaseDecoderWindow interface ──────────────────────────────────────

    def push_result(self, data: dict) -> None:
        icao = data.get("icao")
        if not icao:
            return
        state = self._aircraft.setdefault(icao, {})
        state.update(data)
        state["_ts"] = time.time()

    # ── Internal refresh ─────────────────────────────────────────────────

    def _refresh(self):
        if not self._page_ready:
            return

        now = time.time()

        # Evict stale aircraft
        stale = [k for k, d in self._aircraft.items()
                 if now - d.get("_ts", 0) > _AIRCRAFT_TIMEOUT]
        for icao in stale:
            self._aircraft.pop(icao)
            self._js(f"removeAircraft({json.dumps(icao)})")

        # Update / add positioned aircraft
        positioned = [(icao, d) for icao, d in self._aircraft.items()
                      if d.get("lat") is not None and d.get("lon") is not None]

        for icao, d in positioned:
            color = _alt_color(d.get("alt_ft") or 0)
            cs    = json.dumps((d.get("callsign") or "").strip() or None)
            airline = json.dumps((d.get("airline") or d.get("airline_code") or "").strip() or None)
            country = json.dumps((d.get("country") or d.get("origin_country") or "").strip() or None)
            origin_airport = json.dumps((d.get("origin_airport") or "").strip() or None)
            destination = json.dumps((d.get("destination") or d.get("destination_airport") or "").strip() or None)
            destination_country = json.dumps((d.get("destination_country") or "").strip() or None)
            alt   = json.dumps(d.get("alt_ft"))
            speed = json.dumps(d.get("speed_kt"))
            track = json.dumps(d.get("track_deg"))
            self._js(
                f"updateAircraft("
                f"{json.dumps(icao)},{d['lat']},{d['lon']},"
                f"{json.dumps(color)},{cs},{airline},{country},{origin_airport},{destination},{destination_country},"
                f"{alt},{speed},{track})"
            )

        # Auto-fit on first data
        if positioned and not self._auto_ranged:
            self._auto_ranged = True
            self._fit_view()

        # Update aircraft list widget
        self._update_aircraft_list()

        n_total = len(self._aircraft)
        n_pos   = len(positioned)
        if n_total:
            self._status_lbl.setText(f"{n_total} aircraft  |  {n_pos} with position")
        else:
            self._status_lbl.setText("Waiting for ADS-B data…")

    def _fit_view(self):
        pts = [(d["lat"], d["lon"]) for d in self._aircraft.values()
               if d.get("lat") is not None]
        if not pts:
            return
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        pad  = 0.5
        self._js(
            f"fitBounds("
            f"{min(lats) - pad},{min(lons) - pad},"
            f"{max(lats) + pad},{max(lons) + pad})"
        )

    def _js(self, script: str):
        self._web.page().runJavaScript(script)

    def _update_aircraft_list(self):
        """Update the aircraft list widget with current tracked aircraft."""
        self._list_widget.blockSignals(True)
        self._list_widget.clear()

        # Sort by callsign or ICAO
        items = sorted(self._aircraft.items(),
                      key=lambda x: (x[1].get('callsign') or x[0]).upper())

        for icao, data in items:
            cs = data.get('callsign', '').strip() or icao
            alt = data.get('alt_ft')
            speed = data.get('speed_kt')
            airline = data.get('airline') or data.get('airline_code') or ''
            dest = data.get('destination') or data.get('destination_airport') or ''
            has_position = data.get('lat') is not None and data.get('lon') is not None
            last_seen = int(max(0, time.time() - data.get('_ts', time.time())))

            # Build display text
            parts = ['¤' if has_position else '-', cs]
            if airline:
                parts.append(f"[{airline}]")
            if alt is not None:
                parts.append(f"{int(alt)}ft")
            if speed is not None:
                parts.append(f"{int(speed)}kt")
            if dest:
                parts.append(f"→ {dest}")

            parts.append(f'(last seen: {last_seen}s)')
            label = " | ".join(parts)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, icao)  # Store ICAO as data
            self._list_widget.addItem(item)
            self._list_widget.item(self._list_widget.count() - 1).setForeground(Qt.gray if not has_position else Qt.white)
            self._list_widget.item(self._list_widget.count() - 1).setToolTip(f"ICAO: {icao}\nAirline: {airline}\nAltitude: {alt} ft\nSpeed: {speed} kt\nDestination: {dest}\nLast seen: {last_seen} seconds ago")
        self._list_widget.blockSignals(False)

    def _on_aircraft_selected(self, item: QListWidgetItem):
        """Handle aircraft selection from the list."""
        icao = item.data(Qt.UserRole)
        if icao and icao in self._aircraft:
            data = self._aircraft[icao]
            if data.get('lat') is not None and data.get('lon') is not None:
                # Center map on selected aircraft
                self._js(f"map.setView([{data['lat']}, {data['lon']}], 12);")

    # ── Cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _alt_color(alt_ft: int) -> str:
    """Map altitude to CSS hex colour: cyan → yellow → red."""
    t = max(0.0, min(1.0, (alt_ft or 0) / 40_000))
    if t < 0.5:
        k = t * 2
        r, g, b = int(k * 255), int(200 + k * 21), int(255 - k * 255)
    else:
        k = (t - 0.5) * 2
        r, g, b = 255, int(221 - k * 187), 0
    return f"#{r:02x}{g:02x}{b:02x}"
