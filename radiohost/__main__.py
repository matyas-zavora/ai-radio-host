"""radiohost CLI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .config import LANGUAGES, load_config
from .player import PlayerUnavailable, build_player
from .spotify_api import NotAuthorized, SpotifyClient, SpotifyError

log = logging.getLogger("radiohost")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_auth(cfg, args) -> int:
    SpotifyClient(cfg.spotify_client_id, cfg.spotify_redirect_uri).login()
    return 0


def _spotify(cfg) -> SpotifyClient:
    client = SpotifyClient(cfg.spotify_client_id, cfg.spotify_redirect_uri)
    if not client.authorized:
        raise NotAuthorized("not logged in to Spotify yet, run: radiohost auth")
    return client


def _require_anthropic(cfg=None) -> None:
    if cfg is not None and cfg.llm.provider != "anthropic":
        return
    profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or profile_dir.exists()):
        raise NotAuthorized("ANTHROPIC_API_KEY is not set (put it in .env)")


def build_host(cfg, dry_run: bool):
    """Everything `run` needs, wired up. Raises NotAuthorized with a readable message."""
    from .loop import RadioHost
    from .tts import build_tts

    _require_anthropic(cfg)
    spotify = _spotify(cfg)
    player = build_player(cfg.player.backends, cfg.player.mpris_name, spotify)
    tts = None if dry_run else build_tts(cfg.tts, cfg.host.language)
    return RadioHost(cfg, player, spotify, tts, dry_run=dry_run)


def cmd_run(cfg, args) -> int:
    build_host(cfg, args.dry_run).run()
    return 0


def cmd_gui(cfg, args) -> int:
    from .gui import main as gui_main
    return gui_main()


def cmd_once(cfg, args) -> int:
    """Write (and speak) one break for whatever is next in the queue. Never touches playback."""
    from .audio import ClipPlayer
    from .loop import RadioHost
    from .tts import build_tts

    _require_anthropic(cfg)
    spotify = _spotify(cfg)
    current, queue = spotify.queue()
    if not queue:
        print("Queue is empty (or nothing is playing).")
        return 1
    tts = None if args.dry_run else build_tts(cfg.tts, cfg.host.language)
    host = RadioHost(cfg, player=None, spotify=spotify, tts=tts, dry_run=args.dry_run)
    try:
        brk = host.generate(queue[0], current)
    except Exception as e:  # ScriptError, TTSError, AudioError
        log.error("Break failed: %s", e)
        return 1
    if brk.audio:
        clip = ClipPlayer(cfg.audio_command, cfg.audio_volume)
        clip.start(brk.audio)
        while clip.running():
            time.sleep(0.2)
        if err := clip.failed():
            print(f"Playback failed: {err}")
    return 0


def cmd_queue_test(cfg, args) -> int:
    """Check that the Web API queue predicts the real next track (shuffle!). Read-only."""
    spotify = _spotify(cfg)
    player = build_player(cfg.player.backends, cfg.player.mpris_name, spotify)
    cur = predicted = None
    hits = misses = 0
    print("Watching track changes (Ctrl+C to stop). Don't skip while testing.")
    try:
        while True:
            try:
                s = player.state()
            except PlayerUnavailable as e:
                print(f"player unavailable: {e}")
                time.sleep(2)
                continue
            if s and s.track and not s.track.same_as(cur):
                if predicted is not None:
                    ok = s.track.same_as(predicted)
                    hits, misses = hits + ok, misses + (not ok)
                    print(f"  {'MATCH' if ok else 'MISS '} predicted {predicted} | actual {s.track}"
                          f"   [{hits} match / {misses} miss]")
                cur, predicted = s.track, None
                print(f"Now: {cur} (shuffle={s.shuffle}, via {s.backend})")
            if s and s.track and predicted is None and s.remaining_ms <= 10_000:
                try:
                    api_cur, queue = spotify.queue()
                except SpotifyError as e:
                    print(f"  queue error: {e}")
                    time.sleep(2)
                    continue
                if api_cur and api_cur.same_as(s.track):
                    predicted = queue[0] if queue else None
                    print(f"  queue[0] = {predicted}; next 3: {[str(t) for t in queue[:3]]}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{hits} match / {misses} miss")
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="config.toml (default: ./config.toml, "
                                                    "then ~/.config/ai-radio-host/config.toml)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap = argparse.ArgumentParser(prog="radiohost", description="AI radio host for Spotify")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", parents=[common], help="run the radio host")
    once = sub.add_parser("once", parents=[common],
                          help="make one break for the next queued track and play it, "
                               "without touching Spotify playback")
    for p in (run, once):
        p.add_argument("--dry-run", action="store_true",
                       help="print scripts only: no TTS, no playback control")
        p.add_argument("--lang", choices=sorted(LANGUAGES), help="host language override")
    run.add_argument("--every", type=int, help="break every N tracks")
    sub.add_parser("auth", parents=[common], help="log in to Spotify (PKCE)")
    sub.add_parser("gui", parents=[common], help="open the app window")
    sub.add_parser("queue-test", parents=[common],
                   help="check queue predictions against real track changes")

    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
        if getattr(args, "lang", None):
            cfg.host.language = args.lang
        if getattr(args, "every", None):
            cfg.brk.every_n_tracks = max(1, args.every)
        handler = {"run": cmd_run, "once": cmd_once, "auth": cmd_auth,
                   "queue-test": cmd_queue_test, "gui": cmd_gui}[args.cmd]
        return handler(cfg, args)
    except KeyboardInterrupt:
        log.info("Off air.")
        return 0
    except (NotAuthorized, ValueError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
