"""Twitch OAuth: auth-code flow captured on a fixed loopback port, tokens in
the secrets store, validate/refresh, clear + re-auth on invalid tokens."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
import urllib.parse

import httpx

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SCOPES = ["chat:read", "chat:edit"]
TOKEN_KEY = "twitch_token"
FLOW_TIMEOUT = 300.0  # user has 5 minutes to complete the browser flow

OK_PAGE = (
    b"<html><body style='background:#1e1e1e;color:#eee;font-family:sans-serif;"
    b"display:grid;place-items:center;height:100vh'>"
    b"<p>ChatterSyn is connected to Twitch. You can close this window.</p>"
    b"</body></html>"
)

TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


class TwitchAuthError(Exception):
    pass


@dataclasses.dataclass
class TwitchTokens:
    access_token: str
    refresh_token: str = ""
    user_id: str = ""
    login: str = ""
    scopes: list[str] = dataclasses.field(default_factory=list)
    expires_at: float = 0.0  # wall-clock epoch seconds

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> TwitchTokens | None:
        try:
            data = json.loads(raw)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None


class TwitchAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        store,  # SecretsStore-compatible: get/set/delete(str, str)
        *,
        ports: tuple[int, ...] = (17821, 17822, 17823),
        client: httpx.AsyncClient | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self._store = store
        self._ports = ports
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._owns_client = client is None
        self._server: asyncio.Server | None = None
        self._code_future: asyncio.Future | None = None
        self._redirect_uri = ""

    async def close(self) -> None:
        await self._stop_server()
        if self._owns_client:
            await self._client.aclose()

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # -- token store -----------------------------------------------------------

    def load_tokens(self) -> TwitchTokens | None:
        raw = self._store.get(TOKEN_KEY)
        return TwitchTokens.from_json(raw) if raw else None

    def save_tokens(self, tokens: TwitchTokens) -> None:
        self._store.set(TOKEN_KEY, tokens.to_json())

    def clear(self) -> None:
        self._store.delete(TOKEN_KEY)

    # -- token lifecycle --------------------------------------------------------

    async def validate(self, access_token: str) -> dict | None:
        """Twitch user info for a token; None if the token is rejected."""
        response = await self._client.get(
            VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}
        )
        if response.status_code == 401:
            return None
        response.raise_for_status()
        return response.json()

    async def refresh(self, tokens: TwitchTokens) -> TwitchTokens | None:
        response = await self._client.post(
            TOKEN_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if response.status_code != 200:
            log.warning("token refresh rejected: %s", response.status_code)
            return None
        data = response.json()
        tokens.access_token = data.get("access_token", "")
        tokens.refresh_token = data.get("refresh_token") or tokens.refresh_token
        tokens.scopes = (data.get("scope") or "").split()
        tokens.expires_at = time.time() + int(data.get("expires_in", 0))
        return tokens

    async def ensure_valid(self) -> TwitchTokens | None:
        """Valid tokens for the stored session, refreshing if needed.

        Returns None when (re)authorization is required; the stored session is
        cleared so the UI can offer a fresh flow.
        """
        tokens = self.load_tokens()
        if tokens is None:
            return None
        info = await self.validate(tokens.access_token)
        if info is None:
            if not tokens.refresh_token:
                self.clear()
                return None
            refreshed = await self.refresh(tokens)
            if refreshed is None or not refreshed.access_token:
                self.clear()
                return None
            tokens = refreshed
            info = await self.validate(tokens.access_token)
            if info is None:
                self.clear()
                return None
        if info:
            tokens.login = info.get("login", "")
            tokens.user_id = info.get("user_id", "")
            self.save_tokens(tokens)
        return tokens

    # -- interactive flow ---------------------------------------------------------

    async def begin_flow(self) -> str:
        """Bind a loopback server on the first free configured port and return
        the authorize URL to open in the user's browser."""
        if not self.configured():
            raise TwitchAuthError("Twitch client_id/client_secret are not configured")
        await self._stop_server()
        for port in self._ports:
            try:
                self._server = await asyncio.start_server(
                    self._handle_request, "127.0.0.1", port
                )
            except OSError:
                continue
            bound = self._server.sockets[0].getsockname()[1]
            self._redirect_uri = f"http://localhost:{bound}"
            break
        else:
            raise TwitchAuthError("could not bind any configured loopback port")
        self._code_future = asyncio.get_running_loop().create_future()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "force_verify": "true",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    async def wait_for_code(self, timeout: float = FLOW_TIMEOUT) -> TwitchTokens | None:
        """Wait for the browser redirect, exchange the code, save tokens.

        Returns None on timeout; raises TwitchAuthError on exchange failure.
        """
        if self._code_future is None:
            raise TwitchAuthError("begin_flow() must be called first")
        try:
            code = await asyncio.wait_for(self._code_future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            await self._stop_server()
        tokens = await self._exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            }
        )
        info = await self.validate(tokens.access_token)
        if info:
            tokens.login = info.get("login", "")
            tokens.user_id = info.get("user_id", "")
        self.save_tokens(tokens)
        return tokens

    async def _exchange(self, extra: dict) -> TwitchTokens:
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            **extra,
        }
        response = await self._client.post(TOKEN_URL, params=params)
        if response.status_code != 200:
            raise TwitchAuthError(
                f"token exchange failed ({response.status_code}): {response.text[:200]}"
            )
        data = response.json()
        return TwitchTokens(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            scopes=(data.get("scope") or "").split(),
            expires_at=time.time() + int(data.get("expires_in", 0)),
        )

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not line or line in (b"\r\n", b"\n"):
                    break
            parts = request_line.decode("latin-1", "replace").split(" ")
            path = parts[1] if len(parts) > 1 else "/"
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            code = (query.get("code") or [None])[0]
            body = OK_PAGE
            if code and self._code_future is not None and not self._code_future.done():
                self._code_future.set_result(code)
            elif not code:
                body = b"<html><body>No authorization code in this request.</body></html>"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
        except Exception:  # browser closed early etc. — never fatal
            log.debug("loopback request handling failed", exc_info=True)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
