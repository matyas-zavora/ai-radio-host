"""Main loop: follow playback, count tracks, prepare and run talk breaks."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .audio import ClipPlayer
from .config import CACHE_DIR, Config
from .ducking import Ducker
from .facts import FactFinder
from .models import PlayerState, Track
from .player import PlayerControl, PlayerUnavailable
from .script import ScriptWriter
from .spotify_api import RateLimited, SpotifyClient, SpotifyError
from .tts import TTSProvider, wav_seconds

log = logging.getLogger("radiohost")

TICK = 0.25                 # seconds between player polls
RECHECK_AT_MS = 30_000      # re-read the queue this long before the end (time to regenerate)
FINAL_CHECK_AT_MS = 8_000   # last queue check; a mismatch after this skips the break
MIN_REGEN_MS = 15_000       # don't start regenerating with less time left than this
LATE_BREAK_MS = 4_000       # missed the pre-end pause? still break if the new song is this fresh


@dataclass
class Break:
    next: Track
    script: str
    audio: Path | None   # None in dry-run
    seconds: float = 0.0  # clip length (estimated in dry-run)


@dataclass
class Plan:
    """Everything about the break that follows the current track."""
    for_track: Track
    next: Track | None = None
    gen: Future | None = None
    check: Future | None = None
    check_kind: str = ""
    rechecked: bool = False
    final_checked: bool = False
    verified: bool = False
    retry_at: float = 0.0
    cancelled: str | None = None
    done: bool = False
    pause_instead: bool = False   # talking over the music isn't possible for this break
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> Break | None:
        if self.gen and self.gen.done() and not self.gen.exception():
            return self.gen.result()
        return None


class RadioHost:
    def __init__(self, cfg: Config, player: PlayerControl, spotify: SpotifyClient,
                 tts: TTSProvider | None, dry_run: bool = False):
        self.cfg = cfg
        self.player = player
        self.spotify = spotify
        self.tts = tts
        self.dry_run = dry_run
        self.facts = FactFinder([cfg.host.language])
        self.writer = ScriptWriter(cfg.llm, cfg.host, cfg.brk)
        self.clip = None if dry_run else ClipPlayer(cfg.audio_command, cfg.audio_volume)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="radiohost")

        self.cur: Track | None = None
        self.cur_max_pos = 0
        self.cur_counted = False   # set when a pre-end break already used up this track
        self.was_playing = False
        self.counter = 0
        self.plan: Plan | None = None
        self.last_break_for: Track | None = None
        self._unavailable_since: float | None = None
        # read by the GUI
        self.last_state: PlayerState | None = None
        self.history: deque[dict] = deque(maxlen=50)   # generated breaks, newest last
        self._stop = threading.Event()
        self.talking: Track | None = None   # set while a break clip is playing
        self.make_ducker = lambda: Ducker(cfg.brk.duck_level)
        self.force_break = False            # "break after this song", set by the GUI

    def stop(self) -> None:
        self._stop.set()

    def request_break(self) -> None:
        """Break at the end of the current song, regardless of the counter.
        If it's too late to prepare, it carries over to the next song."""
        self.force_break = True
        log.info("Break requested for the end of this song")

    def plan_status(self) -> str:
        p = self.plan
        if p is None:
            return ""
        if p.cancelled:
            return f"skipped: {p.cancelled}"
        if p.done:
            return "done"
        if p.next is None:
            return "reading queue…"
        if p.gen and not p.gen.done():
            return f"preparing break for {p.next}…"
        if p.verified:
            return f"ready: {p.next} (verified)"
        if p.ready:
            return f"ready: {p.next}"
        return f"next: {p.next}"

    # ---------- generation (worker threads) ----------

    def generate(self, nxt: Track, previous: Track | None) -> Break:
        t0 = time.monotonic()
        facts = self.facts.find(nxt)
        log.info("Facts for %s: %d found", nxt, len(facts.items))
        for f in facts.items:
            log.debug("  fact: %s", f)
        script = self.writer.write(nxt, facts, previous)
        log.info("Script for %s (%d words):\n    %s", nxt, len(script.split()), script)
        entry = {"time": time.strftime("%H:%M"), "track": nxt, "script": script,
                 "facts": len(facts.items), "audio": None}
        self.history.append(entry)
        audio, seconds = None, len(script.split()) / 2.4
        if not self.dry_run:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in CACHE_DIR.glob("break-*.wav"):
                if time.time() - old.stat().st_mtime > 3600:
                    old.unlink(missing_ok=True)
            audio = self.tts.synthesize(script, CACHE_DIR / f"break-{int(time.time() * 1000)}.wav")
            entry["audio"] = audio
            seconds = wav_seconds(audio)
            log.info("Audio ready: %.1fs clip via %s (%.1fs to prepare)",
                     seconds, self.tts.name, time.monotonic() - t0)
        return Break(nxt, script, audio, seconds)

    # ---------- timing ----------

    @property
    def _ducking(self) -> bool:
        return self.cfg.brk.transition == "duck"

    def _lead_ms(self) -> int:
        """Worst-case time before the song's end at which the break starts."""
        b = self.cfg.brk
        return b.max_seconds * 500 + b.fade_down_ms if self._ducking else b.lead_ms

    def _final_at(self) -> int:
        return max(FINAL_CHECK_AT_MS, self._lead_ms() + 4000)

    def _min_regen(self) -> int:
        return max(MIN_REGEN_MS, self._final_at() + 7000)

    def _recheck_at(self) -> int:
        return max(RECHECK_AT_MS, self._min_regen() + 10_000)

    def _queue(self) -> tuple[Track | None, list[Track]]:
        return self.spotify.queue()

    # ---------- main loop ----------

    def run(self) -> None:
        n = self.cfg.brk.every_n_tracks
        Ducker.recover()
        log.info("Radio host on air%s: break every %d track%s, language %s, model %s",
                 " (DRY RUN)" if self.dry_run else "", n, "s" if n != 1 else "",
                 self.cfg.host.language,
                 self.cfg.llm.model if self.cfg.llm.provider == "anthropic"
                 else f"{self.cfg.llm.ollama_model} (Ollama)")
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(TICK)
            log.info("Off air.")
        finally:
            if self.clip:
                self.clip.stop()
            self.pool.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> None:
        try:
            state = self.player.state()
        except PlayerUnavailable as e:
            if self._unavailable_since is None:
                self._unavailable_since = time.monotonic()
                log.warning("Player not reachable (%s); waiting for it to come back", e)
            self.last_state = None
            self._stop.wait(2)
            return
        if self._unavailable_since is not None:
            log.info("Player reachable again")
            self._unavailable_since = None
        self.last_state = state
        if state is None or state.track is None:
            return

        if not state.track.same_as(self.cur):
            self.on_track_change(state)
        else:
            self.cur_max_pos = max(self.cur_max_pos, state.position_ms)
        self.was_playing = state.playing

        if self.counter + 1 < self.cfg.brk.every_n_tracks and not self.force_break:
            return  # no break after this track
        if self.plan is None:
            self.plan = Plan(for_track=state.track)
            log.info("Break due after this track (%s); shuffle=%s", state.track, state.shuffle)
        self.step_plan(state)

        p = self.plan
        if not state.playing or p.done or p.cancelled:
            return
        brk = p.ready
        if self._ducking and not p.pause_instead and brk and p.verified:
            start_at = brk.seconds * 500 + self.cfg.brk.fade_down_ms  # next song starts mid-clip
            if state.remaining_ms <= start_at:
                if state.duration_ms < start_at + 5000:
                    p.pause_instead = True
                    log.info("Song too short to talk over; pausing for this break")
                elif not self.dry_run and not self.make_ducker().available():
                    p.pause_instead = True
                    log.info("Can't lower the player's volume (music not on this computer?); "
                             "pausing for this break")
                else:
                    self.do_transition(state, brk)
                    return
        if state.remaining_ms <= self.cfg.brk.lead_ms:
            if p.verified and brk:
                self.do_break(state, brk)
            else:
                self.cancel(p, "break not ready/verified when the track ended")

    def on_track_change(self, state: PlayerState) -> None:
        prev, played = self.cur, self.cur_max_pos
        new = state.track
        if prev is not None and not self.cur_counted:
            if played >= self.cfg.brk.min_played_seconds * 1000:
                self.counter += 1
                log.debug("Counted %s (played %ds)", prev, played // 1000)
            else:
                log.info("Not counted %s (played only %ds)", prev, played // 1000)

        p = self.plan
        if self.last_break_for is not None:
            if new.same_as(self.last_break_for):
                log.info("Prediction matched: break introduced %s", new)
            else:
                log.warning("Break introduced %s but %s is playing", self.last_break_for, new)
            self.last_break_for = None
        elif (p and not p.done and not p.cancelled and p.ready and p.next
              and new.same_as(p.next) and state.playing and self.was_playing
              and state.position_ms < LATE_BREAK_MS and not self.dry_run):
            log.info("Missed the pre-end pause; doing the break at the start of %s", new)
            self.do_break(state, p.ready, late=True)
        elif p and not p.done:
            if p.next and not new.same_as(p.next):
                self.cancel(p, f"{new} started instead of {p.next}")
            elif not p.cancelled:
                self.cancel(p, "track changed before the break")

        self.plan = None
        self.cur = new
        self.cur_max_pos = state.position_ms
        self.cur_counted = False
        log.info("Now playing: %s [%s] (%d/%d toward next break)", new,
                 _fmt(state.duration_ms), self.counter, self.cfg.brk.every_n_tracks)

    def cancel(self, p: Plan, reason: str) -> None:
        if not p.cancelled:
            p.cancelled = reason
            log.info("Skipping break: %s", reason)

    # ---------- planning ----------

    def _submit_check(self, p: Plan, kind: str) -> None:
        p.check_kind = kind
        p.check = self.pool.submit(self._queue)

    def step_plan(self, state: PlayerState) -> None:
        p = self.plan
        if p.done or p.cancelled:
            return
        now = time.monotonic()
        rem = state.remaining_ms

        if p.gen and p.gen.done() and p.gen.exception():
            e = p.gen.exception()
            log.debug("generation error", exc_info=e)
            self.cancel(p, f"generating the break failed: {type(e).__name__}: {e}")
            return

        if p.check is not None:
            if p.check.done():
                self._handle_check(p, state)
            return

        if now < p.retry_at:
            return
        if p.next is None:
            self._submit_check(p, "initial")
        elif not p.rechecked and self._min_regen() < rem <= self._recheck_at():
            p.rechecked = True
            self._submit_check(p, "recheck")
        elif not p.final_checked and rem <= self._final_at():
            p.final_checked = True
            self._submit_check(p, "final")

    def _handle_check(self, p: Plan, state: PlayerState) -> None:
        kind, fut = p.check_kind, p.check
        p.check = None
        rem = state.remaining_ms

        def retry(delay: float, why: str) -> None:
            if kind == "final" and rem < self._lead_ms() + delay * 1000:
                self.cancel(p, f"final queue check failed ({why})")
                return
            log.debug("queue check (%s) retry in %.1fs: %s", kind, delay, why)
            p.retry_at = time.monotonic() + delay
            if kind == "recheck":
                p.rechecked = False
            if kind == "final":
                p.final_checked = False

        try:
            api_cur, queue = fut.result()
        except RateLimited as e:
            return retry(e.retry_after, "rate limited")
        except SpotifyError as e:
            log.warning("Queue check failed: %s", e)
            return retry(5.0, str(e))
        except Exception as e:  # keep the music going no matter what
            log.warning("Queue check crashed: %s", e)
            return retry(5.0, str(e))

        if api_cur is None or not api_cur.same_as(p.for_track):
            return retry(1.5, f"Web API still reports {api_cur}")  # API lags right after a change
        nxt = queue[0] if queue else None
        if nxt is None:
            return self.cancel(p, "queue is empty")
        if nxt.uri and not nxt.uri.startswith("spotify:track:"):
            return self.cancel(p, f"next item is not a song ({nxt.uri})")

        if p.next is not None and nxt.same_as(p.next):
            if kind == "final":
                p.verified = True
                log.debug("Final check OK: next is %s", nxt)
            return
        if p.next is not None:
            log.info("Queue changed: next is now %s (was %s)", nxt, p.next)
        if rem < self._min_regen():
            if p.next is None:
                return self.cancel(p, "too little time left to prepare a break")
            return self.cancel(p, f"queue changed to {nxt} too late to regenerate")
        p.next = nxt
        p.verified = False
        log.info("Up next: %s%s — preparing break", nxt,
                 f" ({nxt.year})" if nxt.year else "")
        p.gen = self.pool.submit(self.generate, nxt, p.for_track)

    # ---------- the break ----------

    def do_transition(self, state: PlayerState, brk: Break) -> None:
        """Talk over the end of this song and the start of the next one: lower the music,
        play the clip (the next song starts by itself halfway through), raise the music."""
        p = self.plan
        if self.dry_run:
            p.done, self.force_break, self.counter, self.cur_counted = True, False, 0, True
            log.info("[dry run] Would lower the music now and introduce %s over it", brk.next)
            return
        ducker = self.make_ducker()
        if not ducker.duck(self.cfg.brk.fade_down_ms):
            p.pause_instead = True   # the stream vanished; pause at the end instead
            return
        p.done, self.force_break, self.counter, self.cur_counted = True, False, 0, True
        log.info("ON AIR over the music: introducing %s", brk.next)
        self.clip.start(brk.audio)
        self.talking = brk.next
        ending, coming = state.track, brk.next
        user_paused = False
        while self.clip.running():
            time.sleep(0.2)
            ducker.hold()
            if self._stop.is_set():
                self.clip.stop()
                break
            try:
                s = self.player.state()
            except PlayerUnavailable:
                continue
            if s is None or s.track is None:
                continue
            if s.status == "Paused":
                log.info("You paused; stopping the break")
                self.clip.stop()
                user_paused = True
                break
            if not (s.track.same_as(ending) or s.track.same_as(coming)):
                log.info("You changed the song; stopping the break")
                self.clip.stop()
                break
        self.talking = None
        if err := self.clip.failed():
            log.warning("Clip playback failed: %s", err)
        self.last_break_for = coming
        ducker.restore(300 if user_paused else self.cfg.brk.fade_up_ms)
        log.info("Back to the music")

    def do_break(self, state: PlayerState, brk: Break, late: bool = False) -> None:
        p = self.plan
        p.done = True
        self.force_break = False
        self.counter = 0
        if not late:
            self.cur_counted = True  # the ending track belongs to the cycle we just closed
        if self.dry_run:
            log.info("[dry run] Would pause now and introduce %s", brk.next)
            return

        expected = state.track  # the track that must stay loaded while we talk
        try:
            self.player.pause()
            if late:
                self.player.seek_start()
        except PlayerUnavailable as e:
            log.warning("Could not pause (%s); skipping break", e)
            return
        log.info("ON AIR: introducing %s", brk.next)
        self.clip.start(brk.audio)
        self.talking = brk.next

        user_took_over = False
        time.sleep(0.3)  # let the pause land before reading status
        while self.clip.running():
            time.sleep(0.2)
            try:
                s = self.player.state()
            except PlayerUnavailable:
                continue
            if s is None:
                continue
            if self._stop.is_set():
                log.info("Stopping: ending the break early")
                self.clip.stop()
                break
            if s.playing or (s.track and not s.track.same_as(expected)):
                log.info("You took over playback; stopping the break")
                self.clip.stop()
                user_took_over = True
                break
        self.talking = None
        if err := self.clip.failed():
            log.warning("Clip playback failed: %s", err)

        self.last_break_for = None if late else brk.next
        if user_took_over:
            return
        for attempt in range(3):
            try:
                s = self.player.state()
                if s is not None and s.status == "Paused" and s.track.same_as(expected):
                    self.player.play()
                log.info("Back to the music")
                return
            except PlayerUnavailable as e:
                log.warning("Resume attempt %d failed: %s", attempt + 1, e)
                time.sleep(1)
        log.error("Could not resume playback — press play in Spotify")


def _fmt(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"
