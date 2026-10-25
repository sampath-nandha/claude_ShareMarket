"""Kite Connect login flow.

Kite requires an interactive browser login every trading day. Flow:
  1. `mmm auth login` prints/opens the login URL.
  2. The user logs in; Kite redirects to http://127.0.0.1:5010/callback?request_token=...
  3. A tiny local HTTP listener catches the request_token, exchanges it for an access_token and
     stores it in the OS credential store (Windows Credential Manager via `keyring`).

Secrets never touch the repo. Fallback for headless use: paste the request_token manually.
"""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import structlog

from mmm.broker.readonly import ReadOnlyKite
from mmm.settings import get_secrets
from mmm.timeutil import IST, now_ist

log = structlog.get_logger(__name__)

SERVICE = "mmm-kite"


class TokenStore:
    """Credential-store backed secret storage, with .env values as the read-only fallback for keys."""

    def __init__(self) -> None:
        try:
            import keyring  # noqa: WPS433

            self._keyring = keyring
        except Exception:  # pragma: no cover - keyring missing on exotic platforms
            self._keyring = None

    def _get(self, name: str) -> str | None:
        if self._keyring is None:
            return None
        try:
            return self._keyring.get_password(SERVICE, name)
        except Exception as exc:  # keyring backend unavailable
            log.warning("keyring_unavailable", error=str(exc))
            return None

    def _set(self, name: str, value: str) -> None:
        if self._keyring is None:
            raise RuntimeError("keyring is not available; cannot store secret")
        self._keyring.set_password(SERVICE, name, value)

    def api_key(self) -> str:
        return self._get("api_key") or get_secrets().kite_api_key

    def api_secret(self) -> str:
        return self._get("api_secret") or get_secrets().kite_api_secret

    def set_keys(self, api_key: str, api_secret: str) -> None:
        self._set("api_key", api_key)
        self._set("api_secret", api_secret)

    def access_token(self) -> str | None:
        """Return today's access token, or None if absent/expired (tokens die ~6 AM IST next day)."""
        tok = self._get("access_token")
        stamp = self._get("access_token_date")
        if not tok or not stamp:
            return None
        if stamp != now_ist().date().isoformat():
            return None
        return tok

    def set_access_token(self, token: str) -> None:
        self._set("access_token", token)
        self._set("access_token_date", now_ist().date().isoformat())

    def token_issued_on(self) -> str | None:
        return self._get("access_token_date")


def _make_kite(api_key: str):
    from kiteconnect import KiteConnect  # imported lazily so tests don't need network

    return KiteConnect(api_key=api_key)


class _CallbackHandler(BaseHTTPRequestHandler):
    request_token: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        qs = parse_qs(urlparse(self.path).query)
        tok = qs.get("request_token", [None])[0]
        if tok:
            _CallbackHandler.request_token = tok
            body = b"<h2>Kite login captured. You can close this tab.</h2>"
        else:
            body = b"<h2>No request_token in URL.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence stdlib logging
        return


def login_interactive(open_browser: bool = True, timeout_s: int = 300) -> str:
    """Run the local callback listener, open the Kite login page, exchange the token. Returns access_token."""
    store = TokenStore()
    api_key, api_secret = store.api_key(), store.api_secret()
    if not api_key or not api_secret:
        raise RuntimeError("Kite API key/secret not configured. Run `mmm auth set-keys` or fill .env")

    kite = _make_kite(api_key)
    url = kite.login_url()
    redirect = urlparse(get_secrets().kite_redirect_url)
    host, port = redirect.hostname or "127.0.0.1", redirect.port or 5010

    _CallbackHandler.request_token = None
    server = HTTPServer((host, port), _CallbackHandler)
    server.timeout = 1.0
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set() and _CallbackHandler.request_token is None:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    log.info("kite_login_url", url=url)
    print(f"\nOpen this URL to log in to Kite:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    t.join(timeout=timeout_s)
    stop.set()
    server.server_close()

    if _CallbackHandler.request_token is None:
        raise TimeoutError("Did not receive request_token from Kite redirect")
    return exchange_request_token(_CallbackHandler.request_token)


def exchange_request_token(request_token: str) -> str:
    store = TokenStore()
    kite = _make_kite(store.api_key())
    ro = ReadOnlyKite(kite)
    data = ro.generate_session(request_token, api_secret=store.api_secret())
    token = data["access_token"]
    store.set_access_token(token)
    log.info("kite_session_created", user_id=data.get("user_id"), at=datetime.now(tz=IST).isoformat())
    return token


def get_client() -> ReadOnlyKite:
    """Return an authenticated read-only client, or raise if today's token is missing."""
    store = TokenStore()
    token = store.access_token()
    if not token:
        raise RuntimeError("No valid Kite access token for today. Run `mmm auth login`.")
    kite = _make_kite(store.api_key())
    kite.set_access_token(token)
    return ReadOnlyKite(kite)


def auth_status() -> dict[str, str | bool | None]:
    store = TokenStore()
    return {
        "api_key_configured": bool(store.api_key()),
        "token_valid_today": store.access_token() is not None,
        "token_issued_on": store.token_issued_on(),
    }
