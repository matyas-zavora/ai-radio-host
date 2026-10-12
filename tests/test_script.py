import pytest

from radiohost.config import BreakConfig, HostConfig, LLMConfig
from radiohost.models import Facts, Track
from radiohost.script import ScriptError, ScriptWriter

NEXT = Track("Bohemian Rhapsody - Remastered 2011", ("Queen",), "A Night At The Opera (2011 Remaster)",
             uri="spotify:track:abc", release_date="2011-01-01")
PREV = Track("The Days - NOTION Extended Remix", ("Chrystal", "NOTION", "NOTION"))


def writer(lang="en"):
    return ScriptWriter(LLMConfig(provider="ollama"), HostConfig(language=lang), BreakConfig())


def test_prompt_contains_no_spotify_data():
    facts = Facts(["Wikipedia (en, song 'Bohemian Rhapsody'): a song by Queen."])
    prompt = writer().build_prompt(facts, has_previous=True) + writer().system
    for spotify_value in ("Remastered 2011", "2011 Remaster", "2011-01-01", "spotify:track",
                          "The Days", "Chrystal", "NOTION"):
        assert spotify_value not in prompt
    assert "{song}" in prompt and "{prev_song}" in prompt


def test_fill_placeholders():
    w = writer()
    out = w.fill("That was {prev_song} by {prev_artist}. Here's {Song} by { artist }.", NEXT, PREV)
    assert out == "That was The Days by Chrystal and NOTION. Here's Bohemian Rhapsody by Queen."
    assert writer("cs").fill("{prev_artist}", NEXT, PREV) == "Chrystal a NOTION"


def test_unknown_or_unfillable_placeholder_fails():
    with pytest.raises(ScriptError):
        writer().fill("Here's {album}.", NEXT, None)
    with pytest.raises(ScriptError):
        writer().fill("That was {prev_song}.", NEXT, None)   # no previous song known
