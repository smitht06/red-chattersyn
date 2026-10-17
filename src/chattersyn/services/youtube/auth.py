"""YouTube OAuth: desktop installed-app flow on a loopback port.

Uses google-auth-oauthlib's InstalledAppFlow but never its blocking
run_local_server — we bind our own asyncio loopback server on an ephemeral
port, set flow.redirect_uri to match, open the browser ourselves, then
exchange the code with flow.fetch_token (in a worker thread).

Scope youtube.force-ssl authorizes BOTH liveChatMessages.insert and
liveChatMessages.list (verified against the discovery document), so a single
scope covers read + send. REST live chat endpoints do NOT accept API keys.

Google Cloud projects in "Testing" publishing status issue refresh tokens
that expire after 7 days -> RefreshError (invalid_grant) means the user must
re-consent; we clear the stored token and surface an auth_state event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.parse

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from ...config.settings import Settings
from ...core.bus import bridge

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
CLIENT_SECRETS_KEY = "youtube_client_secrets"  # dict(installed: {...}) JSON
TOKEN_KEY = "youtube_token"  # Credentials.to_json() blob

# Shown in the browser after a successful exchange.
OK_PAGE = (
    "<html><body style='font-family:sans-serif;background:#1e1e1e;color:#eee;"
    "padding:40px;text-align:center'><h2>ChatterSyn is connected to YouTube</h2>"
    "<p>You can close this tab and return to the app.</p></body></html>"
)


class YouTubeAuthError(Exception):
    """Fatal setup problem (missing/refused client secrets)."""


class YouTubeAuth:
    def __init__(self, settings: Settings, secrets, *, client=None) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client = client
        self._client_secrets: dict | None = None
        self._creds: Credentials | None = None
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._code_future: asyncio.Future[str | None] | None = None
        self._state = ""
        self._flow = None  # InstalledAppFlow, built lazily

    # -- setup ----------------------------------------------------------------------

    def configured(self) -> bool:
        return self._load_client_secrets()

    def set_client_secrets(self, client_secrets: dict) -> None:
        """Persist a client_secrets.json dict (from the GCloud console download)."""
        if "installed" not in client_secrets and "web" not in client_secrets:
            raise YouTubeAuthError("not a client_secrets.json file")
        installed = client_secrets.get("installed") or client_secrets.get("web")
        if not installed.get("client_id") or not installed.get("client_secret"):
            raise YouTubeAuthError("client_secrets.json lacks client_id/client_secret")
        self._secrets.set(CLIENT_SECRETS_KEY, json.dumps(client_secrets))
        self._client_secrets = client_secrets
        self._flow = None  # rebuild with new config
        log.info("YouTube client secrets stored")

    def _load_client_secrets(self) -> bool:
        if self._client_secrets is not None:
            return True
        blob = self._secrets.get(CLIENT_SECRETS_KEY)
        if not blob:
            return False
        try:
            data = json.loads(blob)
            if "installed" not in data and "web" not in data:
                raise ValueError("missing installed/web section")
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("stored client_secrets invalid: %s", exc)
            return False
        self._client_secrets = data
        return True

    # -- token lifecycle --------------------------------------------------------------

    def token_info(self) -> dict | None:
        """For the Accounts settings page (M5): scopes + expiry, no refresh."""
        if self._creds is None:
            return None
        return {
            "valid": self._creds.valid,
            "expiry": self._creds.expiry.isoformat() if self._creds.expiry else None,
            "scopes": list(self._creds.scopes or []),
        }

    def _load_token(self) -> bool:
        if self._creds is not None:
            return True
        blob = self._secrets.get(TOKEN_KEY)
        if not blob:
            return False
        try:
            self._creds = Credentials.from_authorized_user_info(json.loads(blob))
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            log.warning("stored token invalid: %s", exc)
            return False
        return True

    def _save_token(self) -> None:
        assert self._creds is not None
        self._secrets.set(TOKEN_KEY, self._creds.to_json())

    def clear_token(self) -> None:
        self._creds = None
        self._secrets.delete(TOKEN_KEY)

    async def ensure_valid(self) -> Credentials | None:
        """Load -> refresh if expired -> re-validate. None + auth_state on failure."""
        if not self._load_token():
            return None
        if self._creds.valid:
            return self._creds  # fresh token — never refresh a working grant
        try:
            await asyncio.to_thread(self._creds.refresh, self._request())
        except RefreshError as exc:
            if getattr(exc, "retryable", False):
                # Transient (network/5xx): keep the stored grant for next time —
                # clearing here would destroy a valid refresh token.
                log.warning("YouTube token refresh transiently failed: %s", exc)
                return None
            detail = "youtube re-authorization required — token expired"
            if "invalid_grant" in str(exc):
                detail += " (testing-mode refresh tokens expire after 7 days)"
            log.warning("YouTube token refresh failed: %s", exc)
            self.clear_token()
            bridge.auth_state.emit("youtube", False, detail)
            return None
        except Exception as exc:  # network hiccup: keep token, report
            log.warning("YouTube token refresh error: %s", exc)
            return None
        self._save_token()
        return self._creds

    async def access_token(self) -> str | None:
        """Fresh access token for API calls, refreshing when needed."""
        creds = await self.ensure_valid()
        return creds.token if creds else None

    def _request(self):
        # google-auth needs a Request for refresh; httpx-based wrapper.
        from google.auth.transport.requests import Request

        return Request()

    # -- interactive flow --------------------------------------------------------------

    async def begin_flow(self) -> str:
        """Bind the loopback server, return the authorize URL to open in a browser."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if self._server is not None:
            raise YouTubeAuthError("authorization flow already in progress")
        if not self._load_client_secrets():
            raise YouTubeAuthError("no client secrets — load client_secrets.json first")
        self._server = await asyncio.start_server(self._handle_request, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        # Registered desktop redirect URIs are portless ("http://localhost");
        # RFC 8252 §8.4 matches any port, so the bound port is fine.
        self._flow = InstalledAppFlow.from_client_config(
            self._client_secrets,
            scopes=[SCOPE],
            redirect_uri=f"http://localhost:{self._port}",
        )
        self._code_future = asyncio.get_running_loop().create_future()
        auth_url, state = self._flow.authorization_url(prompt="consent", access_type="offline")
        self._state = state
        return auth_url

    async def wait_for_code(self, timeout: float = 300.0) -> bool:
        """Wait for the browser callback, exchange, persist. False = timeout."""
        if self._code_future is None:
            raise YouTubeAuthError("begin_flow() first")
        try:
            code = await asyncio.wait_for(self._code_future, timeout)
        except TimeoutError:
            return False
        finally:
            self._stop_server()
        if code is None:
            return False
        try:
            await asyncio.to_thread(self._exchange, code)
        except Exception as exc:
            log.warning("YouTube token exchange failed: %s", exc)
            bridge.auth_state.emit("youtube", False, f"authorization failed: {exc}")
            return False
        return self._creds is not None

    def _exchange(self, code: str) -> None:
        try:
            self._flow.fetch_token(code=code)
        except Exception as exc:
            log.warning("YouTube token exchange failed: %s", exc)
            raise
        self._creds = self._flow.credentials
        self._save_token()
        log.info("YouTube account connected")

    def _stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._code_future = None

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("latin-1", "replace").split()
            if len(parts) < 2 or parts[0] != "GET":
                raise ValueError("unexpected request line")
            parsed = urllib.parse.urlparse(parts[1])
            query = urllib.parse.parse_qs(parsed.query)
            code = (query.get("code") or [None])[0]
            state = (query.get("state") or [""])[0]
            ok = code is not None and state == self._state
            body = OK_PAGE if ok else "Authorization failed. You can close this tab."
            if self._code_future is not None and not self._code_future.done():
                self._code_future.set_result(code if ok else None)
            response = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            pass  # best-effort page; the future timeout is the real failure path
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def close(self) -> None:
        self._stop_server()
        if self._client is not None:
            await self._client.aclose()
