"""GTK4 / libadwaita app: on-air control, live status, log, settings, voice picker."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .audio import AudioError, ClipPlayer  # noqa: E402
from .config import (CONFIG_DIR, DEV, LANGUAGES, PROJECT_DIR, Config, load_config,  # noqa: E402
                     save_config, voice_engine)
from .spotify_api import NotAuthorized, SpotifyClient  # noqa: E402
from . import voices as voice_catalog  # noqa: E402
from .tts import wav_seconds  # noqa: E402

APP_ID = "dev.zavoram.RadioHost"
log = logging.getLogger("radiohost")

CLAUDE_CHOICES = [
    ("anthropic:claude-sonnet-5", "Claude Sonnet 5", "Recommended · about $0.002 per break"),
    ("anthropic:claude-haiku-4-5", "Claude Haiku 4.5", "Cheaper · a bit plainer"),
    ("anthropic:claude-opus-5", "Claude Opus 5", "Most capable · about 2.5× the cost"),
]
REPO = "https://github.com/matyas-zavora/ai-radio-host"
CONTACT_LINKS = [
    ("Project page", "Source code, documentation and news", REPO),
    ("Report a bug", "Something doesn't work? Tell me on GitHub", f"{REPO}/issues/new?template=bug_report.yml"),
    ("Suggest an idea", "Share it in Discussions, where others can upvote it",
     f"{REPO}/discussions/new?category=ideas"),
    ("Check for updates", "Latest releases and what's new", f"{REPO}/releases"),
]
CREDITS = [
    ("Made with", "Vibecoded: designed by Matyáš Závora, code written by Claude (Claude Code)"),
    ("Facts", "Wikipedia and MusicBrainz"),
    ("Voices", "Google Cloud Text-to-Speech and Piper"),
    ("Scripts", "Claude by Anthropic, or Ollama"),
    ("Music", "Spotify. Not affiliated with or endorsed by Spotify"),
]


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("ai-radio-host")
    except Exception:  # noqa: BLE001 - running from an unusual checkout
        return "dev"


LOCAL_NOTE = "Free and private (Ollama) · invents facts more often, weak in Czech"
LANG_CODES = list(LANGUAGES)

# The studio light: grey OFF, green READY (on air, waiting), red pulsing ON AIR (talking).
CSS = """
.onair-sign {
  min-width: 280px;
  border-radius: 14px;
  padding: 14px 40px;
  font-size: 34px;
  font-weight: 900;
  letter-spacing: 8px;
  border: 3px solid;
  transition: background-color 250ms, color 250ms, border-color 250ms, box-shadow 250ms;
}
.onair-sign.off {
  background-color: #2b2b2b;
  color: #7c7c7c;
  border-color: #3d3d3d;
}
.onair-sign.ready {
  background-color: #16843a;
  color: #effff3;
  border-color: #3fd36b;
  text-shadow: 0 0 8px #b9ffd0;
  box-shadow: 0 0 18px 3px alpha(#2bd45f, 0.40), inset 0 0 12px alpha(white, 0.15);
}
.onair-sign.live {
  background-color: #d8150c;
  color: #fff6f0;
  border-color: #ff5a40;
  text-shadow: 0 0 10px #ffc9b8;
  animation: onair-pulse 1.6s ease-in-out infinite;
}
@keyframes onair-pulse {
  0%, 100% { box-shadow: 0 0 16px 3px alpha(#ff2a14, 0.45), inset 0 0 14px alpha(white, 0.18); }
  50%      { box-shadow: 0 0 38px 12px alpha(#ff2a14, 0.85), inset 0 0 18px alpha(white, 0.30); }
}
.onair-caption { margin-top: 10px; }
.onair-sign .timer { font-size: 20px; letter-spacing: 3px; font-weight: 800; }
"""


def _fmt(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def _combo(title: str, items: list[str], current: str, labels: list[str] | None = None):
    if current not in items:
        items = items + [current]
        if labels:
            labels = labels + [current]
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels or items))
    row.set_selected(items.index(current))
    row.items = items
    return row


def _spin(title: str, lo: float, hi: float, step: float, value: float, subtitle: str = ""):
    row = Adw.SpinRow.new_with_range(lo, hi, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_value(value)
    return row


class GuiLogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            GLib.idle_add(self.sink, self.format(record))
        except Exception:  # noqa: BLE001 - never let logging break the host
            pass


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App"):
        super().__init__(application=app, title="AI Radio Host")
        self.app = app
        self.set_default_size(760, 820)
        self.cfg: Config = load_config()
        self.clip: ClipPlayer | None = None
        self.history_rows: list[Gtk.Widget] = []
        self.history_len = 0

        self.toasts = Adw.ToastOverlay()
        stack = Adw.ViewStack()
        stack.add_titled_with_icon(self._build_onair(), "onair", "On Air", "media-playback-start-symbolic")
        stack.add_titled_with_icon(self._build_host(), "host", "Host", "audio-input-microphone-symbolic")
        stack.add_titled_with_icon(self._build_settings(), "settings", "Settings", "emblem-system-symbolic")
        stack.add_titled_with_icon(self._build_log(), "log", "Log", "utilities-terminal-symbolic")
        stack.add_titled_with_icon(self._build_contact(), "contact", "Contact", "help-about-symbolic")
        self.toasts.set_child(stack)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)

        self.connect("close-request", self._on_close)
        GLib.timeout_add(250, self._refresh)
        from .setup_wizard import setup_needed
        if setup_needed():
            GLib.idle_add(lambda: self.open_setup() and False)

    def open_setup(self) -> None:
        from .setup_wizard import SetupWizard

        def finished(go_on_air: bool) -> None:
            self.cfg = load_config()
            self._refresh_accounts()
            self.lang.set_selected(self.lang.items.index(self.cfg.host.language))
            self.h_name.set_text(self.cfg.host.name)
            self.h_personality.set_text(self.cfg.host.personality)
            self._load_ollama_models()
            self._load_voices()
            if go_on_air and not self.app.running:
                self.dryrun.set_active(False)
                self.onair.set_active(True)
        SetupWizard(finished).present(self)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def _clip(self) -> ClipPlayer | None:
        if self.clip is None:
            try:
                self.clip = ClipPlayer(self.cfg.audio_command, int(self.volume.get_value()))
            except AudioError as e:
                self.toast(str(e))
        return self.clip

    # ---------- On Air ----------

    def _build_onair(self):
        page = Adw.PreferencesPage()

        self.sign = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                            css_classes=["onair-sign", "off"])
        self.sign_text = Gtk.Label(label="OFF")
        self.sign_timer = Gtk.Label(label="\u00a0", css_classes=["timer"])  # keeps the height
        self.sign.append(self.sign_text)
        self.sign.append(self.sign_timer)
        self.clip_ends_at = 0.0
        self.sign_caption = Gtk.Label(label="", halign=Gtk.Align.CENTER, wrap=True,
                                      justify=Gtk.Justification.CENTER,
                                      css_classes=["dim-label", "onair-caption"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=24, margin_bottom=12)  # room for the glow
        box.append(self.sign)
        box.append(self.sign_caption)
        self.break_btn = Gtk.Button(label="Break after this song", halign=Gtk.Align.CENTER,
                                    margin_top=12, css_classes=["pill"], sensitive=False,
                                    tooltip_text="The host talks at the end of the current song")
        self.break_btn.connect("clicked", lambda _b: self.app.request_break())
        box.append(self.break_btn)
        g = Adw.PreferencesGroup()
        g.add(box)
        page.add(g)

        g = Adw.PreferencesGroup(description="Voice, language and AI model: Host tab.")
        self.onair = Adw.SwitchRow(title="On air", subtitle="Off")
        self.onair.connect("notify::active", self._on_onair_toggled)
        self.dryrun = Adw.SwitchRow(title="Dry run",
                                    subtitle="Only write scripts: no speech, no pausing")
        g.add(self.onair)
        g.add(self.dryrun)
        page.add(g)

        g = Adw.PreferencesGroup(title="Now")
        self.row_track = Adw.ActionRow(title="Now playing", subtitle="—")
        self.row_count = Adw.ActionRow(title="Toward next break", subtitle="—")
        self.row_next = Adw.ActionRow(title="Next break", subtitle="—")
        self.row_next.set_subtitle_lines(3)
        self.row_player = Adw.ActionRow(title="Player", subtitle="—")
        for w in (self.row_track, self.row_count, self.row_next, self.row_player):
            g.add(w)
        page.add(g)

        self.history_group = Adw.PreferencesGroup(
            title="Recent breaks", description="Newest first. Scripts appear here as they are written.")
        page.add(self.history_group)
        return page

    def _on_onair_toggled(self, row, _pspec):
        if row.get_active() and not self.app.running:
            self.cfg = load_config()
            error = self.app.start_host(self.cfg, self.dryrun.get_active())
            if error:
                self.toast(error)
                row.set_active(False)
        elif not row.get_active() and self.app.running:
            self.app.stop_host()
        self.dryrun.set_sensitive(not row.get_active())

    def _update_sign(self, host, running: bool) -> None:
        talking = host.talking if (host is not None and running) else None
        replaying = self.clip is not None and self.clip.running()
        lit = talking is not None or replaying
        if self.app.tray is not None:
            if talking is not None:
                self.app.tray.update("live", f"ON AIR: introducing {talking}")
            elif running:
                st = host.last_state
                now = f"\n{st.track}" if st and st.track else ""
                self.app.tray.update("on", ("Dry run" if host.dry_run else "On air") + now)
            else:
                self.app.tray.update("off", "Off air")
        state = "live" if lit else "ready" if running else "off"
        if not self.sign.has_css_class(state):
            for cls in ("off", "ready", "live"):
                self.sign.remove_css_class(cls)
            self.sign.add_css_class(state)
            self.sign_text.set_label({"off": "OFF", "ready": "READY", "live": "ON AIR"}[state])
        timer = ""
        if talking is not None:
            timer = _fmt(host.talk_remaining_ms())
        elif replaying:
            timer = _fmt(max(0, int((self.clip_ends_at - time.monotonic()) * 1000)))
        elif running:
            eta = host.break_eta()
            if eta and eta[0] == "songs":
                timer = f"{eta[1]} song{'s' if eta[1] != 1 else ''}"
            elif eta and eta[0] == "ms":
                timer = _fmt(eta[1])
            elif eta and eta[0] == "skipped":
                timer = "skipped"
        timer = timer or "\u00a0"   # no-break space: the sign keeps its height when empty
        if self.sign_timer.get_label() != timer:
            self.sign_timer.set_label(timer)
        if talking is not None:
            caption = f"Introducing {talking}"
        elif replaying:
            caption = "Playing a sample"
        elif running and host.dry_run:
            caption = "Dry run: the host only writes scripts"
        elif running:
            caption = "Waiting for the next break. The sign turns red when the host talks."
        else:
            caption = "Turn on air below to start"
        if self.sign_caption.get_label() != caption:
            self.sign_caption.set_label(caption)

    def _refresh(self) -> bool:
        if self.app.window is not self:
            return False  # replaced (e.g. after a reset): stop this timer
        host = self.app.host
        running = self.app.running
        self._update_sign(host, running)
        if not running:
            self.break_btn.set_sensitive(False)
            self.break_btn.set_label("Break after this song")
        elif host.force_break:
            self.break_btn.set_sensitive(False)
            self.break_btn.set_label("Break requested")
        else:
            self.break_btn.set_sensitive(host.talking is None)
            self.break_btn.set_label("Break after this song")
        if self.onair.get_active() != running:
            self.onair.set_active(running)
        if not running:
            self.onair.set_subtitle("Off")
        else:
            self.onair.set_subtitle("Dry run: writing scripts only" if host.dry_run
                                    else "Breaks are on")

        if host is not None:
            st = host.last_state
            if st and st.track:
                icon = "▶" if st.playing else "⏸"
                self.row_track.set_subtitle(
                    f"{icon} {GLib.markup_escape_text(str(st.track))}   "
                    f"{_fmt(st.position_ms)} / {_fmt(st.duration_ms)}")
                self.row_player.set_subtitle(
                    f"{st.backend} · shuffle {'on' if st.shuffle else 'off'}")
            elif running:
                self.row_track.set_subtitle("Waiting for Spotify…")
                self.row_player.set_subtitle("not reachable" if st is None else "nothing loaded")
            n = host.cfg.brk.every_n_tracks
            self.row_count.set_subtitle(f"{min(host.counter, n)} / {n} songs")
            status = host.plan_status() or ("—" if host.counter + 1 < n else "waiting…")
            self.row_next.set_subtitle(GLib.markup_escape_text(status))
            if len(host.history) != self.history_len:
                self._rebuild_history(list(host.history))
        return True

    def _rebuild_history(self, entries: list[dict]) -> None:
        for row in self.history_rows:
            self.history_group.remove(row)
        self.history_rows = []
        for e in reversed(entries):
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"{e['time']} · {e['track']}"),
                                subtitle=GLib.markup_escape_text(e["script"]))
            row.set_subtitle_lines(0)
            row.set_title_lines(1)
            facts = Gtk.Label(label=f"{e['facts']} fact{'s' if e['facts'] != 1 else ''}", css_classes=["dim-label", "caption"])
            row.add_suffix(facts)
            if e.get("audio") and Path(e["audio"]).exists():
                btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Play this break", css_classes=["flat"])
                btn.connect("clicked", lambda _b, p=e["audio"]: self._play(Path(p)))
                row.add_suffix(btn)
            self.history_group.add(row)
            self.history_rows.append(row)
        self.history_len = len(entries)

    def _play(self, path: Path) -> None:
        clip = self._clip()
        if clip:
            clip.start(path)
            self.clip_ends_at = time.monotonic() + wav_seconds(path)

    # ---------- Contact ----------

    def _build_contact(self):
        page = Adw.PreferencesPage()
        header = Adw.StatusPage(icon_name="dev.zavoram.RadioHost", title="AI Radio Host",
                                description="You pick the music. It does the talking.\n\n"
                                            f"Version {app_version()} · by Matyáš Závora\n"
                                            "Free and open source (MIT license)")
        g = Adw.PreferencesGroup()
        g.add(header)
        page.add(g)

        g = Adw.PreferencesGroup(title="Get in touch",
                                 description="Bug reports and ideas go through GitHub.")
        for title, subtitle, url in CONTACT_LINKS:
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            row.connect("activated", lambda r, u=url: Gtk.UriLauncher.new(u).launch(self, None, None, None))
            g.add(row)
        info = Adw.ActionRow(title="Copy system info",
                             subtitle="App, Python, GTK and player versions, to paste into a bug report",
                             activatable=True)
        info.add_suffix(Gtk.Image(icon_name="edit-copy-symbolic"))
        info.connect("activated", lambda *_: self._copy_system_info())
        g.add(info)
        page.add(g)

        g = Adw.PreferencesGroup(title="Credits")
        for title, subtitle in CREDITS:
            g.add(Adw.ActionRow(title=title, subtitle=subtitle))
        page.add(g)
        return page

    def _copy_system_info(self) -> None:
        import platform
        distro = "unknown"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip('"')
        except OSError:
            pass
        cfg = load_config()
        st = self.app.host.last_state if self.app.host else None
        lines = [
            f"AI Radio Host {app_version()}",
            f"Distribution: {distro}",
            f"Python {platform.python_version()}, GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}",
            f"Player backend: {st.backend if st else 'not on air'} (setting: {cfg.player.mpris_name})",
            f"Transition: {cfg.brk.transition}, script writer: "
            + (cfg.llm.model if cfg.llm.provider == "anthropic" else f"Ollama {cfg.llm.ollama_model}"),
            f"Voices: {', '.join(f'{lang}={cfg.tts.voice_for(lang)}' for lang in LANGUAGES)}",
        ]
        text = "\n".join(lines)   # no keys, no paths, no account data
        Gdk.Display.get_default().get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        self.toast("System info copied")

    # ---------- Log ----------

    def _build_log(self):
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        self.log_buf = self.log_view.get_buffer()
        scroll = Gtk.ScrolledWindow(child=self.log_view, vexpand=True)
        self.log_scroll = scroll
        return scroll

    def append_log(self, line: str) -> bool:
        buf = self.log_buf
        adj = self.log_scroll.get_vadjustment()
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        buf.insert(buf.get_end_iter(), line + "\n")
        if buf.get_line_count() > 3000:
            buf.delete(buf.get_start_iter(), buf.get_iter_at_line(500)[1])
        if at_bottom:
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        return False

    # ---------- Settings ----------

    def _build_settings(self):
        c = self.cfg
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Breaks")
        self.s_every = _spin("Break every", 1, 20, 1, c.brk.every_n_tracks, "songs")
        self.s_min = _spin("Shortest break", 5, 60, 1, c.brk.min_seconds, "seconds of speech")
        self.s_max = _spin("Longest break", 5, 90, 1, c.brk.max_seconds, "seconds of speech")
        self.s_played = _spin("Song counts after", 0, 180, 5, c.brk.min_played_seconds,
                              "seconds played (skips before that don't count)")
        self.s_transition = _combo("Transition", ["duck", "pause"], c.brk.transition,
                                   ["Talk over the music (smooth)", "Pause the music"])
        self.s_duck = _spin("Music volume while talking", 0, 100, 5, c.brk.duck_level)
        self.s_duck.connect("notify::value", lambda *_: self._duck_subtitle())
        self._duck_subtitle()
        self.s_lead = _spin("Pause before song end", 200, 3000, 100, c.brk.lead_ms,
                            "milliseconds (pause mode)")
        self.s_transition.connect("notify::selected", lambda *_: self._transition_rows())
        for w in (self.s_every, self.s_transition, self.s_duck, self.s_min, self.s_max,
                  self.s_played, self.s_lead):
            g.add(w)
        self._transition_rows()
        page.add(g)

        g = Adw.PreferencesGroup(title="Advanced")
        self.s_fallback = Adw.SwitchRow(title="Use a local voice if Google fails",
                                        subtitle="Otherwise that break is skipped",
                                        active=c.tts.fallback == "piper")
        self.s_budget = _spin("Google monthly limit", 0, 4_000_000, 50_000,
                              c.tts.google_monthly_char_limit, "characters (free tier: 1M for Chirp 3 HD)")
        self.s_ollama_url = Adw.EntryRow(title="Ollama server (for local models)", text=c.llm.ollama_url)
        for w in (self.s_fallback, self.s_budget, self.s_ollama_url):
            g.add(w)
        page.add(g)

        env_file = (PROJECT_DIR if DEV else CONFIG_DIR) / ".env"
        g = Adw.PreferencesGroup(title="Accounts", description=f"Keys live in {env_file}")
        self.acc_spotify = Adw.ActionRow(title="Spotify")
        self.acc_login = Gtk.Button(label="Log in", valign=Gtk.Align.CENTER)
        self.acc_login.connect("clicked", self._spotify_login)
        self.acc_spotify.add_suffix(self.acc_login)
        self.acc_anthropic = Adw.ActionRow(title="Anthropic")
        self.acc_google = Adw.ActionRow(title="Google Cloud TTS")
        for w in (self.acc_spotify, self.acc_anthropic, self.acc_google):
            g.add(w)
        rerun = Adw.ButtonRow(title="Run setup again", start_icon_name="emblem-system-symbolic")
        rerun.connect("activated", lambda *_: self.open_setup())
        g.add(rerun)
        page.add(g)
        self._refresh_accounts()

        save = Gtk.Button(label="Save breaks and advanced settings", halign=Gtk.Align.CENTER,
                          css_classes=["pill", "suggested-action"])
        save.connect("clicked", self._save_settings)
        g = Adw.PreferencesGroup()
        g.add(save)
        page.add(g)

        reset = Gtk.Button(label="Reset everything and set up again", halign=Gtk.Align.CENTER,
                           css_classes=["pill", "destructive-action"], margin_top=24)
        reset.connect("clicked", lambda *_: self._confirm_reset())
        g = Adw.PreferencesGroup()
        g.add(reset)
        page.add(g)
        return page

    def _duck_subtitle(self) -> None:
        v = int(self.s_duck.get_value())
        self.s_duck.set_subtitle(
            "Music goes silent while the host talks" if v == 0 else
            "Music stays at full volume" if v == 100 else
            f"Music plays at {v} % of its normal volume ({100 - v} % quieter)")

    def _transition_rows(self) -> None:
        duck = self.s_transition.items[self.s_transition.get_selected()] == "duck"
        self.s_duck.set_visible(duck)
        self.s_lead.set_visible(not duck)
        self.s_transition.set_subtitle(
            "The music fades down, the next song starts under the voice. Needs the music "
            "on this computer; otherwise it pauses." if duck
            else "The music stops while the host talks.")

    def _confirm_reset(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Reset everything?",
            body="This deletes all settings, the Spotify login and the saved keys "
                 "(Spotify Client ID, Claude key and workspace, Google key path), "
                 "then starts the setup again.\n\n"
                 "Kept: your Google key file, this month's Google usage counter and "
                 "downloaded voices.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Reset")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "reset" and self.app.reset_all())
        dialog.present(self)

    def _refresh_accounts(self) -> None:
        try:
            ok = SpotifyClient(self.cfg.spotify_client_id, self.cfg.spotify_redirect_uri).authorized
            self.acc_spotify.set_subtitle("Logged in" if ok else "Not logged in")
            self.acc_login.set_label("Log in again" if ok else "Log in")
        except NotAuthorized as e:
            self.acc_spotify.set_subtitle(str(e))
            self.acc_login.set_sensitive(False)
        if load_config().llm.provider == "ollama":
            anthropic_status = "Not needed: using a local model (Ollama)"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            anthropic_status = "API key set" + (
                " (with workspace ID)" if os.environ.get("ANTHROPIC_WORKSPACE_ID") else "")
        else:
            anthropic_status = "ANTHROPIC_API_KEY missing in .env"
        self.acc_anthropic.set_subtitle(anthropic_status)
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds and Path(creds).exists():
            self.acc_google.set_subtitle("Credentials file found")
        else:
            self.acc_google.set_subtitle("Not set up (see README → Google Cloud TTS)")

    def _spotify_login(self, _btn):
        self.acc_login.set_sensitive(False)
        self.toast("Approve the login in your browser…")

        def work():
            try:
                SpotifyClient(self.cfg.spotify_client_id, self.cfg.spotify_redirect_uri).login()
                msg = "Spotify login done"
            except Exception as e:  # noqa: BLE001
                msg = f"Spotify login failed: {e}"
            GLib.idle_add(lambda: (self.toast(msg), self._refresh_accounts(),
                                   self.acc_login.set_sensitive(True)) and False)
        threading.Thread(target=work, daemon=True).start()

    def _save_settings(self, _btn):
        def change(c):
            c.brk.every_n_tracks = int(self.s_every.get_value())
            c.brk.min_seconds = int(self.s_min.get_value())
            c.brk.max_seconds = max(int(self.s_max.get_value()), c.brk.min_seconds)
            c.brk.min_played_seconds = int(self.s_played.get_value())
            c.brk.lead_ms = int(self.s_lead.get_value())
            c.brk.transition = self.s_transition.items[self.s_transition.get_selected()]
            c.brk.duck_level = int(self.s_duck.get_value())
            c.tts.fallback = "piper" if self.s_fallback.get_active() else ""
            c.tts.google_monthly_char_limit = int(self.s_budget.get_value())
            c.llm.ollama_url = self.s_ollama_url.get_text().strip() or c.llm.ollama_url
        self._update_and_apply(change, "Settings saved.")
        self._load_ollama_models()

    # ---------- Host tab: language, name, personality, script writer, voice ----------

    def _build_host(self):
        c = self.cfg
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Host", description="Changes apply right away.")
        self.lang = _combo("Language", LANG_CODES, c.host.language,
                           [LANGUAGES[x] for x in LANG_CODES])
        self.lang.connect("notify::selected", lambda *_: self._on_lang_changed())
        self.h_name = Adw.EntryRow(title="Name (optional)", text=c.host.name, show_apply_button=True)
        self.h_name.connect("apply", lambda *_: self._save_host_text())
        self.h_personality = Adw.EntryRow(title="Personality", text=c.host.personality,
                                          show_apply_button=True)
        self.h_personality.connect("apply", lambda *_: self._save_host_text())
        for w in (self.lang, self.h_name, self.h_personality):
            g.add(w)
        page.add(g)

        g = Adw.PreferencesGroup(title="Script writer", description="The AI that writes what the host says.")
        self.writer = Adw.ComboRow(title="Model")
        self.writer.connect("notify::selected", lambda *_: self._on_writer_changed())
        g.add(self.writer)
        page.add(g)
        self._fill_writer([])
        self._load_ollama_models()

        g = Adw.PreferencesGroup(title="Voice", description="Tick a voice to use it, ▶ to hear it. "
                                                             "Each language keeps its own voice.")
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.volume.set_value(c.audio_volume)
        self.volume.set_size_request(200, -1)
        self.volume.set_valign(Gtk.Align.CENTER)
        self.volume.set_draw_value(True)
        self.volume.set_value_pos(Gtk.PositionType.RIGHT)
        self.volume.set_format_value_func(lambda _s, v: f"{v:.0f} %")
        self.volume.connect("value-changed", self._on_volume)
        self._volume_save = 0
        self.voice_row = Adw.ActionRow(title="Current voice")
        self.voice_row.add_prefix(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        self.voice_row.add_suffix(self.volume)
        self.test_btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                                   css_classes=["flat"], tooltip_text="Play a sample at this volume")
        self.test_btn.connect("clicked", self._test_voice)
        self.voice_row.add_suffix(self.test_btn)
        g.add(self.voice_row)
        self.exp_google = Adw.ExpanderRow(title="Natural voices", subtitle="Google · free tier")
        self.exp_piper = Adw.ExpanderRow(title="Local voices", subtitle="Piper · offline, more robotic")
        g.add(self.exp_google)
        g.add(self.exp_piper)
        page.add(g)
        self.v_rows: dict[Adw.ExpanderRow, list] = {self.exp_google: [], self.exp_piper: []}
        self._radio: Gtk.CheckButton | None = None
        self._load_voices()
        return page

    def _lang(self) -> str:
        return self.lang.items[self.lang.get_selected()]

    def _update_and_apply(self, change, message: str) -> None:
        """Load the saved config, apply `change(cfg)`, save, and restart the host if on air."""
        self.cfg = load_config()
        change(self.cfg)
        save_config(self.cfg)
        self.app.apply_changes()
        self.toast(message + (" Applying…" if self.app.running else ""))

    def _on_lang_changed(self) -> None:
        lang = self._lang()
        if lang == load_config().host.language:
            return
        self._update_and_apply(lambda c: setattr(c.host, "language", lang),
                               f"Host speaks {LANGUAGES[lang]}.")
        self._load_voices()

    def _save_host_text(self) -> None:
        name = self.h_name.get_text().strip()
        personality = self.h_personality.get_text().strip()

        def change(c):
            c.host.name = name
            c.host.personality = personality or c.host.personality
        self._update_and_apply(change, "Host saved.")

    # script writer: one list of Claude models and local Ollama models

    def _fill_writer(self, ollama: list[str]) -> None:
        c = load_config()
        current = (f"anthropic:{c.llm.model}" if c.llm.provider == "anthropic"
                   else f"ollama:{c.llm.ollama_model}")
        options = list(CLAUDE_CHOICES)
        local = list(dict.fromkeys(ollama + ([c.llm.ollama_model] if c.llm.provider == "ollama" else [])))
        options += [(f"ollama:{m}", f"Local: {m}", LOCAL_NOTE) for m in local]
        if current not in [o[0] for o in options]:
            options.append((current, current.split(":", 1)[1], ""))
        self._writer_options = options
        self._writer_filling = True
        self.writer.set_model(Gtk.StringList.new([o[1] for o in options]))
        self.writer.set_selected([o[0] for o in options].index(current))
        self.writer.set_subtitle(options[self.writer.get_selected()][2])
        self._writer_filling = False

    def _load_ollama_models(self) -> None:
        from .script import ollama_models
        url = load_config().llm.ollama_url

        def work():
            try:
                found = ollama_models(url)
            except Exception:  # noqa: BLE001 - no Ollama is normal
                found = []
            GLib.idle_add(lambda: self._fill_writer(found) and False)
        threading.Thread(target=work, daemon=True).start()

    def _on_writer_changed(self) -> None:
        if self._writer_filling:
            return
        key, label, note = self._writer_options[self.writer.get_selected()]
        self.writer.set_subtitle(note)
        provider, model = key.split(":", 1)
        if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            self.toast("Claude needs an API key: Settings → Accounts → Run setup again.")

        def change(c):
            c.llm.provider = provider
            if provider == "anthropic":
                c.llm.model = model
            else:
                c.llm.ollama_model = model
        self._update_and_apply(change, f"Script writer: {label}.")

    # voices: Google and Piper in one radio group

    def _google_ready(self) -> bool:
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        return bool(creds) and Path(creds).exists()

    def _load_voices(self) -> None:
        lang = self._lang()
        self._radio = None
        self._set_voice_rows(self.exp_google, voice_catalog.google_voices(lang))
        self._set_voice_rows(self.exp_piper, None)
        voices_dir = self.cfg.tts.piper_voices_dir

        def work():
            try:
                result, err = voice_catalog.piper_voices(lang, voices_dir), ""
            except Exception as e:  # noqa: BLE001
                result, err = None, f"Could not load the Piper voice list: {e}"
            GLib.idle_add(lambda: self._set_voice_rows(self.exp_piper, result, err) and False)
        threading.Thread(target=work, daemon=True).start()
        self._show_current_voice()

    def _show_current_voice(self) -> None:
        name = load_config().tts.voice_for(self._lang())
        engine = voice_engine(name)
        label = name.rsplit("-", 1)[-1] if engine == "google" else name
        self.voice_row.set_subtitle(f"{label} · {'Google' if engine == 'google' else 'Piper'}")
        self.exp_google.set_expanded(False)
        self.exp_piper.set_expanded(False)

    def _set_voice_rows(self, exp: Adw.ExpanderRow, voices: list | None, error: str = "") -> None:
        for row in self.v_rows[exp]:
            exp.remove(row)
        self.v_rows[exp] = []
        if voices is None:
            row = Adw.ActionRow(title=error or "Loading voice list…")
            exp.add_row(row)
            self.v_rows[exp].append(row)
            return
        lang = self._lang()
        current = load_config().tts.voice_for(lang)
        google = exp is self.exp_google
        usable = self._google_ready() or not google
        if google:
            exp.set_subtitle("Google · free tier" if usable else
                             "Google · needs Google Cloud set up (Settings → Accounts)")
        for v in voices:
            row = Adw.ActionRow(title=GLib.markup_escape_text(v.label), subtitle=v.detail)
            check = Gtk.CheckButton(active=v.name == current, valign=Gtk.Align.CENTER,
                                    sensitive=usable)
            if self._radio is None:
                self._radio = check
            else:
                check.set_group(self._radio)
            check.connect("toggled", self._on_voice_selected, v, lang)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                              css_classes=["flat"], tooltip_text="Play sample", sensitive=usable)
            play.connect("clicked", self._preview, v, lang)
            row.add_suffix(play)
            exp.add_row(row)
            self.v_rows[exp].append(row)

    def _on_voice_selected(self, check, voice, lang):
        if not check.get_active() or load_config().tts.voice_for(lang) == voice.name:
            return

        def change(c):
            c.tts.voices[lang] = voice.name
            c.tts.provider = voice.provider
        self._update_and_apply(change, f"{LANGUAGES[lang]} voice: {voice.label}.")
        self._show_current_voice()

    def _preview(self, btn, voice, lang):
        btn.set_child(Adw.Spinner())
        btn.set_sensitive(False)
        if voice.provider == "piper" and "MB download" in voice.detail:
            self.toast(f"Downloading {voice.label}…")
        tts_cfg = self.cfg.tts

        def work():
            try:
                path, err = voice_catalog.preview(voice, lang, tts_cfg), None
            except Exception as e:  # noqa: BLE001
                path, err = None, str(e)

            def done():
                btn.set_child(None)
                btn.set_icon_name("media-playback-start-symbolic")
                btn.set_sensitive(True)
                if err:
                    self.toast(f"Preview failed: {err[:120]}")
                else:
                    self._play(path)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _test_voice(self, btn) -> None:
        cfg = load_config()
        lang = self._lang()
        name = cfg.tts.voice_for(lang)
        self._preview(btn, voice_catalog.Voice(voice_engine(name), name, name, ""), lang)

    def _on_volume(self, scale) -> None:
        v = int(round(scale.get_value()))
        for clip in (self.clip, getattr(self.app.host, "clip", None)):
            if clip is not None:
                clip.volume = v
        if self._volume_save:
            GLib.source_remove(self._volume_save)

        def save():
            self._volume_save = 0
            cfg = load_config()
            cfg.audio_volume = v
            save_config(cfg)
            self.cfg.audio_volume = v
            return False
        self._volume_save = GLib.timeout_add(600, save)

    # ---------- closing ----------

    def _on_close(self, _win) -> bool:
        if not self.app.running:
            self.app.quit_all()
            return False
        mac = sys.platform == "darwin"
        dialog = Adw.AlertDialog(
            heading="Keep the radio host on air?",
            body="The window goes to the Dock but breaks keep happening." if mac else
                 "The window closes but breaks keep happening. Click the microphone "
                 "in the tray (or open AI Radio Host again) to bring it back.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("quit", "Stop and Quit")
        dialog.add_response("keep", "Keep Running")
        dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("keep")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "keep":
                self.app.run_in_background()
                if mac:
                    self.minimize()
                else:
                    self.set_visible(False)
            elif response == "quit":
                self.app.quit_all()
        dialog.connect("response", on_response)
        dialog.present(self)
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: Window | None = None
        self.host = None
        self.thread: threading.Thread | None = None
        self.held = False
        self.tray = None
        self.log_handler: GuiLogHandler | None = None
        self._restart_pending = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if sys.platform == "darwin":   # the tray needs D-Bus; on macOS the window minimizes instead
            return
        try:
            from .tray import TrayIcon
            self.tray = TrayIcon(on_activate=self.activate, on_toggle_air=self._tray_toggle,
                                 on_break=self.request_break, on_quit=self.quit_all)
        except Exception as e:  # noqa: BLE001 - no tray is fine
            log.warning("No tray icon: %s", e)

    def request_break(self) -> None:
        if self.running:
            self.host.request_break()
            if self.window:
                self.window.toast("The host will talk at the end of this song")

    def _tray_toggle(self) -> None:
        if self.window is None:
            self.activate()
        self.window.onair.set_active(not self.running)

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
            if self.log_handler is None:
                self.log_handler = GuiLogHandler(self.window.append_log)
                self.log_handler.setLevel(logging.INFO)
                logging.getLogger().addHandler(self.log_handler)
            else:
                self.log_handler.sink = self.window.append_log
        if self.held:
            self.release()
            self.held = False
        self.window.present()

    def reset_all(self) -> None:
        """Stop the host, wipe settings and keys, and reopen a fresh window (which starts
        the setup wizard because nothing is configured anymore)."""
        from .config import reset_all
        self.stop_host()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.host = None
        removed = reset_all()
        log.info("Reset: removed %s and the saved keys", ", ".join(p.name for p in removed) or "nothing")
        old = self.window
        self.window = None
        self.activate()
        if old is not None:
            old.destroy()

    def apply_changes(self) -> None:
        """Settings changed: restart the host (if on air) once it isn't talking."""
        if not self.running or self._restart_pending:
            return
        self._restart_pending = True
        GLib.timeout_add(400, self._restart_when_quiet)

    def _restart_when_quiet(self) -> bool:
        if not self.running:
            self._restart_pending = False
            return False
        if self.host.talking is not None:
            return True  # never cut the host off mid-sentence; try again shortly
        old = self.host
        old.stop()
        self.thread.join(timeout=5)
        error = self.start_host(load_config(), old.dry_run)
        self._restart_pending = False
        if error:
            log.error("Couldn't apply the change: %s", error)
            if self.window:
                self.window.toast(f"Couldn't apply: {error}")
        else:
            self.host.counter, self.host.force_break = old.counter, old.force_break
            log.info("Applied new settings")
        return False

    def run_in_background(self) -> None:
        if not self.held:
            self.hold()
            self.held = True

    def start_host(self, cfg: Config, dry_run: bool) -> str | None:
        from .__main__ import build_host
        try:
            self.host = build_host(cfg, dry_run)
        except (NotAuthorized, ValueError, AudioError) as e:
            return str(e)

        def run():
            try:
                self.host.run()
            except Exception:  # noqa: BLE001
                log.exception("Radio host crashed")
        self.thread = threading.Thread(target=run, name="radiohost-loop", daemon=True)
        self.thread.start()
        return None

    def stop_host(self) -> None:
        if self.host is not None:
            self.host.stop()

    def quit_all(self) -> None:
        self.stop_host()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.window and self.window.clip:
            self.window.clip.stop()
        if self.held:
            self.release()
            self.held = False
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        self.quit()


def main() -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "urllib3", "google"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    app = App()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: app.quit_all() or False)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
