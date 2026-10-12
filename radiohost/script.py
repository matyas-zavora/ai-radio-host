"""Write the host's talk break with Claude."""

from __future__ import annotations

import logging
import os

import anthropic

from .config import LANGUAGES, BreakConfig, HostConfig, LLMConfig
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

Rules:
- Speak {language}. Song titles, artist and album names stay as they are.
- Length: {min_words}-{max_words} words ({min_s}-{max_s} seconds spoken). Never longer.
- Use ONLY the facts listed in the FACTS section. Never add trivia, dates, chart \
positions, awards, band members, anecdotes or anything else from your own memory, \
even if you are sure it is true.
- If FACTS is empty, keep it a simple, warm introduction of the song and artist, \
with no factual claims at all.
- The Spotify release date can belong to a reissue, remaster or compilation. Only \
call it the song's release year if nothing suggests a reissue.
- The listener is alone with their own playlist. Don't mention callers, \
advertising, the station name, the weather, or the time of day.
- You may mention the song that just finished in passing.
- Output only the words to be spoken: plain text, no quotes, no stage directions, \
no emojis, no markdown, no lists.
"""


class ScriptError(Exception):
    pass


class ScriptWriter:
    def __init__(self, llm: LLMConfig, host: HostConfig, brk: BreakConfig):
        # Keys not scoped to a workspace need the workspace ID on every request.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        self.client = anthropic.Anthropic(
            timeout=30.0, max_retries=2,
            default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        self.llm = llm
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

    @staticmethod
    def _describe(track: Track) -> str:
        lines = [f"Title: {track.title}", f"Artist: {track.artist}"]
        if track.album:
            lines.append(f"Album: {track.album}")
        if track.release_date:
            lines.append(f"Spotify album release date: {track.release_date}")
        return "\n".join(lines)

    def write(self, next_track: Track, facts: Facts, previous: Track | None = None) -> str:
        parts = [f"NEXT SONG\n{self._describe(next_track)}"]
        if previous:
            parts.append(f"SONG THAT JUST FINISHED\n{previous.artist} – {previous.title}")
        parts.append("FACTS\n" + ("\n".join(f"- {f}" for f in facts.items) if facts else "(none)"))
        parts.append("Write the talk break now.")

        try:
            response = self.client.messages.create(
                model=self.llm.model,
                max_tokens=4000,
                system=self.system,
                output_config={"effort": self.llm.effort},
                messages=[{"role": "user", "content": "\n\n".join(parts)}],
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
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        text = text.strip('"“”„').strip()
        if not text:
            raise ScriptError("LLM returned no text")
        words = len(text.split())
        if words > self.max_words * 1.4:
            raise ScriptError(f"script too long ({words} words, max {self.max_words})")
        return text
