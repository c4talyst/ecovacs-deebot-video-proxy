"""On-Screen Display (OSD) and Video Overlay Engine.

Renders real-time telemetry, battery, cleaning mode, station status,
and maintenance alerts onto RTSP stream frames and Web UI feeds.
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import time
from typing import Any

from av import VideoFrame
from PIL import Image, ImageDraw, ImageFont

from .config import OverlayConfig
from .robot_state import RobotState

_LOGGER = logging.getLogger("ecovacs.overlay")


def _get_font(size: int = 15) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Attempt to load a crisp TrueType font, falling back to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/SFNSMono.ttf",
    ]
    for p in font_paths:
        if Path(p).is_file():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


class StreamOverlay:
    """Manages real-time on-screen telemetry overlay on video frames."""

    def __init__(self, config: OverlayConfig) -> None:
        self.config = config
        self._font_14 = _get_font(14)
        self._font_16 = _get_font(16)
        self._font_22 = _get_font(22)
        self._standby_cache_time: float = 0.0
        self._cached_standby_frame: VideoFrame | None = None

    def update_config(self, new_config: OverlayConfig) -> None:
        self.config = new_config

    def _format_clean_status(self, status: str) -> str:
        s = (status or "idle").lower().replace("_", " ")
        if s in ("clean", "cleaning"):
            return "Cleaning"
        if s in ("dock", "docked"):
            return "Docked"
        if s in ("return", "returning"):
            return "Returning to Dock"
        if s in ("charge", "charging"):
            return "Charging"
        if s in ("pause", "paused"):
            return "Paused"
        if s in ("stop", "stopped"):
            return "Stopped"
        return s.title()

    def _format_station_status(self, status: str) -> str:
        s = (status or "idle").lower().replace("_", " ")
        if "wash" in s:
            return "Washing Mop"
        if "dry" in s:
            return "Drying Mop"
        if "empty" in s or "dust" in s:
            return "Emptying Dustbin"
        if s in ("idle", "none", "standby"):
            return "Idle"
        return s.title()

    def render_overlay(
        self,
        frame: VideoFrame,
        state: RobotState,
        robot_name: str = "Deebot Vacuum",
    ) -> VideoFrame:
        """Render active telemetry badges on live camera frame."""
        if not self.config.enabled:
            return frame

        # Gather enabled chips
        chips: list[tuple[str, tuple[int, int, int]]] = []

        # Hardware & station malfunction alerts (shown with highest priority)
        if state.error_alerts:
            for alert in state.error_alerts[:2]:
                chips.append((f"🚨 {alert}", (248, 113, 113)))
        if self.config.show_timestamp:
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            chips.append((ts_str, (220, 225, 235)))

        if self.config.show_robot_name and robot_name:
            chips.append((f"🤖 {robot_name}", (147, 197, 253)))

        if self.config.show_battery:
            chg = " ⚡" if state.is_charging else ""
            bat_color = (74, 222, 128) if state.battery >= 40 else (250, 204, 21) if state.battery >= 20 else (248, 113, 113)
            chips.append((f"🔋 {state.battery}%{chg}", bat_color))

        if self.config.show_mode:
            clean_str = self._format_clean_status(state.clean_status)
            color = (56, 189, 248) if "Clean" in clean_str else (167, 139, 250) if "Return" in clean_str else (156, 163, 175)
            chips.append((f"Mode: {clean_str}", color))

        if self.config.show_station and state.station_status:
            stn_str = self._format_station_status(state.station_status)
            stn_color = (244, 114, 182) if stn_str != "Idle" else (148, 163, 184)
            chips.append((f"Station: {stn_str}", stn_color))

        if self.config.show_maintenance:
            if state.maintenance_alerts:
                alert_text = "⚠️ " + ", ".join(state.maintenance_alerts[:2])
                chips.append((alert_text, (251, 146, 60)))
            elif state.maintenance:
                # Show lowest consumable
                lowest_item = min(state.maintenance.items(), key=lambda x: x[1])
                chips.append((f"Maint: {lowest_item[0]} {lowest_item[1]}%", (148, 163, 184)))

        if not chips:
            return frame

        img = frame.to_image()
        draw = ImageDraw.Draw(img)
        width, height = img.size
        font = self._font_14

        # Compute chip dimensions
        rendered_chips: list[tuple[str, tuple[int, int, int], int, int]] = []
        pad_x, pad_y = 10, 5
        total_width = 0

        for text, color in chips:
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            chip_w = tw + pad_x * 2
            rendered_chips.append((text, color, chip_w, th))
            total_width += chip_w + 8

        pos = (self.config.position or "top_left").lower()

        if pos == "top_banner":
            # Draw semi-transparent header bar across full width
            banner_height = 36
            overlay_layer = Image.new("RGBA", (width, banner_height), (15, 18, 28, 210))
            img.paste(overlay_layer, (0, 0), overlay_layer)
            draw = ImageDraw.Draw(img)

            curr_x = 16
            for text, color, chip_w, th in rendered_chips:
                draw.text((curr_x, 9), text, fill=color, font=font)
                curr_x += chip_w + 4

        else:
            # Determine start anchor
            if pos == "top_right":
                curr_x = max(16, width - total_width - 16)
                curr_y = 16
            elif pos == "bottom_left":
                curr_x = 16
                curr_y = height - 42
            elif pos == "bottom_right":
                curr_x = max(16, width - total_width - 16)
                curr_y = height - 42
            else:  # top_left default
                curr_x = 16
                curr_y = 16

            # Draw background capsule
            capsule_h = 28
            capsule_w = total_width - 8
            # Semi-transparent dark pill
            pill = Image.new("RGBA", (capsule_w, capsule_h), (14, 18, 28, 210))
            pill_draw = ImageDraw.Draw(pill)
            pill_draw.rounded_rectangle([0, 0, capsule_w - 1, capsule_h - 1], radius=6, outline=(42, 49, 70, 255), width=1)
            img.paste(pill, (curr_x, curr_y), pill)
            draw = ImageDraw.Draw(img)

            tx = curr_x + pad_x
            for text, color, chip_w, th in rendered_chips:
                draw.text((tx, curr_y + 6), text, fill=color, font=font)
                tx += chip_w - pad_x + 4

        return VideoFrame.from_image(img)

    def generate_standby_frame(
        self,
        width: int = 1280,
        height: int = 720,
        state: RobotState | None = None,
        robot_name: str = "Deebot Vacuum",
    ) -> VideoFrame:
        """Render a high-tech standby graphic card when robot is docked and camera is powered down."""
        st = state or RobotState()

        # Cache standby frame for 1 second to eliminate redundant rendering
        now = time.monotonic()
        if self._cached_standby_frame and (now - self._standby_cache_time < 0.9):
            return self._cached_standby_frame

        img = Image.new("RGB", (width, height), (11, 14, 21))
        draw = ImageDraw.Draw(img)

        # Subtle background grid & card
        card_w, card_h = 760, 420
        cx = (width - card_w) // 2
        cy = (height - card_h) // 2

        # Card container
        draw.rounded_rectangle(
            [cx, cy, cx + card_w, cy + card_h],
            radius=16,
            fill=(20, 24, 38),
            outline=(42, 52, 74),
            width=2,
        )

        # Top Accent Header Bar
        draw.rounded_rectangle(
            [cx + 2, cy + 2, cx + card_w - 2, cy + 46],
            radius=14,
            fill=(26, 32, 50),
        )
        draw.text(
            (cx + 24, cy + 14),
            f"ECOVACS DEEBOT VIDEO PROXY • {robot_name.upper()}",
            fill=(147, 197, 253),
            font=self._font_14,
        )

        # Clock
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.text(
            (cx + card_w - 180, cy + 14),
            ts,
            fill=(156, 163, 175),
            font=self._font_14,
        )

        # Standby Title
        status_label = "ROBOT DOCKED & CHARGING" if st.is_charging else "ROBOT DOCKED (STANDBY)"
        status_color = (74, 222, 128) if st.is_charging else (96, 165, 250)
        draw.text(
            (cx + 40, cy + 75),
            status_label,
            fill=status_color,
            font=self._font_22,
        )

        # Subtitle Explanation or Active Malfunction Alert
        if st.error_alerts:
            alert_text = " • ".join(st.error_alerts).upper()
            draw.rounded_rectangle([cx + 40, cy + 110, cx + card_w - 40, cy + 152], radius=8, fill=(80, 20, 25), outline=(239, 68, 68), width=1)
            draw.text((cx + 56, cy + 122), f"🚨 ALERT: {alert_text}", fill=(254, 202, 202), font=self._font_16)
        else:
            draw.text(
                (cx + 40, cy + 112),
                "Camera and headlights are powered down to conserve energy and prevent night glare.",
                fill=(156, 163, 175),
                font=self._font_14,
            )
            draw.text(
                (cx + 40, cy + 132),
                "Live video resumes automatically whenever cleaning or station tasks activate.",
                fill=(107, 114, 128),
                font=self._font_14,
            )
        # Grid of Stats
        grid_y = cy + 175

        # 1. Battery Box
        draw.rounded_rectangle([cx + 40, grid_y, cx + 250, grid_y + 90], radius=10, fill=(15, 18, 28), outline=(36, 44, 62))
        draw.text((cx + 54, grid_y + 14), "BATTERY LEVEL", fill=(156, 163, 175), font=self._font_14)
        chg_tag = " [Charging]" if st.is_charging else ""
        draw.text((cx + 54, grid_y + 40), f"{st.battery}%{chg_tag}", fill=(74, 222, 128), font=self._font_22)

        # 2. Station State Box
        draw.rounded_rectangle([cx + 270, grid_y, cx + 480, grid_y + 90], radius=10, fill=(15, 18, 28), outline=(36, 44, 62))
        draw.text((cx + 284, grid_y + 14), "STATION STATUS", fill=(156, 163, 175), font=self._font_14)
        draw.text((cx + 284, grid_y + 42), self._format_station_status(st.station_status), fill=(244, 114, 182), font=self._font_16)

        # 3. Clean State Box
        draw.rounded_rectangle([cx + 500, grid_y, cx + 710, grid_y + 90], radius=10, fill=(15, 18, 28), outline=(36, 44, 62))
        draw.text((cx + 514, grid_y + 14), "ROBOT MODE", fill=(156, 163, 175), font=self._font_14)
        draw.text((cx + 514, grid_y + 42), self._format_clean_status(st.clean_status), fill=(56, 189, 248), font=self._font_16)

        # Maintenance items row
        maint_y = grid_y + 110
        draw.rounded_rectangle([cx + 40, maint_y, cx + 710, maint_y + 75], radius=10, fill=(15, 18, 28), outline=(36, 44, 62))
        draw.text((cx + 54, maint_y + 10), "CONSUMABLE LIFESPAN", fill=(156, 163, 175), font=self._font_14)

        maint_items = st.maintenance or {"Filter": 88, "Main Brush": 92, "Side Brush": 85, "Mop Pads": 95}
        mx = cx + 54
        for name, pct in list(maint_items.items())[:4]:
            val_color = (74, 222, 128) if pct > 20 else (248, 113, 113)
            draw.text((mx, maint_y + 38), f"{name}: {pct}%", fill=val_color, font=self._font_14)
            mx += 165

        # Footer Status bar
        draw.text(
            (cx + 40, cy + card_h - 32),
            "● RTSP STREAM ACTIVE (MediaMTX 8554)  |  Web UI & MJPEG Live  |  Waiting for Robot Activity...",
            fill=(59, 130, 246),
            font=self._font_14,
        )

        frame = VideoFrame.from_image(img)
        self._cached_standby_frame = frame
        self._standby_cache_time = now
        return frame
