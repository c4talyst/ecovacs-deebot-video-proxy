"""Main entrypoint for Ecovacs Deebot Video Proxy.

Always-on supervisor loop:
1. Ensures MediaMTX RTSP server is active.
2. Launches Web UI & Setup Wizard on port 8080.
3. If unconfigured, waits for user to configure via browser.
4. If configured, establishes P2P WebRTC link to robot camera.
5. Pipes H.264 video frames into MediaMTX RTSP stream.
6. Automatically reconnects when robot/cloud drops session.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web
from av import VideoFrame

from .config import AppConfig, load_config
from .ecovacs_auth import EcovacsAuthClient
from .live_view import EcovacsWebRTCSession, async_send_app_ping, async_start_watch
from .rtsp_streamer import RtspPipeliner
from .web_ui import WebUIService

_LOGGER = logging.getLogger("ecovacs.main")


def _frame_to_jpeg(frame: VideoFrame) -> bytes:
    image = frame.to_image()
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class EcovacsRtspBridge:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop_event = asyncio.Event()
        self.restart_event = asyncio.Event()
        self.session: aiohttp.ClientSession | None = None
        self.auth_client: EcovacsAuthClient | None = None
        self.mediamtx_proc: subprocess.Popen[bytes] | None = None
        self.pipeliner: RtspPipeliner | None = None
        self.active_webrtc: EcovacsWebRTCSession | None = None
        self.robot: dict[str, Any] | None = None

        self.latest_jpeg: bytes | None = None
        self.latest_jpeg_seq = 0
        self._last_jpeg_time = 0.0

    def start_embedded_mediamtx(self) -> None:
        """Launch embedded MediaMTX if available and not already listening on RTSP port."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        res = s.connect_ex(("127.0.0.1", self.config.rtsp.port))
        s.close()
        if res == 0:
            _LOGGER.info("RTSP server already detected on port %d.", self.config.rtsp.port)
            return

        mtx_bins = ["mediamtx", "/usr/local/bin/mediamtx", "/tmp/mediamtx"]
        found_bin = next((b for b in mtx_bins if os.path.isfile(b) and os.access(b, os.X_OK)), None)
        if not found_bin:
            import shutil
            found_bin = shutil.which("mediamtx")

        if found_bin:
            cfg_path = "mediamtx.yml" if os.path.isfile("mediamtx.yml") else "/etc/mediamtx.yml"
            cmd = [found_bin]
            if os.path.isfile(cfg_path):
                cmd.append(cfg_path)
            _LOGGER.info("Launching embedded MediaMTX (%s)...", found_bin)
            mtx_env = dict(os.environ)
            if self.config.rtsp.username and self.config.rtsp.password:
                _LOGGER.info("RTSP stream authentication enabled for reader '%s'.", self.config.rtsp.username)
                mtx_env["MTX_PATHS_ALL_READUSER"] = self.config.rtsp.username
                mtx_env["MTX_PATHS_ALL_READPASS"] = self.config.rtsp.password
            else:
                mtx_env.pop("MTX_PATHS_ALL_READUSER", None)
                mtx_env.pop("MTX_PATHS_ALL_READPASS", None)
            mediamtx_log = open("/tmp/mediamtx.log", "a", encoding="utf-8")
            self.mediamtx_proc = subprocess.Popen(cmd, env=mtx_env, stdout=mediamtx_log, stderr=mediamtx_log)
            time.sleep(1)
        else:
            _LOGGER.warning("MediaMTX binary not found locally. Ensure an RTSP server is running on port %d.", self.config.rtsp.port)

    def trigger_restart(self) -> None:
        """Trigger reconnection loop with updated credentials."""
        _LOGGER.info("Configuration reload triggered. Restarting streaming loop...")
        self.restart_event.set()
        if self.active_webrtc:
            asyncio.create_task(self.active_webrtc.stop())

    def on_video_frame(self, frame: VideoFrame) -> None:
        """Feed frame to FFmpeg RTSP publisher and generate occasional JPEG for Web UI."""
        if self.pipeliner:
            self.pipeliner.push_frame(frame)

        now = time.monotonic()
        if now - self._last_jpeg_time >= 0.2:
            self._last_jpeg_time = now
            try:
                self.latest_jpeg = _frame_to_jpeg(frame)
                self.latest_jpeg_seq += 1
            except Exception:
                pass

    async def _keepalive_loop(self, portal_token: str, robot: dict[str, Any]) -> None:
        """Periodically send appping to the vacuum to prevent idle streaming disconnect."""
        interval = self.config.streamer.appping_interval_seconds
        while not self.stop_event.is_set() and not self.restart_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self.active_webrtc and not self.active_webrtc.stop_event.is_set():
                    _LOGGER.debug("Sending stream keepalive appping to robot...")
                    await async_send_app_ping(
                        self.session,
                        portal_token=portal_token,
                        robot=robot,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.warning("Keepalive appping error: %s", e)

    async def run(self) -> None:
        """Supervisor loop managing continuous streaming & automatic reconnection."""
        self.start_embedded_mediamtx()

        web_port = int(os.environ.get("WEB_PORT", 8080))
        web_service = WebUIService(self, port=web_port)
        web_runner = web.AppRunner(web_service.app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, "0.0.0.0", web_port)
        await site.start()

        _LOGGER.info("=" * 60)
        _LOGGER.info("Ecovacs Video Proxy Web UI: http://0.0.0.0:%d/", web_port)
        _LOGGER.info("=" * 60)

        rtsp_target = f"rtsp://127.0.0.1:{self.config.rtsp.port}/{self.config.rtsp.path}"
        self.pipeliner = RtspPipeliner(
            rtsp_url=rtsp_target,
            width=1280,
            height=720,
            fps=self.config.rtsp.fps,
            bitrate=self.config.rtsp.bitrate,
            preset=self.config.rtsp.preset,
            tune=self.config.rtsp.tune,
        )

        async with aiohttp.ClientSession() as session:
            self.session = session

            while not self.stop_event.is_set():
                self.restart_event.clear()

                # Check if credentials exist
                has_creds = bool(
                    (self.config.ecovacs.access_token and self.config.ecovacs.user_id)
                    or (self.config.ecovacs.username and self.config.ecovacs.password)
                )

                if not has_creds:
                    _LOGGER.info("Bridge is not configured yet. Open http://<host>:%d in your browser to set up.", web_port)
                    while not self.stop_event.is_set() and not self.restart_event.is_set():
                        await asyncio.sleep(1)
                    continue

                self.auth_client = EcovacsAuthClient(
                    session=session,
                    username=self.config.ecovacs.username,
                    password=self.config.ecovacs.password,
                    country=self.config.ecovacs.country,
                    device_id=self.config.ecovacs.device_id,
                    access_token=self.config.ecovacs.access_token,
                    user_id=self.config.ecovacs.user_id,
                )
                try:
                    _LOGGER.info("Authenticating with Ecovacs Cloud...")
                    tokens = await self.auth_client.authenticate()
                    self.config.ecovacs.access_token = tokens.access_token
                    self.config.ecovacs.user_id = tokens.account_user_id
                    devices = await self.auth_client.get_devices()
                    if not devices:
                        _LOGGER.warning("No robots found on account. Waiting for reconfiguration...")
                        await asyncio.sleep(5)
                        continue
                    self.robot = devices[0]
                    if self.config.ecovacs.robot_did:
                        matched = next((d for d in devices if d.get("did") == self.config.ecovacs.robot_did), None)
                        if matched:
                            self.robot = matched

                    _LOGGER.info("Target Vacuum: %s (DID: %s)", self.robot.get("name"), self.robot.get("did"))

                except Exception as e:
                    _LOGGER.error("Authentication failed: %s. Check credentials in Web UI http://<host>:%d", e, web_port)
                    await asyncio.sleep(5)
                    continue

                backoff = self.config.streamer.reconnect_delay_seconds

                # Inner streaming loop for this robot
                while not self.stop_event.is_set() and not self.restart_event.is_set():
                    keepalive_task: asyncio.Task[None] | None = None
                    try:
                        tokens = await self.auth_client.authenticate()
                        self.config.ecovacs.access_token = tokens.access_token
                        self.config.ecovacs.user_id = tokens.account_user_id

                        _LOGGER.info("Requesting video stream session from Ecovacs...")
                        session_info = await async_start_watch(
                            session,
                            portal_url=self.auth_client.portal_url,
                            device_id=tokens.device_id,
                            user_id=tokens.user_id,
                            portal_token=tokens.portal_token,
                            country=self.config.ecovacs.country,
                            pin=self.config.ecovacs.pin,
                            robot=self.robot,
                        )
                        _LOGGER.info("Waking up stream on robot...")
                        await async_send_app_ping(
                            session,
                            portal_token=tokens.portal_token,
                            robot=self.robot,
                        )

                        rtsp_target = f"rtsp://127.0.0.1:{self.config.rtsp.port}/{self.config.rtsp.path}"
                        if self.pipeliner:
                            self.pipeliner.stop()
                        self.pipeliner = RtspPipeliner(
                            rtsp_url=rtsp_target,
                            width=1280,
                            height=720,
                            fps=self.config.rtsp.fps,
                            bitrate=self.config.rtsp.bitrate,
                            preset=self.config.rtsp.preset,
                            tune=self.config.rtsp.tune,
                        )
                        self.pipeliner.start()
                        _LOGGER.info("Connecting WebRTC P2P link...")
                        self.active_webrtc = EcovacsWebRTCSession(
                            http_session=session,
                            session_info=session_info,
                            robot=self.robot,
                            on_video_frame=self.on_video_frame,
                        )

                        success = await self.active_webrtc.start(timeout=self.config.streamer.stream_timeout_seconds)
                        if not success:
                            _LOGGER.warning("WebRTC failed to receive first frame. Retrying in %ds...", backoff)
                            await self.active_webrtc.stop()
                            self.active_webrtc = None
                            await asyncio.sleep(backoff)
                            continue

                        _LOGGER.info("Stream active! Broadcasting to RTSP -> %s", rtsp_target)
                        backoff = self.config.streamer.reconnect_delay_seconds

                        keepalive_task = asyncio.create_task(
                            self._keepalive_loop(tokens.portal_token, self.robot)
                        )

                        while (
                            not self.stop_event.is_set()
                            and not self.restart_event.is_set()
                            and not self.active_webrtc.stop_event.is_set()
                        ):
                            await asyncio.sleep(1)

                        _LOGGER.info("Stream connection ended. Preparing reconnection...")

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        _LOGGER.error("Streamer error: %s. Reconnecting in %ds...", e, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 1.5, 30)
                    finally:
                        if keepalive_task:
                            keepalive_task.cancel()
                        if self.active_webrtc:
                            await self.active_webrtc.stop()
                            self.active_webrtc = None
                        if self.pipeliner:
                            self.pipeliner.stop()

    def stop(self) -> None:
        """Signal clean shutdown."""
        _LOGGER.info("Shutting down Ecovacs Deebot Video Proxy...")
        self.stop_event.set()
        if self.pipeliner:
            self.pipeliner.stop()
        if self.mediamtx_proc:
            try:
                self.mediamtx_proc.terminate()
                self.mediamtx_proc.wait(timeout=2)
            except Exception:
                pass


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))

    bridge = EcovacsRtspBridge(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig_handler() -> None:
        bridge.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(bridge.run())
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        loop.close()


if __name__ == "__main__":
    main()
