"""Check GitHub for a newer release and install it the way this copy was installed."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CACHE_DIR, DATA_DIR, DEV, PROJECT_DIR, Config
from .facts import USER_AGENT

LATEST_URL = "https://api.github.com/repos/matyas-zavora/ai-radio-host/releases/latest"
BUNDLE_ID = "dev.zavoram.RadioHost"


class UpdateError(Exception):
    pass


@dataclass
class Release:
    tag: str
    version: str
    url: str           # release page
    tarball: str
    notes: str


def parse_version(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(tag: str, current: str) -> bool:
    new, cur = parse_version(tag), parse_version(current)
    return new is not None and cur is not None and new > cur


def latest_release() -> Release:
    """The newest published release (GitHub's /latest skips drafts and prereleases)."""
    r = httpx.get(LATEST_URL, timeout=5, follow_redirects=True,
                  headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    data = r.json()
    tag = data["tag_name"]
    asset = next((a["browser_download_url"] for a in data.get("assets", [])
                  if a.get("name", "").endswith(".tar.gz")), None)
    return Release(tag=tag, version=tag.lstrip("v"), url=data.get("html_url", ""),
                   tarball=asset or data["tarball_url"], notes=(data.get("body") or "").strip())


def check(cfg: Config, current: str) -> Release | None:
    """The release to offer, or None (checks off, unknown version, skipped, up to date)."""
    if not cfg.updates.check or parse_version(current) is None:
        return None
    rel = latest_release()
    if rel.version == cfg.updates.skip_version or not is_newer(rel.tag, current):
        return None
    return rel


def install_mode() -> str:
    """source (git checkout), homebrew, installed (install.sh) or unknown."""
    if DEV and (PROJECT_DIR / ".git").exists():
        return "source"
    prefix = str(Path(sys.prefix).resolve())
    if "/Cellar/ai-radio-host/" in prefix or "/opt/ai-radio-host" in sys.prefix:
        return "homebrew"
    if Path(sys.prefix).resolve() == (DATA_DIR / "app").resolve():
        return "installed"
    return "unknown"


def safe_extract(archive: Path, dest: Path) -> None:
    """Unpack a tarball, refusing anything that would land outside `dest`."""
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()
                                                                or m.issym()):
                raise UpdateError(f"unsafe file in the download: {m.name}")
            if m.issym() and (m.linkname.startswith("/") or ".." in Path(m.linkname).parts):
                raise UpdateError(f"unsafe link in the download: {m.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # shortcut: Python < 3.11.4 has no filter; the checks above cover it
            tar.extractall(dest)


def _run(cmd: list, cwd: Path | None = None) -> None:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"{cmd[0]}: {e}") from e
    if p.returncode != 0:
        tail = (p.stdout + p.stderr).strip().splitlines()[-5:]
        raise UpdateError(f"{' '.join(map(str, cmd[:3]))} failed:\n" + "\n".join(tail))


def perform_update(rel: Release, mode: str) -> str:
    """Install `rel`; returns a short message. Settings in ~/.config/ai-radio-host stay as they are."""
    if mode == "installed":
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="update-", dir=CACHE_DIR) as tmp:
            tmp = Path(tmp)
            archive = tmp / "release.tar.gz"
            try:
                with httpx.stream("GET", rel.tarball, timeout=60, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT}) as r:
                    r.raise_for_status()
                    with archive.open("wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
            except httpx.HTTPError as e:
                raise UpdateError(f"download failed: {e}") from e
            src = tmp / "src"
            src.mkdir()
            safe_extract(archive, src)
            script = next(src.glob("*/install.sh"), None) or src / "install.sh"
            if not script.exists():
                raise UpdateError("the download has no install.sh")
            _run(["bash", script], cwd=script.parent)
        return f"Installed {rel.version}."
    if mode == "source":
        git = ["git", "-C", str(PROJECT_DIR)]
        branch = subprocess.run(git + ["branch", "--show-current"], capture_output=True, text=True)
        dirty = subprocess.run(git + ["status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True)
        if branch.stdout.strip() != "main" or dirty.stdout.strip():
            raise UpdateError("This is a source checkout with local changes or not on main. "
                              "Update it yourself: git pull && ./install.sh")
        _run(git + ["pull", "--ff-only"])
        if shutil.which("uv"):
            _run(["uv", "pip", "install", "-q", "--python", sys.executable, "-e", PROJECT_DIR])
        else:
            _run([sys.executable, "-m", "pip", "install", "-q", "-e", PROJECT_DIR])
        return f"Updated the checkout to {rel.version}."
    if mode == "homebrew":
        brew = shutil.which("brew") or next(
            (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(p)), None)
        if not brew:
            raise UpdateError("Homebrew (brew) not found")
        _run([brew, "upgrade", "ai-radio-host"])
        return f"Installed {rel.version} with Homebrew."
    raise UpdateError("Can't tell how this copy was installed, so it can't update itself. "
                      "Download the new version from the release page.")


def relaunch() -> None:
    """Start the app again after it quit. Call once the main loop has ended."""
    if sys.platform == "darwin":
        # exec'ing Python would detach the process from the .app bundle; let macOS reopen it.
        subprocess.Popen(["sh", "-c", f"sleep 1; open -b {BUNDLE_ID}"], start_new_session=True)
        return
    os.execv(sys.executable, [sys.executable, "-m", "radiohost.gui"])
