#!/bin/bash
# Wrapper script for spotify-tracker with network retry logic
# Designed to be run by systemd timer

set -euo pipefail

SCRIPT_DIR="/home/yash/Documents/Coding-Projects/Python/Spotify-Tracker"
VENV_DIR="${SCRIPT_DIR}/venv"
LOG_FILE="${SCRIPT_DIR}/tracker.log"
MAX_RETRIES=3
RETRY_DELAY=30  # seconds

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

check_network() {
    # Check if we can reach Spotify's API endpoint
    curl -s --connect-timeout 5 https://api.spotify.com > /dev/null 2>&1
}

wait_for_network() {
    local attempt=1
    while [ $attempt -le $MAX_RETRIES ]; do
        if check_network; then
            log "Network available"
            return 0
        fi
        log "Network unavailable, attempt $attempt/$MAX_RETRIES. Retrying in ${RETRY_DELAY}s..."
        sleep $RETRY_DELAY
        ((attempt++))
    done
    log "Network unavailable after $MAX_RETRIES attempts, skipping this run"
    return 1
}

main() {
    log "Starting Spotify Tracker"

    cd "$SCRIPT_DIR"

    # Wait for network connectivity
    if ! wait_for_network; then
        exit 0  # Exit gracefully - systemd will retry at next scheduled time
    fi

    # Activate virtual environment
    if [ -f "${VENV_DIR}/bin/activate" ]; then
        source "${VENV_DIR}/bin/activate"
        log "Activated venv at ${VENV_DIR}"
    else
        log "ERROR: Virtual environment not found at ${VENV_DIR}"
        exit 1
    fi

    # Run the tracker
    python spotify-tracker.py
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        log "Spotify Tracker completed successfully"
    else
        log "Spotify Tracker failed with exit code $exit_code"
    fi

    exit $exit_code
}

main
