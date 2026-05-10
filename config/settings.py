"""
Configuration settings for ASDR application
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Flask configuration
DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
PROPAGATE_EXCEPTIONS = True

# HackRF configuration
HACKRF_SAMPLE_RATE = 20_000_000  # 20 MHz
HACKRF_CENTER_FREQ = 100_000_000  # 100 MHz default
HACKRF_LNA_GAIN = 40
HACKRF_VGA_GAIN = 40
HACKRF_AMP_ENABLED = False

# VFO configuration
MAX_VFOS = 10
DEFAULT_BANDWIDTH = 200_000  # 200 kHz
DEFAULT_DEMOD = 'NFM'  # NFM, WFM, AM, USB, LSB, DSB, CW, IQ

# DSP configuration
FFT_SIZE = 4096
WATERFALL_HISTORY = 100
SPECTRUM_UPDATE_RATE = 30  # Hz
AUDIO_SAMPLE_RATE = 48_000

# Demodulation modes
DEMOD_MODES = {
    'NFM': {'bandwidth': 12_500, 'name': 'Narrow FM'},
    'WFM': {'bandwidth': 200_000, 'name': 'Wide FM'},
    'AM': {'bandwidth': 10_000, 'name': 'AM'},
    'USB': {'bandwidth': 2_700, 'name': 'USB'},
    'LSB': {'bandwidth': 2_700, 'name': 'LSB'},
    'DSB': {'bandwidth': 6_000, 'name': 'DSB'},
    'CW': {'bandwidth': 500, 'name': 'CW'},
    'IQ': {'bandwidth': 1_000_000, 'name': 'Raw IQ'},
}

# Decoder configuration
ENABLED_DECODERS = ['POCSAG', 'RDS', 'NOAA_APT', 'AIS', 'ADSB']

# Bookmarks storage
BOOKMARKS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'bookmarks.json')
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'settings.json')

# Server configuration
SOCKETIO_MESSAGE_QUEUE = 'memory://'
MAX_CONNECTIONS = 50
