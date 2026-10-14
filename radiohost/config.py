"""Configuration: config.toml for settings, .env for secrets."""

from __future__ import annotations

import os
import re
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
    transition: str = "pause"      # pause = stop the music | duck = talk over lowered music
    duck_level: int = 30           # music volume while the host talks, % of the original
    fade_down_ms: int = 1500
    fade_up_ms: int = 3000


@dataclass
class HostConfig:
    language: str = "en"
    name: str = ""
    personality: str = DEFAULT_PERSONALITY
    # Saved hosts, name -> personality. The active host (name above) is always one of them.
    profiles: dict[str, str] = field(default_factory=dict)


def select_host(host: HostConfig, name: str) -> None:
    """Make a saved host the active one."""
    host.name, host.personality = name, host.profiles[name]


def update_host(host: HostConfig, name: str, personality: str) -> None:
    """Rename / edit the active host, keeping its place in the list."""
    if name != host.name and name in host.profiles:
        raise ValueError(f"A host named \"{name}\" already exists.")
    host.profiles = {(name if k == host.name else k): (personality if k == host.name else v)
                     for k, v in host.profiles.items()}
    host.name, host.personality = name, personality


def add_host(host: HostConfig, name: str, personality: str) -> None:
    """Save a new host and make it the active one."""
    if not name:
        raise ValueError("Give the new host a name.")
    if name in host.profiles:
        raise ValueError(f"A host named \"{name}\" already exists.")
    host.profiles[name] = personality
    select_host(host, name)


def delete_host(host: HostConfig, name: str) -> None:
    """Delete a saved host; if it was active, the first remaining one becomes active."""
    if len(host.profiles) <= 1:
        raise ValueError("Can't delete the only host.")
    del host.profiles[name]
    if name == host.name:
        select_host(host, next(iter(host.profiles)))


@dataclass
class LLMConfig:
    provider: str = "anthropic"    # anthropic | ollama
    model: str = "claude-sonnet-5"
    effort: str = "medium"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"


def voice_engine(name: str) -> str:
    """Google voices look like en-US-Chirp3-HD-Charon, Piper voices like en_US-ryan-high."""
    return "google" if re.match(r"^[a-z]{2,3}-[A-Z]{2}-", name) else "piper"


@dataclass
class TTSConfig:
    # The voice for each language; the engine (Google or Piper) follows from the name.
    voices: dict[str, str] = field(default_factory=dict)
    provider: str = "google"       # legacy: used only when `voices` has no entry for a language
    fallback: str = "piper"        # "piper" = use the Piper voice if Google fails; "" = skip the break
    google_voices: dict[str, str] = field(default_factory=lambda: {
        "en": "en-US-Chirp3-HD-Charon", "cs": "cs-CZ-Chirp3-HD-Charon"})
    google_monthly_char_limit: int = 900_000
    piper_voices: dict[str, str] = field(default_factory=lambda: {
        "en": "en_US-ryan-high", "cs": "cs_CZ-jirka-medium"})
    piper_voices_dir: Path = DATA_DIR / "piper"

    def voice_for(self, lang: str) -> str:
        if self.voices.get(lang):
            return self.voices[lang]
        return (self.google_voices if self.provider == "google" else self.piper_voices)[lang]


@dataclass
class PlayerConfig:
    backends: list[str] = field(default_factory=lambda: ["mpris", "applescript", "cli", "webapi"])
    mpris_name: str = "auto"       # auto = first running Spotify app; or e.g. "spotify"


@dataclass
class Config:
    brk: BreakConfig = field(default_factory=BreakConfig)
    host: HostConfig = field(default_factory=HostConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    player: PlayerConfig = field(default_factory=PlayerConfig)
    audio_command: str = ""        # "" = auto (pw-play, paplay, mpv, afplay)
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
            cfg.host.profiles = dict(cfg.host.profiles)
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
            if isinstance(cfg.tts.voices, dict):
                cfg.tts.voices = dict(cfg.tts.voices)
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

    # Old configs have no saved hosts: the active host becomes the first one.
    cfg.host.profiles[cfg.host.name] = cfg.host.personality
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
        "host": {k: v for k, v in vars(cfg.host).items() if k != "profiles"},
        "host.profiles": cfg.host.profiles,
        "llm": vars(cfg.llm),
        "tts": {"provider": t.provider, "fallback": t.fallback},
        "tts.voices": {lang: t.voice_for(lang) for lang in LANGUAGES},
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
        lines += [f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', k) else _toml_value(k)} = {_toml_value(v)}"
                  for k, v in values.items()]
        lines.append("")
    path.write_text("\n".join(lines))
    cfg.source = path
    return path


def env_file() -> Path:
    return (PROJECT_DIR if DEV else CONFIG_DIR) / ".env"


def set_env(values: dict[str, str]) -> Path:
    """Write KEY=VALUE pairs into .env (replacing or uncommenting existing lines), mode 600,
    and apply them to this process."""
    path = env_file()
    lines = path.read_text().splitlines() if path.exists() else []
    for key, value in values.items():
        new = f"{key}={value}"
        for i, line in enumerate(lines):
            bare = line.lstrip("# ").strip()
            if bare.startswith(f"{key}="):
                lines[i] = new
                break
        else:
            lines.append(new)
        os.environ[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    return path


SECRET_KEYS = ("SPOTIFY_CLIENT_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID",
               "GOOGLE_APPLICATION_CREDENTIALS")


def reset_all() -> list[Path]:
    """Back to a fresh install: delete config.toml and the Spotify login, and remove the
    keys from .env. Keeps the Google key file, the Google usage counter and voices."""
    removed = []
    for path in {PROJECT_DIR / "config.toml" if DEV else CONFIG_DIR / "config.toml",
                 CONFIG_DIR / "config.toml", CONFIG_DIR / "spotify_token.json"}:
        if path.exists():
            path.unlink()
            removed.append(path)
    env = env_file()
    if env.exists():
        kept = [line for line in env.read_text().splitlines()
                if not line.lstrip("# ").strip().startswith(tuple(k + "=" for k in SECRET_KEYS))]
        env.write_text("\n".join(kept) + "\n")
        env.chmod(0o600)
    for key in SECRET_KEYS:
        os.environ.pop(key, None)
    return removed
