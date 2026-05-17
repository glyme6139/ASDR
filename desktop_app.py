#!/usr/bin/env python3
"""
ASDR Desktop Application Entry Point

Launches the PySide6-based desktop UI for the Auto SDR application.
"""

import sys
import logging
from pathlib import Path
import logging
logging.getLogger('app.decoders.tetra.lower_mac').setLevel(logging.DEBUG)
# Add parent directory to path for app imports
sys.path.insert(0, str(Path(__file__).parent))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from PySide6.QtWidgets import QApplication
from app.desktop.main_window import ASURMainWindow


def main():
    """Launch the desktop application."""
    logger.info("Starting ASDR Desktop Application...")
    
    app = QApplication(sys.argv)
    app.setApplicationName("Auto SDR (ASDR)")
    app.setApplicationVersion("1.0.0")
    
    # Create and show main window
    window = ASURMainWindow()
    window.show()
    
    logger.info("Application window displayed")
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
