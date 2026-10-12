"""Play a clip locally through PipeWire/Pulse, cancellable."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

CANDIDATES = (["pw-play"], ["paplay"], ["mpv", "--no-video", "--really-quiet"])


class AudioError(Exception):
    pass


class ClipPlayer:
    def __init__(self, command: str = "", volume: int = 100):
        self.custom = bool(command)
        if command:
            self.cmd = shlex.split(command)
        else:
            self.cmd = next((c for c in CANDIDATES if shutil.which(c[0])), None)
        if not self.cmd:
            raise AudioError("no audio player found (install pipewire, pulseaudio-utils or mpv)")
        self.volume = volume  # percent; read at the start of each clip
        self._proc: subprocess.Popen | None = None

    def _volume_args(self) -> list[str]:
        v = max(0, min(100, int(self.volume))) / 100
        if self.custom:
            return []
        return {"pw-play": [f"--volume={v:.2f}"], "paplay": [f"--volume={int(v * 65536)}"],
                "mpv": [f"--volume={int(v * 100)}"]}.get(self.cmd[0], [])

    def start(self, path: Path) -> None:
        self.stop()
        try:
            self._proc = subprocess.Popen([*self.cmd, *self._volume_args(), str(path)],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE)
        except OSError as e:
            raise AudioError(f"{self.cmd[0]}: {e}") from e

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def failed(self) -> str | None:
        """stderr if the last clip exited with an error."""
        if self._proc is None or self._proc.poll() in (None, 0):
            return None
        return (self._proc.stderr.read() or b"").decode(errors="replace").strip()[:300] or "error"

    def stop(self) -> None:
        if self.running():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
