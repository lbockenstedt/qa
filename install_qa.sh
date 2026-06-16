#!/bin/bash
set -e

echo "Installing Lab Manager QA Auditor in LXC..."

# Update and install base dependencies
apt-get update
apt-get install -y python3 python3-pip python3-venv git curl

# Create a virtual environment for QA
mkdir -p /opt/lm-qa
cd /opt/lm-qa
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
# Added fastapi and uvicorn for the new WebUI
pip install httpx websockets playwright pycryptodomex fastapi uvicorn

# Install Playwright browsers
playwright install chromium

# Copy source code from the current directory to /opt/lm-qa
# Assuming the script is run from the root of the QA project
cp -r ./* /opt/lm-qa/

echo "--------------------------------------------------"
echo "QA Auditor installed successfully at /opt/lm-qa"
echo "To run tests, use:"
echo "  source /opt/lm-qa/venv/bin/activate"
echo "  python /opt/lm-qa/main.py --hub <hub_ip> --spoke-id <id> --secret <secret>"
echo "WebUI will be available on port 8080"
echo "--------------------------------------------------"
