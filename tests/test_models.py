from radiohost.models import Track, spotify_uri


def test_spotify_uri_forms():
    want = "spotify:track:4yMXKeuRDOIzrh3fBiJYds"
    assert spotify_uri(want) == want
    assert spotify_uri("https://open.spotify.com/track/4yMXKeuRDOIzrh3fBiJYds") == want
    assert spotify_uri("https://open.spotify.com/track/4yMXKeuRDOIzrh3fBiJYds?si=abc") == want
    assert spotify_uri("/com/spotify/track/4yMXKeuRDOIzrh3fBiJYds") == want
    assert spotify_uri("/spotifast/Track/t7370") is None
    assert spotify_uri(None) is None


def test_official_client_track_matches_web_api_track():
    official = Track("Vent", ("NOIT",), uri=spotify_uri("https://open.spotify.com/track/4yMXKeuRDOIzrh3fBiJYds"))
    web_api = Track("Vent", ("NOIT",), uri="spotify:track:4yMXKeuRDOIzrh3fBiJYds")
    assert official.same_as(web_api)
