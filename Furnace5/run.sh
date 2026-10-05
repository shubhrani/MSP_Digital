#!/usr/bin/env bash
# Auto-restart runner script for Furnace 5
# Usage: ./run.sh or bash run.sh

echo "=========================================================="
echo "Starting Furnace 5 Monitor with Auto-Restart Loop"
echo "=========================================================="

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Launching: python3 app.py --no-supervisor"
    python3 app.py --no-supervisor
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Clean shutdown (code 0). Exiting."
        exit 0
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Process exited with code $EXIT_CODE (crash/segfault)."
    echo "Auto-restarting in 2 seconds..."
    sleep 2
done
