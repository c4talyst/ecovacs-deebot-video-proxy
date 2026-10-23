#!/bin/sh
set -e

# Start MediaMTX in the background if enabled
if [ "$DISABLE_MEDIAMTX" != "1" ]; then
    echo "[entrypoint] Starting embedded MediaMTX RTSP server..."
    # Apply optional RTSP authentication if provided
    if [ -n "$RTSP_USERNAME" ] && [ -n "$RTSP_PASSWORD" ]; then
        echo "[entrypoint] Enabling RTSP authentication for reader: $RTSP_USERNAME"
        export MTX_PATHS_ALL_READUSER="$RTSP_USERNAME"
        export MTX_PATHS_ALL_READPASS="$RTSP_PASSWORD"
    fi
    /usr/local/bin/mediamtx /app/mediamtx.yml > /tmp/mediamtx.log 2>&1 &
    sleep 1
fi

# Launch Python RTSP Bridge supervisor in foreground
echo "[entrypoint] Launching Ecovacs Deebot Video Proxy..."
exec python3 -u -m src.main "$@"
