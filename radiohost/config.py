"""Configuration: config.toml for settings, .env for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEV = (PROJECT_DIR / "pyproject.toml").exists()   # running from a source checkout
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ai-radio-host"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ai-radio-host"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ai-radio-host"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ai-radio-host"

LANGUAGES = {"en": "English", "cs": "Czech"}

DEFAULT_PERSONALITY = (
    "a warm late-night radio DJ: relaxed and smooth, a little witty, "
    "talks like a friend keeping the listener company"
)


@dataclass
class BreakConfig:
    every_n_tracks: int = 3
    min_played_seconds: int = 30   # a track counts toward N once it has played this long
    lead_ms: int = 800             # pause this long before the current track ends
    min_seconds: int = 15          # target speech length
    max_seconds: int = 25


@dataclass
class HostConfig:
    language: str = "en"
    name: str = ""
    personality: str = DEFAULT_PERSONALITY


@dataclass
class LLMConfig:
    model: str = "claude-sonnet-5"
    effort: str = "medium"


@dataclass
class TTSConfig:
    provider: str = "google"       # google | piper
    fallback: str = "piper"        # provider to use when the primary fails; "" = none
    google_voices: dict[str, str] = field(default_factory=lambda: {
        "en": "en-US-Chirp3-HD-Charon", "cs": "cs-CZ-Chirp3-HD-Charon"})
    google_monthly_char_limit: int = 900_000
    piper_voices: dict[str, str] = field(default_factory=lambda: {
        "en": "en_US-ryan-high", "cs": "cs_CZ-jirka-medium"})
    piper_voices_dir: Path = DATA_DIR / "piper"


@dataclass
class PlayerConfig:
    backends: list[str] = field(default_factory=lambda: ["mpris", "cli", "webapi"])
    mpris_name: str = "spotifast"


@dataclass
class Config:
    brk: BreakConfig = field(default_factory=BreakConfig)
    host: HostConfig = field(default_factory=HostConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    player: PlayerConfig = field(default_factory=PlayerConfig)
    audio_command: str = ""        # "" = auto (pw-play, paplay, mpv)
    audio_volume: int = 100        # host voice volume, 0-100 %
    spotify_client_id: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    source: Path | None = None


def _apply(obj, data: dict, prefix: str = "") -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ValueError(f"unknown config key: {prefix}{key}")
        current = getattr(obj, key)
        if isinstance(current, Path):
            value = Path(value).expanduser()
        setattr(obj, key, value)


def load_config(path: Path | None = None) -> Config:
    # Source checkout: files next to the code win. Installed: ~/.config/ai-radio-host/.
    if DEV:
        load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(CONFIG_DIR / ".env")  # never overrides what's already set

    candidates = [path] if path else (
        [PROJECT_DIR / "config.toml"] if DEV else []) + [CONFIG_DIR / "config.toml"]
    cfg = Config()
    for p in candidates:
        if p and p.exists():
            data = tomllib.loads(p.read_text())
            _apply(cfg.brk, data.get("break", {}), "break.")
            _apply(cfg.host, data.get("host", {}), "host.")
            _apply(cfg.llm, data.get("llm", {}), "llm.")
            _apply(cfg.player, data.get("player", {}), "player.")
            tts = dict(data.get("tts", {}))
            google = tts.pop("google", {})
            piper = tts.pop("piper", {})
            _apply(cfg.tts, tts, "tts.")
            if "voices" in google:
                cfg.tts.google_voices.update(google.pop("voices"))
            if "monthly_char_limit" in google:
                cfg.tts.google_monthly_char_limit = int(google.pop("monthly_char_limit"))
            if "voices" in piper:
                cfg.tts.piper_voices.update(piper.pop("voices"))
            if "voices_dir" in piper:
                cfg.tts.piper_voices_dir = Path(piper.pop("voices_dir")).expanduser()
            if google or piper:
                raise ValueError(f"unknown tts keys: {sorted(google) + sorted(piper)}")
            cfg.audio_command = data.get("audio", {}).get("command", cfg.audio_command)
            cfg.audio_volume = int(data.get("audio", {}).get("volume", cfg.audio_volume))
            cfg.spotify_redirect_uri = data.get("spotify", {}).get(
                "redirect_uri", cfg.spotify_redirect_uri)
            cfg.source = p
            break

    cfg.spotify_client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if cfg.host.language not in LANGUAGES:
        raise ValueError(f"host.language must be one of {list(LANGUAGES)}")
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, Path):
        v = str(v).replace(str(Path.home()), "~", 1)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Write every setting to config.toml (comments are not kept; see config.example.toml)."""
    path = path or cfg.source or (PROJECT_DIR if DEV else CONFIG_DIR) / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    t = cfg.tts
    sections = {
        "break": vars(cfg.brk),
        "host": vars(cfg.host),
        "llm": vars(cfg.llm),
        "tts": {"provider": t.provider, "fallback": t.fallback},
        "tts.google": {"monthly_char_limit": t.google_monthly_char_limit},
        "tts.google.voices": t.google_voices,
        "tts.piper": {"voices_dir": t.piper_voices_dir},
        "tts.piper.voices": t.piper_voices,
        "player": vars(cfg.player),
        "audio": {"command": cfg.audio_command, "volume": cfg.audio_volume},
        "spotify": {"redirect_uri": cfg.spotify_redirect_uri},
    }
    lines = ["# Written by the AI Radio Host app. All options: config.example.toml", ""]
    for name, values in sections.items():
        lines.append(f"[{name}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in values.items()]
        lines.append("")
    path.write_text("\n".join(lines))
    cfg.source = path
    return path
