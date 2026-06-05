"""
Modular decoder system for ASDR
"""

from .base import (
    BaseDecoder,
    BaseAudioDecoder,
    BaseIQDecoder,
    DecoderRegistry,
    DecoderResult,
    get_decoder_registry,
)
from .modulation import (
    GenericModulationDecoder,
    MODULATION_DECODER_NAMES,
    create_modulation_decoder,
)
from .pocsag import POCSAGDecoder, POCSAGIQDecoder
from .rds import RDSDecoder
from .ais import AISDecoder
from .adsb import ADSBDecoder
from .tetra import TETRADecoder  # now a package: app/decoders/tetra/
from .acars import ACARSDecoder
from .dmr import DMRDecoder
from .manchester import ManchesterDecoder
from .wefax import WEFAXDecoder
from .fsk import FSKDecoder, PRESETS as FSK_PRESETS, FSK_PRESET_NAMES

__all__ = [
    'BaseDecoder',
    'BaseAudioDecoder',
    'BaseIQDecoder',
    'DecoderRegistry',
    'DecoderResult',
    'get_decoder_registry',
    'GenericModulationDecoder',
    'MODULATION_DECODER_NAMES',
    'create_modulation_decoder',
    'POCSAGDecoder',
    'POCSAGIQDecoder',
    'ADSBDecoder',
    'TETRADecoder',
    'ACARSDecoder',
    'DMRDecoder',
    'ManchesterDecoder',
    'WEFAXDecoder',
    'FSKDecoder',
    'FSK_PRESETS',
    'FSK_PRESET_NAMES',
]
