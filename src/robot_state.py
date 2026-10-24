"""Ecovacs Robot and Station Telemetry and State Tracker.

Monitors:
1. Battery status & charging state.
2. Cleaning state / Robot activity (cleaning, returning, idle, docked, error).
3. Station state (mop washing, drying, dustbin emptying, idle).
4. Maintenance / Component lifespan (filters, brushes, mop pads).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import secrets
import time
from typing import Any, Callable

import aiohttp

_LOGGER = logging.getLogger("ecovacs.state")


@dataclass
class RobotState:
    online: bool = True
    battery: int = 100
    is_charging: bool = False
    clean_status: str = "docked"  # "docked", "idle", "cleaning", "returning", "paused", "error"
    clean_mode: str = "auto"
    station_status: str = "idle"  # "idle", "washing_mop", "drying_mop", "emptying", "collecting"
    maintenance: dict[str, int] = field(default_factory=dict)  # e.g. {"filter": 85, "main_brush": 90, "side_brush": 80}
    maintenance_alerts: list[str] = field(default_factory=list)
    error_code: int = 0
    error_msg: str = ""
    last_updated: float = 0.0
    manual_wake_until: float = 0.0

    @property
    def is_active(self) -> bool:
        """Return True if Robot or Station is currently active (or manually awakened)."""
        now = time.time()
        if now < self.manual_wake_until:
            return True

        # Check robot activity
        active_clean_states = {
            "cleaning", "clean", "sweep", "mop", "spot_clean", "custom_clean",
            "returning", "moving", "exploring", "paused", "error", "work", "working"
        }
        if self.clean_status.lower() in active_clean_states:
            return True

        # Check station activity
        active_station_states = {
            "washing", "washing_mop", "drying", "drying_mop", "emptying",
            "dust_collecting", "collecting", "cleaning", "self_cleaning"
        }
        if self.station_status.lower() in active_station_states:
            return True

        # If docked / idle / charging without active station task -> inactive
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "battery": self.battery,
            "is_charging": self.is_charging,
            "clean_status": self.clean_status,
            "clean_mode": self.clean_mode,
            "station_status": self.station_status,
            "maintenance": self.maintenance,
            "maintenance_alerts": self.maintenance_alerts,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "is_active": self.is_active,
            "manual_wake_active": time.time() < self.manual_wake_until,
            "last_updated": self.last_updated,
        }


class RobotStateTracker:
    """Periodically queries robot telemetry via NG-IoT and Cloud APIs."""

    def __init__(
        self,
        session_getter: Callable[[], aiohttp.ClientSession | None],
        auth_getter: Callable[[], tuple[str, str, str]],  # (portal_url, portal_token, user_id)
        robot: dict[str, Any],
        poll_interval: float = 6.0,
    ) -> None:
        self.session_getter = session_getter
        self.auth_getter = auth_getter
        self.robot = robot
        self.poll_interval = poll_interval

        self.state = RobotState(last_updated=time.time())
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._callbacks: list[Callable[[RobotState], None]] = []

    def add_callback(self, cb: Callable[[RobotState], None]) -> None:
        self._callbacks.append(cb)

    def trigger_callbacks(self) -> None:
        for cb in self._callbacks:
            try:
                cb(self.state)
            except Exception as e:
                _LOGGER.debug("State callback error: %s", e)

    def manual_wake(self, duration_seconds: float = 300.0) -> None:
        """Temporarily wake video streaming even if robot is docked."""
        self.state.manual_wake_until = time.time() + duration_seconds
        _LOGGER.info("Manual video streaming wake activated for %d seconds", int(duration_seconds))
        self.trigger_callbacks()

    def clear_manual_wake(self) -> None:
        self.state.manual_wake_until = 0.0
        self.trigger_callbacks()

    def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _send_ngiot_command(
        self,
        session: aiohttp.ClientSession,
        portal_token: str,
        cmd_name: str,
        body_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch query command over NG-IoT endpoint/control."""
        service = self.robot.get("service") or {}
        host = str(service.get("mqs") or "api-ngiot.dc-na.ww.ecouser.net").strip()
        service_id = secrets.token_hex(8)
        timestamp_ms = str(int(time.time() * 1000))

        params = {
            "si": service_id,
            "ct": "q",
            "eid": self.robot.get("did", ""),
            "et": self.robot.get("class", ""),
            "er": self.robot.get("resource", ""),
            "apn": cmd_name,
            "fmt": "j",
        }
        body = {
            "body": body_data or {},
            "header": {
                "channel": "Android",
                "m": "request",
                "pri": 2,
                "reqid": service_id[:6],
                "ts": timestamp_ms,
                "tzc": "UTC",
                "tzm": 0,
                "ver": "0.0.22",
            },
        }
        headers = {
            "Authorization": f"Bearer {portal_token}",
            "X-ECO-REQUEST-ID": service_id,
            "Content-Type": "application/octet-stream",
            "Accept": "application/json",
            "User-Agent": "okhttp/4.9.1",
        }
        url = f"https://{host}/api/iot/endpoint/control"
        try:
            async with session.post(
                url,
                params=params,
                data=json.dumps(body, separators=(",", ":")).encode(),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status == 200:
                    raw = await response.read()
                    if raw:
                        return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            _LOGGER.debug("NG-IoT command %s failed: %s", cmd_name, e)
        return None

    async def _send_devmanager_command(
        self,
        session: aiohttp.ClientSession,
        portal_url: str,
        user_id: str,
        cmd_name: str,
        body_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch JSON-RPC command via cloud devmanager.do REST relay."""
        url = f"{portal_url}/api/iot/devmanager.do"
        params = {
            "mid": self.robot.get("class", ""),
            "did": self.robot.get("did", ""),
            "td": "q",
            "u": user_id,
            "cv": "1.67.3",
            "av": "1.3.1",
        }
        payload = {
            "cmdName": cmd_name,
            "payload": {
                "header": {
                    "pri": "1",
                    "ts": time.time(),
                    "tzm": 480,
                    "ver": "0.0.50",
                },
                "body": body_data or {},
            },
            "payloadType": "j",
            "td": "q",
            "toId": self.robot.get("did", ""),
            "toRes": self.robot.get("resource", ""),
            "toType": self.robot.get("class", ""),
        }
        try:
            async with session.post(
                url,
                params=params,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status == 200:
                    raw = await response.read()
                    if raw:
                        return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            _LOGGER.debug("Devmanager command %s failed: %s", cmd_name, e)
        return None

    @staticmethod
    def _extract_payload(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        body = data.get("body")
        if isinstance(body, dict):
            inner_data = body.get("data")
            if isinstance(inner_data, dict):
                return {**body, **inner_data}
            return body
        inner_data = data.get("data")
        if isinstance(inner_data, dict):
            return inner_data
        return data

    def _parse_charge_response(self, data: dict[str, Any]) -> None:
        """Extract battery and charging status."""
        body = self._extract_payload(data)
        if "isCharging" in body:
            self.state.is_charging = bool(body.get("isCharging"))
        mode = str(body.get("mode") or "").lower()
        if mode in ("slot", "charging", "dock"):
            self.state.is_charging = True
            if self.state.clean_status in ("idle", "stop"):
                self.state.clean_status = "docked"

    def _parse_battery_response(self, data: dict[str, Any]) -> None:
        """Extract battery percentage."""
        body = self._extract_payload(data)
        val = body.get("value") or body.get("percent") or body.get("battery")
        if val is not None:
            try:
                self.state.battery = max(0, min(100, int(val)))
            except (ValueError, TypeError):
                pass

    def _parse_clean_response(self, data: dict[str, Any]) -> None:
        """Extract cleaning state and mode."""
        body = self._extract_payload(data)
        raw_state = str(body.get("cleanState") or body.get("state") or body.get("type") or "").lower()
        act = str(body.get("act") or "").lower()

        if act in ("stop", "pause"):
            self.state.clean_status = act
        elif raw_state:
            if any(k in raw_state for k in ("clean", "auto", "spot", "custom", "sweep", "mop")):
                self.state.clean_status = "cleaning"
            elif any(k in raw_state for k in ("dock", "charge", "slot", "sleep")):
                self.state.clean_status = "docked"
            elif "return" in raw_state or "back" in raw_state:
                self.state.clean_status = "returning"
            else:
                self.state.clean_status = raw_state

    def _parse_station_response(self, data: dict[str, Any]) -> None:
        """Extract station state (mop washing, dustbin emptying, drying)."""
        body = self._extract_payload(data)
        act = str(body.get("act") or body.get("state") or body.get("status") or "").lower()
        if any(k in act for k in ("wash", "mop_wash")):
            self.state.station_status = "washing_mop"
        elif any(k in act for k in ("dry", "mop_dry")):
            self.state.station_status = "drying_mop"
        elif any(k in act for k in ("empty", "dust", "collect")):
            self.state.station_status = "emptying"
        elif act in ("idle", "none", "standby", "0"):
            self.state.station_status = "idle"
        elif act:
            self.state.station_status = act

    def _parse_lifespan_response(self, data: dict[str, Any]) -> None:
        """Extract component wear and construct maintenance alerts."""
        items = data.get("body") or data.get("data") or []
        if isinstance(items, dict):
            items = items.get("data") or [items]
        if not isinstance(items, list):
            return

        maint: dict[str, int] = {}
        alerts: list[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            ctype = str(item.get("type") or item.get("name") or "").lower()
            left = item.get("left")
            total = item.get("total")
            percent = item.get("percent")

            calc_pct = 100
            if percent is not None:
                try:
                    calc_pct = int(percent)
                except (ValueError, TypeError):
                    pass
            elif left is not None and total is not None:
                try:
                    t_val = float(total)
                    if t_val > 0:
                        calc_pct = int((float(left) / t_val) * 100)
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

            label = ctype
            if "brush" in ctype and "side" in ctype:
                label = "Side Brush"
            elif "brush" in ctype:
                label = "Main Brush"
            elif "filter" in ctype or "heap" in ctype:
                label = "Filter"
            elif "mop" in ctype:
                label = "Mop Pads"
            elif "bag" in ctype:
                label = "Dust Bag"

            maint[label] = calc_pct
            if calc_pct <= 10:
                alerts.append(f"{label} Low ({calc_pct}%)")

        if maint:
            self.state.maintenance = maint
        self.state.maintenance_alerts = alerts

    async def poll_once(self) -> RobotState:
        """Poll telemetry from Ecovacs cloud."""
        session = self.session_getter()
        if not session or session.closed:
            return self.state

        portal_url, portal_token, user_id = self.auth_getter()
        if not portal_token:
            return self.state

        # 1. Query battery
        res_bat = await self._send_ngiot_command(session, portal_token, "getBattery")
        if not res_bat:
            res_bat = await self._send_devmanager_command(session, portal_url, user_id, "getBattery")
        if res_bat:
            self._parse_battery_response(res_bat)

        # 2. Query charge state
        res_chg = await self._send_ngiot_command(session, portal_token, "getChargeState")
        if not res_chg:
            res_chg = await self._send_devmanager_command(session, portal_url, user_id, "getChargeState")
        if res_chg:
            self._parse_charge_response(res_chg)

        # 3. Query clean info
        res_cln = await self._send_ngiot_command(session, portal_token, "getCleanInfo_V2")
        if not res_cln:
            res_cln = await self._send_ngiot_command(session, portal_token, "getCleanInfo")
        if not res_cln:
            res_cln = await self._send_devmanager_command(session, portal_url, user_id, "getCleanInfo")
        if res_cln:
            self._parse_clean_response(res_cln)

        # 4. Query station state
        res_stn = await self._send_ngiot_command(session, portal_token, "getStationState")
        if not res_stn:
            res_stn = await self._send_devmanager_command(session, portal_url, user_id, "getStationState")
        if res_stn:
            self._parse_station_response(res_stn)

        # 5. Query lifespan every ~60s
        now = time.time()
        if not self.state.maintenance or (now - self.state.last_updated > 60):
            res_life = await self._send_ngiot_command(
                session, portal_token, "getLifeSpan",
                {"data": ["brush", "sideBrush", "filter", "roundMop", "dustBag"]}
            )
            if not res_life:
                res_life = await self._send_devmanager_command(
                    session, portal_url, user_id, "getLifeSpan",
                    {"data": ["brush", "sideBrush", "filter", "roundMop", "dustBag"]}
                )
            if res_life:
                self._parse_lifespan_response(res_life)

        self.state.last_updated = time.time()
        self.trigger_callbacks()
        return self.state

    async def _poll_loop(self) -> None:
        """Continuous state refresh loop."""
        _LOGGER.info("Starting Ecovacs robot state poller (interval: %0.1fs)...", self.poll_interval)
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.debug("State poll loop error: %s", e)

            # Sleep adaptively: poll faster when active (5s), slower when docked (10s)
            delay = self.poll_interval if self.state.is_active else min(self.poll_interval * 2, 12.0)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
