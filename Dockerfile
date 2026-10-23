FROM python:3.12-slim

ARG TARGETARCH

# Install runtime dependencies & ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install MediaMTX standalone RTSP binary
RUN ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then MTX_ARCH="arm64"; else MTX_ARCH="amd64"; fi && \
    curl -sLf "https://github.com/bluenviron/mediamtx/releases/download/v1.21.2/mediamtx_v1.21.2_linux_${MTX_ARCH}.tar.gz" -o /tmp/mediamtx.tar.gz && \
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx && \
    chmod +x /usr/local/bin/mediamtx && \
    rm -f /tmp/mediamtx.tar.gz

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy config and application source
COPY mediamtx.yml .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY src/ ./src/

# Expose Web UI (8080), RTSP (8554), HLS (8888), WebRTC (8889)
EXPOSE 8080 8554 8888 8889

ENTRYPOINT ["/entrypoint.sh"]
