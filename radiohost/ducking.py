"""Lower ("duck") the music player's own audio stream while the host talks.

Works through `pactl`, which talks to PulseAudio and to PipeWire (pipewire-pulse), so it
covers nearly every Linux desktop. Only the player's stream changes, not the system
volume. Music playing on another device (phone, speaker) can't be ducked; the loop
falls back to pausing then.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

from .config import STATE_DIR

log = logging.getLogger(__name__)

PLAYER_BINARIES = ("spotify", "spotifast", "spotifyd", "ncspot", "spot", "librespot")
STATE_FILE = STATE_DIR / "ducked.json"   # original volume, in case we crash while ducked
USER_TOLERANCE = 3                      # % difference that means "the user moved the slider"


def _pactl(*args: str) -> str:
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f"pactl {args[0]} failed")
    return r.stdout


def player_streams(binaries: tuple[str, ...] = PLAYER_BINARIES) -> dict[int, int]:
    """{sink-input index: volume %} for the music player's audio streams."""
    if not shutil.which("pactl"):
        return {}
    try:
        inputs = json.loads(_pactl("-f", "json", "list", "sink-inputs"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for s in inputs:
        props = s.get("properties", {})
        names = {str(props.get("application.process.binary", "")).lower(),
                 str(props.get("application.name", "")).lower()}
        if names & set(binaries):
            channels = [int(str(c.get("value_percent", "0")).rstrip("%"))
                        for c in s.get("volume", {}).values()]
            found[int(s["index"])] = round(sum(channels) / len(channels)) if channels else 100
    return found


def _set(index: int, percent: int) -> None:
    try:
        _pactl("set-sink-input-volume", str(index), f"{max(0, percent)}%")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("could not set stream %s volume: %s", index, e)


class Ducker:
    """One duck/restore cycle. Never leaves the music quiet: the original volume is saved
    to disk first, and `recover()` restores it on the next start after a crash."""

    def __init__(self, level_percent: int = 30, binaries: tuple[str, ...] = PLAYER_BINARIES):
        self.level = level_percent / 100
        self.binaries = binaries
        self.original = 100
        self.current = 100
        self.last_set: dict[int, int] = {}
        self.released = False   # the user took over the volume: hands off

    def available(self) -> bool:
        return bool(player_streams(self.binaries))

    def duck(self, fade_ms: int) -> bool:
        streams = player_streams(self.binaries)
        if not streams:
            return False
        self.original = max(streams.values())
        self.current = self.original
        self.last_set = dict(streams)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"original": self.original, "binaries": self.binaries}))
        self._fade(round(self.original * self.level), fade_ms)
        return True

    def hold(self) -> None:
        """Call often while talking: catches new streams (some players open one per song)
        and notices when the user changes the volume themselves."""
        if self.released:
            return
        for index, volume in player_streams(self.binaries).items():
            if index in self.last_set and abs(volume - self.last_set[index]) > USER_TOLERANCE:
                log.info("Volume changed by you; leaving it alone")
                self.released = True
                STATE_FILE.unlink(missing_ok=True)
                return
            if index not in self.last_set or volume != self.current:
                _set(index, self.current)
                self.last_set[index] = self.current

    def restore(self, fade_ms: int) -> None:
        if not self.released:
            self._fade(self.original, fade_ms)
        STATE_FILE.unlink(missing_ok=True)

    def _fade(self, target: int, fade_ms: int) -> None:
        start, steps = self.current, max(1, fade_ms // 80)
        for i in range(1, steps + 1):
            if self.released:
                return
            self.current = round(start + (target - start) * i / steps)
            self.hold()
            time.sleep(fade_ms / steps / 1000)

    @staticmethod
    def recover() -> None:
        """After a crash mid-duck, put the music back to its original volume."""
        try:
            saved = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return
        for index in player_streams(tuple(saved.get("binaries", PLAYER_BINARIES))):
            _set(index, int(saved.get("original", 100)))
        STATE_FILE.unlink(missing_ok=True)
        log.info("Restored the music volume left lowered by an earlier crash")
