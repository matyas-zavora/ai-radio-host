"""First-run setup: Spotify app + login, Claude key, voice. Writes .env and config.toml."""

from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path
from typing import Callable

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .config import (CONFIG_DIR, LANGUAGES, env_file, load_config, save_config, set_env,
                     voice_engine)
from .spotify_api import NotAuthorized, SpotifyClient

SPOTIFY_DASHBOARD = "https://developer.spotify.com/dashboard"
ANTHROPIC_KEYS = "https://platform.claude.com/settings/keys"
CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def setup_needed() -> bool:
    cfg = load_config()
    if not cfg.spotify_client_id:
        return True
    if cfg.llm.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        return not SpotifyClient(cfg.spotify_client_id, cfg.spotify_redirect_uri).authorized
    except NotAuthorized:
        return True


def _open(url: str, parent: Gtk.Widget) -> None:
    Gtk.UriLauncher.new(url).launch(parent.get_root(), None, None, None)


def _label(text: str, css: list[str] | None = None) -> Gtk.Label:
    return Gtk.Label(label=text, wrap=True, xalign=0, use_markup=True, css_classes=css or [])


def _status_row(title: str) -> tuple[Adw.ActionRow, Gtk.Image]:
    row = Adw.ActionRow(title=title)
    icon = Gtk.Image(icon_name="emblem-important-symbolic", visible=False)
    row.add_suffix(icon)
    return row, icon


def _set_status(row: Adw.ActionRow, icon: Gtk.Image, ok: bool | None, text: str) -> None:
    row.set_subtitle(GLib.markup_escape_text(text))
    icon.set_visible(ok is not None)
    if ok is not None:
        icon.set_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
        icon.set_css_classes(["success"] if ok else ["error"])


def _in_thread(work: Callable[[], object], done: Callable[[object, Exception | None], None]) -> None:
    def run():
        try:
            result, err = work(), None
        except Exception as e:  # noqa: BLE001 - shown to the user
            result, err = None, e
        GLib.idle_add(lambda: done(result, err) or False)
    threading.Thread(target=run, daemon=True).start()


class SetupWizard(Adw.Dialog):
    def __init__(self, on_finished: Callable[[bool], None]):
        super().__init__(title="Set up AI Radio Host", content_width=620, content_height=720)
        self._on_finished = on_finished
        self._reported = False
        self.cfg = load_config()
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.pages = [self._welcome(), self._spotify(), self._claude(), self._voice(), self._done()]
        self.nav.add(self.pages[0])
        self.connect("closed", lambda *_: self.on_finished(False))

    def on_finished(self, go_on_air: bool) -> None:
        if not self._reported:
            self._reported = True
            self._on_finished(go_on_air)

    # ---------- page scaffolding ----------

    def _page(self, title: str, body: Gtk.Widget, button: str | None = None,
              on_next: Callable[[], None] | None = None) -> tuple[Adw.NavigationPage, Gtk.Button]:
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(body)
        btn = Gtk.Button(label=button or "Continue", halign=Gtk.Align.CENTER,
                         css_classes=["pill", "suggested-action"], margin_top=12, margin_bottom=18)
        btn.connect("clicked", lambda *_: (on_next or self._next)())
        view.add_bottom_bar(btn)
        return Adw.NavigationPage(title=title, child=view), btn

    def _next(self) -> None:
        current = self.nav.get_visible_page()
        i = self.pages.index(current)
        self.nav.push(self.pages[i + 1])

    # ---------- 1. welcome ----------

    def _welcome(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="dev.zavoram.RadioHost", title="Welcome to AI Radio Host",
            description="Every few songs, an AI host introduces the next song in your Spotify "
                        "playlist.\n\nSetup takes about 5 minutes. You'll need:\n"
                        "• Spotify Premium\n"
                        "• an Anthropic account with a few dollars of credit "
                        "(one break costs about $0.002), or a local model in Ollama")
        page, _ = self._page("Welcome", status, "Start")
        return page

    # ---------- 2. Spotify ----------

    def _spotify(self) -> Adw.NavigationPage:
        pp = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(
            title="1. Create a Spotify app",
            description="Spotify needs a small \"app\" on your account so this program can "
                        "read your queue. It's free and takes a minute.")
        open_row = Adw.ActionRow(title="Open the Spotify developer dashboard",
                                 subtitle="Log in → Create app", activatable=True)
        open_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        open_row.connect("activated", lambda r: _open(SPOTIFY_DASHBOARD, r))
        g.add(open_row)
        g.add(Adw.ActionRow(title="App name and description", subtitle="Anything, e.g. Radio Host"))
        uri_row = Adw.ActionRow(title="Redirect URI (paste exactly this)",
                                subtitle=self.cfg.spotify_redirect_uri, subtitle_selectable=True)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                          css_classes=["flat"], tooltip_text="Copy")
        copy.connect("clicked", lambda b: self._copy(self.cfg.spotify_redirect_uri, b))
        uri_row.add_suffix(copy)
        g.add(uri_row)
        g.add(Adw.ActionRow(title="Which API", subtitle="Tick \"Web API\", agree, Save"))
        pp.add(g)

        g = Adw.PreferencesGroup(title="2. Paste the Client ID",
                                 description="On the app's page, under Basic Information. "
                                             "No client secret needed.")
        self.client_id = Adw.EntryRow(title="Client ID", text=self.cfg.spotify_client_id)
        self.client_id.connect("changed", lambda *_: self._spotify_changed())
        g.add(self.client_id)
        pp.add(g)

        g = Adw.PreferencesGroup(title="3. Log in")
        self.sp_row, self.sp_icon = _status_row("Spotify login")
        self.sp_login = Gtk.Button(label="Log in to Spotify", valign=Gtk.Align.CENTER)
        self.sp_login.connect("clicked", lambda *_: self._spotify_login())
        self.sp_row.add_suffix(self.sp_login)
        g.add(self.sp_row)
        pp.add(g)

        page, self.sp_next = self._page("Spotify (1/3)", pp)
        self._spotify_changed()
        return page

    def _copy(self, text: str, widget: Gtk.Widget) -> None:
        Gdk.Display.get_default().get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        widget.set_icon_name("emblem-ok-symbolic")
        GLib.timeout_add(1500, lambda: widget.set_icon_name("edit-copy-symbolic") or False)

    def _spotify_changed(self) -> None:
        cid = self.client_id.get_text().strip()
        valid = bool(CLIENT_ID_RE.match(cid))
        self.sp_login.set_sensitive(valid)
        logged_in = valid and cid == self.cfg.spotify_client_id and SpotifyClient(
            cid, self.cfg.spotify_redirect_uri).authorized
        if not cid:
            _set_status(self.sp_row, self.sp_icon, None, "Paste the Client ID first")
        elif not valid:
            _set_status(self.sp_row, self.sp_icon, False, "That doesn't look like a Client ID "
                                                          "(32 characters, 0-9 and a-f)")
        elif logged_in:
            _set_status(self.sp_row, self.sp_icon, True, "Logged in")
        else:
            _set_status(self.sp_row, self.sp_icon, None, "Opens your browser; click Agree there")
        self.sp_next.set_sensitive(logged_in)

    def _spotify_login(self) -> None:
        cid = self.client_id.get_text().strip()
        set_env({"SPOTIFY_CLIENT_ID": cid})
        self.cfg.spotify_client_id = cid
        self.sp_login.set_sensitive(False)
        _set_status(self.sp_row, self.sp_icon, None, "Waiting for you to click Agree in the browser…")

        def done(_r, err):
            self.sp_login.set_sensitive(True)
            if err:
                _set_status(self.sp_row, self.sp_icon, False, str(err))
                self.sp_next.set_sensitive(False)
            else:
                self._spotify_changed()
        _in_thread(lambda: SpotifyClient(cid, self.cfg.spotify_redirect_uri).login(), done)

    # ---------- 3. Claude ----------

    def _claude(self) -> Adw.NavigationPage:
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            title="Claude writes what the host says",
            description="Create an API key and paste it here. Add a few dollars of credit under "
                        "Billing first; $5 lasts for well over 1,000 breaks.")
        open_row = Adw.ActionRow(title="Open the Anthropic API keys page",
                                 subtitle="Create Key → copy it", activatable=True)
        open_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        open_row.connect("activated", lambda r: _open(ANTHROPIC_KEYS, r))
        g.add(open_row)
        self.key = Adw.PasswordEntryRow(title="API key (sk-ant-…)",
                                        text=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.key.connect("changed", lambda *_: self.cl_next.set_sensitive(False))
        g.add(self.key)
        self.workspace = Adw.EntryRow(title="Workspace ID (wrkspc_…)",
                                      text=os.environ.get("ANTHROPIC_WORKSPACE_ID", ""),
                                      visible=bool(os.environ.get("ANTHROPIC_WORKSPACE_ID")))
        self.workspace.connect("changed", lambda *_: self.cl_next.set_sensitive(False))
        g.add(self.workspace)
        self.cl_row, self.cl_icon = _status_row("Check")
        check = Gtk.Button(label="Check key", valign=Gtk.Align.CENTER)
        check.connect("clicked", lambda *_: self._claude_check())
        self.cl_row.add_suffix(check)
        _set_status(self.cl_row, self.cl_icon, None, "Makes a free test request")
        g.add(self.cl_row)
        pp.add(g)
        self.claude_group = g

        g = Adw.PreferencesGroup()
        self.use_local = Adw.SwitchRow(
            title="Use a local model (Ollama) instead",
            subtitle="Free and private, no account needed. Small models invent facts more "
                     "often and are weak in Czech.")
        g.add(self.use_local)
        pp.add(g)

        g = Adw.PreferencesGroup(
            title="Ollama", description="Install from ollama.com, then e.g.  ollama pull qwen2.5:7b")
        self.ol_url = Adw.EntryRow(title="Server", text=self.cfg.llm.ollama_url)
        self.ol_url.connect("changed", lambda *_: self.cl_next.set_sensitive(False))
        self.ol_model = Adw.ComboRow(title="Model", model=Gtk.StringList.new([self.cfg.llm.ollama_model]))
        self.ol_model.names = [self.cfg.llm.ollama_model]
        self.ol_row, self.ol_icon = _status_row("Check")
        ol_check = Gtk.Button(label="Find models", valign=Gtk.Align.CENTER)
        ol_check.connect("clicked", lambda *_: self._ollama_check())
        self.ol_row.add_suffix(ol_check)
        _set_status(self.ol_row, self.ol_icon, None, "Asks Ollama which models are installed")
        for w in (self.ol_url, self.ol_model, self.ol_row):
            g.add(w)
        pp.add(g)
        self.ollama_group = g

        page, self.cl_next = self._page("AI (2/3)", pp, None, self._ai_next)
        self.use_local.connect("notify::active", lambda *_: self._ai_mode())
        self.use_local.set_active(self.cfg.llm.provider == "ollama")
        self._ai_mode()
        return page

    def _ai_mode(self) -> None:
        local = self.use_local.get_active()
        self.claude_group.set_visible(not local)
        self.ollama_group.set_visible(local)
        if local:
            self.cl_next.set_sensitive(False)
            self._ollama_check()
        else:
            saved = os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")
            self.cl_next.set_sensitive(saved)
            if saved:
                _set_status(self.cl_row, self.cl_icon, None,
                            "Key saved earlier. Check it again if you like.")

    def _ollama_check(self) -> None:
        from .script import ollama_models
        url = self.ol_url.get_text().strip()
        _set_status(self.ol_row, self.ol_icon, None, "Asking Ollama…")

        def done(models, err):
            if err:
                _set_status(self.ol_row, self.ol_icon, False,
                            f"Can't reach Ollama at {url}. Is it running?")
                return
            if not models:
                _set_status(self.ol_row, self.ol_icon, False,
                            "Ollama has no models yet. Run: ollama pull qwen2.5:7b")
                return
            want = self.cfg.llm.ollama_model
            self.ol_model.names = models
            self.ol_model.set_model(Gtk.StringList.new(models))
            self.ol_model.set_selected(models.index(want) if want in models else 0)
            _set_status(self.ol_row, self.ol_icon, True, f"{len(models)} model(s) found")
            self.cl_next.set_sensitive(True)
        _in_thread(lambda: ollama_models(url), done)

    def _ai_next(self) -> None:
        cfg = load_config()
        if self.use_local.get_active():
            cfg.llm.provider = "ollama"
            cfg.llm.ollama_url = self.ol_url.get_text().strip()
            cfg.llm.ollama_model = self.ol_model.names[self.ol_model.get_selected()]
        else:
            cfg.llm.provider = "anthropic"
        save_config(cfg)
        self._next()

    def _claude_check(self) -> None:
        import anthropic

        key = self.key.get_text().strip()
        ws = self.workspace.get_text().strip()
        if not key.startswith("sk-ant-"):
            _set_status(self.cl_row, self.cl_icon, False, "Keys start with sk-ant-")
            return
        _set_status(self.cl_row, self.cl_icon, None, "Checking…")

        def work():
            client = anthropic.Anthropic(
                api_key=key, max_retries=1, timeout=15,
                default_headers={"anthropic-workspace-id": ws} if ws else None)
            client.models.list(limit=1)

        def done(_r, err):
            if err is None:
                values = {"ANTHROPIC_API_KEY": key}
                if ws:
                    values["ANTHROPIC_WORKSPACE_ID"] = ws
                set_env(values)
                _set_status(self.cl_row, self.cl_icon, True, "Key works")
                self.cl_next.set_sensitive(True)
                return
            msg = getattr(err, "message", None) or str(err)
            if "workspace" in msg.lower():
                self.workspace.set_visible(True)
                msg = ("This key isn't tied to a workspace. Paste the workspace ID "
                       "(Settings → Workspaces in the console), or create the key inside a "
                       "workspace, then check again.")
            elif isinstance(err, anthropic.AuthenticationError):
                msg = "The key wasn't accepted. Copy it again from the console."
            _set_status(self.cl_row, self.cl_icon, False, msg)
        _in_thread(work, done)

    # ---------- 4. voice & language ----------

    def _voice(self) -> Adw.NavigationPage:
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Host")
        codes = list(LANGUAGES)
        self.lang = Adw.ComboRow(title="Language",
                                 model=Gtk.StringList.new([LANGUAGES[c] for c in codes]))
        self.lang.set_selected(codes.index(self.cfg.host.language))
        self.lang.codes = codes
        self.name = Adw.EntryRow(title="Host name (optional)", text=self.cfg.host.name or "Felix Moon")
        g.add(self.lang)
        g.add(self.name)
        pp.add(g)

        g = Adw.PreferencesGroup(title="Voice", description="You can change this any time.")
        self.v_piper = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.v_google = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self.v_piper)
        row = Adw.ActionRow(title="Piper (free, offline)",
                            subtitle="Works right away. Sounds a bit robotic.")
        row.add_prefix(self.v_piper)
        row.set_activatable_widget(self.v_piper)
        g.add(row)
        self.g_row = Adw.ActionRow(title="Google (natural voices)",
                                   subtitle="Free tier. Needs a Google Cloud key file "
                                            "(README: Google Cloud TTS).")
        self.g_row.add_prefix(self.v_google)
        self.g_row.set_activatable_widget(self.v_google)
        pick = Gtk.Button(label="Choose key file…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda *_: self._pick_google_key())
        self.g_row.add_suffix(pick)
        g.add(self.g_row)
        pp.add(g)

        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        self.google_key_ok = bool(creds) and Path(creds).exists()
        if self.google_key_ok:
            self.g_row.set_subtitle(f"Key file: {creds}")
        self.v_google.set_sensitive(self.google_key_ok)
        uses_google = voice_engine(self.cfg.tts.voice_for(self.cfg.host.language)) == "google"
        (self.v_google if self.google_key_ok and uses_google else self.v_piper).set_active(True)

        page, _ = self._page("Voice (3/3)", pp, "Finish", self._finish)
        return page

    def _pick_google_key(self) -> None:
        dialog = Gtk.FileDialog(title="Google Cloud service account key (.json)")
        filt = Gtk.FileFilter(name="JSON key")
        filt.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dialog.set_filters(store)

        def chosen(d, result):
            try:
                src = Path(d.open_finish(result).get_path())
            except GLib.Error:
                return  # cancelled
            try:
                import json
                if json.loads(src.read_text()).get("type") != "service_account":
                    raise ValueError("not a service account key")
            except (OSError, ValueError) as e:
                self.g_row.set_subtitle(f"That file isn't a Google service account key ({e})")
                return
            dest = CONFIG_DIR / "gcp-tts-key.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            dest.chmod(0o600)
            set_env({"GOOGLE_APPLICATION_CREDENTIALS": str(dest)})
            self.google_key_ok = True
            self.v_google.set_sensitive(True)
            self.v_google.set_active(True)
            self.g_row.set_subtitle(f"Key file saved to {dest}")
        dialog.open(self.get_root(), None, chosen)

    def _finish(self) -> None:
        cfg = load_config()
        cfg.host.language = self.lang.codes[self.lang.get_selected()]
        cfg.host.name = self.name.get_text().strip()
        google = self.v_google.get_active() and self.google_key_ok
        cfg.tts.provider = "google" if google else "piper"
        cfg.tts.fallback = "piper"
        # Keep a voice the user already picked for that engine; otherwise use its default.
        defaults = cfg.tts.google_voices if google else cfg.tts.piper_voices
        for lang in LANGUAGES:
            if voice_engine(cfg.tts.voice_for(lang)) != cfg.tts.provider:
                cfg.tts.voices[lang] = defaults[lang]
        save_config(cfg)
        self._next()

    # ---------- 5. done ----------

    def _done(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="emblem-ok-symbolic", title="You're all set",
            description="Start playing your playlist in Spotify, then go on air. The host talks "
                        f"every few songs.\n\nYour keys are saved in {env_file()}")
        page, _ = self._page("Done", status, "Go on air", self._go_on_air)
        page.set_can_pop(False)
        return page

    def _go_on_air(self) -> None:
        self.on_finished(True)
        self.force_close()
