#!/bin/bash
# Lab Manager QA Auditor - Single Line Deployment Script
# This script creates a Proxmox LXC container and installs the QA Auditor.

set -e

# Default values
VMID=${1:-1000}
HUB_IP=${2:-"localhost"}
GHOST_ID=${3:-"qa-auditor"}
SECRET=${4:-"default-secret"}
TEMPLATE="local:vztmpl/debian-12-standard_12.0-1_amd64.tar.zst"

echo "🚀 Deploying Lab Manager QA Auditor to VMID: $VMID..."

# 1. Create LXC Container
if ! pct list | grep -q "^$VMID"; then
    echo "Creating container $VMID..."
    pct create $VMID --template $TEMPLATE --storage local-lvm --net0 name=eth0,bridge=vmbr0,ip=dhcp \
        --memory 2048 --cores 2 --ostype ubuntu --password "password"
else
    echo "Container $VMID already exists. Skipping creation."
fi

# 2. Start Container
pct start $VMID
sleep 5 # Wait for boot

# 3. Install Dependencies and Setup QA Auditor
echo "Installing software inside container..."
pct exec $VMID -- bash -c "
    apt-get update && apt-get install -y python3 python3-pip python3-venv git curl
    mkdir -p /opt/lm-qa
    cd /opt/lm-qa
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install httpx websockets playwright pycryptodomex
"

# 4. Deploy Source Code
# We clone the repo and copy the qa folder specifically
REPO_URL="https://github.com/lbockenstedt/lm.git"
pct exec $VMID -- bash -c "
    git clone $REPO_URL /tmp/lm-repo
    cp -r /tmp/lm-repo/qa/* /opt/lm-qa/
    rm -rf /tmp/lm-repo
"

# 5. Initialize Playwright
pct exec $VMID -- bash -c "
    /opt/lm-qa/venv/bin/python3 -m playwright install chromium
"

# 6. Final configuration
echo "--------------------------------------------------"
echo "✅ QA Auditor Deployed successfully!"
echo "Container ID: $VMID"
echo "Installation Path: /opt/lm-qa"
echo ""
echo "To run the tests now:"
echo "pct exec $VMID -- /opt/lm-qa/venv/bin/python3 /opt/lm-qa/main.py --hub $HUB_IP --spoke-id $GHOST_ID --secret $SECRET"
echo "--------------------------------------------------"
