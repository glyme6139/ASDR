#!/usr/bin/env python3
"""
ASDR Desktop Application Launcher

Simple wrapper to launch the desktop UI with proper paths and environment.
"""

import subprocess
import sys
from pathlib import Path

# Get the directory of this script
app_dir = Path(__file__).parent

# Run the desktop app
result = subprocess.run(
    [sys.executable, str(app_dir / "desktop_app.py")],
    cwd=str(app_dir)
)

sys.exit(result.returncode)
