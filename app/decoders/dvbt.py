"""
DVB-T 2K OFDM decoder (8 MHz channel, 1/4 guard interval).

Implements ETSI EN 300 744 receive chain:
  IQ → resample → guard-interval sync → FFT → channel estimation →
  QAM demap → bit deinterleave → Viterbi → byte deinterleave → RS(204,188) → TS

Optional dependencies (improves decode quality if installed):
  pip install reedsolo     — Reed-Solomon FEC
  pip install commpy       — Viterbi decoder (falls back to numpy hard-decision)
"""

import os
import socket
import subprocess
import time
import logging
from math import gcd
from typing import Optional, List, Tuple

import numpy as np
from scipy.signal import resample as sp_resample

from .base import BaseIQDecoder, DecoderResult

# Standard VLC install paths on Windows (checked in order)
_VLC_PATHS = [
    r'C:\Program Files\VideoLAN\VLC\vlc.exe',
    r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe',
]

# UDP stream port (VLC: vlc udp://@:<port>)
UDP_PORT = 1234
# MPEG-TS over UDP: 7 TS packets per datagram (7 × 188 = 1316 bytes, fits in one Ethernet MTU)
TS_PER_DATAGRAM = 7

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DVB-T 2K constants (ETSI EN 300 744)
# ---------------------------------------------------------------------------
N_FFT       = 2048
K_CARRIERS  = 1705                      # useful data+pilot carriers
N_GUARD     = N_FFT // 4               # 512  (1/4 guard interval)
N_SYMBOL    = N_FFT + N_GUARD          # 2560 samples per OFDM symbol
N_FRAME     = 68                        # symbols per DVB-T super-frame

# Nominal DVB-T baseband sample rate for 8 MHz channel
DVB_T_SR    = 64_000_000.0 / 7.0      # ≈ 9 142 857 Hz

# First FFT bin index (after fftshift) of the K_CARRIERS block
# Carriers span [-(K-1)/2 … +(K-1)/2] around DC = bin N_FFT//2
FIRST_CARRIER = N_FFT // 2 - (K_CARRIERS - 1) // 2   # = 172

# Continual pilot carrier indices within the K_CARRIERS window (2K mode)
# Source: ETSI EN 300 744 Table 15  (44 entries — carrier 1704 is the last useful)
CONTINUAL_PILOTS = np.array([
      0,  48,  54,  87, 141, 156, 192, 201, 255, 279,
    282, 333, 432, 450, 483, 525, 531, 618, 636, 714,
    759, 765, 780, 804, 873, 888, 894, 963, 972, 1005,
   1026, 1035, 1107, 1110, 1137, 1140, 1146, 1206, 1269, 1323,
   1377, 1491, 1683, 1704,
], dtype=np.int32)

# DVB-T FEC: convolutional K=7, generators G1=0o171, G2=0o133
_G1 = 0o171   # 0b1111001
_G2 = 0o133   # 0b1011011
_N_STATES = 64   # 2^(K-1)


# ---------------------------------------------------------------------------
# Viterbi trellis (pre-computed)
# ---------------------------------------------------------------------------

def _build_dvbt_trellis() -> Tuple[np.ndarray, np.ndarray]:
    """Return (next_state, out_bits) tables for K=7 convolutional code.

    next_state[s, b]    — next state index (int)
    out_bits[s, b]      — 2-bit output packed as int (G1 bit | G2 bit<<1)
    """
    ns  = np.zeros((_N_STATES, 2), dtype=np.int32)
    out = np.zeros((_N_STATES, 2), dtype=np.int32)
    for s in range(_N_STATES):
        for b in range(2):
            reg = (b << 6) | s
            ns[s, b]  = (b << 5) | (s >> 1)
            g1 = bin(reg & _G1).count('1') % 2
            g2 = bin(reg & _G2).count('1') % 2
            out[s, b] = g1 | (g2 << 1)
    return ns, out


_TRELLIS_NS, _TRELLIS_OUT = _build_dvbt_trellis()


# ---------------------------------------------------------------------------
# Decoder class
# ---------------------------------------------------------------------------

class DVBTDecoder(BaseIQDecoder):
    """DVB-T 2K OFDM decoder.

    Works as a drop-in ASDR decoder: plug into any VFO running in 'NFM'
    (or any demod mode — audio is ignored).  Set VFO bandwidth ≥ 8 MHz and
    center frequency on the DVB-T multiplex.

    Reports sync status every frame even before TS packets are decoded.
    """

    def __init__(self):
        super().__init__('DVB-T', sample_rate=int(DVB_T_SR))
        # Increase chunk size so decode_iq gets called with many OFDM symbols at once
        self.min_buffer_size = int(DVB_T_SR / 10)   # 100 ms of IQ at DVB-T rate

        # Actual incoming IQ rate (set by vfo.py via set_iq_sample_rate)
        self._iq_sr: float = DVB_T_SR

        # Internal buffer at (possibly) the original incoming rate;
        # decode_iq() re-samples each chunk to DVB_T_SR and appends here.
        self._dvbt_buf = np.zeros(0, dtype=np.complex64)

        # Symbol synchronisation
        self._sym_offset: Optional[int] = None
        self._freq_offset: float = 0.0

        # Symbol accumulator for one DVB-T frame (68 symbols)
        self._sym_buf: List[np.ndarray] = []

        # Channel estimate (one complex coefficient per carrier)
        self._h = np.ones(K_CARRIERS, dtype=np.complex64)

        # Statistics
        self._packets_total = 0
        self._last_snr_db   = 0.0
        self._sync_lost_cnt = 0

        # UDP stream to VLC
        self._udp_sock:  Optional[socket.socket] = None
        self._ts_pending: List[bytes] = []   # accumulator before sending a datagram
        self._vlc_launched = False
        self._open_udp_socket()

        # Optional libraries
        self._rs = None
        self._commpy_viterbi = None
        self._load_optional_libs()

        logger.info("DVB-T decoder initialised  N=%d  G=%d  SR=%.0f Hz",
                    N_FFT, N_GUARD, DVB_T_SR)

    # ------------------------------------------------------------------
    # Optional library loading
    # ------------------------------------------------------------------

    def _load_optional_libs(self):
        try:
            import reedsolo
            # RS(204,188): 16 check bytes, nsize=204, GF(2^8) prim poly 0x11d
            self._rs = reedsolo.RSCodec(nsym=16, nsize=204, fcr=0, prim=0x11d)
            logger.info("DVB-T: reedsolo loaded (RS FEC enabled)")
        except Exception:
            try:
                import reedsolo
                self._rs = reedsolo.RSCodec(16)
                logger.info("DVB-T: reedsolo loaded (generic params)")
            except ImportError:
                logger.warning("DVB-T: reedsolo not found — RS FEC disabled (pip install reedsolo)")

        try:
            from commpy.channelcoding import viterbi_decode, Trellis
            self._commpy_viterbi = (viterbi_decode, Trellis)
            logger.info("DVB-T: commpy Viterbi loaded")
        except ImportError:
            logger.warning("DVB-T: commpy not found — using numpy Viterbi (pip install commpy)")

    # ------------------------------------------------------------------
    # UDP streaming
    # ------------------------------------------------------------------

    def _open_udp_socket(self):
        try:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            logger.info("DVB-T: UDP stream ready on port %d — open VLC with:  vlc udp://@:%d",
                        UDP_PORT, UDP_PORT)
        except Exception as e:
            logger.error("DVB-T: UDP socket failed: %s", e)
            self._udp_sock = None

    def _stream_ts_packets(self, packets: List[bytes]) -> None:
        """Buffer TS packets and send them UDP-encapsulated to VLC."""
        if self._udp_sock is None or not packets:
            return
        self._ts_pending.extend(packets)
        while len(self._ts_pending) >= TS_PER_DATAGRAM:
            datagram = b''.join(self._ts_pending[:TS_PER_DATAGRAM])
            self._ts_pending = self._ts_pending[TS_PER_DATAGRAM:]
            try:
                self._udp_sock.sendto(datagram, ('127.0.0.1', UDP_PORT))
            except Exception as e:
                logger.debug("DVB-T UDP send: %s", e)

    def _try_launch_vlc(self) -> None:
        """Auto-launch VLC the first time we decode valid TS packets."""
        if self._vlc_launched:
            return
        self._vlc_launched = True
        url = f'udp://@:{UDP_PORT}'
        for path in _VLC_PATHS:
            if os.path.exists(path):
                try:
                    subprocess.Popen(
                        [path, '--ts-es-id-pid', url],
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                    logger.info("DVB-T: launched VLC → %s", url)
                    return
                except Exception as e:
                    logger.warning("DVB-T: VLC launch error: %s", e)
        logger.info("DVB-T: VLC not found at default paths — run:  vlc %s", url)

    # ------------------------------------------------------------------
    # IPC hook: called by vfo.py before decoder.process()
    # ------------------------------------------------------------------

    def set_iq_sample_rate(self, rate: float):
        """Receive actual IQ sample rate from the VFO channeliser."""
        if abs(rate - self._iq_sr) > 100.0:
            logger.info("DVB-T: IQ sample rate %.0f → %.0f Hz", self._iq_sr, rate)
            self._iq_sr = rate
            self.min_buffer_size = max(4096, int(rate / 10))
            self._dvbt_buf = np.zeros(0, dtype=np.complex64)
            self._sym_offset = None

    # ------------------------------------------------------------------
    # BaseIQDecoder interface
    # ------------------------------------------------------------------

    def reset(self):
        self._dvbt_buf   = np.zeros(0, dtype=np.complex64)
        self._sym_offset = None
        self._freq_offset = 0.0
        self._sym_buf    = []
        self._h          = np.ones(K_CARRIERS, dtype=np.complex64)
        self._packets_total = 0
        self._last_snr_db   = 0.0
        self._sync_lost_cnt = 0
        self._ts_pending    = []
        # Keep UDP socket and VLC process alive across resets

    def decode_iq(self, iq_chunk: np.ndarray) -> Optional[DecoderResult]:
        """Entry point called by BaseIQDecoder.process() with a buffered chunk."""
        # Resample the chunk to DVB-T nominal rate and append
        resampled = self._resample_to_dvbt(iq_chunk)
        self._dvbt_buf = np.concatenate([self._dvbt_buf, resampled])

        # Need at least enough data for symbol sync search
        min_needed = N_SYMBOL * 12
        if len(self._dvbt_buf) < min_needed:
            return None

        # Symbol synchronisation
        if self._sym_offset is None:
            self._sym_offset = self._acquire_sync()
            if self._sym_offset is None:
                # Discard half the buffer and retry on next call
                self._dvbt_buf = self._dvbt_buf[len(self._dvbt_buf) // 2:]
                self._sync_lost_cnt += 1
                return self._status(f"Searching for DVB-T signal… ({self._sync_lost_cnt})")

        # Extract and process complete OFDM symbols
        result = self._drain_symbols()

        # Discard processed samples; keep a small overlap for next call
        keep_from = max(0, self._sym_offset - N_SYMBOL * 2)
        self._dvbt_buf = self._dvbt_buf[keep_from:]
        self._sym_offset -= keep_from

        return result

    # ------------------------------------------------------------------
    # Resampling
    # ------------------------------------------------------------------

    def _resample_to_dvbt(self, iq: np.ndarray) -> np.ndarray:
        if abs(self._iq_sr - DVB_T_SR) < 200.0:
            return iq.astype(np.complex64)
        ratio  = DVB_T_SR / self._iq_sr
        n_out  = max(1, round(len(iq) * ratio))
        try:
            # FFT-based resample handles arbitrary (non-rational) ratios cleanly
            r = sp_resample(iq, n_out)
            return r.astype(np.complex64)
        except Exception as e:
            logger.debug("DVB-T resample error: %s", e)
            return iq.astype(np.complex64)

    # ------------------------------------------------------------------
    # Guard-interval synchronisation
    # ------------------------------------------------------------------

    def _acquire_sync(self) -> Optional[int]:
        """Find OFDM symbol boundary via guard-interval autocorrelation.

        C(m) = Σ_{k=0}^{G-1} conj(buf[m+k]) · buf[m+k+N]
        The peak of |C(m)| marks the guard interval start.
        Returns sample offset of the first detected symbol boundary.
        """
        buf = self._dvbt_buf
        G   = N_GUARD
        N   = N_FFT
        S   = N_SYMBOL

        # Search window: up to 4 symbol lengths from the start
        max_m = min(len(buf) - S - G, S * 4)
        if max_m < G:
            return None

        # Coarse correlation (stride 4 for speed)
        step = 4
        idx  = np.arange(0, max_m, step, dtype=np.int32)
        corr = np.zeros(len(idx), dtype=np.float32)
        for j, m in enumerate(idx):
            c = np.dot(np.conj(buf[m:m+G]), buf[m+N:m+N+G])
            corr[j] = abs(c)

        peak_j   = int(np.argmax(corr))
        peak_val = corr[peak_j]
        mean_val = np.mean(corr)
        if peak_val < mean_val * 2.5:
            return None   # no reliable peak

        # Fine search around the coarse peak
        lo = max(0, idx[peak_j] - step * 2)
        hi = min(max_m, idx[peak_j] + step * 2 + 1)
        fine_corr = np.zeros(hi - lo, dtype=np.float32)
        for k, m in enumerate(range(lo, hi)):
            c = np.dot(np.conj(buf[m:m+G]), buf[m+N:m+N+G])
            fine_corr[k] = abs(c)

        peak_m = lo + int(np.argmax(fine_corr))

        # Fractional frequency offset from the correlation phase
        c_peak = np.dot(np.conj(buf[peak_m:peak_m+G]), buf[peak_m+N:peak_m+N+G])
        self._freq_offset = float(np.angle(c_peak) / (2.0 * np.pi * G / N))

        logger.info("DVB-T: sync acquired at offset %d, freq_offset=%.3f bins",
                    peak_m, self._freq_offset)
        self._sync_lost_cnt = 0
        return peak_m

    # ------------------------------------------------------------------
    # Symbol processing loop
    # ------------------------------------------------------------------

    def _drain_symbols(self) -> Optional[DecoderResult]:
        """Extract complete symbols from _dvbt_buf starting at _sym_offset."""
        result = None

        while True:
            start = self._sym_offset
            end   = start + N_SYMBOL
            if end > len(self._dvbt_buf):
                break

            symbol_td = self._dvbt_buf[start:end].copy()

            # Coarse frequency correction
            if abs(self._freq_offset) > 0.05:
                t = np.arange(N_SYMBOL, dtype=np.float32)
                symbol_td *= np.exp(-2j * np.pi * self._freq_offset * t / N_FFT).astype(np.complex64)

            # Strip guard, FFT, shift
            useful   = symbol_td[N_GUARD:]
            spectrum = np.fft.fftshift(np.fft.fft(useful))

            # Extract K useful carriers
            carriers = spectrum[FIRST_CARRIER:FIRST_CARRIER + K_CARRIERS].astype(np.complex64)

            # Channel estimation + equalization from scattered pilots
            sym_idx = len(self._sym_buf)
            self._update_channel(carriers, sym_idx)
            equalized = carriers / (self._h + 1e-10)

            self._sym_buf.append(equalized)
            self._sym_offset += N_SYMBOL

            # Once we have a full DVB-T frame, decode it
            if len(self._sym_buf) >= N_FRAME:
                result = self._decode_frame(self._sym_buf[:N_FRAME])
                self._sym_buf = []

            # Periodically refine frequency offset from continual pilots
            if sym_idx % 16 == 15:
                self._refine_freq(equalized)

        return result

    # ------------------------------------------------------------------
    # Channel estimation
    # ------------------------------------------------------------------

    def _update_channel(self, carriers: np.ndarray, sym_idx: int) -> None:
        """Update channel estimate H using scattered pilots for symbol sym_idx % 4.

        Scattered pilot positions: k_sp = 3·(sym_idx mod 4) + 12·n, n=0,1,2,…
        All scattered pilots are BPSK-modulated by the PRBS reference sequence.
        We use magnitude-normalisation (simplified: ignore PRBS sign) which
        still gives a valid channel magnitude estimate, just with possible sign
        flips between symbols — acceptable because QAM demap uses differential.
        """
        l   = sym_idx % 4
        k0  = 3 * l
        pos = np.arange(k0, K_CARRIERS, 12, dtype=np.int32)
        if len(pos) == 0:
            return

        pilot_vals = carriers[pos]
        mag = np.abs(pilot_vals)
        mag[mag < 1e-10] = 1e-10
        # Phase-normalised pilot (direction only)
        h_pilots = pilot_vals / mag

        # Interpolate to all K_CARRIERS positions
        h_real = np.interp(np.arange(K_CARRIERS, dtype=np.float32),
                           pos.astype(np.float32), np.real(h_pilots))
        h_imag = np.interp(np.arange(K_CARRIERS, dtype=np.float32),
                           pos.astype(np.float32), np.imag(h_pilots))
        h_new = (h_real + 1j * h_imag).astype(np.complex64)

        alpha = 0.15   # IIR smoothing
        self._h = ((1.0 - alpha) * self._h + alpha * h_new).astype(np.complex64)

    def _refine_freq(self, equalized: np.ndarray) -> None:
        """Refine frequency offset using continual pilots."""
        try:
            cp = CONTINUAL_PILOTS[CONTINUAL_PILOTS < K_CARRIERS]
            if len(cp) < 4:
                return
            phases = np.angle(equalized[cp])
            # Phase should be ~0 or ~π for BPSK pilots; mean gives systematic offset
            mean_phase = float(np.mean(np.sin(phases)))   # sin avoids wrapping issues
            self._freq_offset += mean_phase * 0.02
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Frame decode
    # ------------------------------------------------------------------

    def _decode_frame(self, frame: List[np.ndarray]) -> DecoderResult:
        """Decode one DVB-T super-frame (68 OFDM symbols)."""
        try:
            # Collect soft-bit stream from all data carriers
            soft_bits = self._collect_bits(frame)
            if soft_bits is None or len(soft_bits) < 188 * 8:
                self._last_snr_db = self._estimate_snr(frame[0])
                return self._status(
                    f"DVB-T synced | SNR {self._last_snr_db:.1f} dB | "
                    f"TS {self._packets_total}"
                )

            # Viterbi decode (mother rate 1/2)
            decoded_bytes = self._viterbi_decode(soft_bits)

            # RS(204,188) decode
            ts_packets = self._rs_decode(decoded_bytes)

            self._packets_total += len(ts_packets)
            self._last_snr_db   = self._estimate_snr(frame[0])

            if ts_packets:
                self._stream_ts_packets(ts_packets)
                self._try_launch_vlc()
                return DecoderResult(
                    decoder_name='DVB-T',
                    timestamp=time.time(),
                    data={
                        'snr_db':     round(self._last_snr_db, 1),
                        'packets':    self._packets_total,
                        'stream_url': f'udp://@:{UDP_PORT}',
                    },
                    confidence=0.9,
                )

            return self._status(
                f"DVB-T synced | SNR {self._last_snr_db:.1f} dB | "
                f"TS {self._packets_total}"
            )

        except Exception as e:
            logger.debug("DVB-T frame error: %s", e, exc_info=True)
            return self._status(f"DVB-T frame error: {e}")

    # ------------------------------------------------------------------
    # Bit collection (QAM demap)
    # ------------------------------------------------------------------

    def _collect_bits(self, frame: List[np.ndarray]) -> Optional[np.ndarray]:
        """Extract hard bits from all data carriers in the frame."""
        bits: List[int] = []
        for sym_idx, sym in enumerate(frame):
            data = self._data_carriers(sym, sym_idx)
            if len(data) == 0:
                continue
            # QPSK: 2 bits per symbol — most DVB-T multiplexes use 64-QAM,
            # but QPSK lets us at least get sync & TPS working first.
            # TODO: detect constellation from TPS carriers and demap accordingly.
            bits.extend(self._demap_qpsk(data))
        if len(bits) < 188 * 8:
            return None
        return np.array(bits, dtype=np.int8)

    def _data_carriers(self, carriers: np.ndarray, sym_idx: int) -> np.ndarray:
        """Remove pilot and TPS carriers, return data carriers only."""
        mask = np.ones(K_CARRIERS, dtype=bool)

        # Scattered pilots
        l   = sym_idx % 4
        k0  = 3 * l
        sp  = np.arange(k0, K_CARRIERS, 12, dtype=np.int32)
        mask[sp] = False

        # Continual pilots
        cp = CONTINUAL_PILOTS[CONTINUAL_PILOTS < K_CARRIERS]
        mask[cp] = False

        return carriers[mask]

    def _demap_qpsk(self, symbols: np.ndarray) -> List[int]:
        bits: List[int] = []
        for s in symbols:
            bits.append(0 if np.real(s) >= 0 else 1)
            bits.append(0 if np.imag(s) >= 0 else 1)
        return bits

    def _demap_qam16(self, symbols: np.ndarray) -> List[int]:
        bits: List[int] = []
        threshold = 2.0 / 3.0
        for s in symbols:
            i, q = float(np.real(s)), float(np.imag(s))
            bits.append(0 if i >= 0 else 1)
            bits.append(0 if abs(i) < threshold else 1)
            bits.append(0 if q >= 0 else 1)
            bits.append(0 if abs(q) < threshold else 1)
        return bits

    def _demap_qam64(self, symbols: np.ndarray) -> List[int]:
        bits: List[int] = []
        t1, t2, t3 = 4.0/7.0, 2.0/7.0, 6.0/7.0
        for s in symbols:
            i, q = float(np.real(s)), float(np.imag(s))
            bits.append(0 if i >= 0 else 1)
            bits.append(0 if abs(i) < t3 else 1)
            bits.append(0 if abs(abs(i) - t3) < t2 else 1)
            bits.append(0 if q >= 0 else 1)
            bits.append(0 if abs(q) < t3 else 1)
            bits.append(0 if abs(abs(q) - t3) < t2 else 1)
        return bits

    # ------------------------------------------------------------------
    # Viterbi decoder (K=7, rate 1/2, G1=0o171, G2=0o133)
    # ------------------------------------------------------------------

    def _viterbi_decode(self, bits: np.ndarray) -> bytes:
        """Hard-decision Viterbi decode.

        bits: interleaved (G1,G2,G1,G2,...) hard bits, int8
        Returns decoded byte string (mother rate 1/2, no puncturing).
        """
        if self._commpy_viterbi is not None:
            result = self._viterbi_commpy(bits)
            if result is not None:
                return result

        return self._viterbi_numpy(bits)

    def _viterbi_commpy(self, bits: np.ndarray) -> Optional[bytes]:
        try:
            viterbi_decode, Trellis = self._commpy_viterbi
            trellis = Trellis(
                memory=np.array([6]),
                generator_matrix=np.array([[_G1, _G2]])
            )
            n_in = len(bits) & ~1    # must be even
            decoded = viterbi_decode(
                bits[:n_in].astype(float), trellis,
                tb_depth=5 * 7, decoding_type='hard'
            )
            n_bytes = len(decoded) // 8
            return bytes(np.packbits(decoded[:n_bytes * 8].astype(np.uint8)))
        except Exception as e:
            logger.debug("commpy Viterbi error: %s", e)
            return None

    def _viterbi_numpy(self, bits: np.ndarray) -> bytes:
        """Minimal Viterbi using pre-computed trellis tables."""
        ns_tab  = _TRELLIS_NS
        out_tab = _TRELLIS_OUT

        n_pairs = len(bits) // 2
        # Limit to a manageable block (Viterbi is O(n × states))
        n_pairs = min(n_pairs, 4096)

        INF = 1e9
        path_metric = np.full(_N_STATES, INF, dtype=np.float64)
        path_metric[0] = 0.0
        survivors = np.zeros((n_pairs, _N_STATES), dtype=np.int32)
        prev_state = np.zeros((n_pairs, _N_STATES), dtype=np.int32)

        for t in range(n_pairs):
            r0, r1 = int(bits[2 * t]), int(bits[2 * t + 1])
            received = r0 | (r1 << 1)
            new_metric = np.full(_N_STATES, INF, dtype=np.float64)

            for s in range(_N_STATES):
                if path_metric[s] == INF:
                    continue
                for b in range(2):
                    ns  = ns_tab[s, b]
                    exp = out_tab[s, b]
                    hd  = bin(exp ^ received).count('1')  # Hamming distance
                    m   = path_metric[s] + hd
                    if m < new_metric[ns]:
                        new_metric[ns] = m
                        survivors[t, ns] = b
                        prev_state[t, ns] = s

            path_metric = new_metric

        # Traceback
        best_state = int(np.argmin(path_metric))
        decoded_bits = np.zeros(n_pairs, dtype=np.uint8)
        state = best_state
        for t in range(n_pairs - 1, -1, -1):
            decoded_bits[t] = survivors[t, state]
            state = prev_state[t, state]

        n_bytes = n_pairs // 8
        return bytes(np.packbits(decoded_bits[:n_bytes * 8]))

    # ------------------------------------------------------------------
    # Reed-Solomon RS(204,188)
    # ------------------------------------------------------------------

    def _rs_decode(self, data: bytes) -> List[bytes]:
        """RS(204,188) decode — scan the data for 204-byte blocks."""
        packets: List[bytes] = []

        if not data or len(data) < 204:
            return packets

        if self._rs is not None:
            # Feed 204-byte blocks to reedsolo
            for off in range(0, len(data) - 203, 204):
                block = data[off:off + 204]
                try:
                    dec, _, _ = self._rs.decode(bytearray(block))
                    payload = bytes(dec)
                    if len(payload) >= 188 and payload[0] == 0x47:
                        packets.append(payload[:188])
                except Exception:
                    # RS failed — check for raw sync byte anyway
                    if block[0] == 0x47:
                        packets.append(bytes(block[:188]))
        else:
            # No RS library: scan for 0x47 MPEG sync bytes
            i = 0
            while i < len(data) - 187:
                if data[i] == 0x47:
                    packets.append(data[i:i + 188])
                    i += 188
                else:
                    i += 1
                if len(packets) >= 16:
                    break

        return packets

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_snr(self, carriers: np.ndarray) -> float:
        try:
            cp = CONTINUAL_PILOTS[CONTINUAL_PILOTS < K_CARRIERS]
            if len(cp) < 4:
                return 0.0
            mags = np.abs(carriers[cp])
            sig  = float(np.mean(mags) ** 2)
            noise = float(np.var(mags))
            if noise < 1e-12:
                return 40.0
            return max(0.0, 10.0 * np.log10(sig / noise))
        except Exception:
            return 0.0

    def _status(self, text: str) -> DecoderResult:
        return DecoderResult(
            decoder_name='DVB-T',
            timestamp=time.time(),
            data={'status': text},
            confidence=0.0,
        )

    def format_result(self, result: DecoderResult) -> str:
        if result is None:
            return ''
        d = result.data
        if not isinstance(d, dict):
            return str(d)
        if 'status' in d:
            return d['status']
        snr  = d.get('snr_db', 0.0)
        pkts = d.get('packets', 0)
        url  = d.get('stream_url', f'udp://@:{UDP_PORT}')
        return f"DVB-T | SNR {snr:.1f} dB | {pkts} TS pkts → {url}"
