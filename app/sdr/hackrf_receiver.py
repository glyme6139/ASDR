"""
HackRF SDR receiver interface
"""
import threading
import numpy as np
import queue
import logging
from typing import Optional, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HackRFConfig:
    """HackRF configuration"""
    center_freq: float = 100_000_000  # Hz
    sample_rate: float = 20_000_000  # Hz
    rx_vga_gain: float = 40  # 0-62 dB
    lna_gain: float = 24  # 0, 8, 16, 24, 32, 40 dB
    if_gain: float = 20  # 0-62 dB in 8 dB steps
    tx_vga_gain: float = 0
    amp_enabled: bool = False  # RF amplifier


class HackRFReceiver:
    """HackRF receiver interface"""
    
    def __init__(self, config: Optional[HackRFConfig] = None):
        self.config = config or HackRFConfig()
        self.is_running = False
        self.device = None
        self.receive_thread = None
        
        # IQ data queue
        self.iq_queue = queue.Queue(maxsize=10)
        self.iq_buffer = np.zeros(0, dtype=np.complex64)
        
        # Callbacks
        self.on_iq_data: Optional[Callable] = None
        self.on_error: Optional[Callable] = None
        self.on_connected: Optional[Callable] = None
        
        # Statistics
        self.samples_received = 0
        self.errors = 0
        
        logger.info("HackRFReceiver initialized")
    
    def connect(self) -> bool:
        """Connect to HackRF device"""
        try:
            # Import python_hackrf
            try:
                from python_hackrf import pyhackrf
            except ImportError as e:
                logger.error(f"Import error: {e}")
                logger.error("HackRF library not installed. Install with: pip install python-hackrf")
                self._simulate_device()
                if self.on_connected:
                    self.on_connected()
                return True
            
            try:
                # Initialize the HackRF library
                logger.info("Initializing HackRF library...")
                pyhackrf.pyhackrf_init()
                logger.info("HackRF library initialized successfully")
                
                # Try to open device
                logger.info("Opening HackRF device...")
                self.device = pyhackrf.pyhackrf_open()
                logger.info(f"Device opened successfully: {self.device}")
                
                # Configure device
                logger.info("Configuring HackRF device...")

                # # Set baseband filter bandwidth
                # try:
                #     baseband_bw = pyhackrf.pyhackrf_compute_baseband_filter_bw_round_down_lt(self.config.sample_rate)
                #     logger.info(f"Setting baseband filter bandwidth to {baseband_bw} Hz...")
                #     self.device.pyhackrf_set_baseband_filter_bandwidth(int(baseband_bw))
                #     logger.info("Baseband filter bandwidth set successfully")
                # except Exception as e:
                #     logger.warning(f"Could not set baseband filter bandwidth: {e}")
                
                logger.info(f"Setting sample rate to {self.config.sample_rate} Hz...")
                self.device.pyhackrf_set_sample_rate(int(self.config.sample_rate))
                logger.info("Sample rate set successfully")
                
                logger.info(f"Setting center frequency to {self.config.center_freq} Hz...")
                self.device.pyhackrf_set_freq(int(self.config.center_freq))
                logger.info("Center frequency set successfully")
                

                
                logger.info(f"Setting LNA gain to {self.config.lna_gain} dB...")
                self.device.pyhackrf_set_lna_gain(int(self.config.lna_gain))
                logger.info("LNA gain set successfully")
                
                logger.info(f"Setting VGA gain to {self.config.rx_vga_gain} dB...")
                self.device.pyhackrf_set_vga_gain(int(self.config.rx_vga_gain))
                logger.info("VGA gain set successfully")
                
                # Disable antenna and amp by default
                self.device.pyhackrf_set_antenna_enable(False)
                self.device.pyhackrf_set_amp_enable(self.config.amp_enabled)
                logger.info("Antenna and amp disabled")
                
                logger.info(f"HackRF connected: {self.config.sample_rate/1e6:.1f} MHz SR, "
                           f"Center: {self.config.center_freq/1e6:.2f} MHz")
                
                if self.on_connected:
                    self.on_connected()
                return True
                
            except Exception as e:
                logger.warning(f"HackRF device not found, using simulator: {e}", exc_info=True)
                self._simulate_device()
                if self.on_connected:
                    self.on_connected()
                return True
        
        except Exception as e:
            logger.error(f"Connection error: {e}", exc_info=True)
            if self.on_error:
                self.on_error(str(e))
            return False
    
    def _simulate_device(self):
        """Create a simulated device for testing"""
        logger.info("Using simulated HackRF device")
        self.device = None  # Marker that we're in simulation mode
    
    def start_receiver(self):
        """Start receiving IQ data"""
        if self.is_running:
            return
        
        self.is_running = True
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()
        logger.info("Receiver started")
    
    def stop_receiver(self):
        """Stop receiving and close the device."""
        self.is_running = False
        try:
            if self.device and hasattr(self.device, 'pyhackrf_stop_rx'):
                self.device.pyhackrf_stop_rx()
        except Exception as e:
            logger.warning(f"Error stopping RX: {e}")
        try:
            if self.device and hasattr(self.device, 'pyhackrf_close'):
                self.device.pyhackrf_close()
                logger.info("HackRF device closed")
        except Exception as e:
            logger.warning(f"Error closing device: {e}")
        if self.receive_thread:
            self.receive_thread.join(timeout=2)
        logger.info("Receiver stopped")

    def pause_streaming(self):
        """Stop the receive loop without closing the device (for mid-stream changes)."""
        self.is_running = False
        if self.device:
            try:
                self.device.pyhackrf_stop_rx()
            except Exception as e:
                logger.warning(f"pyhackrf_stop_rx error: {e}")
        if self.receive_thread:
            self.receive_thread.join(timeout=2)
        logger.info("Streaming paused")

    def resume_streaming(self):
        """Restart the receive loop. Device must already be open."""
        if self.is_running:
            return
        self.is_running = True
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()
        logger.info("Streaming resumed")
    
    def _receive_loop(self):
        """Main receive loop"""
        try:
            if self.device is None:
                # Simulation mode
                self._simulate_receive_loop()
            else:
                # Real HackRF mode
                self._real_receive_loop()
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            if self.on_error:
                self.on_error(str(e))
    
    def _simulate_receive_loop(self):
        """Simulate IQ data reception for testing"""
        import time
        
        chunk_size = 16384
        freq_offset = 0
        phase = 0
        
        while self.is_running:
            try:
                # Generate simulated IQ data with multiple sine waves
                t = np.arange(chunk_size) / self.config.sample_rate
                
                # Signal 1: 1 MHz offset, amplitude 0.5
                signal1 = 0.5 * np.exp(2j * np.pi * 1_000_000 * t)
                
                # Signal 2: -500 kHz offset, amplitude 0.3
                signal2 = 0.3 * np.exp(2j * np.pi * (-500_000) * t)
                
                # Noise
                noise = 0.1 * (np.random.randn(chunk_size) + 1j * np.random.randn(chunk_size))
                
                iq_data = (signal1 + signal2 + noise).astype(np.complex64)
                
                self.iq_buffer = np.concatenate([self.iq_buffer, iq_data])
                self.samples_received += len(iq_data)
                
                if self.on_iq_data and len(self.iq_buffer) >= chunk_size:
                    chunk = self.iq_buffer[:chunk_size]
                    self.iq_buffer = self.iq_buffer[chunk_size:]
                    self.on_iq_data(chunk)
                
                time.sleep(0.01)  # Simulate processing delay
            
            except Exception as e:
                logger.error(f"Simulation error: {e}")
                self.errors += 1
                self.stop_receiver()
    
    def _real_receive_loop(self):
        """Real HackRF receive loop"""
        try:
            logger.info("Starting HackRF RX...")
            self.device.set_rx_callback(self._rx_callback)
            logger.info("RX callback set")
            self.device.pyhackrf_start_rx()
            logger.info("HackRF RX started")
            
            # Keep receiving until stopped
            while self.is_running:
                import time
                time.sleep(0.1)
        
        except Exception as e:
            logger.error(f"HackRF receive error: {e}", exc_info=True)
            if self.on_error:
                self.on_error(str(e))
    
    def _rx_callback(self, device, buffer, buffer_length, valid_length):
        """Callback for received IQ data
        
        Args:
            device: HackRF device handle
            buffer: Buffer containing the IQ data
            buffer_length: Total buffer length
            valid_length: Length of valid data in buffer
        """
        if not self.is_running:
            return 0
        
        try:
            # Extract valid data from buffer
            # valid_length is in bytes, each IQ sample is 2 bytes (I and Q as int8)
            num_samples = valid_length // 2
            
            # Convert to numpy array and extract I/Q pairs
            iq_data = np.frombuffer(buffer[:valid_length], dtype=np.int8).copy()
            # De-interleave I and Q components
            iq_data = iq_data[0::2] + 1j * iq_data[1::2]
            # Normalize from [-128, 127] to [-1, 1]
            iq_data = iq_data.astype(np.complex64) / 128.0
            
            self.samples_received += num_samples
            
            if self.on_iq_data:
                self.on_iq_data(iq_data)
            
            return 0  # Return 0 for success
        
        except Exception as e:
            logger.error(f"Callback error: {e}", exc_info=True)
            self.errors += 1
            return 0  # Return 0 even on error to continue receiving
    
    def set_center_frequency(self, freq: float):
        """Set center frequency"""
        self.config.center_freq = freq
        try:
            if self.device:
                self.device.pyhackrf_set_freq(int(freq))
            logger.info(f"Center frequency set to {freq/1e6:.2f} MHz")
        except Exception as e:
            logger.error(f"Frequency error: {e}")
    
    def set_sample_rate(self, rate: float):
        """Set sample rate"""
        self.config.sample_rate = rate
        try:
            if self.device:
                logger.info(f"Setting sample rate to {rate} Hz...")
                self.device.pyhackrf_set_sample_rate(int(rate))
            logger.info(f"Sample rate set to {rate/1e6:.1f} MHz")
        except Exception as e:
            logger.error(f"Sample rate error: {e}")
        logger.info(f"Sample rate set to {rate/1e6:.1f} MHz")
    
    def set_lna_gain(self, gain: float):
        """Set LNA gain (0, 8, 16, 24, 32, 40 dB)"""
        self.config.lna_gain = gain
        try:
            if self.device:
                self.device.pyhackrf_set_lna_gain(int(gain))
            logger.info(f"LNA gain set to {gain} dB")
        except Exception as e:
            logger.error(f"LNA gain error: {e}")
    
    def set_vga_gain(self, gain: float):
        """Set VGA gain (0-62 dB)"""
        self.config.rx_vga_gain = gain
        try:
            if self.device:
                self.device.pyhackrf_set_vga_gain(int(gain))
            logger.info(f"VGA gain set to {gain} dB")
        except Exception as e:
            logger.error(f"VGA gain error: {e}")
    
    def set_amp_enable(self, enabled: bool):
        """Enable or disable the built-in RF amplifier (~11 dB)"""
        self.config.amp_enabled = enabled
        try:
            if self.device:
                self.device.pyhackrf_set_amp_enable(enabled)
            logger.info(f"Amp {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"Amp enable error: {e}")

    def set_if_gain(self, gain: float):
        """Set IF gain (0-62 dB, 8 dB steps)"""
        self.config.if_gain = gain
        try:
            if self.device:
                # Some HackRF implementations use this
                pass
            logger.info(f"IF gain set to {gain} dB")
        except Exception as e:
            logger.error(f"IF gain error: {e}")
    
    def get_device_info(self) -> dict:
        """Get device information"""
        try:
            if self.device:
                return {
                    'name': 'HackRF One',
                    'serial': getattr(self.device, 'serialno', 'UNKNOWN'),
                    'connected': True,
                }
            else:
                return {
                    'name': 'HackRF (Simulated)',
                    'serial': 'SIM-001',
                    'connected': True,
                }
        except:
            return {'connected': False}
    
    def get_stats(self) -> dict:
        """Get receiver statistics"""
        return {
            'samples_received': self.samples_received,
            'errors': self.errors,
            'center_freq': self.config.center_freq,
            'sample_rate': self.config.sample_rate,
            'is_running': self.is_running,
        }
    
    def disconnect(self):
        """Disconnect from device"""
        self.stop_receiver()
        if self.device:
            try:
                self.device.close()
            except:
                pass
        self.device = None
        logger.info("HackRF disconnected")
