"""
Flask application initialization and configuration
"""
import logging
import os
from flask import Flask, render_template
from flask_cors import CORS
from flask_socketio import SocketIO
from config.settings import SECRET_KEY, DEBUG, SOCKETIO_MESSAGE_QUEUE
from app.sdr.hackrf_receiver import HackRFReceiver, HackRFConfig
from app.sdr.vfo import VFOManager
from app.decoders.base import get_decoder_registry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_app(config=None):
    """Application factory"""
    
    app = Flask(__name__, 
                static_folder=os.path.join(os.path.dirname(__file__), '..', 'static'),
                static_url_path='/static',
                template_folder=os.path.join(os.path.dirname(__file__), '..', 'templates'))
    
    # Configuration
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['DEBUG'] = DEBUG
    
    # CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", message_queue=SOCKETIO_MESSAGE_QUEUE)
    
    # Initialize SDR components
    hackrf_config = HackRFConfig()
    receiver = HackRFReceiver(hackrf_config)
    vfo_manager = VFOManager(
        center_freq=hackrf_config.center_freq,
        sample_rate=hackrf_config.sample_rate,
        max_vfos=10
    )
    
    # Store components in app context
    app.receiver = receiver
    app.vfo_manager = vfo_manager
    app.socketio = socketio
    
    # Initialize decoder registry
    decoder_registry = get_decoder_registry()
    
    # Register default decoders
    try:
        from app.decoders import POCSAGDecoder, RDSDecoder, AISDecoder, ADSBDecoder
        
        decoders = [
            POCSAGDecoder(),
            RDSDecoder(),
            AISDecoder(),
            ADSBDecoder(),
        ]
        
        for decoder in decoders:
            decoder_registry.register(decoder)
        
        logger.info(f"Registered {len(decoders)} decoders")
    except Exception as e:
        logger.error(f"Error registering decoders: {e}")
    
    app.decoder_registry = decoder_registry
    
    # Register blueprints
    from app.api import api_bp, websocket_handlers
    app.register_blueprint(api_bp)
    websocket_handlers.register_handlers(socketio, app)
    
    # Register home route
    @app.route('/')
    def index():
        """Serve the main application page"""
        return render_template('index.html')
    
    logger.info("Flask application created")
    return app
