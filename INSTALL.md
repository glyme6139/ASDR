# 📦 ASDR Installation Guide

Detailed installation instructions for Windows, macOS, and Linux.

## Table of Contents

- [System Requirements](#system-requirements)
- [Windows Installation](#windows-installation)
- [macOS Installation](#macos-installation)
- [Linux Installation](#linux-installation)
- [Docker Installation](#docker-installation)
- [Troubleshooting](#troubleshooting)

---

## System Requirements

### Minimum
- **CPU**: Dual-core 2.0 GHz
- **RAM**: 2 GB
- **Disk**: 500 MB free
- **Python**: 3.8 or higher
- **Browser**: Chrome, Firefox, Edge, Safari (recent versions)

### Recommended
- **CPU**: Quad-core 2.5 GHz
- **RAM**: 4 GB
- **Disk**: 1 GB free
- **Python**: 3.10 or higher
- **USB**: USB 3.0 for HackRF

### Optional Hardware
- **HackRF One**: Main supported SDR device
- **LimeSDR**: Should work (untested)
- **USRP**: With GNU Radio backend

---

## Windows Installation

### Step 1: Install Python

1. Visit https://www.python.org/downloads/
2. Download **Python 3.10+** Windows installer
3. Run installer and check:
   - ☑️ "Add Python to PATH"
   - ☑️ "Install pip"
4. Click **"Install Now"**
5. Verify installation:
   ```cmd
   python --version
   pip --version
   ```

### Step 2: Install HackRF Drivers (Optional)

For HackRF hardware support:

1. Visit https://github.com/greatscottgadgets/hackrf/releases
2. Download latest `hackrf_2024.X.X_DriverInstaller.exe`
3. Run installer
4. Follow on-screen instructions

### Step 3: Download ASDR

1. Download ASDR as ZIP file
2. Extract to desired location (e.g., `C:\Users\YourName\ASDR`)

### Step 4: Install ASDR

**Option A: Automated (Recommended)**

1. Double-click **`run.bat`** file
2. Wait for setup to complete
3. Browser will open automatically to `http://localhost:5000`

**Option B: Manual**

```cmd
# Navigate to ASDR folder
cd C:\Users\YourName\ASDR

# Create virtual environment
python -m venv venv

# Activate virtual environment
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run application
python run.py
```

### Step 5: Open ASDR

Browser should open automatically. If not, open:
```
http://localhost:5000
```

---

## macOS Installation

### Step 1: Install Python

**Using Homebrew (Recommended)**

```bash
# Install Homebrew if not present
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.10

# Verify
python3 --version
pip3 --version
```

**Using Installer**

1. Visit https://www.python.org/downloads/
2. Download macOS installer
3. Run installer
4. Follow on-screen instructions

### Step 2: Install HackRF Support (Optional)

```bash
# Using Homebrew
brew install hackrf

# Or build from source
git clone https://github.com/greatscottgadgets/hackrf.git
cd hackrf/host
mkdir build
cd build
cmake ..
make
sudo make install
```

### Step 3: Download ASDR

```bash
# Using Git
git clone <repository-url> ASDR
cd ASDR

# Or extract ZIP file
unzip ASDR.zip
cd ASDR
```

### Step 4: Install ASDR

```bash
# Make run script executable
chmod +x run.sh

# Run setup and start
./run.sh
```

Or manually:

```bash
# Create virtual environment
python3 -m venv venv

# Activate
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run
python run.py
```

### Step 5: Open ASDR

In browser:
```
http://localhost:5000
```

---

## Linux Installation

### Ubuntu/Debian

**Step 1: Install Python and Dependencies**

```bash
# Update package list
sudo apt update

# Install Python and build tools
sudo apt install python3.10 python3-pip python3-venv build-essential

# Install HackRF dependencies (optional)
sudo apt install libhackrf-dev libhackrf0
```

**Step 2: Download ASDR**

```bash
# Clone repository
git clone <repository-url> ASDR
cd ASDR

# Or extract ZIP
unzip ASDR.zip
cd ASDR
```

**Step 3: Install ASDR**

```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Make run script executable
chmod +x run.sh

# Run
./run.sh
```

### Fedora/RHEL

```bash
# Install dependencies
sudo dnf install python3-pip python3-devel gcc-c++ libusb-devel

# Download ASDR (as above)
git clone <repository-url> ASDR
cd ASDR

# Setup virtual environment (as above)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python run.py
```

### Arch Linux

```bash
# Install dependencies
sudo pacman -S python pip libusb

# Install HackRF (optional)
sudo pacman -S hackrf

# Download and install ASDR
git clone <repository-url> ASDR
cd ASDR

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python run.py
```

---

## Docker Installation

For isolated, reproducible deployments:

### Dockerfile

Create `Dockerfile` in project root:

```dockerfile
FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libhackrf-dev \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy project
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 5000

# Run
CMD ["python", "run.py", "--host", "0.0.0.0"]
```

### Build and Run

```bash
# Build image
docker build -t asdr:latest .

# Run container
docker run -p 5000:5000 --device /dev/bus/usb asdr:latest

# Or with USB access on Linux
docker run -p 5000:5000 \
    --device /dev/bus/usb \
    -v /dev/bus/usb:/dev/bus/usb \
    asdr:latest
```

---

## Troubleshooting

### Python Not Found

**Windows**
```cmd
# Check PATH
echo %PATH%

# Reinstall Python with "Add to PATH" checked
```

**macOS/Linux**
```bash
# Use explicit python3
python3 --version

# Or add alias to ~/.bashrc or ~/.zshrc
alias python=python3
```

### pip Fails to Install Packages

```bash
# Update pip
python -m pip install --upgrade pip

# Install with verbose output
pip install -vvv -r requirements.txt

# Use pre-built wheels
pip install --prefer-binary -r requirements.txt
```

### HackRF Not Detected

**Windows**
1. Install HackRF driver (see instructions above)
2. Check Device Manager for "HackRF One"
3. Use simulator mode if device unavailable

**Linux**
```bash
# Check USB devices
lsusb

# Grant USB access
sudo usermod -a -G dialout $USER
sudo usermod -a -G plugdev $USER

# Logout and login for changes to take effect
```

**macOS**
```bash
# Check USB devices
system_profiler SPUSBDataType | grep -i hackrf

# Reinstall drivers if needed
brew uninstall hackrf
brew install hackrf
```

### Port 5000 Already in Use

```bash
# Find process using port 5000
# Windows
netstat -ano | findstr :5000

# macOS/Linux
lsof -i :5000

# Run on different port
python run.py --port 8080
```

### Virtual Environment Issues

**Recreate virtual environment**

```bash
# Windows
rmdir /s venv
python -m venv venv
venv\Scripts\activate

# macOS/Linux
rm -rf venv
python3 -m venv venv
source venv/bin/activate
```

### Module Import Errors

```bash
# Ensure virtual environment is activated
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Reinstall requirements
pip install --force-reinstall -r requirements.txt
```

### WebSocket Connection Failed

1. Check firewall settings
2. Ensure no proxy interference
3. Try different port:
   ```bash
   python run.py --port 5001
   ```
4. Check browser console for errors (F12)

---

## Advanced Configuration

### Running on Non-Local IP

```bash
# Make accessible from network
python run.py --host 0.0.0.0

# Then access from another machine
http://your-machine-ip:5000
```

### Using with Reverse Proxy (Nginx)

```nginx
server {
    listen 80;
    server_name example.com;

    location / {
        proxy_pass http://localhost:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
```

### SSL/HTTPS

```bash
# Generate self-signed certificate
openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365

# Run with SSL
python run.py --cert cert.pem --key key.pem
```

---

## Performance Tuning

### For Low-End Systems

Edit `config/settings.py`:

```python
FFT_SIZE = 2048           # Reduce from 4096
WATERFALL_HISTORY = 50    # Reduce from 100
SPECTRUM_UPDATE_RATE = 15 # Reduce from 30
MAX_VFOS = 5             # Reduce from 10
```

### For High-Performance Systems

```python
FFT_SIZE = 8192
WATERFALL_HISTORY = 200
SPECTRUM_UPDATE_RATE = 60
MAX_VFOS = 20
```

---

## Next Steps

1. ✅ Installation complete!
2. 📖 Read **QUICKSTART.md** for first steps
3. 🔧 Check **README.md** for full features
4. 👨‍💻 See **DEVELOPER_GUIDE.md** for customization

---

## Support

If you encounter issues:

1. Check this guide thoroughly
2. Review **README.md** troubleshooting section
3. Check application logs in terminal
4. Try with `--debug` flag:
   ```bash
   python run.py --debug
   ```

---

**Happy SDR receiving! 🛰️**
