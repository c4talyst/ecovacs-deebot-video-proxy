"""Ecovacs AWS Kinesis WebRTC Live View Session Manager."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from types import MethodType
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

import websockets
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from av import AudioFrame

_LOGGER = logging.getLogger("ecovacs.live_view")

APP_ID = "ecovacs"
APP_VERSION = "3.14.0"
APP_CHANNEL = "google_play"
VIDEO_API_VERSION = "2.1.0"
SIGNING_PREFIX = "ecovacs2ea31cf06e6711eaa0aff7b9558a534e"
REALM = "ecouser.net"


def _signature(timestamp_ms: str) -> str:
    return hashlib.sha1(f"{SIGNING_PREFIX}{timestamp_ms}".encode("utf-8")).hexdigest()


def encode_live_view_pin(pin: str, robot: dict[str, Any]) -> str:
    """Encode the PIN using the device family's scheme."""
    if not pin:
        return ""
    model_names = (robot.get("device_name"), robot.get("model"))
    is_goat = any(
        isinstance(name, str) and name.strip().casefold().startswith("goat ")
        for name in model_names
    )
    if is_goat:
        return hashlib.sha256(f"goat_{pin}".encode("utf-8")).hexdigest()
    return hashlib.md5(f"eco_{pin}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KinesisSession:
    region: str
    channel_name: str
    client_id: str
    access_key_id: str
    secret_access_key: str
    session_token: str


class SilentAudioTrack(MediaStreamTrack):
    """Audio track transmitting silence frames to satisfy Ecovacs sendrecv expectation."""
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.sample_rate = 48_000
        self.samples_per_frame = 960
        self._timestamp = 0
        self._started_at: float | None = None

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        if self._started_at is None:
            self._started_at = loop.time()
        self._timestamp += self.samples_per_frame
        wait = self._started_at + (self._timestamp / self.sample_rate) - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)

        frame = AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)
        frame.sample_rate = self.sample_rate
        frame.pts = self._timestamp
        return frame


async def async_send_app_ping(
    session: Any,
    *,
    portal_token: str,
    robot: dict[str, Any],
) -> None:
    """Send stream-start appping command to robot's NG-IoT broker."""
    service = robot.get("service") or {}
    host = str(service.get("mqs") or "api-ngiot.dc-na.ww.ecouser.net").strip()
    service_id = secrets.token_hex(8)
    timestamp_ms = str(int(time.time() * 1000))
    params = {
        "si": service_id,
        "ct": "m",
        "eid": robot["did"],
        "et": robot["class"],
        "er": robot["resource"],
        "apn": "appping",
        "fmt": "j",
    }
    body = {
        "body": {},
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
    async with session.post(
        f"https://{host}/api/iot/endpoint/control",
        params=params,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=headers,
        timeout=20,
    ) as response:
        await response.read()


async def async_start_watch(
    session: Any,
    *,
    portal_url: str,
    device_id: str,
    user_id: str,
    portal_token: str,
    country: str,
    pin: str,
    robot: dict[str, Any],
) -> KinesisSession:
    """Request AWS Kinesis WebRTC session credentials via start_watch/v2."""
    track_id = secrets.token_hex(8)
    timestamp_ms = str(int(time.time() * 1000))

    auth = {
        "with": "users",
        "userid": user_id,
        "realm": REALM,
        "token": portal_token,
        "resource": device_id,
    }

    params = {
        "videoTrackId": track_id,
        "lang": "en",
        "plat": "Android",
        "av": VIDEO_API_VERSION,
        "did": robot["did"],
        "mid": robot["class"],
        "res": robot["resource"],
        "pwd": encode_live_view_pin(pin, robot),
        "auth": json.dumps(auth, separators=(",", ":")),
        "channel": APP_CHANNEL,
    }

    headers = {
        "country": country,
        "Accept": "application/json",
        "sign": _signature(timestamp_ms),
        "userid": user_id,
        "token": portal_token,
        "Authorization": f"Bearer {portal_token}",
        "v": APP_VERSION,
        "appid": APP_ID,
        "plat": "android",
        "lang": "EN",
        "Content-Type": "application/json",
        "ts": timestamp_ms,
    }

    url = f"{portal_url.rstrip('/')}/api/appsvr/akvs/start_watch/v2"

    async with session.get(url, params=params, headers=headers, timeout=30) as response:
        raw = await response.text()
        status = response.status

    if status >= 400:
        raise RuntimeError(f"ECOVACS start_watch failed with HTTP {status}")

    data = json.loads(raw)
    if str(data.get("ret", "")).lower() != "ok" or not isinstance(data.get("credentials"), dict):
        raise RuntimeError(f"ECOVACS start_watch error: {data}")

    creds = data["credentials"]
    return KinesisSession(
        region=str(data["region"]),
        channel_name=str(data["channel"]),
        client_id=str(data["client_id"]),
        access_key_id=str(creds["AccessKeyId"]),
        secret_access_key=str(creds["SecretAccessKey"]),
        session_token=str(creds["SessionToken"]),
    )


# --- AWS SigV4 Helpers ---

def _aws_quote(value: str) -> str:
    return quote(value, safe="-_.~")


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _sigv4_headers(
    info: KinesisSession,
    *,
    method: str,
    url: str,
    service: str,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    parsed = urlparse(url)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    canonical_query = "&".join(f"{_aws_quote(str(k))}={_aws_quote(str(v))}" for k, v in query_pairs)

    signing_headers: dict[str, str] = {
        "host": parsed.netloc,
        "x-amz-date": amz_date,
        "x-amz-security-token": info.session_token,
    }
    if extra_headers:
        signing_headers.update({k.lower(): str(v).strip() for k, v in extra_headers.items()})

    names = sorted(signing_headers)
    canonical_headers = "".join(f"{name}:{' '.join(signing_headers[name].split())}\n" for name in names)
    signed_headers = ";".join(names)
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_request = "\n".join([method.upper(), parsed.path or "/", canonical_query, canonical_headers, signed_headers, payload_hash])
    scope = f"{date_stamp}/{info.region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    signature = hmac.new(_signing_key(info.secret_access_key, date_stamp, info.region, service), string_to_sign.encode(), hashlib.sha256).hexdigest()

    output = {
        "Host": parsed.netloc,
        "X-Amz-Date": amz_date,
        "X-Amz-Security-Token": info.session_token,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={info.access_key_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
    }
    if extra_headers:
        output.update(extra_headers)
    return output


async def _aws_json_post(http: Any, info: KinesisSession, *, url: str, payload: dict[str, Any], service: str) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    extra = {"Content-Type": "application/json"}
    headers = _sigv4_headers(info, method="POST", url=url, service=service, body=body, extra_headers=extra)
    async with http.post(url, data=body, headers=headers, timeout=30) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"AWS {service} request failed HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


def _signed_wss(info: KinesisSession, arn: str, endpoint: str) -> str:
    parsed = urlparse(endpoint)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    service = "kinesisvideo"
    scope = f"{date_stamp}/{info.region}/{service}/aws4_request"

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-ChannelARN": arn,
        "X-Amz-ClientId": info.client_id,
        "X-Amz-Credential": f"{info.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": "299",
        "X-Amz-Security-Token": info.session_token,
        "X-Amz-SignedHeaders": "host",
    })

    canonical_query = "&".join(f"{_aws_quote(str(k))}={_aws_quote(str(v))}" for k, v in sorted(params.items()))
    canonical_request = "\n".join(["GET", parsed.path or "/", canonical_query, f"host:{parsed.netloc}\n", "host", hashlib.sha256(b"").hexdigest()])
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    signature = hmac.new(_signing_key(info.secret_access_key, date_stamp, info.region, service), string_to_sign.encode(), hashlib.sha256).hexdigest()

    return urlunparse(("wss", parsed.netloc, parsed.path, parsed.params, f"{canonical_query}&X-Amz-Signature={signature}", parsed.fragment))


# --- SDP & Candidate Formatting ---

def _encode(value: str) -> str:
    return base64.b64encode(value.encode()).decode("ascii")


def _decode(value: str) -> str:
    return base64.b64decode(value).decode("utf-8", errors="replace")


def _message(action: str, payload: str, correlation_id: str | None = None) -> str:
    data = {"action": action, "recipientClientId": "MASTER", "messagePayload": _encode(payload)}
    if correlation_id:
        data["correlationId"] = correlation_id
    return json.dumps(data, separators=(",", ":"))

async def _wait_ice(pc: RTCPeerConnection, timeout: float = 20) -> None:
    if pc.iceGatheringState == "complete":
        return
    event = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def _changed() -> None:
        if pc.iceGatheringState == "complete":
            event.set()

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("ICE gathering timed out after %ds; continuing with available candidates", int(timeout))



def _rewrite_offer(sdp: str) -> str:
    lines = sdp.replace("\r\n", "\n").split("\n")
    session_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    kind: str | None = None
    body: list[str] = []

    for line in lines:
        if line.startswith("m="):
            if kind is not None:
                sections.append((kind, body))
            kind = line.split("=", 1)[1].split(" ", 1)[0]
            body = [line]
        elif kind is None:
            if line:
                session_lines.append(line)
        else:
            body.append(line)
    if kind is not None:
        sections.append((kind, body))

    if "a=extmap-allow-mixed" not in session_lines:
        session_lines.append("a=extmap-allow-mixed")
    if "a=msid-semantic: WMS" not in session_lines:
        session_lines.append("a=msid-semantic: WMS")

    out_sections: list[list[str]] = []
    for skind, sbody in sections:
        nb: list[str] = []
        for line in sbody:
            if line.startswith("a=rtcp-rsize"):
                continue
            if line.startswith("a=extmap:") and "urn:ietf:params:rtp-hdrext:sdes:mid" in line:
                nb.append(line)
                nb.append("a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time")
                continue
            nb.append(line)
        if skind == "audio" and not any(l.startswith("a=msid:") for l in nb):
            nb.append("a=msid:KvsLocalMediaStream KvsAudioTrack")
        out_sections.append(nb)

    out_lines = list(session_lines)
    for s in out_sections:
        out_lines.extend(s)
    return "\r\n".join(out_lines).rstrip("\r\n") + "\r\n"


def _ensure_ice_options(lines: list[str]) -> list[str]:
    copy = list(lines)
    if any(line.startswith("a=ice-options:") for line in copy):
        return [
            "a=ice-options:trickle renomination" if l.startswith("a=ice-options:") else l
            for l in copy
        ]
    idx = next((i + 1 for i, l in enumerate(copy) if l.startswith("a=ice-pwd:")), len(copy))
    copy.insert(idx, "a=ice-options:trickle renomination")
    return copy


def _trickle_offer(gathered_sdp: str, audio_ssrc: int, cname: str) -> tuple[str, list[tuple[str, int, str]]]:
    source = gathered_sdp.replace("\r\n", "\n").split("\n")
    candidates: list[tuple[str, int, str]] = []
    mid: str | None = None
    mline = -1
    for line in source:
        if line.startswith("m="):
            mline += 1
            mid = None
        elif line.startswith("a=mid:"):
            mid = line.split(":", 1)[1]
        elif line.startswith("a=candidate:"):
            candidates.append((mid or str(mline), mline, line[2:]))

    lines = [l for l in source if l and not l.startswith("a=candidate:") and l != "a=end-of-candidates"]
    rebuilt: list[str] = []
    kind: str | None = None
    ssrc_added = False
    for line in lines:
        if line.startswith("m="):
            kind = line.split("=", 1)[1].split(" ", 1)[0]
            rebuilt.append(line)
            continue
        if line.startswith("a=ice-options:"):
            rebuilt.append("a=ice-options:trickle renomination")
            continue
        if line.startswith("a=fingerprint:"):
            if "sha-256" in line.lower():
                rebuilt.append(line)
            continue
        rebuilt.append(line)
        if kind == "audio" and line == "a=msid:KvsLocalMediaStream KvsAudioTrack" and not ssrc_added:
            rebuilt.append(f"a=ssrc:{audio_ssrc} cname:{cname}")
            rebuilt.append(f"a=ssrc:{audio_ssrc} msid:KvsLocalMediaStream KvsAudioTrack")
            ssrc_added = True

    final: list[str] = []
    session: list[str] = []
    section: list[str] = []
    for line in rebuilt:
        if line.startswith("m="):
            if section:
                final.extend(_ensure_ice_options(section))
            else:
                final.extend(session)
            section = [line]
        elif section:
            section.append(line)
        else:
            session.append(line)
    if section:
        final.extend(_ensure_ice_options(section))
    else:
        final.extend(session)

    return "\r\n".join(final).rstrip("\r\n") + "\r\n", candidates


def _candidate_payload(candidate: str, mid: str, index: int) -> str:
    return json.dumps({"candidate": candidate, "sdpMid": mid, "sdpMLineIndex": index}, separators=(",", ":"))


class EcovacsWebRTCSession:
    """Manages active WebRTC connection to Ecovacs vacuum camera."""

    _OPUS_SILENCE = b"\xF8\xFF\xFE"

    def __init__(
        self,
        http_session: Any,
        session_info: KinesisSession,
        robot: dict[str, Any],
        on_video_frame: Callable[[Any], None] | None = None,
    ) -> None:
        self.http = http_session
        self.session_info = session_info
        self.robot = robot
        self.on_video_frame = on_video_frame

        self.pc: RTCPeerConnection | None = None
        self.websocket: Any = None
        self.silent_audio = SilentAudioTrack()
        self.stop_event = asyncio.Event()
        self.first_frame_event = asyncio.Event()
        self.video_frames = 0
        self.audio_frames = 0
        self.status = "starting"
        self.error: str | None = None
        self._media_tasks: list[asyncio.Task[Any]] = []
        self._receive_task: asyncio.Task[Any] | None = None
        self._opus_count = 0
        self._wrapped: set[int] = set()
        self._cname = uuid.uuid4().hex[:16]

    def _install_opus_rewriter(self) -> None:
        if self.pc is None:
            return
        transceiver = next((item for item in self.pc.getTransceivers() if item.kind == "audio"), None)
        if transceiver is None:
            return
        transport = getattr(transceiver.sender, "transport", None)
        if transport is None or id(transport) in self._wrapped:
            return
        original = getattr(transport, "_send_rtp", None)
        if original is None:
            return
        viewer = self

        async def rewritten(transport_self: Any, data: bytes, *, _original: Any = original) -> Any:
            raw = bytes(data)
            if len(raw) >= 12 and (raw[0] >> 6) == 2 and (raw[1] & 0x7F) == 111 and not (192 <= raw[1] <= 223):
                payload = viewer._OPUS_SILENCE
                viewer._opus_count += 1
                header = bytearray(raw[:12])
                header[0] = 0x80
                header[1] = 0x6F
                if viewer._opus_count == 1:
                    header[1] |= 0x80
                return await _original(bytes(header) + payload)
            return await _original(raw)

        transport._send_rtp = MethodType(rewritten, transport)
        self._wrapped.add(id(transport))

    async def _consume_video(self, track: Any) -> None:
        try:
            while not self.stop_event.is_set():
                frame = await track.recv()
                self.video_frames += 1
                if self.video_frames == 1:
                    self.status = "streaming"
                    self.first_frame_event.set()
                    _LOGGER.info("WebRTC live stream established! First frame received.")

                if self.on_video_frame:
                    self.on_video_frame(frame)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = str(exc)
                self.status = "error"
                _LOGGER.error("WebRTC video track receiver error: %s", exc)
                self.stop_event.set()

    async def _consume_audio(self, track: Any) -> None:
        try:
            while not self.stop_event.is_set():
                await track.recv()
                self.audio_frames += 1
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_signaling_msg(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            message = json.loads(raw)
        except Exception:
            return

        kind = str(message.get("messageType") or message.get("action") or "").upper()
        encoded = str(message.get("messagePayload") or "")

        if not encoded:
            if kind == "GO_AWAY":
                _LOGGER.warning("Signaling received GO_AWAY from robot")
                self.stop_event.set()
            return

        payload = _decode(encoded)

        if kind == "SDP_ANSWER" and self.pc is not None:
            answer = json.loads(payload)
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=str(answer["sdp"]), type=str(answer.get("type", "answer")))
            )
            _LOGGER.debug("WebRTC remote SDP answer applied successfully")
        elif kind == "GO_AWAY":
            _LOGGER.warning("Signaling received GO_AWAY from robot")
            self.stop_event.set()

    async def start(self, timeout: float = 30) -> bool:
        """Establish WebRTC session with the robot."""
        _LOGGER.debug("Describing AWS Kinesis signaling channel...")
        arn_res = await _aws_json_post(
            self.http, self.session_info,
            url=f"https://kinesisvideo.{self.session_info.region}.amazonaws.com/describeSignalingChannel",
            payload={"ChannelName": self.session_info.channel_name},
            service="kinesisvideo",
        )
        arn = str(arn_res["ChannelInfo"]["ChannelARN"])

        ep_res = await _aws_json_post(
            self.http, self.session_info,
            url=f"https://kinesisvideo.{self.session_info.region}.amazonaws.com/getSignalingChannelEndpoint",
            payload={
                "ChannelARN": arn,
                "SingleMasterChannelEndpointConfiguration": {"Protocols": ["WSS", "HTTPS"], "Role": "VIEWER"},
            },
            service="kinesisvideo",
        )
        endpoints = {item["Protocol"]: item["ResourceEndpoint"] for item in ep_res["ResourceEndpointList"]}
        wss_endpoint, https_endpoint = endpoints["WSS"], endpoints["HTTPS"]

        ice_res = await _aws_json_post(
            self.http, self.session_info,
            url=f"{https_endpoint.rstrip('/')}/v1/get-ice-server-config",
            payload={"ChannelARN": arn, "ClientId": self.session_info.client_id, "Service": "TURN"},
            service="kinesisvideo",
        )
        ice_servers = [RTCIceServer(urls=[f"stun:stun.kinesisvideo.{self.session_info.region}.amazonaws.com:443"])]
        for s in ice_res.get("IceServerList", []):
            if s.get("Uris"):
                ice_servers.append(RTCIceServer(urls=[str(u) for u in s["Uris"]], username=s.get("Username"), credential=s.get("Password")))

        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        setattr(self.pc, "_RTCPeerConnection__cname", self._cname)

        audio = self.pc.addTransceiver(self.silent_audio, direction="sendrecv")
        video = self.pc.addTransceiver("video", direction="recvonly")

        # Set H.264 preference
        capabilities = RTCRtpSender.getCapabilities("video")
        h264 = [c for c in capabilities.codecs if c.mimeType.lower() == "video/h264"]
        if h264:
            video.setCodecPreferences(h264)
        # Opus rewriter will be installed once DTLS transport is bound in setLocalDescription

        @self.pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind == "video":
                self._media_tasks.append(asyncio.create_task(self._consume_video(track)))
            elif track.kind == "audio":
                self._media_tasks.append(asyncio.create_task(self._consume_audio(track)))

        @self.pc.on("connectionstatechange")
        async def _connection_state() -> None:
            if self.pc:
                _LOGGER.debug("WebRTC peer connection state: %s", self.pc.connectionState)
                if self.pc.connectionState in {"failed", "closed"}:
                    self.stop_event.set()

        # Connect WebSocket
        signed_wss = _signed_wss(self.session_info, arn, wss_endpoint)
        self.websocket = await websockets.connect(
            signed_wss, ping_interval=20, ping_timeout=20, close_timeout=10, max_size=8 * 1024 * 1024
        )

        # Generate & send offer
        offer = await self.pc.createOffer()
        offer = RTCSessionDescription(sdp=_rewrite_offer(offer.sdp), type=offer.type)
        await self.pc.setLocalDescription(offer)
        self._install_opus_rewriter()
        await _wait_ice(self.pc)

        local = self.pc.localDescription
        if not local:
            raise RuntimeError("WebRTC local offer generation failed")

        audio_ssrc = int(getattr(audio.sender, "_ssrc"))
        trickle_sdp, candidates = _trickle_offer(local.sdp, audio_ssrc, self._cname)
        offer_payload = json.dumps({"type": local.type, "sdp": trickle_sdp}, separators=(",", ":"))

        await self.websocket.send(_message("SDP_OFFER", offer_payload, correlation_id=str(uuid.uuid4())))

        sent: set[tuple[str, int, str]] = set()
        for mid, index, candidate in candidates:
            key = (mid, index, candidate)
            if key not in sent:
                sent.add(key)
                await self.websocket.send(_message("ICE_CANDIDATE", _candidate_payload(candidate, mid, index)))

        async def _recv_loop() -> None:
            try:
                async for incoming in self.websocket:
                    await self._handle_signaling_msg(incoming)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if not self.stop_event.is_set():
                    _LOGGER.warning("Signaling loop closed: %s", e)
                    self.stop_event.set()

        self._receive_task = asyncio.create_task(_recv_loop())

        try:
            await asyncio.wait_for(self.first_frame_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self.status = "timeout"
            return False

    async def stop(self) -> None:
        self.stop_event.set()
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        for task in self._media_tasks:
            task.cancel()
        self._media_tasks.clear()

        try:
            self.silent_audio.stop()
        except Exception:
            pass

        if self.pc:
            await self.pc.close()
            self.pc = None

        if self.websocket:
            await self.websocket.close()
            self.websocket = None
