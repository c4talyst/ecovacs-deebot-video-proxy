"""Interactive Setup Wizard for Ecovacs Deebot Video Proxy.

Authorizes this client device ID with Ecovacs, verifies email code if challenged,
discovers all vacuums on the account, and writes ready-to-run config.yaml and .env.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

import aiohttp
import yaml

from .ecovacs_auth import EcovacsAuthClient


async def run_setup() -> None:
    print("=" * 65)
    print("   ECOVACS DEEBOT VIDEO PROXY - INTERACTIVE SETUP WIZARD")
    print("=" * 65)
    print("This tool will authorize this client device with your Ecovacs account,")
    print("handle one-time email verification, discover your vacuums, and generate")
    print("your configuration files.\n")

    # Prompt for credentials
    default_country = os.environ.get("ECOVACS_COUNTRY", "za")
    username = input("Enter Ecovacs account email: ").strip()
    if not username:
        print("Error: Account email is required.")
        sys.exit(1)

    import getpass
    password = getpass.getpass("Enter Ecovacs account password: ").strip()
    if not password:
        print("Error: Password is required.")
        sys.exit(1)

    country = input(f"Enter 2-letter country code [{default_country}]: ").strip().lower() or default_country
    device_id = secrets.token_hex(16)

    print(f"\nGenerated persistent Device ID: {device_id}")
    print(f"Connecting to Ecovacs ({country})...\n")

    async with aiohttp.ClientSession() as session:
        auth = EcovacsAuthClient(
            session=session,
            username=username,
            password=password,
            country=country,
            device_id=device_id,
        )

        try:
            print("1. Attempting login...")
            await auth.authenticate()
        except RuntimeError as e:
            if "Device verification required" in str(e):
                print("\n" + "!" * 65)
                print("Ecovacs security check: Device verification required!")
                print("Requesting one-time code sent to your email...")
                await auth.request_email_verification_code()
                print("Code dispatched! Check your email inbox for a verification code.")
                print("!" * 65 + "\n")

                code = input("Enter the verification code from your email: ").strip()
                if not code:
                    print("Error: Verification code cannot be empty.")
                    sys.exit(1)

                await auth.verify_device_code(code)
                print("Device successfully verified! Continuing login...")
                await auth.authenticate()
            else:
                print(f"Error during authentication: {e}")
                sys.exit(1)

        print("\n2. Discovering vacuums on account...")
        devices = await auth.get_devices()
        if not devices:
            print("Warning: No Ecovacs robots found on this account.")
            sys.exit(1)

        print(f"Found {len(devices)} vacuum(s):")
        for i, d in enumerate(devices):
            print(f"  [{i + 1}] Name:  {d.get('name')}")
            print(f"      Model: {d.get('model')} (Class: {d.get('class')})")
            print(f"      DID:   {d.get('did')}")

        selected_did = devices[0].get("did", "")
        if len(devices) > 1:
            choice = input(f"\nSelect robot [1-{len(devices)}] (default 1): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(devices):
                selected_did = devices[int(choice) - 1].get("did", "")

        pin = input("\nEnter camera/video manager PIN (leave blank if disabled): ").strip()
        rtsp_user = input("Enter optional stream & web username (protects RTSP, WebRTC, HLS, Web UI): ").strip()
        rtsp_pass = input("Enter optional stream & web password: ").strip() if rtsp_user else ""
        # Generate config.yaml
        cfg_dict = {
            "ecovacs": {
                "username": username,
                "password": password,
                "country": country,
                "device_id": device_id,
                "access_token": auth.access_token,
                "user_id": auth.user_id,
                "pin": pin,
                "robot_did": selected_did,
            },
            "rtsp": {
                "path": "live",
                "port": 8554,
                "fps": 16,
                "bitrate": "1500k",
                "preset": "ultrafast",
                "tune": "zerolatency",
                "username": rtsp_user,
                "password": rtsp_pass,
            },
            "streamer": {
                "reconnect_delay_seconds": 5,
                "appping_interval_seconds": 35,
                "stream_timeout_seconds": 30,
                "active_only_video": False,
                "standby_fps": 2,
            },
            "overlay": {
                "enabled": True,
                "show_timestamp": True,
                "show_battery": True,
                "show_mode": True,
                "show_station": True,
                "show_maintenance": True,
                "show_robot_name": True,
                "position": "top_left",
            },
            "logging": {
                "level": "INFO",
            },
        }
        save_dir = Path("/config") if Path("/config").is_dir() else Path(".")
        cfg_file = save_dir / "config.yaml"
        env_file = save_dir / ".env"

        with open(cfg_file, "w", encoding="utf-8") as f:
            yaml.dump(cfg_dict, f, default_flow_style=False)

        # Generate .env
        env_content = f"""# Generated by Ecovacs Deebot Video Proxy Setup Wizard
ECOVACS_USERNAME="{username}"
ECOVACS_PASSWORD="{password}"
ECOVACS_COUNTRY="{country}"
ECOVACS_DEVICE_ID="{device_id}"
ECOVACS_ACCESS_TOKEN="{auth.access_token}"
ECOVACS_USER_ID="{auth.user_id}"
ECOVACS_PIN="{pin}"
ECOVACS_ROBOT_DID="{selected_did}"

RTSP_PATH="live"
RTSP_PORT=8554
RTSP_FPS=16
RTSP_BITRATE="1500k"
RTSP_USERNAME="{rtsp_user}"
RTSP_PASSWORD="{rtsp_pass}"
ACTIVE_ONLY_VIDEO="false"
OVERLAY_ENABLED="true"
OVERLAY_SHOW_TIMESTAMP="true"
OVERLAY_SHOW_BATTERY="true"
OVERLAY_SHOW_MODE="true"
OVERLAY_SHOW_STATION="true"
OVERLAY_SHOW_MAINTENANCE="true"
OVERLAY_SHOW_ROBOT_NAME="true"
OVERLAY_POSITION="top_left"
LOG_LEVEL="INFO"
"""
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(env_content)

        print("\n" + "=" * 65)
        print("SETUP COMPLETED SUCCESSFULLY!")
        print(f"Generated:\n  - {cfg_file}\n  - {env_file}")
        print("\nYou can now start the RTSP bridge with:")
        print("  docker compose up -d")
        print("or standalone with:")
        print("  python3 -m src.main")
        print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_setup())
