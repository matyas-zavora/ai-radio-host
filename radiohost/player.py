"""Player control behind one interface: MPRIS (Linux) or AppleScript (macOS) → Spotifast CLI →
Spotify Web API."""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.wrappers import Properties

from .models import PlayerState, Track, spotify_uri
from .spotify_api import SpotifyClient, SpotifyError, track_from_json

log = logging.getLogger(__name__)


class PlayerUnavailable(Exception):
    pass


class PlayerControl:
    name = "base"

    def state(self) -> PlayerState | None:
        """Current state, None when nothing is loaded. Raises PlayerUnavailable."""
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def seek_start(self) -> None:
        """Jump to the start of the current track."""
        raise NotImplementedError


# Spotify players that expose MPRIS, in the order "auto" tries them.
KNOWN_PLAYERS = ("spotify", "spotifast", "spotifyd", "ncspot", "spot")
NO_POSITION_AFTER = 3.0   # seconds of "Playing at 0:00" before we stop trusting Position


class MprisPlayer(PlayerControl):
    name = "mpris"

    def __init__(self, player_name: str = "auto"):
        self.player_name = player_name
        self.addr: DBusAddress | None = None
        self._conn: DBusConnection | None = None
        self._trackid: str | None = None
        self._zero_since: float | None = None

    def _bus_name(self) -> str:
        """Resolve which MPRIS service to talk to ("auto" = first running Spotify player)."""
        prefix = "org.mpris.MediaPlayer2."
        if self.player_name != "auto":
            return prefix + self.player_name
        names = self._raw_call(new_method_call(
            DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                        interface="org.freedesktop.DBus"), "ListNames"))[0]
        for player in KNOWN_PLAYERS:   # spotifyd registers e.g. "spotifyd.instance1234"
            for n in names:
                if n == prefix + player or n.startswith(f"{prefix}{player}.instance"):
                    return n
        raise PlayerUnavailable("no Spotify player found on D-Bus")

    def _address(self) -> DBusAddress:
        if self.addr is None:
            self.addr = DBusAddress("/org/mpris/MediaPlayer2", bus_name=self._bus_name(),
                                    interface="org.mpris.MediaPlayer2.Player")
            log.info("Using MPRIS player %s", self.addr.bus_name)
        return self.addr

    def _call(self, make_msg):
        """make_msg(address) -> message. Re-resolves the player if it went away."""
        try:
            return self._raw_call(make_msg(self._address()))
        except PlayerUnavailable:
            if self.player_name == "auto":
                self.addr = None  # maybe another Spotify app is running now
            raise

    def _raw_call(self, msg):
        try:
            if self._conn is None:
                self._conn = open_dbus_connection(bus="SESSION")
            reply = self._conn.send_and_get_reply(msg, timeout=1.0)
        except Exception as e:  # broken socket, timeout
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            raise PlayerUnavailable(f"D-Bus: {e}") from e
        if reply.header.message_type == MessageType.error:
            raise PlayerUnavailable(f"MPRIS: {reply.body[0] if reply.body else 'error'}")
        return reply.body

    def state(self) -> PlayerState | None:
        props = {k: v[1] for k, v in self._call(lambda a: Properties(a).get_all())[0].items()}
        meta = {k: v[1] for k, v in props.get("Metadata", {}).items()}
        status = props.get("PlaybackStatus", "Stopped")
        if not meta.get("xesam:title"):
            return None
        self._trackid = meta.get("mpris:trackid")
        track = Track(
            title=meta.get("xesam:title", ""),
            artists=tuple(meta.get("xesam:artist", [])),
            album=meta.get("xesam:album", ""),
            uri=spotify_uri(meta.get("xesam:url")) or spotify_uri(meta.get("mpris:trackid")),
            duration_ms=meta.get("mpris:length", 0) // 1000,
        )
        position_ms = props.get("Position", 0) // 1000
        # Some clients (the official Spotify app, historically) always report Position 0.
        # Then let the fallback chain (Web API) provide state instead.
        if status == "Playing" and position_ms == 0:
            now = time.monotonic()
            self._zero_since = self._zero_since or now
            if now - self._zero_since > NO_POSITION_AFTER:
                raise PlayerUnavailable("player reports no playback position")
        else:
            self._zero_since = None
        return PlayerState(status=status, track=track,
                           position_ms=position_ms,
                           duration_ms=track.duration_ms or 0,
                           shuffle=props.get("Shuffle"), backend=self.name)

    def pause(self) -> None:
        self._call(lambda a: new_method_call(a, "Pause"))

    def play(self) -> None:
        self._call(lambda a: new_method_call(a, "Play"))

    def seek_start(self) -> None:
        if not self._trackid:
            self.state()
        self._call(lambda a: new_method_call(a, "SetPosition", "ox", (self._trackid, 0)))


APPLESCRIPT_STATE = """
if application "Spotify" is running then
  tell application "Spotify"
    if player state is stopped then return "stopped"
    set t to current track
    return (player state as string) & tab & (name of t) & tab & (artist of t) & tab ¬
      & (album of t) & tab & (id of t) & tab & (duration of t) & tab ¬
      & ((round (player position * 1000)) as string) & tab & (shuffling as string)
  end tell
end if
return "notrunning"
"""


def parse_applescript_state(out: str) -> PlayerState | None:
    """Parse APPLESCRIPT_STATE output. Raises PlayerUnavailable when Spotify isn't running."""
    fields = out.rstrip("\n").split("\t")
    if fields[0] == "notrunning":
        raise PlayerUnavailable("the Spotify app isn't running")
    if len(fields) < 8 or not fields[1]:
        return None
    status = {"playing": "Playing", "paused": "Paused"}.get(fields[0], "Stopped")
    track = Track(title=fields[1], artists=(fields[2],), album=fields[3],
                  uri=spotify_uri(fields[4]), duration_ms=int(fields[5] or 0))
    return PlayerState(status=status, track=track, position_ms=int(fields[6] or 0),
                       duration_ms=track.duration_ms or 0, shuffle=fields[7] == "true",
                       backend=AppleScriptPlayer.name)


def osascript(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PlayerUnavailable(f"osascript: {e}") from e
    if r.returncode != 0:   # e.g. -1743: the user hasn't allowed controlling Spotify yet
        raise PlayerUnavailable(f"AppleScript: {r.stderr.strip()[:200]}")
    return r.stdout


class AppleScriptPlayer(PlayerControl):
    """The Spotify desktop app on macOS. macOS asks once for permission to control Spotify."""
    name = "applescript"

    def state(self) -> PlayerState | None:
        return parse_applescript_state(osascript(APPLESCRIPT_STATE))

    def pause(self) -> None:
        osascript('tell application "Spotify" to pause')

    def play(self) -> None:
        osascript('tell application "Spotify" to play')

    def seek_start(self) -> None:
        osascript('tell application "Spotify" to set player position to 0')


class CliPlayer(PlayerControl):
    """`spotifast` CLI (only with the Spotifast player). Its output has no track URI, so matching
    falls back to title+artist."""
    name = "cli"

    def __init__(self, binary: str = "spotifast"):
        self.binary = binary

    def _run(self, *args: str) -> str:
        try:
            r = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PlayerUnavailable(f"spotifast CLI: {e}") from e
        if r.returncode != 0:
            raise PlayerUnavailable(f"spotifast {' '.join(args)}: {r.stderr.strip()[:200]}")
        return r.stdout

    def state(self) -> PlayerState | None:
        # state, title, artists, album, position_ms, duration_ms, volume, shuffle, ...
        fields = self._run("now-playing", "--raw").rstrip("\n").split("\t")
        if len(fields) < 8 or not fields[1]:
            return None
        status = {"playing": "Playing", "paused": "Paused"}.get(fields[0].lower(), "Stopped")
        track = Track(title=fields[1], artists=tuple(a.strip() for a in fields[2].split(",")),
                      album=fields[3], duration_ms=int(fields[5] or 0))
        return PlayerState(status=status, track=track, position_ms=int(fields[4] or 0),
                           duration_ms=int(fields[5] or 0), shuffle=fields[7] == "on",
                           backend=self.name)

    def pause(self) -> None:
        self._run("pause")

    def play(self) -> None:
        self._run("play")

    def seek_start(self) -> None:
        self._run("seek-to", "0")


class WebApiPlayer(PlayerControl):
    """Spotify Connect via Web API. Polls at most every `interval` s and interpolates."""
    name = "webapi"

    def __init__(self, client: SpotifyClient, interval: float = 2.0):
        self.client = client
        self.interval = interval
        self._last: PlayerState | None = None
        self._last_at = 0.0

    def state(self) -> PlayerState | None:
        now = time.monotonic()
        if self._last and now - self._last_at < self.interval:
            s = self._last
            pos = s.position_ms + (int((now - self._last_at) * 1000) if s.playing else 0)
            return PlayerState(s.status, s.track, min(pos, s.duration_ms), s.duration_ms,
                               s.shuffle, self.name)
        try:
            data = self.client.playback()
        except SpotifyError as e:
            raise PlayerUnavailable(f"Web API: {e}") from e
        self._last_at = now
        track = track_from_json((data or {}).get("item"))
        if not track:
            self._last = None
            return None
        self._last = PlayerState(
            status="Playing" if data.get("is_playing") else "Paused", track=track,
            position_ms=data.get("progress_ms") or 0, duration_ms=track.duration_ms or 0,
            shuffle=data.get("shuffle_state"), backend=self.name)
        return self._last

    def _do(self, fn, *args) -> None:
        try:
            fn(*args)
        except SpotifyError as e:
            raise PlayerUnavailable(f"Web API: {e}") from e
        self._last = None  # force a fresh read next time

    def pause(self) -> None:
        self._do(self.client.pause)

    def play(self) -> None:
        self._do(self.client.play)

    def seek_start(self) -> None:
        self._do(self.client.seek, 0)


class FallbackPlayer(PlayerControl):
    """Try backends in order; commands go to the backend that last answered state()."""

    def __init__(self, backends: list[PlayerControl]):
        self.backends = backends
        self.active: PlayerControl | None = None

    @property
    def name(self) -> str:
        return self.active.name if self.active else "none"

    def _try(self, op: str, order: list[PlayerControl]):
        errors = []
        for b in order:
            try:
                result = getattr(b, op)()
            except PlayerUnavailable as e:
                errors.append(f"{b.name}: {e}")
                continue
            if op == "state" and b is not self.active:
                log.info("Player backend: %s", b.name)
                self.active = b
            return result
        if op == "state" and self.active is not None:
            log.warning("No player backend responds (%s)", "; ".join(errors))
            self.active = None
        raise PlayerUnavailable("; ".join(errors))

    def state(self) -> PlayerState | None:
        return self._try("state", self.backends)

    def _command(self, op: str) -> None:
        first = [self.active] if self.active else []
        self._try(op, first + [b for b in self.backends if b is not self.active])

    def pause(self) -> None:
        self._command("pause")

    def play(self) -> None:
        self._command("play")

    def seek_start(self) -> None:
        self._command("seek_start")


def build_player(backends: list[str], mpris_name: str,
                 spotify: SpotifyClient | None) -> FallbackPlayer:
    made: list[PlayerControl] = []
    mac = sys.platform == "darwin"
    for name in backends:
        if name == "mpris":
            if not mac:   # no D-Bus on macOS
                made.append(MprisPlayer(mpris_name))
        elif name == "applescript":
            if mac:
                made.append(AppleScriptPlayer())
        elif name == "cli":
            made.append(CliPlayer())
        elif name == "webapi":
            if spotify is not None:
                made.append(WebApiPlayer(spotify))
        else:
            raise ValueError(f"unknown player backend: {name}")
    return FallbackPlayer(made)
