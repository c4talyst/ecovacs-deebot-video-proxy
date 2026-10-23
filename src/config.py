"""Configuration loader for Ecovacs Deebot Video Proxy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EcovacsConfig:
    username: str = ""
    password: str = ""
    country: str = "za"
    device_id: str = ""
    access_token: str = ""
    user_id: str = ""
    pin: str = ""
    robot_did: str = ""


@dataclass
class RtspConfig:
    path: str = "live"
    port: int = 8554
    fps: int = 16
    bitrate: str = "1500k"
    preset: str = "ultrafast"
    tune: str = "zerolatency"
    username: str = ""
    password: str = ""

@dataclass
class StreamerConfig:
    reconnect_delay_seconds: int = 5
    appping_interval_seconds: int = 35
    stream_timeout_seconds: int = 30


@dataclass
class AppConfig:
    ecovacs: EcovacsConfig = field(default_factory=EcovacsConfig)
    rtsp: RtspConfig = field(default_factory=RtspConfig)
    streamer: StreamerConfig = field(default_factory=StreamerConfig)
    log_level: str = "INFO"


def _load_env_file(env_path: Path) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    if not env_path.is_file():
        return env_vars
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip("\"' \t\r\n")
    except Exception:
        pass
    return env_vars


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML file and environment variables."""
    raw_cfg: dict[str, Any] = {}

    # 1. Try loading config.yaml
    paths_to_try = [
        Path(config_path) if config_path else None,
        Path("/config/config.yaml"),
        Path("config/config.yaml"),
        Path("config.yaml"),
    ]
    for p in paths_to_try:
        if p and p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                    if isinstance(loaded, dict):
                        raw_cfg = loaded
                        break
            except Exception:
                pass

    # 2. Try loading .env
    env_file = _load_env_file(Path("/config/.env")) or _load_env_file(Path(".env"))
    def get_val(env_key: str, yaml_section: str, yaml_key: str, default: Any) -> Any:
        # Env var in os.environ > .env file > YAML config > default
        if env_key in os.environ and os.environ[env_key].strip():
            return os.environ[env_key].strip()
        if env_key in env_file and env_file[env_key].strip():
            return env_file[env_key].strip()
        section = raw_cfg.get(yaml_section)
        if isinstance(section, dict) and yaml_key in section and section[yaml_key] is not None:
            return section[yaml_key]
        return default

    eco = EcovacsConfig(
        username=str(get_val("ECOVACS_USERNAME", "ecovacs", "username", "")),
        password=str(get_val("ECOVACS_PASSWORD", "ecovacs", "password", "")),
        country=str(get_val("ECOVACS_COUNTRY", "ecovacs", "country", "za")).lower(),
        device_id=str(get_val("ECOVACS_DEVICE_ID", "ecovacs", "device_id", "")),
        access_token=str(get_val("ECOVACS_ACCESS_TOKEN", "ecovacs", "access_token", "")),
        user_id=str(get_val("ECOVACS_USER_ID", "ecovacs", "user_id", "")),
        pin=str(get_val("ECOVACS_PIN", "ecovacs", "pin", "")),
        robot_did=str(get_val("ECOVACS_ROBOT_DID", "ecovacs", "robot_did", "")),
    )

    rtsp = RtspConfig(
        path=str(get_val("RTSP_PATH", "rtsp", "path", "live")).strip("/"),
        port=int(get_val("RTSP_PORT", "rtsp", "port", 8554)),
        fps=int(get_val("RTSP_FPS", "rtsp", "fps", 16)),
        bitrate=str(get_val("RTSP_BITRATE", "rtsp", "bitrate", "1500k")),
        preset=str(get_val("RTSP_PRESET", "rtsp", "preset", "ultrafast")),
        tune=str(get_val("RTSP_TUNE", "rtsp", "tune", "zerolatency")),
        username=str(get_val("RTSP_USERNAME", "rtsp", "username", "")),
        password=str(get_val("RTSP_PASSWORD", "rtsp", "password", "")),
    )
    streamer = StreamerConfig(
        reconnect_delay_seconds=int(get_val("RECONNECT_DELAY_SECONDS", "streamer", "reconnect_delay_seconds", 5)),
        appping_interval_seconds=int(get_val("APPPING_INTERVAL_SECONDS", "streamer", "appping_interval_seconds", 35)),
        stream_timeout_seconds=int(get_val("STREAM_TIMEOUT_SECONDS", "streamer", "stream_timeout_seconds", 30)),
    )

    log_level = str(get_val("LOG_LEVEL", "logging", "level", "INFO")).upper()

    return AppConfig(
        ecovacs=eco,
        rtsp=rtsp,
        streamer=streamer,
        log_level=log_level,
    )
