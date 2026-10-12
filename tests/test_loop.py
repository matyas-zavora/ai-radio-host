"""Offline simulation of the main loop with a fake player and queue."""

import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

from radiohost import loop as loop_mod
from radiohost.config import Config
from radiohost.loop import Break, RadioHost
from radiohost.models import PlayerState, Track


def T(n: int) -> Track:
    return Track(title=f"Song {n}", artists=(f"Artist {n}",), uri=f"spotify:track:{n}",
                 duration_ms=60_000)


class FakePlayer:
    def __init__(self):
        self.track, self.pos, self.status = T(1), 0, "Playing"
        self.calls: list[str] = []
        self.on_state = None  # hook to simulate the user during a break

    def state(self):
        if self.on_state:
            self.on_state(self)
        return PlayerState(self.status, self.track, self.pos, 60_000, shuffle=True, backend="fake")

    def pause(self):
        self.calls.append("pause")
        self.status = "Paused"

    def play(self):
        self.calls.append("play")
        self.status = "Playing"

    def seek_start(self):
        self.calls.append("seek_start")
        self.pos = 0


class FakeSpotify:
    def __init__(self, player: FakePlayer):
        self.player = player
        self.next: dict[str, Track | None] = {}

    def queue(self):
        cur = self.player.track
        nxt = self.next.get(cur.uri, T(int(cur.uri.split(":")[-1]) + 1))
        return cur, [nxt] if nxt else []


class FakeClip:
    def __init__(self):
        self.started = []
        self._running = 0

    def start(self, path):
        self.started.append(path)
        self._running = 3

    def running(self):
        self._running -= 1
        return self._running > 0

    def stop(self):
        self._running = 0

    def failed(self):
        return None


class FakeDucker:
    def __init__(self, player, available=True):
        self.player, self._available = player, available
        self.events = []

    def available(self):
        return self._available

    def duck(self, fade_ms):
        self.events.append(("duck", self.player.pos))
        return self._available

    def hold(self):
        pass

    def restore(self, fade_ms):
        self.events.append(("restore", fade_ms))


class InlinePool:
    """Runs submitted work synchronously so the test is deterministic."""
    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, **kw):
        pass


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)
    cfg = Config()
    cfg.brk.every_n_tracks = 2
    cfg.brk.transition = "pause"
    player = FakePlayer()
    spotify = FakeSpotify(player)
    h = RadioHost.__new__(RadioHost)
    h.cfg, h.player, h.spotify, h.tts, h.dry_run = cfg, player, spotify, None, False
    h.clip, h.pool = FakeClip(), InlinePool()
    h.cur, h.cur_max_pos, h.cur_counted, h.was_playing = None, 0, False, False
    h.counter, h.plan, h.last_break_for, h._unavailable_since = 0, None, None, None
    h.generated = []
    h.last_state, h.history, h._stop = None, [], threading.Event()
    h.talking, h.force_break = None, False

    def generate(nxt, prev):
        h.generated.append(nxt)
        return Break(nxt, f"intro {nxt.title}", Path("/dev/null"), seconds=20)
    h.generate = generate
    h.ducker = FakeDucker(player)
    h.make_ducker = lambda: h.ducker
    return h


def play_through(h, start=0, end=60_000, step=250):
    p = h.player
    for pos in range(start, end + 1, step):
        p.pos = pos
        h.tick()


def next_track(h, n):
    h.player.track, h.player.pos = T(n), 0
    h.tick()


def test_break_after_every_second_track(host):
    play_through(host)                 # track 1 ends: no break (1/2)
    assert host.clip.started == []
    next_track(host, 2)
    play_through(host)                 # track 2 ends: break for track 3
    assert host.generated == [T(3)]
    assert len(host.clip.started) == 1
    assert host.player.calls == ["pause", "play"]
    next_track(host, 3)
    assert host.counter == 0           # track 2 belongs to the closed cycle
    play_through(host)
    next_track(host, 4)
    assert host.counter == 1
    play_through(host)
    assert len(host.clip.started) == 2  # breaks again after 2 more tracks


def test_skipped_short_track_does_not_count(host):
    play_through(host, end=10_000)
    next_track(host, 2)                # track 1 played 10s: not counted
    assert host.counter == 0


def test_queue_change_regenerates(host):
    host.counter = 1
    play_through(host, end=5_000)      # plan made: next = 2
    host.spotify.next["spotify:track:1"] = T(9)
    play_through(host, start=5_000)    # 30s recheck sees 9 → regenerate
    assert host.generated == [T(2), T(9)]
    assert len(host.clip.started) == 1


def test_late_queue_change_skips_break(host):
    host.counter = 1
    play_through(host, end=45_000)
    host.spotify.next["spotify:track:1"] = T(9)   # after the 30s recheck, before the final one
    play_through(host, start=45_000)
    assert host.clip.started == []
    assert host.player.calls == []
    assert "too late" in host.plan.cancelled


def test_manual_pause_is_respected(host):
    host.counter = 1
    play_through(host, end=50_000)
    host.player.status = "Paused"
    play_through(host, start=50_000)   # paused through the end window
    assert host.player.calls == []
    assert host.clip.started == []


def test_user_presses_play_during_break(host):
    host.counter = 1

    def user(p):
        if p.calls == ["pause"]:
            p.status = "Playing"       # user hits play mid-break
    host.player.on_state = user
    play_through(host)
    assert host.player.calls == ["pause"]  # we never pressed play


def test_slow_pause_status_does_not_stop_the_break(host):
    host.counter = 1
    stale = {"polls": 3}

    def slow_pause(p):              # Spotify on macOS: "Playing" for a moment after pause
        if p.calls == ["pause"] and stale["polls"] > 0:
            stale["polls"] -= 1
            p.status = "Playing"
        elif p.calls == ["pause"]:
            p.status = "Paused"
    host.player.on_state = slow_pause
    play_through(host)
    assert len(host.clip.started) == 1
    assert host.player.calls == ["pause", "play"]   # clip ran to the end, then we resumed


def test_empty_queue_skips_and_keeps_counter(host):
    host.counter = 1
    host.spotify.next["spotify:track:1"] = None
    play_through(host)
    assert host.clip.started == []
    next_track(host, 2)
    assert host.counter == 2           # break is still due on the next track
    play_through(host)
    assert len(host.clip.started) == 1


def test_generation_failure_keeps_music(host):
    def boom(nxt, prev):
        raise RuntimeError("LLM down")
    host.generate = boom
    host.counter = 1
    play_through(host)
    assert host.player.calls == []
    assert "failed" in host.plan.cancelled


def test_break_now_ignores_counter(host):
    host.request_break()               # counter is 0, normally no break for 2 songs
    play_through(host)
    assert len(host.clip.started) == 1
    assert host.force_break is False


def test_break_now_too_late_carries_over(host):
    play_through(host, end=56_000)     # 4s left: too late to prepare
    host.request_break()
    play_through(host, start=56_000)
    assert host.clip.started == []
    next_track(host, 2)
    assert host.force_break is True    # still wanted at the end of song 2
    play_through(host)
    assert len(host.clip.started) == 1


# ---------- talking over the music ----------

def test_transition_talks_over_the_song_end_without_pausing(host):
    host.cfg.brk.transition = "duck"
    host.counter = 1
    play_through(host)
    assert len(host.clip.started) == 1
    assert host.player.calls == []                       # never paused
    (kind, pos), (kind2, fade) = host.ducker.events
    assert kind == "duck" and kind2 == "restore" and fade == host.cfg.brk.fade_up_ms
    # a 20 s clip: the next song starts halfway, so the voice starts ~10 s + fade before the end
    assert 60_000 - pos == pytest.approx(10_000 + host.cfg.brk.fade_down_ms, abs=300)
    next_track(host, 2)
    assert host.counter == 0                             # song 1 closed the cycle


def test_transition_falls_back_to_pause_when_volume_cant_be_lowered(host):
    host.cfg.brk.transition = "duck"
    host.ducker._available = False                       # e.g. music plays on a phone
    host.counter = 1
    play_through(host)
    assert host.player.calls == ["pause", "play"]
    assert len(host.clip.started) == 1


def test_user_pausing_during_transition_restores_volume_fast_and_stays_paused(host):
    host.cfg.brk.transition = "duck"
    host.counter = 1

    def user(p):
        if host.ducker.events and p.status == "Playing" and host.talking:
            p.status = "Paused"
    host.player.on_state = user
    play_through(host)
    assert host.player.calls == []                       # we never press play
    assert host.ducker.events[-1] == ("restore", 300)


# ---------- the READY / ON AIR timers ----------

def test_break_eta_counts_songs_then_time(host):
    host.cfg.brk.every_n_tracks = 3
    play_through(host, end=1_000)
    assert host.break_eta() == ("songs", 2)              # break after the 3rd song
    play_through(host, start=1_000)
    next_track(host, 2)
    assert host.break_eta() == ("songs", 1)
    play_through(host)
    next_track(host, 3)
    host.player.pos = 20_000
    host.tick()
    # pause mode: the host starts talking lead_ms before the end of a 60 s song
    assert host.break_eta() == ("ms", 60_000 - 20_000 - host.cfg.brk.lead_ms)


def test_break_eta_in_duck_mode_uses_half_the_clip(host):
    host.cfg.brk.transition = "duck"
    host.counter = 1
    play_through(host, end=30_000)                       # break prepared, 20 s clip
    assert host.break_eta() == ("ms", 30_000 - (10_000 + host.cfg.brk.fade_down_ms))


def test_break_now_shows_time_not_songs(host):
    host.cfg.brk.every_n_tracks = 5
    play_through(host, end=1_000)
    host.request_break()
    assert host.break_eta()[0] == "ms"
