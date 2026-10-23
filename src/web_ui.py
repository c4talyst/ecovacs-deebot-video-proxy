"""Web UI & Setup Wizard Server for Ecovacs Deebot Video Proxy.

Provides a clean browser-based interface for:
1. Unraid / Proxmox / Docker initial setup (email verification, robot discovery).
2. Live stream monitor and RTSP stream URL display.
3. Telemetry and settings management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
import yaml

from .ecovacs_auth import EcovacsAuthClient
from .config import AppConfig, load_config

_LOGGER = logging.getLogger("ecovacs.web_ui")

HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Ecovacs Deebot Video Proxy</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --bg: #0b0d13;
            --surface: #141722;
            --surface-card: #191d2b;
            --surface-hover: #222738;
            --border: #2a3144;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --text: #f3f4f6;
            --text-muted: #9ca3af;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0; padding: 20px; background: var(--bg); color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            min-height: 100vh;
        }
        .container { max-width: 680px; width: 100%; }
        
        .header {
            text-align: center; margin-bottom: 24px;
        }
        .header h1 { margin: 0 0 6px 0; font-size: 24px; font-weight: 700; }
        .header p { margin: 0; color: var(--text-muted); font-size: 14px; }

        .card {
            background: var(--surface-card); border-radius: 12px; border: 1px solid var(--border);
            padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); margin-bottom: 16px;
        }

        .step-indicator {
            display: flex; justify-content: space-between; margin-bottom: 24px; position: relative;
        }
        .step-indicator::before {
            content: ""; position: absolute; top: 14px; left: 20px; right: 20px; height: 2px;
            background: var(--border); z-index: 1;
        }
        .step {
            position: relative; z-index: 2; background: var(--surface-card);
            padding: 0 8px; display: flex; flex-direction: column; align-items: center; gap: 4px;
        }
        .step-num {
            width: 28px; height: 28px; border-radius: 50%; background: var(--surface);
            border: 2px solid var(--border); display: flex; align-items: center; justify-content: center;
            font-size: 12px; font-weight: 700; color: var(--text-muted);
        }
        .step.active .step-num { border-color: var(--accent); background: var(--accent); color: #fff; }
        .step.done .step-num { border-color: var(--success); background: var(--success); color: #fff; }
        .step-label { font-size: 11px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }

        .form-group { margin-bottom: 16px; }
        label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text); }
        .hint { font-size: 11.5px; color: var(--text-muted); margin-top: 4px; }
        input, select {
            width: 100%; padding: 10px 12px; background: var(--surface);
            border: 1px solid var(--border); border-radius: 8px; color: #fff; font-size: 14px;
            outline: none; transition: border-color 0.15s ease;
        }
        input:focus, select:focus { border-color: var(--accent); }

        .btn {
            background: var(--accent); color: #fff; border: none; padding: 11px 18px;
            border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer;
            transition: all 0.15s ease; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
            width: 100%;
        }
        .btn:hover { background: var(--accent-hover); }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .btn-outline {
            background: transparent; border: 1px solid var(--border); color: var(--text);
        }
        .btn-outline:hover { background: var(--surface-hover); }

        .spinner {
            width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3);
            border-top-color: #fff; border-radius: 50%; animation: spin 0.6s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .alert {
            padding: 12px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; display: none;
        }
        .alert-error { background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); color: #fca5a5; }
        .alert-success { background: rgba(16, 185, 129, 0.15); border: 1px solid var(--success); color: #6ee7b7; }
        .alert-info { background: rgba(59, 130, 246, 0.15); border: 1px solid var(--accent); color: #93c5fd; }

        /* Robot Selection Cards */
        .robot-card {
            background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
            padding: 12px 16px; margin-bottom: 10px; cursor: pointer; transition: all 0.15s ease;
            display: flex; align-items: center; justify-content: space-between;
        }
        .robot-card:hover { border-color: #3b4258; }
        .robot-card.selected { border-color: var(--accent); background: rgba(59, 130, 246, 0.08); }
        .robot-info h4 { margin: 0 0 4px 0; font-size: 15px; }
        .robot-info p { margin: 0; font-size: 12px; color: var(--text-muted); font-family: monospace; }

        /* Live Stream Dashboard View */
        .stream-view { display: none; }
        .stream-box {
            background: #000; border-radius: 8px; overflow: hidden; aspect-ratio: 16 / 9;
            width: 100%; display: flex; align-items: center; justify-content: center; margin-bottom: 16px;
        }
        .stream-box img { width: 100%; height: 100%; object-fit: contain; }
        .url-box {
            background: var(--surface); padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border);
            font-family: monospace; font-size: 13px; color: #60a5fa; margin-bottom: 16px;
            display: flex; align-items: center; justify-content: space-between; word-break: break-all;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>ECOVACS DEEBOT VIDEO PROXY</h1>
            <p>Direct LAN WebRTC, RTSP, HLS & Web Video Proxy</p>
        </div>

        <!-- Setup Form Card -->
        <div class="card" id="setup-card">
            <div class="step-indicator">
                <div class="step active" id="st-1">
                    <div class="step-num">1</div>
                    <div class="step-label">Account</div>
                </div>
                <div class="step" id="st-2">
                    <div class="step-num">2</div>
                    <div class="step-label">Verify</div>
                </div>
                <div class="step" id="st-3">
                    <div class="step-num">3</div>
                    <div class="step-label">Select Robot</div>
                </div>
            </div>

            <div class="alert alert-error" id="alert-error"></div>
            <div class="alert alert-success" id="alert-success"></div>
            <div class="alert alert-info" id="alert-info"></div>

            <!-- STEP 1: Account Login -->
            <div id="step-1">
                <div class="form-group">
                    <label>Ecovacs Account Email</label>
                    <input type="email" id="input-email" placeholder="name@example.com" required>
                </div>
                <div class="form-group">
                    <label>Account Password</label>
                    <input type="password" id="input-password" placeholder="••••••••••••" required>
                </div>
                <div class="form-group">
                    <label>Country Code</label>
                    <input type="text" id="input-country" value="za" maxlength="4" placeholder="za, us, de, uk">
                    <div class="hint">Two-letter country code where your Ecovacs account was created.</div>
                </div>
                <button class="btn" id="btn-login" onclick="submitLogin()">
                    <span>Connect Account</span>
                </button>
            </div>

            <!-- STEP 2: Email Verification Challenge -->
            <div id="step-2" style="display: none;">
                <p style="font-size: 14px; color: var(--text-muted); margin-bottom: 16px;">
                    Ecovacs dispatched a one-time security code to your email. Enter it below to permanently authorize this container.
                </p>
                <div class="form-group">
                    <label>Verification Code</label>
                    <input type="text" id="input-code" placeholder="6-digit code" style="font-size: 20px; letter-spacing: 4px; text-align: center;">
                </div>
                <button class="btn" id="btn-verify" onclick="submitVerify()">
                    <span>Verify Code</span>
                </button>
            </div>

            <!-- STEP 3: Robot Selection -->
            <div id="step-3" style="display: none;">
                <label style="margin-bottom: 10px;">Select Target Vacuum</label>
                <div id="robot-list"></div>
                
                <div class="form-group" style="margin-top: 16px;">
                    <label>Camera / Video Manager PIN</label>
                    <input type="password" id="input-pin" placeholder="Leave blank if password is disabled in app">
                </div>
                <div class="form-group">
                    <label>RTSP Mount Path</label>
                    <input type="text" id="input-path" value="live">
                    <div class="hint">Will stream at <code>rtsp://&lt;host&gt;:8554/live</code></div>
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                    <div class="form-group">
                        <label>RTSP Username (Optional)</label>
                        <input type="text" id="input-rtsp-user" placeholder="e.g. admin">
                    </div>
                    <div class="form-group">
                        <label>RTSP Password (Optional)</label>
                        <input type="password" id="input-rtsp-pass" placeholder="e.g. secret">
                    </div>
                </div>
                <div class="hint" style="margin-bottom: 16px;">Set a username and password to password-protect your RTSP stream in Frigate/VLC.</div>
                <button class="btn" id="btn-save" onclick="saveAndStart()">
                    <span>Save Configuration & Start Streaming</span>
                </button>
            </div>
        </div>

        <!-- Live Dashboard Card (Displayed once configured) -->
        <div class="card stream-view" id="dashboard-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h3 style="margin: 0;" id="dash-robot-name">Target Vacuum</h3>
                <span style="color: var(--success); font-weight: bold; font-size: 13px;" id="dash-status">STREAMING</span>
            </div>

            <div class="stream-box">
                <img id="dash-img" src="/mjpeg" alt="Live Stream">
            </div>

            <label>RTSP Stream URL (Frigate / VLC / Go2RTC):</label>
            <div class="url-box" id="rtsp-url-display">rtsp://localhost:8554/live</div>

            <div style="display: flex; gap: 10px; margin-top: 16px;">
                <a href="/snapshot.jpg" target="_blank" class="btn btn-outline" style="text-decoration: none;">Download Snapshot</a>
                <button class="btn btn-outline" style="color: var(--danger); border-color: rgba(239, 68, 68, 0.4);" onclick="resetConfig()">Reconfigure</button>
            </div>
        </div>
    </div>

    <script>
        let discoveredRobots = [];
        let selectedDID = "";
        let hostIP = window.location.hostname;

        function setAlert(type, msg) {
            ['error', 'success', 'info'].forEach(t => {
                const el = document.getElementById("alert-" + t);
                el.style.display = "none";
                el.textContent = "";
            });
            if (type && msg) {
                const el = document.getElementById("alert-" + type);
                el.textContent = msg;
                el.style.display = "block";
            }
        }

        function setBtnLoading(id, loading) {
            const btn = document.getElementById(id);
            if (!btn) return;
            btn.disabled = loading;
            if (loading) {
                btn.dataset.origText = btn.innerHTML;
                btn.innerHTML = '<span class="spinner"></span> <span>Please wait...</span>';
            } else if (btn.dataset.origText) {
                btn.innerHTML = btn.dataset.origText;
            }
        }

        async function checkStatus() {
            try {
                const res = await fetch("/api/config-status");
                const data = await res.json();
                if (data.configured) {
                    document.getElementById("setup-card").style.display = "none";
                    document.getElementById("dashboard-card").style.display = "block";
                    document.getElementById("dash-robot-name").textContent = data.robotName || "Ecovacs Vacuum";
                    let rtspUrl = "rtsp://";
                    if (data.hasAuth) {
                        rtspUrl += encodeURIComponent(data.rtspUser) + ":••••••@";
                    }
                    rtspUrl += hostIP + ":" + (data.rtspPort || 8554) + "/" + (data.rtspPath || "live");
                    document.getElementById("rtsp-url-display").textContent = rtspUrl;
                    const statusEl = document.getElementById("dash-status");
                    if (statusEl) {
                        statusEl.textContent = data.streaming ? "STREAMING" : "CONNECTING...";
                        statusEl.style.color = data.streaming ? "var(--success)" : "var(--warning)";
                    }
                } else {
                    document.getElementById("setup-card").style.display = "block";
                    document.getElementById("dashboard-card").style.display = "none";
                }
            } catch (e) {
                console.error("Status check failed", e);
            }
        }

        async function submitLogin() {
            const email = document.getElementById("input-email").value.trim();
            const password = document.getElementById("input-password").value.trim();
            const country = document.getElementById("input-country").value.trim().toLowerCase();

            if (!email || !password) {
                setAlert("error", "Email and password are required.");
                return;
            }

            setBtnLoading("btn-login", true);
            setAlert(null);

            try {
                const res = await fetch("/api/setup/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username: email, password, country })
                });
                const data = await res.json();
                setBtnLoading("btn-login", false);

                if (data.status === "verification_required") {
                    setAlert("info", "Verification code sent! Please check your email inbox.");
                    document.getElementById("step-1").style.display = "none";
                    document.getElementById("step-2").style.display = "block";
                    document.getElementById("st-1").classList.add("done");
                    document.getElementById("st-2").classList.add("active");
                } else if (data.status === "success") {
                    handleDiscoveredRobots(data.devices);
                } else {
                    setAlert("error", data.error || "Login failed");
                }
            } catch (e) {
                setBtnLoading("btn-login", false);
                setAlert("error", "Network error: " + e.message);
            }
        }

        async function submitVerify() {
            const code = document.getElementById("input-code").value.trim();
            if (!code) {
                setAlert("error", "Enter the verification code from your email.");
                return;
            }

            setBtnLoading("btn-verify", true);
            setAlert(null);

            try {
                const res = await fetch("/api/setup/verify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code })
                });
                const data = await res.json();
                setBtnLoading("btn-verify", false);

                if (data.status === "success") {
                    handleDiscoveredRobots(data.devices);
                } else {
                    setAlert("error", data.error || "Verification failed");
                }
            } catch (e) {
                setBtnLoading("btn-verify", false);
                setAlert("error", "Network error: " + e.message);
            }
        }

        function handleDiscoveredRobots(devices) {
            discoveredRobots = devices || [];
            if (!discoveredRobots.length) {
                setAlert("error", "No Ecovacs robots found on this account.");
                return;
            }

            document.getElementById("step-1").style.display = "none";
            document.getElementById("step-2").style.display = "none";
            document.getElementById("step-3").style.display = "block";
            document.getElementById("st-1").classList.add("done");
            document.getElementById("st-2").classList.add("done");
            document.getElementById("st-3").classList.add("active");

            const container = document.getElementById("robot-list");
            container.innerHTML = "";

            discoveredRobots.forEach((r, idx) => {
                const div = document.createElement("div");
                div.className = "robot-card" + (idx === 0 ? " selected" : "");
                const infoDiv = document.createElement("div");
                infoDiv.className = "robot-info";
                const h4 = document.createElement("h4");
                h4.textContent = r.name || "Deebot Vacuum";
                const p = document.createElement("p");
                p.textContent = (r.model || "") + " • DID: " + (r.did || "");
                infoDiv.appendChild(h4);
                infoDiv.appendChild(p);
                div.appendChild(infoDiv);
                div.onclick = () => {
                    document.querySelectorAll(".robot-card").forEach(c => c.classList.remove("selected"));
                    div.classList.add("selected");
                    selectedDID = r.did;
                };
                container.appendChild(div);
            });
            selectedDID = discoveredRobots[0].did;
            setAlert("success", `Found ${discoveredRobots.length} robot(s). Select one to proceed.`);
        }

        async function saveAndStart() {
            const pin = document.getElementById("input-pin").value.trim();
            const rtspPath = document.getElementById("input-path").value.trim() || "live";
            const rtspUser = document.getElementById("input-rtsp-user").value.trim();
            const rtspPass = document.getElementById("input-rtsp-pass").value.trim();

            setBtnLoading("btn-save", true);
            setAlert(null);

            try {
                const res = await fetch("/api/setup/save", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ robot_did: selectedDID, pin, path: rtspPath, rtsp_user: rtspUser, rtsp_pass: rtspPass })
                });
                const data = await res.json();
                setBtnLoading("btn-save", false);

                if (data.success) {
                    await checkStatus();
                } else {
                    setAlert("error", data.error || "Save failed");
                }
            } catch (e) {
                setBtnLoading("btn-save", false);
                setAlert("error", "Network error: " + e.message);
            }
        }

        async function resetConfig() {
            if (!confirm("Reconfigure Ecovacs Deebot Video Proxy?")) return;
            await fetch("/api/setup/reset", { method: "POST" });
            location.reload();
        }

        checkStatus();
        setInterval(checkStatus, 5000);
    </script>
</body>
</html>
"""


class WebUIService:
    """Aiohttp Web UI and Setup API."""

    def __init__(self, bridge: Any, host: str = "0.0.0.0", port: int = 8080):
        self.bridge = bridge
        self.host = host
        self.port = port
        self.app = web.Application()
        self.temp_auth_client: EcovacsAuthClient | None = None
        self.temp_creds: dict[str, Any] = {}

        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/config-status", self.handle_config_status)
        self.app.router.add_post("/api/setup/login", self.handle_setup_login)
        self.app.router.add_post("/api/setup/verify", self.handle_setup_verify)
        self.app.router.add_post("/api/setup/save", self.handle_setup_save)
        self.app.router.add_post("/api/setup/reset", self.handle_setup_reset)

        # Proxied stream endpoints
        self.app.router.add_get("/snapshot.jpg", self.handle_snapshot)
        self.app.router.add_get("/mjpeg", self.handle_mjpeg)

    def _get_config_save_path(self) -> Path:
        """Find best persistent path for config.yaml."""
        candidates = [Path("/config"), Path("/app/config"), Path("config"), Path(".")]
        for c in candidates:
            if c.is_dir():
                return c / "config.yaml"
        return Path("config.yaml")

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_UI, content_type="text/html")

    async def handle_config_status(self, request: web.Request) -> web.Response:
        configured = bool(
            (self.bridge.config.ecovacs.access_token and self.bridge.config.ecovacs.user_id)
            or (self.bridge.config.ecovacs.username and self.bridge.config.ecovacs.password)
        )
        robot_name = (self.bridge.robot or {}).get("name") if hasattr(self.bridge, "robot") and self.bridge.robot else "Ecovacs Deebot"
        return web.json_response({
            "configured": configured,
            "streaming": bool(self.bridge.active_webrtc and not self.bridge.active_webrtc.stop_event.is_set()),
            "robotName": robot_name,
            "rtspPath": self.bridge.config.rtsp.path,
            "rtspPort": self.bridge.config.rtsp.port,
            "hasAuth": bool(self.bridge.config.rtsp.username and self.bridge.config.rtsp.password),
            "rtspUser": self.bridge.config.rtsp.username,
        })

    async def handle_setup_login(self, request: web.Request) -> web.Response:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        country = str(data.get("country", "za")).strip().lower()

        if not username or not password:
            return web.json_response({"status": "error", "error": "Email and password required"}, status=400)

        self.temp_creds = {"username": username, "password": password, "country": country}
        self.temp_auth_client = EcovacsAuthClient(
            session=self.bridge.session,
            username=username,
            password=password,
            country=country,
        )

        try:
            await self.temp_auth_client.authenticate()
            devices = await self.temp_auth_client.get_devices()
            return web.json_response({"status": "success", "devices": devices})
        except RuntimeError as e:
            if "Device verification required" in str(e):
                await self.temp_auth_client.request_email_verification_code()
                return web.json_response({"status": "verification_required"})
            return web.json_response({"status": "error", "error": str(e)}, status=400)

    async def handle_setup_verify(self, request: web.Request) -> web.Response:
        if not self.temp_auth_client:
            return web.json_response({"status": "error", "error": "Session expired. Restart login."}, status=400)

        data = await request.json()
        code = str(data.get("code", "")).strip()
        if not code:
            return web.json_response({"status": "error", "error": "Verification code required"}, status=400)

        try:
            await self.temp_auth_client.verify_device_code(code)
            await self.temp_auth_client.authenticate()
            devices = await self.temp_auth_client.get_devices()
            return web.json_response({"status": "success", "devices": devices})
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=400)

    async def handle_setup_save(self, request: web.Request) -> web.Response:
        if not self.temp_auth_client:
            return web.json_response({"success": False, "error": "No active setup session"}, status=400)

        data = await request.json()
        robot_did = str(data.get("robot_did", "")).strip()
        pin = str(data.get("pin", "")).strip()
        rtsp_path = str(data.get("path", "live")).strip().strip("/")
        rtsp_user = str(data.get("rtsp_user", "")).strip()
        rtsp_pass = str(data.get("rtsp_pass", "")).strip()

        # Update in-memory config
        self.bridge.config.ecovacs.username = self.temp_creds.get("username", "")
        self.bridge.config.ecovacs.password = self.temp_creds.get("password", "")
        self.bridge.config.ecovacs.country = self.temp_creds.get("country", "za")
        self.bridge.config.ecovacs.device_id = self.temp_auth_client.device_id
        self.bridge.config.ecovacs.access_token = self.temp_auth_client.access_token
        self.bridge.config.ecovacs.user_id = self.temp_auth_client.user_id
        self.bridge.config.ecovacs.pin = pin
        self.bridge.config.ecovacs.robot_did = robot_did
        self.bridge.config.rtsp.path = rtsp_path
        self.bridge.config.rtsp.username = rtsp_user
        self.bridge.config.rtsp.password = rtsp_pass
        # Save to config.yaml
        save_file = self._get_config_save_path()
        yaml_content = {
            "ecovacs": {
                "username": self.bridge.config.ecovacs.username,
                "password": self.bridge.config.ecovacs.password,
                "country": self.bridge.config.ecovacs.country,
                "device_id": self.bridge.config.ecovacs.device_id,
                "access_token": self.bridge.config.ecovacs.access_token,
                "user_id": self.bridge.config.ecovacs.user_id,
                "pin": self.bridge.config.ecovacs.pin,
                "robot_did": self.bridge.config.ecovacs.robot_did,
            },
            "rtsp": {
                "path": self.bridge.config.rtsp.path,
                "port": self.bridge.config.rtsp.port,
                "fps": self.bridge.config.rtsp.fps,
                "bitrate": self.bridge.config.rtsp.bitrate,
                "preset": self.bridge.config.rtsp.preset,
                "tune": self.bridge.config.rtsp.tune,
                "username": self.bridge.config.rtsp.username,
                "password": self.bridge.config.rtsp.password,
            },
            "streamer": {
                "reconnect_delay_seconds": self.bridge.config.streamer.reconnect_delay_seconds,
                "appping_interval_seconds": self.bridge.config.streamer.appping_interval_seconds,
                "stream_timeout_seconds": self.bridge.config.streamer.stream_timeout_seconds,
            },
            "logging": {
                "level": self.bridge.config.log_level,
            },
        }

        with open(save_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, default_flow_style=False)
        _LOGGER.info("Saved configuration to %s", save_file)

        # Trigger streamer loop restart
        self.bridge.trigger_restart()
        return web.json_response({"success": True})

    async def handle_setup_reset(self, request: web.Request) -> web.Response:
        save_file = self._get_config_save_path()
        if save_file.is_file():
            save_file.unlink()
        self.bridge.config.ecovacs.access_token = ""
        self.bridge.config.ecovacs.user_id = ""
        self.bridge.trigger_restart()
        return web.json_response({"success": True})

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        if self.bridge.latest_jpeg:
            return web.Response(body=self.bridge.latest_jpeg, content_type="image/jpeg")
        return web.Response(text="No frame available", status=503)

    async def handle_mjpeg(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200, reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        await response.prepare(request)
        last_seq = -1

        try:
            while not self.bridge.stop_event.is_set():
                if self.bridge.latest_jpeg and self.bridge.latest_jpeg_seq != last_seq:
                    last_seq = self.bridge.latest_jpeg_seq
                    data = self.bridge.latest_jpeg
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    )
                    await response.write(header + data + b"\r\n")
                await asyncio.sleep(0.04)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, aiohttp.ClientError):
            pass
        return response
