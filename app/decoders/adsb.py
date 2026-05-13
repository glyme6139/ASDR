"""
ADS-B / Mode-S decoder for aircraft surveillance at 1090 MHz.

Receives narrowband IQ at nb_sr from the VFO channelizer.
Requires nb_sr >= 4 MHz (2+ samples per 0.5 µs half-chip).

For best results: center frequency = 1090 MHz, VFO bandwidth = 8-20 MHz.
"""
import time
import logging
import re
import numpy as np
import requests
import airportsdata
import pycountry
from typing import Optional, Tuple
from collections import deque

from .base import BaseDecoder, DecoderResult

logger = logging.getLogger(__name__)

_AUDIO_RATE   = 48_000
_CHUNK        = 2048
_MIN_NB_SR    = 4_000_000   # minimum sample rate for reliable decoding
_CRC_GEN      = 0xFFF409    # Mode-S CRC-24 generator polynomial
_OPEN_SKY_BASE = 'https://opensky-network.org/api'
_LOOKUP_TTL    = 600
_HTTP_TIMEOUT  = 4

_AIRPORTS = airportsdata.load('ICAO')

# 6-bit character map for callsign decoding (index 0-63)
_CS_MAP = '#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######'
_CALLSIGN_RE = re.compile(r'^([A-Z0-9]{3})([A-Z0-9]{1,5})?$')


class ADSBDecoder(BaseDecoder):
    """Mode-S / ADS-B decoder.  Processes IQ; audio argument is ignored."""

    def __init__(self):
        super().__init__('ADSB', sample_rate=int(_MIN_NB_SR))
        self._aircraft: dict = {}   # ICAO → accumulated state
        self._pending: deque = deque()
        self._lookup_cache: dict = {}

    # ------------------------------------------------------------------
    # BaseDecoder interface
    # ------------------------------------------------------------------

    def process(self, data: np.ndarray, audio=None) -> Optional[DecoderResult]:
        if not self.is_enabled or data is None or len(data) < 64:
            return None

        nb_sr = len(data) * _AUDIO_RATE / _CHUNK
        if nb_sr < _MIN_NB_SR:
            return None

        mag = np.abs(data).astype(np.float32)
        self._find_messages(mag, nb_sr)

        return self._pending.popleft() if self._pending else None

    def reset(self):
        self._aircraft.clear()
        self._pending.clear()

    def format_result(self, result: DecoderResult) -> str:
        d = result.data if isinstance(result.data, dict) else {}
        parts = [f"[{d.get('icao', '?')}]"]
        cs = d.get('callsign', '').strip()
        if cs:
            parts.append(cs)
        if d.get('airline'):
            parts.append(f"AIRLINE:{d['airline']}")
        if d.get('country'):
            parts.append(f"COUNTRY:{d['country']}")
        if d.get('origin_airport'):
            parts.append(f"ORIGIN:{d['origin_airport']}")
        if d.get('destination'):
            parts.append(f"DEST:{d['destination']}")
        if d.get('destination_country'):
            parts.append(f"DEST_COUNTRY:{d['destination_country']}")
        if d.get('alt_ft') is not None:
            parts.append(f"ALTITUDE:{d['alt_ft']}ft ")
        if d.get('speed_kt') is not None:
            parts.append(f"SPEED:{d['speed_kt']}kt ")
        if d.get('track_deg') is not None:
            parts.append(f"TRACK:{d['track_deg']:.0f}° ")
        if d.get('vs_fpm') is not None:
            parts.append(f"VERTICAL SPEED:{d['vs_fpm']:+d}fpm ")
        if d.get('lat') is not None and d.get('lon') is not None:
            parts.append(f"POSITION:{d['lat']:.4f},{d['lon']:.4f}")
        return ' '.join(parts)

    # ------------------------------------------------------------------
    # Preamble search
    # ------------------------------------------------------------------

    def _find_messages(self, mag: np.ndarray, nb_sr: float):
        hcs   = nb_sr * 0.5e-6             # samples per half-chip (float)
        hcs_i = max(1, round(hcs))
        bit_s = 2 * hcs_i                  # samples per bit
        pre_s = 16 * hcs_i                 # preamble = 8 µs = 16 half-chips
        long_s  = pre_s + 112 * bit_s
        short_s = pre_s +  56 * bit_s
        n = len(mag)

        if n < long_s:
            return

        n_cand = n - long_s
        if n_cand <= 0:
            return

        # Preamble pulse positions (half-chip offsets from candidate start):
        #   HIGH: 0, 2, 7, 9   LOW: 1, 3, 4, 5, 6, 8, 10
        high_offs = [o * hcs_i for o in (0, 2, 7, 9)]
        low_offs  = [o * hcs_i for o in (1, 3, 4, 5, 6, 8, 10)]

        high_sum = np.zeros(n_cand, dtype=np.float32)
        for off in high_offs:
            if off + n_cand <= n:
                high_sum += mag[off:off + n_cand]
        high_sum /= len(high_offs)

        low_sum = np.zeros(n_cand, dtype=np.float32)
        for off in low_offs:
            if off + n_cand <= n:
                low_sum += mag[off:off + n_cand]
        low_sum /= len(low_offs)

        noise_floor = float(np.percentile(mag, 50))
        scores      = high_sum / (low_sum + 1e-6)
        candidates  = np.where((scores > 2.5) & (high_sum > noise_floor * 1.5))[0]

        if len(candidates) == 0:
            return

        # Non-maximum suppression: cluster candidates within pre_s distance,
        # keep the position with the highest score in each cluster.
        selected: list = []
        cands = list(candidates)
        ci = 0
        while ci < len(cands):
            cj = ci + 1
            while cj < len(cands) and cands[cj] - cands[ci] < pre_s:
                cj += 1
            cluster = cands[ci:cj]
            best = cluster[int(np.argmax(scores[cluster]))]
            selected.append(best)
            ci = cj

        for i in selected:
            ds = i + pre_s      # data start sample
            if ds + 112 * bit_s <= n:
                bits = self._decode_bits(mag, ds, 112, hcs_i)
                if self._check_crc(bits):
                    r = self._decode_message(bits)
                    if r:
                        self._pending.append(r)
                    continue
            if ds + 56 * bit_s <= n:
                bits = self._decode_bits(mag, ds, 56, hcs_i)
                if self._check_crc(bits):
                    r = self._decode_message(bits)
                    if r:
                        self._pending.append(r)

    # ------------------------------------------------------------------
    # PPM bit decode (vectorized)
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_bits(mag: np.ndarray, start: int, n_bits: int, hcs_i: int) -> np.ndarray:
        bit_size = 2 * hcs_i
        if hcs_i == 1:
            idx = start + np.arange(n_bits) * 2
            return (mag[idx] > mag[idx + 1]).astype(np.uint8)
        bit_idx   = np.arange(n_bits)
        hc_idx    = np.arange(hcs_i)
        first_idx = start + bit_idx[:, None] * bit_size + hc_idx[None, :]
        sec_idx   = first_idx + hcs_i
        return (mag[first_idx].sum(axis=1) > mag[sec_idx].sum(axis=1)).astype(np.uint8)

    # ------------------------------------------------------------------
    # CRC-24 / Mode-S
    # ------------------------------------------------------------------

    @staticmethod
    def _bits_to_bytes(bits: np.ndarray) -> bytes:
        n = len(bits) // 8
        out = bytearray(n)
        for i in range(n):
            b = 0
            for j in range(8):
                b = (b << 1) | int(bits[i * 8 + j])
            out[i] = b
        return bytes(out)

    @classmethod
    def _check_crc(cls, bits: np.ndarray) -> bool:
        if len(bits) not in (56, 112):
            return False
        data = cls._bits_to_bytes(bits)
        crc = 0
        for byte in data:
            crc ^= byte << 16
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= _CRC_GEN
        return (crc & 0xFFFFFF) == 0

    @staticmethod
    def _bits_to_int(bits) -> int:
        v = 0
        for b in bits:
            v = (v << 1) | int(b)
        return v

    # ------------------------------------------------------------------
    # Message decoding
    # ------------------------------------------------------------------

    def _decode_message(self, bits: np.ndarray) -> Optional[DecoderResult]:
        df = self._bits_to_int(bits[0:5])
        if df not in (17, 18, 19):
            return None

        icao = f'{self._bits_to_int(bits[8:32]):06X}'
        me   = bits[32:88]
        tc   = self._bits_to_int(me[0:5])
        now  = time.time()

        state = self._aircraft.setdefault(icao, {})

        if 1 <= tc <= 4:
            state['callsign'] = self._decode_callsign(me)
        elif 9 <= tc <= 18:
            alt_ft, cpr_f, cpr_lat, cpr_lon = self._decode_position_msg(me)
            if alt_ft is not None:
                state['alt_ft'] = alt_ft
            key = 'cpr_odd' if cpr_f else 'cpr_even'
            state[key]          = (cpr_lat, cpr_lon)
            state[key + '_t']   = now
            lat, lon = self._cpr_global(state, now)
            if lat is not None:
                state['lat'] = lat
                state['lon'] = lon
        elif tc == 19:
            speed, track, vs = self._decode_velocity(me)
            if speed is not None: state['speed_kt']  = speed
            if track is not None: state['track_deg'] = track
            if vs    is not None: state['vs_fpm']    = vs

        state.update(self._lookup_metadata(icao, state.get('callsign')))

        result_data: dict = {'icao': icao}
        for k in (
            'callsign', 'airline', 'airline_code', 'flight_number', 'country',
            'origin_airport', 'origin_country', 'destination', 'destination_airport',
            'destination_country', 'alt_ft', 'speed_kt', 'track_deg', 'vs_fpm',
            'lat', 'lon',
        ):
            if k in state:
                result_data[k] = state[k]

        return DecoderResult(decoder_name='ADSB', timestamp=now, data=result_data)

    def _lookup_metadata(self, icao: str, callsign: Optional[str] = None) -> dict:
        now = time.time()
        cached = self._lookup_cache.get(icao)
        if cached and now - cached.get('_ts', 0.0) < _LOOKUP_TTL:
            return {k: v for k, v in cached.items() if k != '_ts'}

        meta: dict = {}
        callsign_clean = self._clean_callsign(callsign or '')
        if callsign_clean:
            airline_code, flight_number = self._split_callsign(callsign_clean)
            if airline_code:
                meta['airline_code'] = airline_code
                meta['airline'] = airline_code
            if flight_number:
                meta['flight_number'] = flight_number
            meta['callsign'] = callsign_clean

        try:
            response = requests.get(
                f'{_OPEN_SKY_BASE}/states/all?icao24={icao.lower()}',
                timeout=_HTTP_TIMEOUT,
            )
            if response.ok:
                payload = response.json()
                states = payload.get('states') or []
                if states:
                    origin_country = states[0][2]
                    if origin_country:
                        meta['country'] = origin_country
                    live_callsign = self._clean_callsign(states[0][1] or '')
                    if live_callsign and not meta.get('callsign'):
                        meta['callsign'] = live_callsign
        except Exception:
            pass

        try:
            end = int(now)
            begin = end - 6 * 3600
            response = requests.get(
                f'{_OPEN_SKY_BASE}/flights/aircraft?icao24={icao.lower()}&begin={begin}&end={end}',
                timeout=_HTTP_TIMEOUT,
            )
            if response.ok:
                flights = response.json() or []
                flight = None
                for item in reversed(flights):
                    if item.get('estArrivalAirport') or item.get('estDepartureAirport'):
                        flight = item
                        break
                if flight:
                    dep_code = flight.get('estDepartureAirport')
                    arr_code = flight.get('estArrivalAirport')
                    if dep_code:
                        dep = self._format_airport(dep_code)
                        if dep:
                            meta['origin_airport'] = dep
                            airport_info = _AIRPORTS.get(dep_code.upper())
                            if airport_info:
                                meta['origin_country'] = self._country_name(airport_info.get('country'))
                    if arr_code:
                        arr = self._format_airport(arr_code)
                        if arr:
                            meta['destination_airport'] = arr
                            meta['destination'] = arr
                            airport_info = _AIRPORTS.get(arr_code.upper())
                            if airport_info:
                                meta['destination_country'] = self._country_name(airport_info.get('country'))
        except Exception:
            pass

        self._lookup_cache[icao] = {'_ts': now, **meta}
        return meta

    @staticmethod
    def _clean_callsign(callsign: str) -> str:
        return callsign.strip().upper()

    @staticmethod
    def _split_callsign(callsign: str) -> Tuple[Optional[str], Optional[str]]:
        match = _CALLSIGN_RE.match(callsign.strip().upper())
        if not match:
            return None, None
        airline_code, flight_number = match.groups()
        return airline_code, flight_number

    @staticmethod
    def _country_name(code: Optional[str]) -> Optional[str]:
        if not code:
            return None
        country = pycountry.countries.get(alpha_2=code.upper())
        return country.name if country else code.upper()

    def _format_airport(self, icao_code: Optional[str]) -> Optional[str]:
        if not icao_code:
            return None
        info = _AIRPORTS.get(icao_code.upper())
        if not info:
            return icao_code.upper()
        label = info.get('iata') or info.get('icao') or icao_code.upper()
        name = info.get('name')
        country = self._country_name(info.get('country'))
        parts = [label]
        if name:
            parts.append(name)
        if country:
            parts.append(country)
        return ' - '.join(parts)

    # ------------------------------------------------------------------
    # TC-specific decoders
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_callsign(me: np.ndarray) -> str:
        chars = []
        for i in range(8):
            idx = ADSBDecoder._bits_to_int(me[8 + i * 6: 8 + i * 6 + 6])
            chars.append(_CS_MAP[idx] if idx < len(_CS_MAP) else '#')
        return ''.join(chars).rstrip('#').strip()

    @staticmethod
    def _decode_position_msg(me: np.ndarray) -> Tuple:
        alt_raw = ADSBDecoder._bits_to_int(me[8:20])
        q_bit   = (alt_raw >> 4) & 1
        if q_bit:
            upper   = (alt_raw >> 5) & 0x7F   # bits 11..5
            lower   = alt_raw & 0x0F           # bits 3..0
            alt_ft  = ((upper << 4) | lower) * 25 - 1000
        else:
            alt_ft = None                      # Gillham code — skip

        cpr_f   = int(me[21])
        cpr_lat = ADSBDecoder._bits_to_int(me[22:39])
        cpr_lon = ADSBDecoder._bits_to_int(me[39:56])
        return alt_ft, cpr_f, cpr_lat, cpr_lon

    @staticmethod
    def _decode_velocity(me: np.ndarray) -> Tuple:
        subtype = ADSBDecoder._bits_to_int(me[5:8])
        if subtype not in (1, 2):
            return None, None, None

        # ME bit layout for TC=19, subtype 1/2 (ground speed):
        # [TC(5)][ST(3)][IC(1)][IFR(1)][NAC(3)][EW_DIR(1)][EW_VEL(10)]
        # [NS_DIR(1)][NS_VEL(10)][VRT_SRC(1)][VRT_SIGN(1)][VRT(9)][resv(2)][diff(8)]
        ew_dir = int(me[13])
        ew_raw = ADSBDecoder._bits_to_int(me[14:24])   # 10 bits
        ns_dir = int(me[24])
        ns_raw = ADSBDecoder._bits_to_int(me[25:35])   # 10 bits

        if ew_raw == 0 or ns_raw == 0:
            return None, None, None

        ew_kt = ew_raw - 1
        ns_kt = ns_raw - 1
        if ew_dir: ew_kt = -ew_kt
        if ns_dir: ns_kt = -ns_kt

        speed = round((ew_kt ** 2 + ns_kt ** 2) ** 0.5)
        track = round((90.0 - float(np.degrees(np.arctan2(ns_kt, ew_kt)))) % 360.0, 1)

        vr_sign = int(me[36])
        vr_raw  = ADSBDecoder._bits_to_int(me[37:46])  # 9 bits
        vs: Optional[int] = None
        if vr_raw != 0:
            vs = (vr_raw - 1) * 64
            if vr_sign: vs = -vs

        return speed, track, vs

    # ------------------------------------------------------------------
    # CPR global position decode
    # ------------------------------------------------------------------

    def _cpr_global(self, state: dict, now: float) -> Tuple:
        if 'cpr_even' not in state or 'cpr_odd' not in state:
            return None, None
        if abs(state.get('cpr_even_t', 0) - state.get('cpr_odd_t', 0)) > 10:
            return None, None

        lat0, lon0 = state['cpr_even']   # raw 17-bit CPR integers
        lat1, lon1 = state['cpr_odd']

        j = int(np.floor(59.0 * lat0 / 131072 - 60.0 * lat1 / 131072 + 0.5))

        Rlat0 = (360.0 / 60) * (j % 60 + lat0 / 131072)
        Rlat1 = (360.0 / 59) * (j % 59 + lat1 / 131072)
        if Rlat0 >= 270: Rlat0 -= 360
        if Rlat1 >= 270: Rlat1 -= 360

        nl0 = self._nl(Rlat0)
        if nl0 != self._nl(Rlat1):
            return None, None

        m = int(np.floor(lon0 * (nl0 - 1) / 131072 - lon1 * nl0 / 131072 + 0.5))

        if state.get('cpr_even_t', 0) >= state.get('cpr_odd_t', 0):
            rlat = Rlat0
            ni   = max(nl0, 1)
            rlon = (360.0 / ni) * (m % ni + lon0 / 131072)
        else:
            rlat = Rlat1
            ni   = max(nl0 - 1, 1)
            rlon = (360.0 / ni) * (m % ni + lon1 / 131072)

        if rlon > 180:  rlon -= 360
        if not (-90 <= rlat <= 90):
            return None, None

        return round(rlat, 5), round(rlon, 5)

    @staticmethod
    def _nl(lat: float) -> int:
        alat = abs(lat)
        if alat >= 87.0: return 1
        if alat == 0.0:  return 59
        nz = 15.0
        a  = 1.0 - np.cos(np.pi / (2.0 * nz))
        b  = np.cos(np.radians(alat)) ** 2
        return max(1, int(np.floor(2.0 * np.pi / np.arccos(1.0 - a / b))))
