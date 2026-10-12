"""Text-to-speech providers: Google Cloud TTS (cloud) and Piper (local). Output is WAV."""

from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import sys
import threading
import wave
from pathlib import Path

from .config import STATE_DIR, TTSConfig

log = logging.getLogger(__name__)


class TTSError(Exception):
    pass


class TTSProvider:
    name = "base"

    def synthesize(self, text: str, out: Path) -> Path:
        raise NotImplementedError


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, voice: str, voices_dir: Path):
        self.voice_name = voice
        self.voices_dir = voices_dir
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        if self._voice is not None:
            return self._voice
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise TTSError("piper-tts is not installed (pip install 'ai-radio-host[piper]')") from e
        model = self.voices_dir / f"{self.voice_name}.onnx"
        if not model.exists():
            log.info("Downloading Piper voice %s ...", self.voice_name)
            self.voices_dir.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([sys.executable, "-m", "piper.download_voices", self.voice_name,
                                "--download-dir", str(self.voices_dir)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not model.exists():
                raise TTSError(f"could not download Piper voice {self.voice_name}: "
                               f"{r.stderr.strip()[-300:]}")
        self._voice = PiperVoice.load(model)
        return self._voice

    def synthesize(self, text: str, out: Path) -> Path:
        with self._lock:
            voice = self._load()
            try:
                with wave.open(str(out), "wb") as wf:
                    voice.synthesize_wav(text, wf)
            except Exception as e:  # onnxruntime / phonemizer errors
                raise TTSError(f"Piper failed: {e}") from e
        return out


class CharBudget:
    """Monthly character counter, so the Google free tier is never exceeded by accident.
    Only counts this app's usage."""

    def __init__(self, limit: int, path: Path = STATE_DIR / "google_tts_usage.json"):
        self.limit = limit
        self.path = path

    def _load(self) -> dict:
        month = dt.date.today().strftime("%Y-%m")
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return data if data.get("month") == month else {"month": month, "chars": 0}

    def check(self, n: int) -> None:
        data = self._load()
        if data["chars"] + n > self.limit:
            raise TTSError(f"Google TTS monthly budget reached "
                           f"({data['chars']}/{self.limit} chars in {data['month']})")

    def add(self, n: int) -> int:
        data = self._load()
        data["chars"] += n
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        return data["chars"]


class GoogleTTS(TTSProvider):
    name = "google"

    def __init__(self, voice: str, monthly_char_limit: int):
        self.voice = voice                                  # e.g. en-US-Chirp3-HD-Charon
        self.language_code = "-".join(voice.split("-")[:2])
        self.budget = CharBudget(monthly_char_limit)
        self._client = None

    def synthesize(self, text: str, out: Path) -> Path:
        self.budget.check(len(text))
        try:
            from google.cloud import texttospeech as tts
        except ImportError as e:
            raise TTSError("google-cloud-texttospeech is not installed "
                           "(pip install 'ai-radio-host[google]')") from e
        try:
            if self._client is None:
                self._client = tts.TextToSpeechClient()
            response = self._client.synthesize_speech(
                input=tts.SynthesisInput(text=text),
                voice=tts.VoiceSelectionParams(language_code=self.language_code, name=self.voice),
                audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16),
                timeout=20,
            )
        except Exception as e:  # google.api_core errors, missing/invalid credentials
            raise TTSError(f"Google TTS failed: {e}") from e
        out.write_bytes(response.audio_content)  # LINEAR16 comes with a WAV header
        used = self.budget.add(len(text))
        log.debug("Google TTS chars this month: %d/%d", used, self.budget.limit)
        return out


class FallbackTTS(TTSProvider):
    def __init__(self, primary: TTSProvider, fallback: TTSProvider | None):
        self.primary, self.fallback = primary, fallback
        self.name = primary.name

    def synthesize(self, text: str, out: Path) -> Path:
        try:
            return self.primary.synthesize(text, out)
        except TTSError as e:
            if self.fallback is None:
                raise
            log.warning("%s; using %s instead", e, self.fallback.name)
            return self.fallback.synthesize(text, out)


def build_tts(cfg: TTSConfig, language: str) -> TTSProvider:
    def make(name: str) -> TTSProvider:
        if name == "piper":
            return PiperTTS(cfg.piper_voices[language], cfg.piper_voices_dir)
        if name == "google":
            return GoogleTTS(cfg.google_voices[language], cfg.google_monthly_char_limit)
        raise ValueError(f"unknown TTS provider: {name}")

    primary = make(cfg.provider)
    fallback = make(cfg.fallback) if cfg.fallback and cfg.fallback != cfg.provider else None
    return FallbackTTS(primary, fallback)


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path)) as wf:
            return wf.getnframes() / float(wf.getframerate())
    except (OSError, wave.Error, EOFError):
        return 0.0
