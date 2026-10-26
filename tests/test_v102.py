"""Verification test suite for v1.0.2 features.

Covers:
1. Config parsing (active_only_video, OverlayConfig, env vars).
2. RobotState & activity transitions (docked, cleaning, station active, manual wake).
3. Overlay engine (chip rendering, positions, standby frame generation, zero-overhead bypass).
4. WebUI REST API endpoints (/api/config-status, /api/settings/overlay, /api/settings/stream, /api/robot/wake, /api/robot/sleep).
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import os
import sys
import time
from pathlib import Path

from av import VideoFrame
from PIL import Image
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop, TestClient, TestServer

from src.config import AppConfig, OverlayConfig, StreamerConfig, load_config, _to_bool
from src.robot_state import RobotState, RobotStateTracker
from src.overlay import StreamOverlay
from src.web_ui import WebUIService


def test_config() -> None:
    print("[1/4] Testing config parsing and defaults...")
    cfg = load_config()
    assert cfg.streamer.active_only_video is True, f"Default active_only_video must be True, got {cfg.streamer.active_only_video}"
    assert hasattr(cfg.streamer, "standby_fps"), "StreamerConfig missing standby_fps"
    assert hasattr(cfg, "overlay"), "AppConfig missing overlay"
    assert isinstance(cfg.overlay, OverlayConfig), "AppConfig.overlay must be OverlayConfig"
    assert cfg.overlay.position == "top_banner", f"Default overlay position must be top_banner, got {cfg.overlay.position}"
    # Test boolean helper
    assert _to_bool("true") is True
    assert _to_bool("1") is True
    assert _to_bool("false") is False
    assert _to_bool("0") is False
    assert _to_bool(None, default=True) is True

    # Test env var override
    os.environ["ACTIVE_ONLY_VIDEO"] = "false"
    os.environ["OVERLAY_SHOW_BATTERY"] = "false"
    os.environ["OVERLAY_POSITION"] = "bottom_right"
    cfg2 = load_config()
    assert cfg2.streamer.active_only_video is False
    assert cfg2.overlay.show_battery is False
    assert cfg2.overlay.position == "bottom_right"
    os.environ.pop("ACTIVE_ONLY_VIDEO", None)
    os.environ.pop("OVERLAY_SHOW_BATTERY", None)
    os.environ.pop("OVERLAY_POSITION", None)
    print("  -> Config parsing OK.")


def test_robot_state() -> None:
    print("[2/4] Testing RobotState activity logic...")
    st = RobotState()
    # Default is docked and idle
    st.clean_status = "docked"
    st.station_status = "idle"
    st.is_charging = True
    assert st.is_active is False, "Docked and idle robot must be inactive"

    # Robot starts cleaning -> must be active
    st.clean_status = "cleaning"
    assert st.is_active is True, "Cleaning robot must be active"

    # Robot returning to dock -> must be active
    st.clean_status = "returning"
    assert st.is_active is True, "Returning robot must be active"

    # Robot docked, but station is washing mop -> must be active!
    st.clean_status = "docked"
    st.station_status = "washing_mop"
    assert st.is_active is True, "Station washing mop must be active"

    # Station emptying dustbin -> must be active!
    st.station_status = "emptying"
    assert st.is_active is True, "Station emptying dustbin must be active"

    # Station idle, robot docked -> inactive!
    st.station_status = "idle"
    assert st.is_active is False, "Station idle and robot docked must be inactive"

    # Manual wake
    st.manual_wake_until = time.time() + 60
    assert st.is_active is True, "Manual wake must make state active"
    st.manual_wake_until = 0.0
    assert st.is_active is False, "Cleared manual wake must revert to inactive"

    # Test tracker response parsing
    tracker = RobotStateTracker(
        session_getter=lambda: None,
        auth_getter=lambda: ("", "", ""),
        robot={"did": "test_did", "class": "test_class", "resource": "res"},
    )
    tracker._parse_work_state({
        "body": {
            "data": {
                "paused": 0,
                "robotState": {
                    "cleanState": {"type": "freeClean"},
                    "state": "cleaning",
                },
                "stationState": {"state": "idle"}
            }
        }
    })
    assert tracker.state.clean_status == "cleaning"
    assert tracker.state.clean_mode == "freeClean"
    assert tracker.state.is_active is True

    tracker._parse_battery_response({"body": {"data": {"value": 84}}})
    assert tracker.state.battery == 84, f"Expected 84, got {tracker.state.battery}"

    tracker._parse_charge_response({"body": {"data": {"isCharging": 1, "mode": "slot"}}})
    assert tracker.state.is_charging is True

    tracker._parse_station_response({"body": {"data": {"act": "mop_wash"}}})
    assert tracker.state.station_status == "washing_mop"

    tracker._parse_lifespan_response({
        "body": {
            "data": [
                {"type": "filter", "left": 10, "total": 100},
                {"type": "sideBrush", "left": 80, "total": 100},
            ]
        }
    })
    assert "Filter" in tracker.state.maintenance
    assert tracker.state.maintenance["Filter"] == 10
    assert len(tracker.state.maintenance_alerts) == 1
    assert "Filter Low" in tracker.state.maintenance_alerts[0]

    tracker._parse_error_codes([322, 314])
    assert tracker.state.error_codes == [322]
    assert tracker.state.error_alerts == ["Clean Water Tank Malfunction"]
    assert tracker.state.error_msg == "Clean Water Tank Malfunction"

    tracker._parse_error_codes([314])
    assert tracker.state.error_codes == []
    assert tracker.state.error_alerts == []
    assert tracker.state.error_msg == ""
    print("  -> RobotState logic & response parsing OK.")


def test_overlay() -> None:
    print("[3/4] Testing StreamOverlay rendering & standby frame...")
    over_cfg = OverlayConfig(
        enabled=True,
        show_timestamp=True,
        show_battery=True,
        show_mode=True,
        show_station=True,
        show_maintenance=True,
        show_robot_name=True,
        position="top_banner",
    )
    overlay = StreamOverlay(over_cfg)

    # 1. Base frame
    base_img = Image.new("RGB", (1280, 720), (30, 30, 30))
    in_frame = VideoFrame.from_image(base_img)

    st = RobotState(
        battery=88,
        is_charging=True,
        clean_status="cleaning",
        station_status="idle",
        maintenance={"Filter": 85, "Main Brush": 90},
        error_alerts=["Clean Water Tank Malfunction"],
    )

    out_frame = overlay.render_overlay(in_frame, st, "Deebot T30 Pro")
    assert out_frame.width == 1280
    assert out_frame.height == 720

    # 2. Test different positions
    for pos in ["top_left", "top_right", "bottom_left", "bottom_right"]:
        overlay.config.position = pos
        f = overlay.render_overlay(in_frame, st, "Deebot T30 Pro")
        assert f.width == 1280

    # 3. Test overlay disabled bypass (must return exact same frame object)
    overlay.config.enabled = False
    f_bypassed = overlay.render_overlay(in_frame, st, "Deebot T30 Pro")
    assert f_bypassed is in_frame, "Disabled overlay must return original frame with zero overhead"
    overlay.config.enabled = True

    # 4. Test standby frame generation
    standby = overlay.generate_standby_frame(1280, 720, st, "Deebot T30 Pro")
    assert standby.width == 1280
    assert standby.height == 720
    s_img = standby.to_image()
    assert s_img.size == (1280, 720)

    # 5. Test standby frame across different overlay positions
    for pos in ["top_banner", "top_left", "top_right", "bottom_left", "bottom_right"]:
        overlay.update_config(OverlayConfig(enabled=True, position=pos))
        sb = overlay.generate_standby_frame(1280, 720, st, "Deebot T30 Pro")
        assert sb.width == 1280
        assert sb.height == 720

    # 6. Test standby frame with overlay disabled
    overlay.update_config(OverlayConfig(enabled=False))
    sb_disabled = overlay.generate_standby_frame(1280, 720, st, "Deebot T30 Pro")
    assert sb_disabled.width == 1280
    assert sb_disabled.height == 720
    print("  -> StreamOverlay rendering & standby generation OK.")


class MockBridge:
    def __init__(self) -> None:
        self.config = AppConfig()
        self.config.rtsp.username = ""
        self.config.rtsp.password = ""
        self.active_webrtc = None
        self.robot = {"name": "Test Vacuum"}
        self.latest_jpeg = b"\xff\xd8\xff\xe0testjpeg"
        self.latest_jpeg_seq = 1
        self.stop_event = asyncio.Event()
        self.restart_event = asyncio.Event()
        self.mode_change_event = asyncio.Event()
        self.overlay = StreamOverlay(self.config.overlay)
        self.state_tracker = RobotStateTracker(
            session_getter=lambda: None,
            auth_getter=lambda: ("", "", ""),
            robot=self.robot,
        )
        self.is_standby = False

    def trigger_restart(self) -> None:
        pass


async def test_web_ui_endpoints() -> None:
    print("[4/4] Testing WebUI REST API endpoints...")
    bridge = MockBridge()
    web_svc = WebUIService(bridge, port=8899)
    app = web_svc.app

    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. GET /api/config-status
        resp = await client.get("/api/config-status")
        assert resp.status == 200
        data = await resp.json()
        assert "activeOnlyVideo" in data
        assert "overlay" in data
        assert "robotState" in data
        assert data["overlay"]["enabled"] is True
        assert data["overlay"]["show_timestamp"] is True

        # 2. POST /api/settings/stream
        resp = await client.post("/api/settings/stream", json={"active_only_video": True})
        assert resp.status == 200
        res_data = await resp.json()
        assert res_data["success"] is True
        assert res_data["active_only_video"] is True
        assert bridge.config.streamer.active_only_video is True

        resp = await client.post("/api/settings/overlay", json={
            "show_timestamp": False,
            "position": "top_banner",
        })
        assert resp.status == 200
        res_data = await resp.json()
        assert res_data["success"] is True
        assert res_data["overlay"]["show_timestamp"] is False
        assert res_data["overlay"]["position"] == "top_banner"
        assert bridge.config.overlay.show_timestamp is False
        assert bridge.overlay.config.show_timestamp is False
        assert bridge.overlay.config.position == "top_banner"

        # 4. POST /api/robot/wake
        resp = await client.post("/api/robot/wake", json={"duration": 120})
        assert bridge.state_tracker.state.is_active is True

        # 5. POST /api/robot/sleep
        resp = await client.post("/api/robot/sleep")
        assert resp.status == 200
        assert bridge.state_tracker.state.manual_wake_until == 0.0

        # 6. GET /snapshot.jpg
        resp = await client.get("/snapshot.jpg")
        assert resp.status == 200
        body = await resp.read()
        assert body.startswith(b"\xff\xd8")

        print("  -> WebUI REST API endpoints OK.")
    finally:
        await client.close()


async def test_web_ui_auth() -> None:
    print("[5/5] Testing WebUI session auth, popup prevention & basic auth compatibility...")
    bridge = MockBridge()
    bridge.config.ecovacs.username = "test@example.com"
    bridge.config.ecovacs.password = "secret"
    bridge.config.rtsp.username = "admin"
    bridge.config.rtsp.password = "secretpass"

    web_svc = WebUIService(bridge, port=8899)
    app = web_svc.app

    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. Unauthenticated request to / (HTML UI) must return 200 without basic auth challenge
        resp = await client.get("/")
        assert resp.status == 200
        assert "WWW-Authenticate" not in resp.headers

        # 1b. Unauthenticated request to /favicon.ico must return 200 without basic auth challenge
        resp = await client.get("/favicon.ico")
        assert resp.status == 200
        assert "image/svg+xml" in resp.headers.get("Content-Type", "")
        assert "WWW-Authenticate" not in resp.headers

        # 2. Unauthenticated request to /api/config-status must return 200 with requiresAuth=True, authenticated=False
        resp = await client.get("/api/config-status")
        assert resp.status == 200
        data = await resp.json()
        assert data["requiresAuth"] is True
        assert data["authenticated"] is False
        assert "WWW-Authenticate" not in resp.headers

        # 3. Unauthenticated request to protected API endpoint (/api/settings/stream) must return 401 without WWW-Authenticate (no browser popup!)
        resp = await client.post("/api/settings/stream", json={"active_only_video": True})
        assert resp.status == 401
        data = await resp.json()
        assert data["authenticated"] is False
        assert "WWW-Authenticate" not in resp.headers, "WWW-Authenticate header must NOT be sent to API to avoid browser popup dialog!"
        # 4. Failed login attempt
        resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
        assert resp.status == 401
        data = await resp.json()
        assert data["success"] is False
        assert "WWW-Authenticate" not in resp.headers

        # 5. Successful login attempt - sets session cookie
        resp = await client.post("/api/auth/login", json={"username": "admin", "password": "secretpass"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        set_cookies = resp.headers.getall("Set-Cookie", [])
        assert any("ecovacs_session=" in c for c in set_cookies)

        # 6. Authenticated request using the session cookie
        resp = await client.get("/api/config-status")
        assert resp.status == 200
        data = await resp.json()
        assert data["authenticated"] is True
        assert data["hasAuth"] is True
        assert "robotState" in data

        # 7. Authenticated POST to settings using session cookie
        resp = await client.post("/api/settings/stream", json={"active_only_video": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

        # 8. Authenticated GET /snapshot.jpg using session cookie
        resp = await client.get("/snapshot.jpg")
        assert resp.status == 200
        body = await resp.read()
        assert body.startswith(b"\xff\xd8")

        # 9. Logout - sets ecovacs_logged_out cookie and deletes ecovacs_session
        resp = await client.post("/api/auth/logout")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        logout_cookies = resp.headers.getall("Set-Cookie", [])
        assert any("ecovacs_logged_out=" in c for c in logout_cookies)

        # 10. Post-logout request to /api/config-status even with cached browser Basic Auth must remain logged out!
        basic_creds = base64.b64encode(b"admin:secretpass").decode("ascii")
        resp = await client.get("/api/config-status", headers={"Authorization": f"Basic {basic_creds}"})
        assert resp.status == 200
        data = await resp.json()
        assert data["authenticated"] is False, "Logged out browser must NOT be auto-reauthenticated by cached Basic Auth!"

        # 11. Post-logout request to protected API endpoint should now be 401 even if browser sends cached Basic Auth
        resp = await client.post(
            "/api/settings/stream",
            json={"active_only_video": True},
            headers={"Authorization": f"Basic {basic_creds}"},
        )
        assert resp.status == 401

        # 12. External integrations (curl, Home Assistant) using HTTP Basic Auth without browser cookies
        clean_client = TestClient(TestServer(app))
        await clean_client.start_server()
        try:
            resp = await clean_client.post(
                "/api/settings/stream",
                json={"active_only_video": True},
                headers={"Authorization": f"Basic {basic_creds}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True

            # HTTP Basic Auth on /snapshot.jpg
            resp = await clean_client.get(
                "/snapshot.jpg",
                headers={"Authorization": f"Basic {basic_creds}"},
            )
            assert resp.status == 200
            body = await resp.read()
            assert body.startswith(b"\xff\xd8")

            # Query param authentication for direct embeds: /snapshot.jpg?user=admin&pass=secretpass
            resp = await clean_client.get("/snapshot.jpg?user=admin&pass=secretpass")
            assert resp.status == 200
            body = await resp.read()
            assert body.startswith(b"\xff\xd8")
        finally:
            await clean_client.close()
        # 14. CLI / curl detection on unauthenticated raw stream: curl gets WWW-Authenticate challenge
        resp = await client.get("/snapshot.jpg", headers={"User-Agent": "curl/7.81.0"})
        assert resp.status == 401
        assert "WWW-Authenticate" in resp.headers

        # 15. Browser request on unauthenticated raw stream: browser does NOT get WWW-Authenticate (no popup modal!)
        resp = await client.get("/snapshot.jpg", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        assert resp.status == 401
        assert "WWW-Authenticate" not in resp.headers

        print("  -> Session auth, popup prevention & basic auth compatibility OK.")
    finally:
        await client.close()

def main() -> None:
    print("=" * 60)
    print("RUNNING ECOVACS DEEBOT VIDEO PROXY v1.1.0 TEST SUITE")
    print("=" * 60)
    test_config()
    test_robot_state()
    test_overlay()
    asyncio.run(test_web_ui_endpoints())
    asyncio.run(test_web_ui_auth())
    print("=" * 60)
    print("ALL TESTS PASSED SUCCESSFULLY! (v1.1.0 verified)")
    print("=" * 60)


if __name__ == "__main__":
    main()
