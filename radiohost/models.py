"""Small data types shared across modules."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


@dataclass(frozen=True)
class Track:
    title: str
    artists: tuple[str, ...]
    album: str = ""
    uri: str | None = None          # spotify:track:...; None when the backend can't tell (CLI)
    release_date: str | None = None  # "1997", "1997-05" or "1997-05-21"
    duration_ms: int | None = None

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def year(self) -> str | None:
        return self.release_date[:4] if self.release_date else None

    def same_as(self, other: Track | None) -> bool:
        """Same track? Prefer URIs; fall back to normalized title + first artist."""
        if other is None:
            return False
        if self.uri and other.uri:
            return self.uri == other.uri
        return (_norm(self.title) == _norm(other.title)
                and _norm(self.artists[0] if self.artists else "")
                == _norm(other.artists[0] if other.artists else ""))

    def __str__(self) -> str:
        return f"{self.artist} – {self.title}"


@dataclass
class PlayerState:
    status: str                 # "Playing" | "Paused" | "Stopped"
    track: Track | None
    position_ms: int
    duration_ms: int
    shuffle: bool | None = None
    backend: str = ""

    @property
    def playing(self) -> bool:
        return self.status == "Playing"

    @property
    def remaining_ms(self) -> int:
        return max(0, self.duration_ms - self.position_ms)


@dataclass
class Facts:
    items: list[str] = field(default_factory=list)  # "source: fact" lines

    def __bool__(self) -> bool:
        return bool(self.items)
