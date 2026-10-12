from radiohost.config import Config, TTSConfig, load_config, save_config, voice_engine


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
