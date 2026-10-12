"""Real facts about a track from MusicBrainz and Wikipedia. Never raises; may return nothing."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse

import httpx

from .models import Facts, Track, _norm

log = logging.getLogger(__name__)

# Wikimedia and MusicBrainz ask for a contact in the User-Agent; set RADIOHOST_CONTACT to yours.
USER_AGENT = "ai-radio-host/0.1 (personal hobby project; " + os.environ.get(
    "RADIOHOST_CONTACT", "no contact given") + ")"

MUSIC_WORDS = re.compile(
    r"singer|band|rapper|musician|\bdj\b|producer|group|duo|trio|composer|songwriter|"
    r"vocalist|orchestra|zpěv|skupin|kapel|hudebn|raper|producent|skladatel|písničkář",
    re.I)
SONG_WORDS = re.compile(r"\bsong\b|\bsingle\b|píseň|písn|singl", re.I)
ALBUM_WORDS = re.compile(r"\balbum\b|\bep\b|mixtape|soundtrack", re.I)

# " - Remastered 2011", " - Radio Edit", " (feat. X)", "[Live]" ...
_SUFFIX = re.compile(r"\s+-\s+.*$")
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*(feat\.|ft\.|with |remaster|version|edit|mix|live|mono|stereo|demo)[^\)\]]*[\)\]]", re.I)


_CLEAN_TAG = re.compile(r"[a-z][a-z &'-]{1,24}")


def base_title(title: str) -> str:
    t = _BRACKETS.sub("", title)
    t = _SUFFIX.sub("", t)
    return t.strip() or title


def _sentences(text: str, max_chars: int = 700) -> str:
    text = " ".join(re.sub(r"\[[^\]]{1,30}\]", "", text).split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    return cut[: cut.rfind(". ") + 1] or cut


class FactFinder:
    def __init__(self, languages: list[str]):
        self.languages = list(dict.fromkeys(languages + ["en"]))  # host language first, then en
        self.http = httpx.Client(timeout=httpx.Timeout(6.0, connect=3.0),
                                 headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._mb_lock = threading.Lock()
        self._mb_last = 0.0

    def find(self, track: Track) -> Facts:
        facts = Facts()
        title = base_title(track.title)
        artist = track.artists[0] if track.artists else ""
        # No MusicBrainz recording dates: search ranks reissues first and gives wrong years.
        for step in (self._musicbrainz_artist,
                     self._wiki_song, self._wiki_artist, self._wiki_album):
            try:
                facts.items.extend(step(track, title, artist))
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.debug("fact lookup %s failed: %s", step.__name__, e)
        return facts

    # ---------- MusicBrainz (1 request/second) ----------

    def _mb(self, entity: str, query: str) -> dict:
        with self._mb_lock:
            wait = self._mb_last + 1.1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._mb_last = time.monotonic()
        return self._get(f"https://musicbrainz.org/ws/2/{entity}",
                         query=query, fmt="json", limit=5)

    @staticmethod
    def _q(s: str) -> str:
        return '"' + s.replace("\\", "").replace('"', "") + '"'

    def _musicbrainz_artist(self, track: Track, title: str, artist: str) -> list[str]:
        data = self._mb("artist", f"artist:{self._q(artist)}")
        for a in data.get("artists", []):
            if a.get("score", 0) < 95 or _norm(a.get("name", "")) != _norm(artist):
                continue
            bits = []
            kind = (a.get("type") or "").lower()
            if kind:
                bits.append(f"is a {kind}" if kind != "other" else "")
            area = (a.get("begin-area") or a.get("area") or {}).get("name")
            if area:
                bits.append(f"from {area}")
            begin = (a.get("life-span") or {}).get("begin")
            if begin:
                bits.append(f"{'formed' if kind == 'group' else 'born/started'} {begin}")
            if a.get("disambiguation"):
                bits.append(f"({a['disambiguation']})")
            tags = [t["name"] for t in sorted(a.get("tags", []), key=lambda t: -t.get("count", 0))
                    if t.get("count", 0) > 0 and _CLEAN_TAG.fullmatch(t["name"])][:4]
            name = a.get("name", artist)  # MusicBrainz's own spelling, not Spotify's
            out = []
            if any(bits):
                out.append(f"MusicBrainz: {name} " + ", ".join(b for b in bits if b) + ".")
            if tags:
                out.append(f"MusicBrainz: genre tags for {name}: {', '.join(tags)}.")
            return out
        return []

    # ---------- Wikipedia ----------

    def _get(self, url: str, **params) -> dict:
        for attempt in (0, 1):
            r = self.http.get(url, params=params or None)
            if r.status_code in (429, 503) and attempt == 0:  # MusicBrainz rate-limits with 503
                time.sleep(float(r.headers.get("Retry-After", "1")) if
                          r.headers.get("Retry-After", "").isdigit() else 1.0)
                continue
            r.raise_for_status()
            return r.json()
        return {}

    def _wiki_search(self, lang: str, query: str) -> list[dict]:
        return self._get(f"https://{lang}.wikipedia.org/w/rest.php/v1/search/page",
                         q=query, limit=5).get("pages", [])

    def _wiki_summary(self, lang: str, key: str) -> dict:
        return self._get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                         + urllib.parse.quote(key, safe=""))

    def _wiki_find(self, query: str, accept, label: str) -> list[str]:
        for lang in self.languages:
            for page in self._wiki_search(lang, query):
                desc = page.get("description") or ""
                if not accept(page.get("title", ""), desc):
                    continue
                summary = self._wiki_summary(lang, page["key"])
                if summary.get("type") == "disambiguation" or not summary.get("extract"):
                    continue
                if not accept(summary.get("title", ""), desc + " " + summary["extract"][:300]):
                    continue
                return [f"Wikipedia ({lang}, {label} '{summary['title']}'): "
                        f"{_sentences(summary['extract'])}"]
        return []

    def _wiki_song(self, track: Track, title: str, artist: str) -> list[str]:
        a = _norm(artist)
        return self._wiki_find(
            f"{title} {artist} song",
            lambda t, d: _norm(title) in _norm(t) and bool(SONG_WORDS.search(d)) and a in _norm(d),
            "song")

    def _wiki_artist(self, track: Track, title: str, artist: str) -> list[str]:
        a = _norm(artist)
        return self._wiki_find(
            artist,
            lambda t, d: _norm(t).startswith(a) and bool(MUSIC_WORDS.search(d)),
            "artist")

    def _wiki_album(self, track: Track, title: str, artist: str) -> list[str]:
        album = base_title(track.album)
        if not album or _norm(album) == _norm(title):  # singles: album page == song page
            return []
        a = _norm(artist)
        return self._wiki_find(
            f"{album} {artist} album",
            lambda t, d: _norm(album) in _norm(t) and bool(ALBUM_WORDS.search(d)) and a in _norm(d),
            "album")
