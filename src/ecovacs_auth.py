"""Ecovacs Cloud Authentication and Device Discovery Client."""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import hashlib
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()
_LOGGER = logging.getLogger("ecovacs.auth")

_CLIENT_KEY = "1520391301804"
_CLIENT_SECRET = "6c319b2a5cd3e66e39159c2e28f2fce9"
_AUTH_CLIENT_KEY = "1520391491841"
_AUTH_CLIENT_SECRET = "77ef58ce3afbe337da74aa8c5ab963a9"


@dataclass
class AuthTokens:
    user_id: str          # Portal user_id (for start_watch and devmanager)
    account_user_id: str  # Account user_id (for getAuthCode and config persistence)
    portal_token: str
    access_token: str
    device_id: str


class EcovacsAuthClient:
    """Handles Ecovacs OpenAPI authentication and device list discovery."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        country: str = "za",
        device_id: str = "",
        access_token: str = "",
        user_id: str = "",
    ):
        self.session = session
        self.username = username
        self.password = password
        self.country = country.lower()
        self.device_id = device_id or secrets.token_hex(16)
        self.access_token = access_token
        self.user_id = user_id
        self.portal_user_id: str | None = None
        self.portal_token: str | None = None
        self.portal_url = "https://portal-ww.ecouser.net"

    def _sign_api(self, params: dict[str, Any], meta: dict[str, Any], key: str, secret: str) -> dict[str, Any]:
        sign_data = {**meta, **params}
        sorted_keys = sorted(sign_data.keys())
        sign_str = key + "".join(f"{k}={sign_data[k]}" for k in sorted_keys) + secret
        signature = md5(sign_str)
        return {**params, "authSign": signature, "authAppkey": key}

    async def _call_private_api(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        meta = {
            "lang": "EN",
            "appCode": "global_e",
            "appVersion": "3.14.0",
            "channel": "google_play",
            "deviceType": "1",
            "country": self.country,
            "deviceId": self.device_id,
        }
        url = (
            f"https://gl-{self.country}-api.ecovacs.com/v1/private/"
            f"{self.country}/EN/{self.device_id}/global_e/3.14.0/google_play/1/{endpoint}"
        )
        now = time.time()
        req_meta = {
            "requestId": md5(str(now)),
            "authTimespan": int(now * 1000),
            "authTimeZone": "GMT-8",
            **params,
        }
        signed = self._sign_api(req_meta, meta, _CLIENT_KEY, _CLIENT_SECRET)
        async with self.session.get(url, params=signed) as r:
            data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected response from {endpoint}: {data}")
            return data

    async def request_email_verification_code(self) -> dict[str, Any]:
        """Request a one-time verification code sent to the account email address."""
        _LOGGER.info("Fetching Ecovacs public key for account encryption...")
        cfg = await self._call_private_api("common/getConfig", {"keys": "PUBLIC.KEY.CONFIG"})
        pk_val = json.loads(cfg["data"][0]["value"])["publicKey"]
        pk_der = base64.b64decode(pk_val)
        pub_key = load_der_public_key(pk_der)

        enc_user = base64.b64encode(pub_key.encrypt(self.username.encode(), padding.PKCS1v15())).decode()

        _LOGGER.info("Dispatching email verification code request...")
        res = await self._call_private_api(
            "user/sendEmailVerifyCode",
            {
                "encryptEmail": enc_user,
                "verifyType": "EMAIL_VERIFY_DEVICE",
                "supportChar": "N",
                "isForce": "N",
            },
        )
        if res.get("code") != "0000":
            raise RuntimeError(f"Failed to request verification code: {res}")
        return res

    async def verify_device_code(self, code: str) -> tuple[str, str]:
        """Verify the email one-time code to authorize this device_id permanently."""
        _LOGGER.info("Verifying device ID with code: %s...", code.strip())
        cfg = await self._call_private_api("common/getConfig", {"keys": "PUBLIC.KEY.CONFIG"})
        pk_val = json.loads(cfg["data"][0]["value"])["publicKey"]
        pk_der = base64.b64decode(pk_val)
        pub_key = load_der_public_key(pk_der)
        enc_user = base64.b64encode(pub_key.encrypt(self.username.encode(), padding.PKCS1v15())).decode()

        res = await self._call_private_api(
            "user/verifyDevice",
            {
                "encryptAccount": enc_user,
                "backUpEmail": "",
                "verifyCode": code.strip(),
                "model": "Pixel 7",
                "system": "Android 14",
            },
        )
        if res.get("code") != "0000":
            raise RuntimeError(f"Device verification failed: {res}")

        data = res["data"]
        self.user_id = str(data["uid"])
        self.access_token = str(data["accessToken"])
        _LOGGER.info("Device successfully verified! UID: %s", self.user_id)
        return self.access_token, self.user_id

    async def _login_with_password(self) -> None:
        if not (self.username and self.password):
            raise ValueError("Either (access_token, user_id) or (username, password) must be provided.")

        _LOGGER.info("Authenticating with username/password...")
        login_res = await self._call_private_api(
            "user/login",
            {
                "account": self.username,
                "password": md5(self.password),
            },
        )
        code = login_res.get("code")
        if code == "1013":
            raise RuntimeError(
                "Device verification required (code 1013). "
                "Run setup wizard to authorize this device with an email code."
            )
        if code != "0000":
            raise RuntimeError(f"Ecovacs login failed: {login_res.get('msg')} (code: {code})")

        self.user_id = str(login_res["data"]["uid"])
        self.access_token = str(login_res["data"]["accessToken"])

    async def authenticate(self) -> AuthTokens:
        """Authenticate and obtain fresh portal tokens."""
        # 1. Use stored access_token if available to avoid password login challenge
        if not (self.access_token and self.user_id):
            await self._login_with_password()

        # 2. Exchange accessToken for authCode
        now = time.time()
        auth_params = {
            "uid": self.user_id,
            "accessToken": self.access_token,
            "bizType": "ECOVACS_IOT",
            "deviceId": self.device_id,
            "authTimespan": int(now * 1000),
        }
        sign_str = _AUTH_CLIENT_KEY + "".join(f"{k}={v}" for k, v in sorted({"openId": "global", **auth_params}.items())) + _AUTH_CLIENT_SECRET
        auth_params["authSign"] = md5(sign_str)
        auth_params["authAppkey"] = _AUTH_CLIENT_KEY

        url = f"https://gl-{self.country}-openapi.ecovacs.com/v1/global/auth/getAuthCode"
        async with self.session.get(url, params=auth_params) as ar:
            auth_res = await ar.json(content_type=None)

        # Fallback to password login if access_token expired or UID was invalid (e.g. code 0004)
        if auth_res.get("code") != "0000":
            if self.username and self.password:
                _LOGGER.info("OpenAPI session expired (%s); refreshing access token via login...", auth_res.get("msg"))
                await self._login_with_password()
                now = time.time()
                auth_params = {
                    "uid": self.user_id,
                    "accessToken": self.access_token,
                    "bizType": "ECOVACS_IOT",
                    "deviceId": self.device_id,
                    "authTimespan": int(now * 1000),
                }
                sign_str = _AUTH_CLIENT_KEY + "".join(f"{k}={v}" for k, v in sorted({"openId": "global", **auth_params}.items())) + _AUTH_CLIENT_SECRET
                auth_params["authSign"] = md5(sign_str)
                auth_params["authAppkey"] = _AUTH_CLIENT_KEY
                async with self.session.get(url, params=auth_params) as ar2:
                    auth_res = await ar2.json(content_type=None)

            if auth_res.get("code") != "0000":
                raise RuntimeError(f"getAuthCode failed: {auth_res}")

        auth_code = auth_res["data"]["authCode"]

        # 3. Mint portal token via loginByItToken
        portal_data = {
            "edition": "ECOGLOBLE",
            "userId": self.user_id,
            "token": auth_code,
            "realm": "ecouser.net",
            "resource": self.device_id,
            "org": "ECOWW",
            "last": "",
            "country": self.country.upper(),
            "todo": "loginByItToken",
        }
        async with self.session.post(f"{self.portal_url}/api/users/user.do", json=portal_data) as pr:
            portal_res = await pr.json(content_type=None)
            if portal_res.get("result") != "ok":
                raise RuntimeError(f"loginByItToken failed: {portal_res}")
            self.portal_token = portal_res["token"]
            # Crucial: store short portal UID separately; NEVER overwrite account self.user_id!
            self.portal_user_id = str(portal_res.get("userId") or self.user_id)

        return AuthTokens(
            user_id=self.portal_user_id,
            account_user_id=self.user_id,
            portal_token=self.portal_token,
            access_token=self.access_token,
            device_id=self.device_id,
        )

    async def get_devices(self) -> list[dict[str, Any]]:
        """Retrieve list of robots associated with this Ecovacs account."""
        if not self.portal_token:
            await self.authenticate()

        portal_uid = self.portal_user_id or self.user_id
        dev_url = f"{self.portal_url}/api/appsvr/app.do"
        dev_body = {
            "userid": portal_uid,
            "lang": "EN",
            "platform": "android",
            "appVer": "3.14.0",
            "channel": "google_play",
            "aliliving": True,
            "share": True,
            "software": "robotui_config",
            "auth": {
                "resource": self.device_id,
                "token": self.portal_token,
                "realm": "ecouser.net",
                "userid": portal_uid,
                "with": "users",
            },
            "todo": "GetGlobalDeviceList",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "appid": "ecovacs",
            "token": self.portal_token,
            "userid": portal_uid,
            "Authorization": f"Bearer {self.portal_token}",
        }
        async with self.session.post(dev_url, json=dev_body, headers=headers) as dr:
            dev_res = await dr.json(content_type=None)
            devices = dev_res.get("devices") or []
            return [dict(d) for d in devices if isinstance(d, dict)]
