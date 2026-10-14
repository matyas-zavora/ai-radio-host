import io
import sys
import tarfile

import pytest

from radiohost import updates
from radiohost.config import Config, load_config, save_config


def test_version_compare():
    assert updates.parse_version("v0.4.0") == (0, 4, 0)
    assert updates.is_newer("v0.10.0", "0.9.1")
    assert updates.is_newer("v0.4.1", "0.4")
    assert not updates.is_newer("v0.4.0", "0.4.0")
    assert not updates.is_newer("v0.3.9", "0.4.0")
    assert not updates.is_newer("v1.0.0", "dev")
    assert not updates.is_newer("nightly", "0.4.0")


GITHUB_JSON = {"tag_name": "v0.5.0", "html_url": "https://github.com/x/releases/tag/v0.5.0",
               "tarball_url": "https://api.github.com/x/tarball/v0.5.0", "body": " Notes \n",
               "assets": []}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_latest_release_parsing(monkeypatch):
    monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: FakeResponse(GITHUB_JSON))
    rel = updates.latest_release()
    assert (rel.tag, rel.version, rel.notes) == ("v0.5.0", "0.5.0", "Notes")
    assert rel.tarball.endswith("tarball/v0.5.0")
    asset = {"name": "ai-radio-host-0.5.0.tar.gz", "browser_download_url": "https://dl/a.tar.gz"}
    monkeypatch.setattr(updates.httpx, "get",
                        lambda *a, **k: FakeResponse({**GITHUB_JSON, "assets": [asset]}))
    assert updates.latest_release().tarball == "https://dl/a.tar.gz"


def test_check_skips(monkeypatch):
    calls = []
    monkeypatch.setattr(updates.httpx, "get",
                        lambda *a, **k: calls.append(1) or FakeResponse(GITHUB_JSON))
    cfg = Config()
    assert updates.check(cfg, "0.4.0").version == "0.5.0"
    assert updates.check(cfg, "0.5.0") is None             # up to date
    cfg.updates.skip_version = "0.5.0"
    assert updates.check(cfg, "0.4.0") is None             # skipped
    calls.clear()
    cfg.updates.check = False
    assert updates.check(cfg, "0.4.0") is None             # turned off: no request at all
    cfg.updates.check = True
    assert updates.check(cfg, "dev") is None               # unknown version: no request
    assert calls == []


def test_updates_config_round_trip(tmp_path):
    cfg = Config()
    cfg.updates.check, cfg.updates.skip_version = False, "0.5.0"
    again = load_config(save_config(cfg, tmp_path / "config.toml"))
    assert (again.updates.check, again.updates.skip_version) == (False, "0.5.0")


def test_install_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "DEV", False)
    monkeypatch.setattr(updates, "DATA_DIR", tmp_path / "share")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "share/app"))
    assert updates.install_mode() == "installed"
    monkeypatch.setattr(sys, "prefix", "/opt/homebrew/opt/ai-radio-host/libexec")
    assert updates.install_mode() == "homebrew"
    monkeypatch.setattr(sys, "prefix", "/usr")
    assert updates.install_mode() == "unknown"
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updates, "DEV", True)
    monkeypatch.setattr(updates, "PROJECT_DIR", tmp_path)
    assert updates.install_mode() == "source"


def _tar(path, name, data=b"x", link=None):
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
        else:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("name,link", [("../evil.sh", None), ("/etc/evil", None),
                                       ("pkg/link", "../../outside")])
def test_safe_extract_rejects_escapes(tmp_path, name, link):
    _tar(tmp_path / "a.tar.gz", name, link=link)
    (tmp_path / "out").mkdir()
    with pytest.raises(updates.UpdateError):
        updates.safe_extract(tmp_path / "a.tar.gz", tmp_path / "out")


def test_safe_extract_ok(tmp_path):
    _tar(tmp_path / "a.tar.gz", "pkg/install.sh", b"echo hi")
    (tmp_path / "out").mkdir()
    updates.safe_extract(tmp_path / "a.tar.gz", tmp_path / "out")
    assert (tmp_path / "out/pkg/install.sh").read_bytes() == b"echo hi"


def test_unknown_mode_does_not_update():
    rel = updates.Release("v9.0.0", "9.0.0", "", "", "")
    with pytest.raises(updates.UpdateError):
        updates.perform_update(rel, "unknown")
