#!/usr/bin/env bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# Binance Futures Bot — systemd service setup / start helper
#
# Usage:
#   sudo bash setup-systemd.sh install        # install + enable + start
#   sudo bash setup-systemd.sh uninstall      # stop + disable + remove
#   sudo bash setup-systemd.sh start          # start (already installed)
#   sudo bash setup-systemd.sh stop           # stop
#   sudo bash setup-systemd.sh restart        # restart
#   sudo bash setup-systemd.sh status         # status
#   sudo bash setup-systemd.sh live           # switch to live mode (+create .env.live if missing)
#   sudo bash setup-systemd.sh demo           # switch to testnet mode
#   sudo bash setup-systemd.sh paper          # switch to paper mode (no keys)
#   sudo bash setup-systemd.sh log            # tail live log
#   sudo bash setup-systemd.sh verify         # validate config / .env syntax
#
# Prerequisites:
#   - Bot installed at this machine
#   - Python venv created (.venv)  -> ./deploy/setup-systemd.sh bootstrap
#   - .env file with BOT_MODE set
#

set -euo pipefail

BOT_DIR="/home/azzatssins/AgentOS_Memory/binance-futures-strict-bot"
SERVICE_NAME="binance-bot.service"
SERVICE_FILE="$BOT_DIR/deploy/binance-bot.service"
VENV_PY="$BOT_DIR/.venv/bin/python"
SYSTEMD_DIR="/etc/systemd/system"

# ------------------------------------------------------------------- helpers
info()  { echo "[+] $*"; }
warn()  { echo "[-] $*" >&2; }
fail()  { echo "✗ $*" >&2; exit 1; }

# --------------------------------------------------------------- install
do_install() {
    info "Installing $SERVICE_NAME..."

    if [ ! -d "$BOT_DIR" ]; then
        fail "Bot directory not found: $BOT_DIR"
    fi
    if [ ! -x "$VENV_PY" ]; then
        fail "Venv python not found: $VENV_PY\n  Run: $BOT_DIR/deploy/bootstrap.sh"
    fi

    # Copy the service file into systemd (replace placeholders)
    if [ ! -f "$SERVICE_FILE" ]; then
        warn "Service template not found; install will fail."
    fi

    # Validate systemd syntax before enabling
    if command -v systemctl >/dev/null 2>&1; then
        cp "$SERVICE_FILE" "$SYSTEMD_DIR/$SERVICE_NAME"
        systemctl daemon-reload
        systemctl enable "$SERVICE_NAME"
        info "Service enabled at $SYSTEMD_DIR/$SERVICE_NAME"
    else
        warn "systemctl not available (not running as root). Copy the service file manually:"
        warn "  sudo cp $SERVICE_FILE $SYSTEMD_DIR/$SERVICE_NAME"
        warn "  sudo systemctl daemon-reload"
        warn "  sudo systemctl enable --now $SERVICE_NAME"
    fi

    do_start
}

# --------------------------------------------------------------- start
do_start() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "$SERVICE_NAME is already running"
    else
        info "Starting $SERVICE_NAME..."
        systemctl start "$SERVICE_NAME"
        sleep 2
        do_status
    fi
}

# --------------------------------------------------------------- stop
do_stop() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Stopping $SERVICE_NAME..."
        systemctl stop "$SERVICE_NAME"
    else
        info "$SERVICE_NAME is not running"
    fi
}

# ------------------------------------------------------------- restart
do_restart() {
    do_stop
    sleep 2
    do_start
}

# --------------------------------------------------------------- status
do_status() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl status "$SERVICE_NAME" --no-pager -l || true
    else
        info "systemctl not available; cannot show status."
    fi
}

# --------------------------------------------------------------- mode switch
do_mode() {
    local mode="$1"
    local env_file=".env.${mode}"
    local env_path="$BOT_DIR/$env_file"

    if [ ! -f "$env_path" ]; then
        warn "Missing $env_path"
        info "Creating empty .env from template..."
        if [ ! -f "$env_path.example" ]; then
            warn "No template found for $mode mode."
            return 1
        fi
        cp "$env_path.example" "$env_path"
        info "Created $env_path (fill in your keys)."
    fi

    # Write BOT_MODE into .env so the service picks it up
    local old_mode
    old_mode=$(grep -E "^BOT_MODE=" "$env_path" 2>/dev/null | head -1 || true)
    if [ -n "$old_mode" ]; then
        info "Current BOT_MODE in .env: $(echo "$old_mode" | cut -d= -f2)"
    fi

    do_stop
    sleep 2
    do_start
    info "Mode switched to ${mode} (BOT_MODE set in $env_path)"
}

# --------------------------------------------------------------- log
do_log() {
    if command -v journalctl >/dev/null 2>&1; then
        journalctl -u "$SERVICE_NAME" -f --no-pager
    else
        tail -f "$BOT_DIR/lifecycle.jsonl" 2>/dev/null || true
    fi
}

# ------------------------------------------------------------- bootstrap
do_bootstrap() {
    info "Bootstrapping venv & dependencies..."
    if [ ! -d "$BOT_DIR/.venv" ]; then
        "$VENV_PY" -m venv "$BOT_DIR/.venv" || fail "venv creation failed"
    fi
    "$BOT_DIR/.venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt" || warn "requirements install failed (non-fatal; run manually to check)"
    info "Venv ready."
}

# --------------------------------------------------------------- verify
do_verify() {
    info "Verifying environment..."
    if [ ! -f "$BOT_DIR/requirements.txt" ]; then
        fail "requirements.txt missing"
    fi
    "$VENV_PY" -c "import config, run_executor, metrics; print('imports OK')" || warn "module import check failed"
    if command -v pytest >/dev/null 2>&1; then
        local rc
        rc=$("$VENV_PY" -m pytest -q --no-header -p no:cacheprovider 2>&1 | tail -1)
        info "Test summary: $rc"
    fi
    info "Verification complete."
}

# ====================================================================== main
case "${1:-}" in
    install)
        do_bootstrap
        do_install
        ;;
    uninstall)
        do_stop
        if command -v systemctl >/dev/null 2>&1; then
            systemctl disable "$SERVICE_NAME" 2>/dev/null || true
            rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
            systemctl daemon-reload
            info "Service removed."
        else
            warn "systemctl not available. Remove the service file manually."
        fi
        ;;
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    live)    do_mode live ;;
    demo)    do_mode demo ;;
    paper)   do_mode paper ;;
    log)     do_log ;;
    verify)  do_verify ;;
    bootstrap) do_bootstrap ;;
    *)
        echo "Binance Futures Bot — systemd setup helper"
        echo ""
        echo "Usage: $0 {install|uninstall|start|stop|restart|status|"
        echo "              live|demo|paper|log|verify|bootstrap}"
        echo ""
        echo "  install     Install service + start (bootstrap venv first)"
        echo "  uninstall   Remove service + stop"
        echo "  start       Start the bot service"
        echo "  stop        Stop the bot service"
        echo "  restart     Restart the bot service"
        echo "  status      Show service status"
        echo "  live        Switch BOT_MODE=live (real money)"
        echo "  demo        Switch BOT_MODE=testnet (paper money)"
        echo "  paper       Switch BOT_MODE=paper (no keys)"
        echo "  log         Tail live service log"
        echo "  verify      Check imports + run tests"
        echo "  bootstrap   Create venv + install requirements"
        echo ""
        exit 1
        ;;
esac
