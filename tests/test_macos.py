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


def test_duck_and_restore_reach_exact_volumes(monkeypatch, tmp_path):
    volumes = {0: 80}

    def set_volume(index, percent):
        volumes[index] = percent - 1   # Spotify on macOS reads back one less than it was set to
    monkeypatch.setattr(ducking, "player_streams", lambda *a: dict(volumes))
    monkeypatch.setattr(ducking, "_set", set_volume)
    monkeypatch.setattr(ducking, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ducking, "STATE_FILE", tmp_path / "ducked.json")
    d = ducking.Ducker(level_percent=25)
    assert d.duck(fade_ms=0)
    assert volumes[0] == 20 - 1 and not d.released
    d.hold()                                       # the off-by-one isn't mistaken for the user
    assert not d.released
    d.restore(fade_ms=0)
    assert volumes[0] == 80 - 1
    assert not (tmp_path / "ducked.json").exists()
