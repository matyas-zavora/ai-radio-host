"""Voice catalogs (Piper, Google Chirp 3 HD) and preview clips."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CACHE_DIR, TTSConfig
from .tts import make_tts

PIPER_INDEX = "https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json"
PIPER_LOCALES = {"en": ("en_US", "en_GB"), "cs": ("cs_CZ",)}
GOOGLE_LOCALES = {"en": "en-US", "cs": "cs-CZ"}

# Chirp 3 HD voices exist for both en-US and cs-CZ under the same names.
GOOGLE_MALE = ["Achird", "Algenib", "Algieba", "Alnilam", "Charon", "Enceladus", "Fenrir",
               "Iapetus", "Orus", "Puck", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
               "Umbriel", "Zubenelgenubi"]
GOOGLE_FEMALE = ["Achernar", "Aoede", "Autonoe", "Callirrhoe", "Despina", "Erinome", "Gacrux",
                 "Kore", "Laomedeia", "Leda", "Pulcherrima", "Sulafat", "Vindemiatrix", "Zephyr"]

SAMPLES = {
    "en": "That was a good one. Stay with me, because up next is a song that sounds "
          "even better this late at night.",
    "cs": "To byla pecka. Zůstaňte se mnou, protože teď přijde písnička, "
          "která v tuhle pozdní hodinu zní ještě líp.",
}


@dataclass
class Voice:
    provider: str     # "piper" | "google"
    name: str         # config value
    label: str
    detail: str


def google_voices(lang: str) -> list[Voice]:
    loc = GOOGLE_LOCALES[lang]
    out = [Voice("google", f"{loc}-Chirp3-HD-{n}", n, "male · Chirp 3 HD") for n in GOOGLE_MALE]
    out += [Voice("google", f"{loc}-Chirp3-HD-{n}", n, "female · Chirp 3 HD")
            for n in GOOGLE_FEMALE]
    return sorted(out, key=lambda v: v.label)


def piper_voices(lang: str, voices_dir: Path) -> list[Voice]:
    """Single-speaker Piper voices for the language (index cached for a week)."""
    cache = CACHE_DIR / "piper-voices.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        index = json.loads(cache.read_text())
    else:
        r = httpx.get(PIPER_INDEX, timeout=15, follow_redirects=True)
        r.raise_for_status()
        index = r.json()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(index))
    out = []
    for key, v in sorted(index.items()):
        if not key.startswith(PIPER_LOCALES[lang]) or v.get("num_speakers", 1) != 1:
            continue
        size = sum(f.get("size_bytes", 0) for f in v.get("files", {}).values()) / 1e6
        locale, name, quality = key.split("-", 2)
        here = (voices_dir / f"{key}.onnx").exists()
        out.append(Voice("piper", key, f"{name.replace('_', ' ').title()} ({quality})",
                         f"{locale} · {'downloaded' if here else f'{size:.0f} MB download'}"))
    return out


def preview(voice: Voice, lang: str, cfg: TTSConfig) -> Path:
    """Synthesize (or reuse) a sample clip for this voice."""
    out = CACHE_DIR / "previews" / f"{voice.name}-{lang}.wav"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tts = make_tts(voice.name, cfg)
    tmp = out.with_suffix(".tmp.wav")
    tts.synthesize(SAMPLES[lang], tmp)
    tmp.replace(out)
    return out
