# AI Radio Host

A personal radio host for your Spotify playlist. You play music in the
Spotify desktop app as usual. Every N songs (default 3), right before the next
song starts, playback pauses, an AI host introduces the upcoming song in 15–25
seconds of speech, and then the music resumes.

Needs Spotify Premium (Spotify's developer API requires it) and Linux.
Alternative Spotify players work too, for example
[Spotifast](https://github.com/crmne/spotifast), spotifyd, ncspot and Spot.
The app finds whichever one is running.

- **Real facts only.** Facts come from Wikipedia (in your host language, then
  English) and MusicBrainz. The AI is told to use only those facts. When none
  are found, the host does a simple intro with no trivia.
- **No gap.** The break (facts → script → speech) is prepared while the current
  song is still playing.
- **English or Czech.** Set `host.language`, or pass `--lang cs` / `--lang en`.
- **Claude or a local model.** Scripts come from Claude by default, or from a
  free local model through Ollama.
- **Two TTS providers.** Google Cloud TTS (free tier, with a monthly character
  guard) and Piper (free, local, offline).

## How it works

```
 Spotify app ──MPRIS (D-Bus)──► player.py ◄── Web API fallback
                                  │ track, position, pause/play
                                  ▼
                              loop.py ── every N tracks ──► spotify_api.py: GET /me/player/queue
                                  │                              │ next track
                                  │                              ▼
                                  │   facts.py (Wikipedia, MusicBrainz) → script.py (Claude / Ollama)
                                  │                              → tts.py (Google / Piper) → WAV
                                  ▼
                 ~0.8 s before the song ends: pause → audio.py plays clip → resume
```

- The **current track and its position** come from the Spotify app over MPRIS,
  the standard Linux media-player interface. It's local, fast and has no rate
  limits. If MPRIS doesn't answer, or doesn't report the playback position (the
  official Spotify app has had this bug), the app uses the Spotify Web API
  instead. That works with any Spotify Connect device. With Spotifast, its
  `spotifast` CLI is an extra fallback.
- The **queue** only exists in the Web API (`GET /me/player/queue`). It is read
  when a break is due (at the start of the song before the break), again 30 s
  before the end (so a changed queue can be regenerated), and a final time 8 s
  before the end.
- If the track that's about to play doesn't match the prepared break, the break
  is skipped.

### Edge cases

| Situation | What happens |
|---|---|
| You skip a song | It counts toward N only if it played ≥ 30 s. A break prepared for the wrong song is dropped. |
| You change the queue | It's caught by the 30 s recheck and the break is regenerated. If the change comes later, the break is skipped. |
| You pause | Nothing happens. The app never presses play on its own, except to resume after its own break. |
| You press play during a break | The clip stops and you keep control. |
| Shuffle | Works if Spotify reports the shuffled order. Check this with `radiohost queue-test`. |
| Queue empty / next item is a podcast | The break is skipped. The counter stays, so the next song gets the break. |
| Spotify closed or restarted | The app logs it, waits, and picks up again when it's back. |
| Spotify 429 / expired token | It backs off by `Retry-After`. Tokens refresh automatically. |
| Facts, LLM or TTS fail | The break is skipped silently and the music keeps going. If Google TTS fails or hits its budget, Piper is used. |

## Install

Requires Linux, Spotify Premium, the Spotify desktop app (or another Spotify
player), PipeWire/PulseAudio and, on Arch,
`sudo pacman -S python-gobject gtk4 libadwaita`. For the AI you need either an
Anthropic API account with a few dollars of credit (best results) or
[Ollama](https://ollama.com) with a local model (free).

```bash
# the repo is private, so download with the GitHub CLI (gh auth login once)
gh release download --repo matyas-zavora/ai-radio-host --pattern 'ai-radio-host-*.tar.gz'
tar xzf ai-radio-host-*.tar.gz && cd ai-radio-host-*/
./install.sh
```

This installs into `~/.local/share/ai-radio-host`, adds `radiohost` and
`radiohost-gui` to `~/.local/bin`, adds **AI Radio Host** to your launcher, and
creates `~/.config/ai-radio-host/config.toml` and `.env` (mode 600). To update,
run a newer release's `./install.sh`; your settings are kept.
`./install.sh --uninstall` removes the app but keeps your settings.

Then open **AI Radio Host**. On first launch, a setup wizard walks you through
the Spotify app and login, the AI (a Claude API key, or a local model in
Ollama) and the voice, and saves everything for you. You can run it again from Settings → Accounts. The manual
steps below are only needed if you prefer to edit the files yourself.

An installed copy reads `config.toml` and `.env` from `~/.config/ai-radio-host/`.
A source checkout (for development) reads them from the project folder.

## Setup

### 1. Install

Requires Python 3.11+, Linux, a running Spotify app, and PipeWire or PulseAudio.

```bash
cd ~/projects/ai-radio-host
uv venv .venv && uv pip install -e '.[piper,google]'
# or: python -m venv .venv && .venv/bin/pip install -e '.[piper,google]'
cp .env.example .env && chmod 600 .env
cp config.example.toml config.toml      # optional, all keys have defaults
```

### 2. Spotify developer app

Spotify dev mode needs a Premium account on the app owner and allows one
dev-mode Client ID per account.

1. Go to <https://developer.spotify.com/dashboard> → **Create app**.
2. Name and description: anything.
3. **Redirect URI:** `http://127.0.0.1:8888/callback`. It must be exactly this.
   Spotify doesn't accept `localhost`; loopback IPs only.
4. **Which API/SDKs:** tick **Web API**. Save.
5. Copy the **Client ID** into `.env` as `SPOTIFY_CLIENT_ID=...`. No client
   secret is needed, because the app uses Authorization Code with PKCE.
6. Run `.venv/bin/radiohost auth`. A browser opens, you click **Agree**, and the
   token is saved to `~/.config/ai-radio-host/spotify_token.json` (mode 600).
   It refreshes automatically after that.

Scopes requested: `user-read-playback-state`, `user-read-currently-playing`
(both needed for the queue) and `user-modify-playback-state` (needed only for
the Web API fallback).

### 3. The AI that writes the script

**Claude (default).** Create a key at <https://platform.claude.com/settings/keys>
and put it in `.env` as `ANTHROPIC_API_KEY=...`. If the key isn't tied to a
workspace, also add `ANTHROPIC_WORKSPACE_ID=wrkspc_...`. One break costs about
$0.002 with `claude-sonnet-5`.

**Local model with [Ollama](https://ollama.com) (free, private).** Install
Ollama, `ollama pull qwen2.5:7b` (or any model you like), then pick
**Ollama** in Settings → AI model or in the setup wizard, or set
`[llm] provider = "ollama"`. No account or key needed. Small local models
follow the "only use the given facts" rule less reliably, and in a test
`qwen2.5:7b` invented details and wrote poor Czech. Expect weaker breaks than
with Claude, especially in Czech. The first break after a while can take
several seconds longer while the model loads.

### 4. TTS

**Piper (local, free).** Works out of the box. Voices download automatically
on first use to `~/.local/share/ai-radio-host/piper` (`en_US-ryan-high`,
`cs_CZ-jirka-medium`, about 180 MB in total). To use only Piper, set
`[tts] provider = "piper"`.

**Google Cloud TTS (default).** The free tier covers 1M characters/month for
Chirp 3 HD voices and 4M for WaveNet/Standard. One break is about 350
characters.

1. Go to <https://console.cloud.google.com> → create a project.
2. Link a billing account. Google requires one even for the free tier.
3. **APIs & Services → Enable APIs** → enable **Cloud Text-to-Speech API**.
4. **IAM & Admin → Service Accounts** → create one (no roles needed) →
   **Keys → Add key → JSON**. Save the file somewhere private, e.g.
   `~/.config/ai-radio-host/gcp-tts-key.json`, then `chmod 600` it.
5. In `.env`, add `GOOGLE_APPLICATION_CREDENTIALS=/full/path/to/that.json`.
6. Optional but recommended: set a budget alert in Billing.

`tts.google.monthly_char_limit` (default 900k) stops Google usage before the
free tier runs out, and Piper takes over. The counter lives in
`~/.local/state/ai-radio-host/google_tts_usage.json` and only counts this
app's usage.

Voices: the same male voice, Charon, is set for both `en-US` and `cs-CZ`. The
other Chirp 3 HD voices are listed
[here](https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types).

### Optional: hard spending cap for Google

Google Cloud budgets only send alerts. `tools/gcp-billing-cap/` adds a real cap: a
budget whose Pub/Sub notifications trigger a Cloud Function that **unlinks billing
from the project** once spending reaches the budget, which stops every paid API.
Needs the `gcloud` CLI logged in as the project owner.

```bash
tools/gcp-billing-cap/setup.sh PROJECT_ID 0 1     # cut at the first real charge; 1 CZK budget for email alerts
```

The script is safe to re-run. It ends with a test message that only logs
"would unlink". Google reports costs a few hours late, so a little can slip past
the cap. To undo a cut-off, link the billing account to the project again in the
console.

## App (GUI)

```bash
.venv/bin/radiohost-gui        # or: .venv/bin/radiohost gui
```

To add it to your app launcher:
`sed "s|@VENV@|$PWD/.venv|" data/dev.zavoram.RadioHost.desktop > ~/.local/share/applications/dev.zavoram.RadioHost.desktop`

| Tab | What it does |
|---|---|
| **On Air** | On/off switch, dry-run switch and language. Shows the current song, the count toward the next break, the next break's status, and recent scripts (▶ replays one). |
| **Log** | The live log. |
| **Settings** | Breaks, host personality, AI provider (Claude or Ollama) and model, speech provider, Google budget, account status (Spotify login button included), **Run setup again**, and at the bottom **Reset everything and set up again**. Reset deletes the settings, the Spotify login and the keys in `.env`, then opens the setup wizard. It keeps your Google key file, the monthly Google usage counter and downloaded voices. Changes apply the next time you go on air. |
| **Voices** | Every Google Chirp 3 HD and Piper voice for the chosen language. ▶ plays a sample; tick a voice to use it. |

If you close the window while on air, the app asks whether to keep running in
the background. Opening the app again brings the window back.

## Usage

```bash
.venv/bin/radiohost queue-test          # first: check queue predictions with shuffle on (read-only)
.venv/bin/radiohost run --dry-run       # print scripts only: no TTS, no pausing
.venv/bin/radiohost run --dry-run --every 1   # a script for every track, for fast testing
.venv/bin/radiohost once                # speak one break for the next queued song now (playback untouched)
.venv/bin/radiohost once --lang cs      # same in Czech
.venv/bin/radiohost run                 # the real thing
.venv/bin/radiohost run --lang cs -v    # Czech host, debug logging (shows the facts used)
```

`once` plays the clip over your music without pausing it; it's only for
testing voices.

The log shows the current track, the count toward the next break, the facts
count, the full script text, and why any break was skipped.

## Configuration

`config.toml` (gitignored) holds the settings and `.env` (gitignored) holds the
secrets. See `config.example.toml` for every option: break frequency, lead
time, length, language, personality, model, TTS provider/voices/budget, player
backend order and audio command.

## Layout

| Module | Role |
|---|---|
| `radiohost/player.py` | `PlayerControl` interface: MPRIS (any Spotify app), Spotifast CLI, Web API, plus a fallback chain |
| `radiohost/spotify_api.py` | PKCE login, token refresh, 429 back-off, queue, playback |
| `radiohost/facts.py` | Wikipedia + MusicBrainz lookups |
| `radiohost/script.py` | The host prompt; Claude or Ollama; validation |
| `radiohost/setup_wizard.py` | First-run setup wizard |
| `radiohost/tts.py` | `TTSProvider`: Google, Piper, fallback, monthly budget |
| `radiohost/audio.py` | Clip playback (pw-play / paplay / mpv) |
| `radiohost/loop.py` | State machine: counting, planning, queue checks, the break |
| `radiohost/config.py` | config.toml + .env loading |
