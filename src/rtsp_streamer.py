"""RTSP Streaming Pipeline using FFmpeg and MediaMTX."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from typing import Any

from av import VideoFrame

_LOGGER = logging.getLogger("ecovacs.rtsp")


class RtspPipeliner:
    """Consumes raw uncompressed video frames and pushes H.264 over RTSP to MediaMTX."""

    def __init__(
        self,
        rtsp_url: str = "rtsp://127.0.0.1:8554/live",
        width: int = 1280,
        height: int = 720,
        fps: int = 16,
        bitrate: str = "1500k",
        preset: str = "ultrafast",
        tune: str = "zerolatency",
    ) -> None:
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.preset = preset
        self.tune = tune

        self.proc: subprocess.Popen[bytes] | None = None
        self._running = False
        self._frame_count = 0
        self._queue: queue.Queue[VideoFrame | None] = queue.Queue(maxsize=3)
        self._worker_thread: threading.Thread | None = None
        self._log_file: Any = None

    def start(self) -> None:
        """Launch FFmpeg subprocess in TCP RTSP publishing mode."""
        if self.proc and self.proc.poll() is None:
            return

        self.stop()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", self.preset,
            "-tune", self.tune,
            "-b:v", self.bitrate,
            "-maxrate", self.bitrate,
            "-bufsize", "3000k",
            "-g", str(self.fps * 2),  # 2 second GOP for instant keyframe in Frigate/VLC
            "-pix_fmt", "yuv420p",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            self.rtsp_url,
        ]

        _LOGGER.info("Starting FFmpeg RTSP publisher -> %s", self.rtsp_url)
        self._log_file = open("/tmp/ffmpeg.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
        )
        self._running = True
        self._frame_count = 0

        self._worker_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._worker_thread.start()

    def _writer_loop(self) -> None:
        """Background thread feeding uncompressed frames to FFmpeg stdin."""
        while self._running:
            try:
                frame = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if frame is None or not self._running:
                break

            if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
                continue

            try:
                yuv_frame = frame.reformat(width=self.width, height=self.height, format="yuv420p")
                raw_bytes = bytes(yuv_frame.planes[0]) + bytes(yuv_frame.planes[1]) + bytes(yuv_frame.planes[2])
                self.proc.stdin.write(raw_bytes)
                self.proc.stdin.flush()
                self._frame_count += 1
            except (BrokenPipeError, OSError):
                _LOGGER.warning("FFmpeg pipe closed. Stopping current publisher.")
                break

    def push_frame(self, frame: VideoFrame) -> None:
        """Queue a VideoFrame without blocking the asyncio event loop."""
        if not self._running or not self.proc or self.proc.poll() is not None:
            self.start()

        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # Drop oldest frame to maintain zero-latency
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def stop(self) -> None:
        """Terminate FFmpeg subprocess and worker thread cleanly."""
        self._running = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
        self._worker_thread = None

        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

        _LOGGER.debug("FFmpeg RTSP publisher stopped.")
