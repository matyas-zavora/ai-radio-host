"""GTK4 / libadwaita app: on-air control, live status, log, settings, voice picker."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .audio import AudioError, ClipPlayer  # noqa: E402
from .config import CONFIG_DIR, DEV, LANGUAGES, PROJECT_DIR, Config, load_config, save_config  # noqa: E402
from .spotify_api import NotAuthorized, SpotifyClient  # noqa: E402
from . import voices as voice_catalog  # noqa: E402

APP_ID = "dev.zavoram.RadioHost"
log = logging.getLogger("radiohost")

MODELS = ["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5"]
EFFORTS = ["low", "medium", "high"]
PROVIDERS = ["google", "piper"]
FALLBACKS = ["", "piper", "google"]
LANG_CODES = list(LANGUAGES)

# The studio "ON AIR" light: dark when idle, glowing red while the host speaks.
CSS = """
.onair-sign {
  background-color: #241010;
  color: #4d2622;
  border: 3px solid #3a1a17;
  border-radius: 14px;
  padding: 14px 40px;
  font-size: 34px;
  font-weight: 900;
  letter-spacing: 8px;
  transition: background-color 250ms, color 250ms, border-color 250ms;
}
.onair-sign.lit {
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
        stack.add_titled_with_icon(self._build_log(), "log", "Log", "utilities-terminal-symbolic")
        stack.add_titled_with_icon(self._build_settings(), "settings", "Settings", "emblem-system-symbolic")
        stack.add_titled_with_icon(self._build_voices(), "voices", "Voices", "audio-speakers-symbolic")
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
            self.s_name.set_text(self.cfg.host.name)
            self.s_provider.set_selected(self.s_provider.items.index(self.cfg.tts.provider))
            self.s_fallback.set_selected(self.s_fallback.items.index(self.cfg.tts.fallback))
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

        self.sign = Gtk.Label(label="ON AIR", halign=Gtk.Align.CENTER, css_classes=["onair-sign"])
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

        g = Adw.PreferencesGroup(title="Host")
        self.onair = Adw.SwitchRow(title="On air", subtitle="Off")
        self.onair.connect("notify::active", self._on_onair_toggled)
        self.dryrun = Adw.SwitchRow(title="Dry run",
                                    subtitle="Only write scripts: no speech, no pausing")
        self.lang = _combo("Language", LANG_CODES, self.cfg.host.language,
                           [LANGUAGES[c] for c in LANG_CODES])
        self.lang.connect("notify::selected", self._on_lang_changed)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.volume.set_value(self.cfg.audio_volume)
        self.volume.set_size_request(220, -1)
        self.volume.set_valign(Gtk.Align.CENTER)
        self.volume.set_draw_value(True)
        self.volume.set_value_pos(Gtk.PositionType.RIGHT)
        self.volume.set_format_value_func(lambda _s, v: f"{v:.0f} %")
        self.volume.connect("value-changed", self._on_volume)
        vol_row = Adw.ActionRow(title="Host voice volume",
                                subtitle="Applies from the next clip")
        vol_row.add_prefix(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        vol_row.add_suffix(self.volume)
        self.test_btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                                   css_classes=["flat"], tooltip_text="Test: play a sample at this volume")
        self.test_btn.connect("clicked", self._test_voice)
        vol_row.add_suffix(self.test_btn)
        self._volume_save = 0
        for w in (self.onair, self.dryrun, self.lang, vol_row):
            g.add(w)
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

    def _test_voice(self, btn) -> None:
        cfg = load_config()
        lang = self.lang.items[self.lang.get_selected()]
        provider = cfg.tts.provider
        name = (cfg.tts.google_voices if provider == "google" else cfg.tts.piper_voices)[lang]
        voice = voice_catalog.Voice(provider, name, name, "")
        btn.set_sensitive(False)

        def work():
            try:
                path, err = voice_catalog.preview(voice, lang, cfg.tts), None
            except Exception as e:  # noqa: BLE001
                path, err = None, str(e)

            def done():
                btn.set_sensitive(True)
                if err:
                    self.toast(f"Test failed ({provider}): {err[:120]}")
                else:
                    self._play(path)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

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

    def _on_lang_changed(self, row, _pspec):
        self.cfg = load_config()  # don't write back a stale copy
        self.cfg.host.language = row.items[row.get_selected()]
        save_config(self.cfg)
        if self.app.running:
            self.toast("Language saved. Turn On air off and on to switch.")

    def _on_onair_toggled(self, row, _pspec):
        if row.get_active() and not self.app.running:
            self.cfg = load_config()
            self.cfg.host.language = self.lang.items[self.lang.get_selected()]
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
        if lit != self.sign.has_css_class("lit"):
            (self.sign.add_css_class if lit else self.sign.remove_css_class)("lit")
        if talking is not None:
            caption = f"Introducing {talking}"
        elif replaying:
            caption = "Playing a sample"
        elif running and host.dry_run:
            caption = "Dry run: the host only writes scripts"
        elif running:
            caption = "Music on. The light comes on when the host talks."
        else:
            caption = "Off air"
        if self.sign_caption.get_label() != caption:
            self.sign_caption.set_label(caption)

    def _refresh(self) -> bool:
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
        self.s_lead = _spin("Pause before song end", 200, 3000, 100, c.brk.lead_ms, "milliseconds")
        for w in (self.s_every, self.s_min, self.s_max, self.s_played, self.s_lead):
            g.add(w)
        page.add(g)

        g = Adw.PreferencesGroup(title="Host")
        self.s_name = Adw.EntryRow(title="On-air name (optional)", text=c.host.name)
        self.s_personality = Adw.EntryRow(title="Personality", text=c.host.personality)
        g.add(self.s_name)
        g.add(self.s_personality)
        page.add(g)

        g = Adw.PreferencesGroup(title="AI model", description="Sonnet 5 costs about $0.002 per break.")
        self.s_model = _combo("Model", MODELS, c.llm.model)
        self.s_effort = _combo("Effort", EFFORTS, c.llm.effort)
        g.add(self.s_model)
        g.add(self.s_effort)
        page.add(g)

        g = Adw.PreferencesGroup(title="Speech", description="Pick voices on the Voices tab.")
        self.s_provider = _combo("Provider", PROVIDERS, c.tts.provider,
                                 ["Google Cloud (natural)", "Piper (local, free)"])
        self.s_fallback = _combo("If it fails", FALLBACKS, c.tts.fallback,
                                 ["Skip the break", "Use Piper", "Use Google"])
        self.s_budget = _spin("Google monthly limit", 0, 4_000_000, 50_000,
                              c.tts.google_monthly_char_limit, "characters (free tier: 1M for Chirp 3 HD)")
        for w in (self.s_provider, self.s_fallback, self.s_budget):
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

        save = Gtk.Button(label="Save settings", halign=Gtk.Align.CENTER,
                          css_classes=["pill", "suggested-action"])
        save.connect("clicked", self._save_settings)
        g = Adw.PreferencesGroup()
        g.add(save)
        page.add(g)
        return page

    def _refresh_accounts(self) -> None:
        try:
            ok = SpotifyClient(self.cfg.spotify_client_id, self.cfg.spotify_redirect_uri).authorized
            self.acc_spotify.set_subtitle("Logged in" if ok else "Not logged in")
            self.acc_login.set_label("Log in again" if ok else "Log in")
        except NotAuthorized as e:
            self.acc_spotify.set_subtitle(str(e))
            self.acc_login.set_sensitive(False)
        key = os.environ.get("ANTHROPIC_API_KEY")
        self.acc_anthropic.set_subtitle(
            ("API key set" + (" (with workspace ID)" if os.environ.get("ANTHROPIC_WORKSPACE_ID") else ""))
            if key else "ANTHROPIC_API_KEY missing in .env")
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
        c = self.cfg = load_config()
        c.host.language = self.lang.items[self.lang.get_selected()]
        c.brk.every_n_tracks = int(self.s_every.get_value())
        c.brk.min_seconds = int(self.s_min.get_value())
        c.brk.max_seconds = max(int(self.s_max.get_value()), c.brk.min_seconds)
        c.brk.min_played_seconds = int(self.s_played.get_value())
        c.brk.lead_ms = int(self.s_lead.get_value())
        c.host.name = self.s_name.get_text().strip()
        c.host.personality = self.s_personality.get_text().strip() or c.host.personality
        c.llm.model = self.s_model.items[self.s_model.get_selected()]
        c.llm.effort = self.s_effort.items[self.s_effort.get_selected()]
        c.tts.provider = self.s_provider.items[self.s_provider.get_selected()]
        c.tts.fallback = self.s_fallback.items[self.s_fallback.get_selected()]
        c.tts.google_monthly_char_limit = int(self.s_budget.get_value())
        path = save_config(c)
        self.toast(f"Saved to {path.name}" + (". Turn On air off and on to apply."
                                              if self.app.running else ""))

    # ---------- Voices ----------

    def _build_voices(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            description="Press ▶ to hear a sample, tick a voice to use it. Piper voices download "
                        "on first use. Google previews need Google Cloud set up.")
        self.v_lang = _combo("Language", LANG_CODES, self.cfg.host.language,
                             [LANGUAGES[c] for c in LANG_CODES])
        self.v_lang.connect("notify::selected", lambda *_: self._load_voices())
        g.add(self.v_lang)
        page.add(g)
        self.v_google = Adw.PreferencesGroup(title="Google Cloud (Chirp 3 HD)")
        self.v_piper = Adw.PreferencesGroup(title="Piper (local)")
        page.add(self.v_google)
        page.add(self.v_piper)
        self.v_rows: dict[Adw.PreferencesGroup, list] = {self.v_google: [], self.v_piper: []}
        self._load_voices()
        return page

    def _set_voice_rows(self, group, voices: list | None, error: str = "") -> None:
        for row in self.v_rows[group]:
            group.remove(row)
        self.v_rows[group] = []
        lang = self.v_lang.items[self.v_lang.get_selected()]
        if voices is None:
            row = Adw.ActionRow(title=error or "Loading voice list…")
            group.add(row)
            self.v_rows[group].append(row)
            return
        provider = "google" if group is self.v_google else "piper"
        current = (self.cfg.tts.google_voices if provider == "google" else self.cfg.tts.piper_voices)[lang]
        google_ready = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")) and Path(
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")).exists()
        radio_group = None
        for v in voices:
            row = Adw.ActionRow(title=GLib.markup_escape_text(v.label), subtitle=v.detail)
            check = Gtk.CheckButton(active=v.name == current, valign=Gtk.Align.CENTER)
            if radio_group is None:
                radio_group = check
            else:
                check.set_group(radio_group)
            check.connect("toggled", self._on_voice_selected, v, lang)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                              css_classes=["flat"], tooltip_text="Play sample")
            if provider == "google" and not google_ready:
                play.set_sensitive(False)
                play.set_tooltip_text("Set up Google Cloud TTS first")
            play.connect("clicked", self._preview, v, lang)
            row.add_suffix(play)
            group.add(row)
            self.v_rows[group].append(row)

    def _load_voices(self) -> None:
        lang = self.v_lang.items[self.v_lang.get_selected()]
        self._set_voice_rows(self.v_google, voice_catalog.google_voices(lang))
        self._set_voice_rows(self.v_piper, None)
        voices_dir = self.cfg.tts.piper_voices_dir

        def work():
            try:
                result, err = voice_catalog.piper_voices(lang, voices_dir), ""
            except Exception as e:  # noqa: BLE001
                result, err = None, f"Could not load Piper voice list: {e}"
            GLib.idle_add(lambda: self._set_voice_rows(self.v_piper, result, err) and False)
        threading.Thread(target=work, daemon=True).start()

    def _on_voice_selected(self, check, voice, lang):
        if not check.get_active():
            return
        self.cfg = load_config()
        voices = self.cfg.tts.google_voices if voice.provider == "google" else self.cfg.tts.piper_voices
        if voices.get(lang) == voice.name:
            return
        voices[lang] = voice.name
        save_config(self.cfg)
        note = "" if self.cfg.tts.provider == voice.provider else \
            f" (Speech provider is {self.cfg.tts.provider}; change it in Settings to use this)"
        self.toast(f"{LANGUAGES[lang]} voice: {voice.label}{note}")

    def _preview(self, btn, voice, lang):
        spinner = Adw.Spinner()
        btn.set_child(spinner)
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

    # ---------- closing ----------

    def _on_close(self, _win) -> bool:
        if not self.app.running:
            self.app.quit_all()
            return False
        dialog = Adw.AlertDialog(
            heading="Keep the radio host on air?",
            body="The window closes but breaks keep happening. Click the microphone "
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

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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
            handler = GuiLogHandler(self.window.append_log)
            handler.setLevel(logging.INFO)
            logging.getLogger().addHandler(handler)
        if self.held:
            self.release()
            self.held = False
        self.window.present()

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
