"""macOS pieces that can be checked without a Mac: AppleScript parsing and the ducking fade."""

import pytest

from radiohost import ducking
from radiohost.player import PlayerUnavailable, parse_applescript_state


def test_applescript_state_parses():
    s = parse_applescript_state(
        "playing\tBohemian Rhapsody\tQueen\tA Night at the Opera\t"
        "spotify:track:4u7EnebtmKWzUH433cf5Qv\t354320\t12345\ttrue\n")
    assert s.status == "Playing" and s.shuffle is True
    assert s.track.title == "Bohemian Rhapsody" and s.track.artists == ("Queen",)
    assert s.track.uri == "spotify:track:4u7EnebtmKWzUH433cf5Qv"
    assert (s.position_ms, s.duration_ms) == (12345, 354320)


def test_applescript_not_running_and_stopped():
    with pytest.raises(PlayerUnavailable):
        parse_applescript_state("notrunning\n")
    assert parse_applescript_state("stopped\n") is None


def fake_spotify(monkeypatch, tmp_path, volume):
    """Spotify's AppleScript volume as measured on a Mac: set N reads back N-1, except 0 and 100."""
    state = {"volume": volume}

    def osascript(script):
        if "set sound volume to" in script:
            n = int(script.rsplit(" ", 1)[1])
            state["volume"] = n - 1 if 0 < n < 100 else n
            return ""
        return str(state["volume"])
    monkeypatch.setattr(ducking, "MAC", True)
    monkeypatch.setattr(ducking, "_osascript", osascript)
    monkeypatch.setattr(ducking, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ducking, "STATE_FILE", tmp_path / "ducked.json")
    return state


@pytest.mark.parametrize("start", [16, 80, 99, 100])
def test_duck_and_restore_reach_exact_volumes(monkeypatch, tmp_path, start):
    spotify = fake_spotify(monkeypatch, tmp_path, start)
    d = ducking.Ducker(level_percent=25)
    assert d.duck(fade_ms=0)
    assert spotify["volume"] == round(start * 0.25) and not d.released
    d.hold()
    assert not d.released
    d.restore(fade_ms=0)
    assert spotify["volume"] == start or (start == 99 and spotify["volume"] == 100)
    assert not (tmp_path / "ducked.json").exists()


def test_volume_does_not_drift_over_many_breaks(monkeypatch, tmp_path):
    spotify = fake_spotify(monkeypatch, tmp_path, 16)
    for _ in range(10):
        d = ducking.Ducker(level_percent=30)
        d.duck(fade_ms=0)
        d.restore(fade_ms=0)
    assert spotify["volume"] == 16
