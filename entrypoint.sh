#!/bin/sh
set -e

# Launch Python RTSP Bridge supervisor in foreground
echo "[entrypoint] Launching Ecovacs Deebot Video Proxy..."
exec python3 -u -m src.main "$@"
