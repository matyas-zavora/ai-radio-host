"""Write the host's talk break with Claude or a local model.

The model never receives data from Spotify. It gets only facts from Wikipedia and
MusicBrainz, and refers to the songs through placeholders ({song}, {artist},
{prev_song}, {prev_artist}) that are filled in locally afterwards. Spotify's terms
forbid "ingesting Spotify Content" (including metadata) into AI models.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import anthropic
import httpx

from .config import LANGUAGES, BreakConfig, HostConfig, LLMConfig
from .facts import base_title
from .models import Facts, Track

log = logging.getLogger(__name__)

WORDS_PER_SECOND = {"en": 2.5, "cs": 2.2}
# USD per million tokens (input, output), for the cost line in the log
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-opus-5": (5.0, 25.0)}

SYSTEM_TEMPLATE = """\
You are the voice of a personal radio show. Persona: {personality}.{name_line}

Between songs you do a short talk break that introduces the NEXT song, the way a \
charismatic radio host would: a bit of personality, a smooth transition, then the \
song and artist name.

You are not told the song's name directly. Write {{song}} where its title goes and \
{{artist}} where the artist's name goes; they are replaced with the real names before \
you are heard. Name the next song and its artist once each, using these placeholders, \
not the names from the facts, so nothing is said twice.

Rules:
- Speak {language}. Song titles, artist and album names stay as they are. Put the \
placeholders where a name can stand unchanged (no grammatical endings attached).
- Length: {min_words}-{max_words} words ({min_s}-{max_s} seconds spoken). Never longer.
- Use ONLY the facts listed in the FACTS section. Never add trivia, dates, chart \
positions, awards, band members, anecdotes or anything else from your own memory, \
even if you are sure it is true.
- If FACTS is empty, keep it a simple, warm introduction of the song and artist, \
with no factual claims at all.
- The listener is alone with their own playlist. Don't mention callers, \
advertising, the station name, the weather, or the time of day.
- Output only the words to be spoken: plain text, no quotes, no stage directions, \
no emojis, no markdown, no lists.
"""


class ScriptError(Exception):
    pass


THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)  # reasoning models (qwen3, deepseek-r1)
PLACEHOLDER = re.compile(r"\{\s*(song|artist|prev_song|prev_artist)\s*\}|\{(\w+)\}", re.I)
AND = {"en": "and", "cs": "a"}


def _join_artists(artists: tuple[str, ...], lang: str) -> str:
    names = list(dict.fromkeys(a for a in artists if a))   # "NOTION, NOTION" -> "NOTION"
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + f" {AND.get(lang, 'and')} " + names[-1]


class ScriptWriter:
    def __init__(self, llm: LLMConfig, host: HostConfig, brk: BreakConfig):
        self.llm = llm
        if llm.provider == "anthropic":
            # Keys not scoped to a workspace need the workspace ID on every request.
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            self.client = anthropic.Anthropic(
                timeout=30.0, max_retries=2,
                default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        elif llm.provider == "ollama":
            # Local models can be slow on CPU; there's a whole song to wait.
            self.http = httpx.Client(base_url=llm.ollama_url.rstrip("/"), timeout=120.0)
        else:
            raise ValueError(f"unknown llm.provider: {llm.provider}")
        self.lang = host.language
        rate = WORDS_PER_SECOND.get(self.lang, 2.4)
        self.max_words = int(brk.max_seconds * rate)
        self.system = SYSTEM_TEMPLATE.format(
            personality=host.personality,
            name_line=(f" Your on-air name is {host.name}. Mention it now and then, about every "
                       "third break, never in every one." if host.name else ""),
            language=LANGUAGES[self.lang],
            min_words=int(brk.min_seconds * rate), max_words=self.max_words,
            min_s=brk.min_seconds, max_s=brk.max_seconds,
        )
        if brk.transition == "duck":
            self.system += ("- You talk over the music: the previous song fades out under your "
                            "voice, and the next song starts quietly underneath about halfway "
                            "through. Keep one continuous flow and name the next song in the "
                            "second half, like a DJ talking over an intro.\n")

    @staticmethod
    def build_prompt(facts: Facts, has_previous: bool, now: datetime | None = None) -> str:
        """The user message. Contains no Spotify data: only facts, placeholders and the time."""
        now = now or datetime.now()
        part = ("night" if now.hour < 5 else "morning" if now.hour < 12 else
                "afternoon" if now.hour < 17 else "evening" if now.hour < 22 else "night")
        parts = [f"LOCAL TIME\n{now:%A %H:%M} ({part}). If you greet the listener or mention the "
                 "time of day, match this, even if your personality describes another time of day: "
                 "keep the personality's style, not its clock.",
                 "NEXT SONG\nRefer to it as {song} by {artist}."]
        if has_previous:
            parts.append("SONG THAT JUST FINISHED\nYou may mention it in passing as {prev_song} "
                         "by {prev_artist}. Say nothing else about it.")
        parts.append("FACTS ABOUT THE NEXT SONG (from Wikipedia and MusicBrainz)\n" + (
            "\n".join(f"- {f}" for f in facts.items) if facts else "(none)"))
        parts.append("Write the talk break now.")
        return "\n\n".join(parts)

    def fill(self, text: str, next_track: Track, previous: Track | None) -> str:
        """Put the real names into the placeholders, locally."""
        values = {"song": base_title(next_track.title),
                  "artist": _join_artists(next_track.artists, self.lang)}
        if previous:
            values.update(prev_song=base_title(previous.title),
                          prev_artist=_join_artists(previous.artists, self.lang))
        text = PLACEHOLDER.sub(lambda m: values.get((m.group(1) or "").lower(), m.group(0)), text)
        if PLACEHOLDER.search(text):
            raise ScriptError(f"unknown placeholder left in script: {PLACEHOLDER.search(text).group(0)}")
        return text

    def write(self, next_track: Track, facts: Facts, previous: Track | None = None) -> str:
        user = self.build_prompt(facts, previous is not None)

        text = self._claude(user) if self.llm.provider == "anthropic" else self._ollama(user)
        text = text.strip('"“”„').strip()
        if not text:
            raise ScriptError("LLM returned no text")
        text = self.fill(text, next_track, previous)
        words = len(text.split())
        if words > self.max_words * 1.4:
            raise ScriptError(f"script too long ({words} words, max {self.max_words})")
        return text

    def _claude(self, user: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.llm.model,
                max_tokens=4000,
                system=self.system,
                output_config={"effort": self.llm.effort},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIConnectionError as e:
            raise ScriptError(f"Anthropic API unreachable: {e}") from e
        except anthropic.RateLimitError as e:
            raise ScriptError("Anthropic rate limit") from e
        except anthropic.APIStatusError as e:
            raise ScriptError(f"Anthropic API error {e.status_code}: {e.message}") from e

        u = response.usage
        price = PRICES.get(self.llm.model)
        cost = (f", ~${(u.input_tokens * price[0] + u.output_tokens * price[1]) / 1e6:.4f}"
                if price else "")
        log.info("LLM usage: %d in / %d out tokens%s", u.input_tokens, u.output_tokens, cost)
        if response.stop_reason != "end_turn":
            raise ScriptError(f"LLM stopped with {response.stop_reason}")
        return " ".join(b.text for b in response.content if b.type == "text").strip()

    def _ollama(self, user: str) -> str:
        try:
            r = self.http.post("/api/chat", json={
                "model": self.llm.ollama_model,
                "stream": False,
                "messages": [{"role": "system", "content": self.system},
                             {"role": "user", "content": user}],
                "options": {"temperature": 0.7, "num_predict": 400},
            })
        except httpx.HTTPError as e:
            raise ScriptError(f"Ollama unreachable at {self.llm.ollama_url}: {e}") from e
        if r.status_code == 404:
            raise ScriptError(f"Ollama has no model '{self.llm.ollama_model}' "
                              f"(run: ollama pull {self.llm.ollama_model})")
        if r.status_code != 200:
            raise ScriptError(f"Ollama error {r.status_code}: {r.text[:200]}")
        data = r.json()
        log.info("LLM usage (Ollama %s): %s in / %s out tokens, %.1fs", self.llm.ollama_model,
                 data.get("prompt_eval_count", "?"), data.get("eval_count", "?"),
                 data.get("total_duration", 0) / 1e9)
        if data.get("done_reason") == "length":
            raise ScriptError("local model ran out of tokens")
        return THINK_BLOCK.sub("", data.get("message", {}).get("content", "")).strip()


def ollama_models(url: str) -> list[str]:
    """Models installed in an Ollama server (raises httpx.HTTPError if unreachable)."""
    r = httpx.get(url.rstrip("/") + "/api/tags", timeout=5)
    r.raise_for_status()
    return sorted(m["name"] for m in r.json().get("models", []))
