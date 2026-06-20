#!/bin/bash
set -euo pipefail

# ============================================================
# Lab Manager — QA Auditor Spoke Installer
#
# Deploys the QA test-runner as a managed LM spoke.
# Safe to re-run (updates code, preserves credentials).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/qa/main/install_qa.sh \
#     | sudo bash -s -- --hub ws://HUB_IP:8765 --admin-token LM_ADMIN_TOKEN
# ============================================================

HUB_URL="ws://localhost:8765"
SPOKE_ID="qa-spoke-1"
SPOKE_SECRET=""
HUB_SECRET=""
ADMIN_TOKEN=""
LM_USER="admin"
LM_PASSWORD=""
BUGFIXER_URL=""
QA_API_PORT="8090"
SVC_USER="svc_lm"
LM_DIR="/opt/lm"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub)           HUB_URL="$2";       shift ;;
        --id|--name)     SPOKE_ID="$2";      shift ;;
        --secret)        SPOKE_SECRET="$2";  shift ;;
        --hub-secret)    HUB_SECRET="$2";    shift ;;
        --admin-token)   ADMIN_TOKEN="$2";   shift ;;
        --user)          LM_USER="$2";       shift ;;
        --password)      LM_PASSWORD="$2";   shift ;;
        --bugfixer)      BUGFIXER_URL="$2";  shift ;;
        --api-port)      QA_API_PORT="$2";   shift ;;
        --all-prereqs)   ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

ADMIN_TOKEN="${ADMIN_TOKEN:-${LM_ADMIN_TOKEN:-}}"
[ "$(id -u)" -eq 0 ] || { echo "❌ Must be run as root."; exit 1; }

GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✅  $*${NC}"; }
warn() { echo -e "${YLW}⚠️   $*${NC}"; }
step() { echo -e "\n${GRN}━━  $*  ━━${NC}"; }

step "Lab Manager — QA Auditor Spoke Installer"

# ── System packages ───────────────────────────────────────────────────────────
step "Installing system packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3 python3-venv python3-pip git curl jq
ok "Packages ready"

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SVC_USER" &>/dev/null; then
    useradd -r -s /bin/false -M "$SVC_USER"
    ok "Created service user $SVC_USER"
fi

# ── Spoke secret ─────────────────────────────────────────────────────────────
EXISTING_SECRET=""
[ -f "$LM_DIR/qa/.env" ] && \
    EXISTING_SECRET=$(grep "^SPOKE_SECRET=" "$LM_DIR/qa/.env" | cut -d= -f2-)

if [ -z "$SPOKE_SECRET" ]; then
    if [ -n "$EXISTING_SECRET" ]; then
        SPOKE_SECRET="$EXISTING_SECRET"
        ok "Reusing existing spoke secret"
    elif [ -n "$ADMIN_TOKEN" ]; then
        API_HOST=$(echo "$HUB_URL" | sed 's|wss\?://||' | cut -d: -f1)
        SPOKE_SECRET=$(curl -sf -X POST "http://$API_HOST:8000/setup/generate-secret" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $ADMIN_TOKEN" \
            -d "{\"spoke_id\": \"$SPOKE_ID\"}" | jq -r '.secret' 2>/dev/null) || SPOKE_SECRET=""
        [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" = "null" ] && \
            { echo "❌ Could not fetch spoke secret from Hub."; exit 1; }
        ok "Spoke secret fetched from Hub"
    else
        echo "❌ No secret available. Provide --secret or --admin-token."; exit 1
    fi
fi

# ── Clone / update repo ───────────────────────────────────────────────────────
step "Installing QA Auditor"
mkdir -p "$LM_DIR"
if [ -d "$LM_DIR/qa/.git" ]; then
    echo "   Updating existing install"
    git -C "$LM_DIR/qa" pull --rebase --autostash origin main -q
else
    echo "   Cloning QA Auditor repo"
    git clone -q https://github.com/lbockenstedt/qa.git "$LM_DIR/qa"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
rm -rf "$LM_DIR/qa/venv"
python3 -m venv "$LM_DIR/qa/venv"
"$LM_DIR/qa/venv/bin/pip" install --upgrade pip -q
[ -f "$LM_DIR/qa/requirements.txt" ] && \
    "$LM_DIR/qa/venv/bin/pip" install -r "$LM_DIR/qa/requirements.txt" -q

# Playwright for WebUI smoke tests (optional — skip if it fails)
"$LM_DIR/qa/venv/bin/pip" install playwright -q 2>/dev/null && \
    "$LM_DIR/qa/venv/bin/playwright" install chromium 2>/dev/null && \
    ok "Playwright installed" || warn "Playwright install skipped — WebUI smoke tests will not run"

ok "QA Auditor dependencies installed"

# ── .env ──────────────────────────────────────────────────────────────────────
cat > "$LM_DIR/qa/.env" <<DOTENV
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=${HUB_SECRET:-}
LM_USER=${LM_USER:-admin}
LM_PASSWORD=${LM_PASSWORD:-}
BUGFIXER_URL=${BUGFIXER_URL:-}
QA_API_PORT=${QA_API_PORT:-8090}
DOTENV
chmod 600 "$LM_DIR/qa/.env"

# ── systemd unit ──────────────────────────────────────────────────────────────
cat > /etc/systemd/system/lm-qa.service <<SYSD
[Unit]
Description=Lab Manager Spoke - QA Auditor
After=network.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$LM_DIR/qa
EnvironmentFile=$LM_DIR/qa/.env
Environment="PYTHONPATH=$LM_DIR/core/src:$LM_DIR/qa"
ExecStart=$LM_DIR/qa/venv/bin/python3 control_plane.py \
    --id $SPOKE_ID \
    --secret $SPOKE_SECRET \
    --hub-secret "${HUB_SECRET:-}" \
    --hub $HUB_URL \
    --api-port ${QA_API_PORT:-8090}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
systemctl enable lm-qa
systemctl restart lm-qa
ok "QA Auditor service started"

chown -R "$SVC_USER:$SVC_USER" "$LM_DIR/qa" 2>/dev/null || true

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "this-host")
echo ""
echo "══════════════════════════════════════════"
ok "QA Auditor installation complete!"
echo "══════════════════════════════════════════"
echo "  LM Hub:     $HUB_URL"
echo "  Spoke ID:   $SPOKE_ID"
echo "  QA WebUI:   http://$LOCAL_IP:${QA_API_PORT:-8090}/"
echo "  Status:     sudo systemctl status lm-qa"
