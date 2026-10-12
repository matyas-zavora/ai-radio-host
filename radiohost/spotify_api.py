"""Spotify Web API client: PKCE auth, token refresh, queue, playback fallback."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from .config import CONFIG_DIR
from .models import Track

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-read-currently-playing user-modify-playback-state"
TOKEN_FILE = CONFIG_DIR / "spotify_token.json"


class SpotifyError(Exception):
    pass


class NotAuthorized(SpotifyError):
    pass


class RateLimited(SpotifyError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


def track_from_json(item: dict | None) -> Track | None:
    if not item:
        return None
    if item.get("type") == "episode":
        show = (item.get("show") or {}).get("name", "")
        return Track(title=item.get("name", ""), artists=(show,), uri=item.get("uri"),
                     release_date=item.get("release_date"), duration_ms=item.get("duration_ms"))
    album = item.get("album") or {}
    return Track(
        title=item.get("name", ""),
        artists=tuple(a.get("name", "") for a in item.get("artists", [])),
        album=album.get("name", ""),
        uri=item.get("uri"),
        release_date=album.get("release_date"),
        duration_ms=item.get("duration_ms"),
    )


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, token_file: Path = TOKEN_FILE):
        if not client_id:
            raise NotAuthorized("SPOTIFY_CLIENT_ID is not set (put it in .env)")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.http = httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0))
        self._lock = threading.Lock()
        self._token: dict | None = None
        self.blocked_until = 0.0
        if token_file.exists():
            self._token = json.loads(token_file.read_text())

    # ---------- auth ----------

    @property
    def authorized(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def _save_token(self, data: dict) -> None:
        old_refresh = (self._token or {}).get("refresh_token")
        data.setdefault("refresh_token", old_refresh)  # refresh responses may omit it
        data["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        self._token = data
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.write_text(json.dumps(data))
        tmp.replace(self.token_file)

    def login(self, timeout: float = 180) -> None:
        """Interactive Authorization Code + PKCE flow via a loopback redirect."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": self.redirect_uri, "code_challenge_method": "S256",
            "code_challenge": challenge, "scope": SCOPES, "state": state,
        })

        redirect = urllib.parse.urlparse(self.redirect_uri)
        result: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != redirect.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
                ok = "code" in result and result.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(("Logged in, you can close this tab." if ok
                                  else "Login failed, check the terminal.").encode())

            def log_message(self, *args):
                pass

        try:
            server = HTTPServer((redirect.hostname, redirect.port or 80), Handler)
        except OSError as e:
            raise NotAuthorized(f"can't listen on {redirect.hostname}:{redirect.port} ({e})") from e
        server.timeout = 1
        print(f"Opening browser for Spotify login. If it doesn't open, visit:\n{url}\n")
        webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while "code" not in result and "error" not in result:
            if time.monotonic() > deadline:
                server.server_close()
                raise NotAuthorized(
                    "no answer from the Spotify login. Check that the app's redirect URI is "
                    f"exactly {self.redirect_uri}, then try again")
            server.handle_request()
        server.server_close()
        if "error" in result or result.get("state") != state:
            raise NotAuthorized(f"Spotify login failed: {result.get('error', 'state mismatch')}")

        r = self.http.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": self.redirect_uri, "client_id": self.client_id,
            "code_verifier": verifier,
        })
        if r.status_code != 200:
            raise NotAuthorized(f"token exchange failed: {r.status_code} {r.text}")
        self._save_token(r.json())
        print(f"Token saved to {self.token_file}")

    def _refresh(self) -> None:
        if not self.authorized:
            raise NotAuthorized("not logged in, run: radiohost auth")
        r = self.http.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._token["refresh_token"],
            "client_id": self.client_id,
        })
        if r.status_code in (400, 401):
            raise NotAuthorized(f"refresh rejected ({r.text}), run: radiohost auth")
        if r.status_code != 200:
            raise SpotifyError(f"token refresh failed: {r.status_code}")
        self._save_token(r.json())
        log.debug("Spotify token refreshed")

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if (force_refresh or not self._token
                    or time.time() > self._token.get("expires_at", 0) - 60):
                self._refresh()
            return self._token["access_token"]

    # ---------- requests ----------

    def request(self, method: str, path: str, **kwargs) -> dict | None:
        wait = self.blocked_until - time.time()
        if wait > 0:
            raise RateLimited(wait)
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._access_token(force_refresh=attempt == 1)}"}
            try:
                r = self.http.request(method, API + path, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                raise SpotifyError(f"{method} {path}: {e}") from e
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 429:
                retry = float(r.headers.get("Retry-After", "5"))
                self.blocked_until = time.time() + retry
                log.warning("Spotify rate limit hit, backing off %.0fs", retry)
                raise RateLimited(retry)
            if r.status_code == 204 or not r.content:
                return None
            if r.status_code >= 400:
                raise SpotifyError(f"{method} {path}: {r.status_code} {r.text[:200]}")
            return r.json()
        raise NotAuthorized("Spotify keeps returning 401, run: radiohost auth")

    def queue(self) -> tuple[Track | None, list[Track]]:
        data = self.request("GET", "/me/player/queue") or {}
        current = track_from_json(data.get("currently_playing"))
        return current, [t for t in map(track_from_json, data.get("queue") or []) if t]

    def playback(self) -> dict | None:
        return self.request("GET", "/me/player", params={"additional_types": "track,episode"})

    def pause(self) -> None:
        self.request("PUT", "/me/player/pause")

    def play(self) -> None:
        self.request("PUT", "/me/player/play")

    def seek(self, position_ms: int) -> None:
        self.request("PUT", "/me/player/seek", params={"position_ms": position_ms})
