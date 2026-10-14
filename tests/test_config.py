import pytest

from radiohost.config import (Config, HostConfig, TTSConfig, add_host, delete_host, load_config,
                              save_config, select_host, update_host, voice_engine)


def test_voice_engine():
    assert voice_engine("en-US-Chirp3-HD-Charon") == "google"
    assert voice_engine("cs-CZ-Chirp3-HD-Charon") == "google"
    assert voice_engine("en_US-ryan-high") == "piper"
    assert voice_engine("cs_CZ-jirka-medium") == "piper"


def test_voice_for_prefers_per_language_choice_then_legacy_provider():
    t = TTSConfig(provider="google")
    assert t.voice_for("cs") == "cs-CZ-Chirp3-HD-Charon"
    t.provider = "piper"
    assert t.voice_for("en") == "en_US-ryan-high"
    t.voices["en"] = "en-US-Chirp3-HD-Achernar"   # mixing engines per language is fine
    assert t.voice_for("en") == "en-US-Chirp3-HD-Achernar"
    assert t.voice_for("cs") == "cs_CZ-jirka-medium"


def test_voices_round_trip(tmp_path):
    cfg = Config()
    cfg.tts.voices = {"en": "en-US-Chirp3-HD-Achernar", "cs": "cs_CZ-jirka-medium"}
    path = save_config(cfg, tmp_path / "config.toml")
    again = load_config(path)
    assert again.tts.voice_for("en") == "en-US-Chirp3-HD-Achernar"
    assert again.tts.voice_for("cs") == "cs_CZ-jirka-medium"


def test_old_config_without_profiles_loads_active_host_as_only_profile(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[host]\nname = "Felix"\npersonality = "calm"\n')
    cfg = load_config(path)
    assert cfg.host.profiles == {"Felix": "calm"}


def test_profiles_round_trip(tmp_path):
    cfg = Config()
    cfg.host.profiles = {"": "unnamed", "Felix Moon": 'says "hi"'}
    add_host(cfg.host, "Nora", "night owl")
    again = load_config(save_config(cfg, tmp_path / "config.toml"))
    assert again.host.name == "Nora" and again.host.personality == "night owl"
    assert again.host.profiles == {"": "unnamed", "Felix Moon": 'says "hi"', "Nora": "night owl"}


def test_select_update_delete_hosts():
    h = HostConfig(name="A", personality="a", profiles={"A": "a", "B": "b"})
    select_host(h, "B")
    assert (h.name, h.personality) == ("B", "b")
    update_host(h, "C", "c")                       # rename keeps the place in the list
    assert list(h.profiles.items()) == [("A", "a"), ("C", "c")]
    with pytest.raises(ValueError):
        update_host(h, "A", "x")                   # names stay unique
    with pytest.raises(ValueError):
        add_host(h, "", "x")                       # new hosts need a name
    delete_host(h, "C")                            # deleting the active one selects another
    assert (h.name, h.personality, h.profiles) == ("A", "a", {"A": "a"})
    with pytest.raises(ValueError):
        delete_host(h, "A")                        # never delete the last host
