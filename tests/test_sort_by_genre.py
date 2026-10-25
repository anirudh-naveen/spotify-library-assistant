import json

import pytest

import sort_by_genre as sbg

PID = "37i9dQZF1DXcBWIGoYBM5M"


# ---------------------------------------------------------------- norm

@pytest.mark.parametrize("raw, expected", [
    ("Pop", "pop"),
    ("K-Pop", "k pop"),
    ("drum_and_bass", "drum and bass"),
    ("Drum & Bass", "drum and bass"),
    ("  Hip-Hop  ", "hip hop"),
    ("post - hardcore", "post hardcore"),
    ("", ""),
])
def test_norm(raw, expected):
    assert sbg.norm(raw) == expected


def test_parse_toptags_filters_normalizes_and_dedupes():
    data = {"toptags": {"tag": [
        {"name": "Hip-Hop", "count": 100},
        {"name": "hip hop", "count": 80},   # duplicate after normalizing
        {"name": "", "count": 50},          # empty name
        {"name": "rap", "count": "40"},     # count as a string
        {"name": "seen live", "count": 3},  # below MIN_TAG_WEIGHT
    ]}}
    assert sbg.parse_toptags(data) == ["hip hop", "rap"]


def test_parse_toptags_single_tag_object_and_empty():
    assert sbg.parse_toptags({"toptags": {"tag": {"name": "Jazz", "count": 100}}}) == ["jazz"]
    assert sbg.parse_toptags({}) == []


def test_parse_toptags_respects_max_tags():
    data = {"toptags": {"tag": [{"name": f"t{i}", "count": 100} for i in range(20)]}}
    assert len(sbg.parse_toptags(data)) == sbg.MAX_TAGS


# ---------------------------------------------------------------- parse_playlist_id

@pytest.mark.parametrize("text", [
    f"https://open.spotify.com/playlist/{PID}",
    f"https://open.spotify.com/playlist/{PID}?si=abc123",
    f"https://open.spotify.com/intl-de/playlist/{PID}",
    f"spotify:playlist:{PID}",
    f"spotify:user:someone:playlist:{PID}",
    PID,
    f"  {PID}  ",
])
def test_parse_playlist_id_accepts(text):
    assert sbg.parse_playlist_id(text) == PID


@pytest.mark.parametrize("text", [
    "My Big Playlist",
    "https://open.spotify.com/album/" + PID,
    "short123",
    PID + "X",  # 23 characters: not an ID
    f"https://open.spotify.com/playlist/{PID}X",
])
def test_parse_playlist_id_rejects(text):
    assert sbg.parse_playlist_id(text) is None


# ---------------------------------------------------------------- match_genres

GENRES = {
    "EDM": {"edm", "house"},
    "Pop": {"pop", "dance pop"},
    "Electronic": {"electronic", "house"},
}


def lookup(table):
    calls = []

    def tag_lookup(artist):
        calls.append(artist)
        return table.get(artist, [])

    tag_lookup.calls = calls
    return tag_lookup


def test_match_multiple_playlists():
    hits, seen = sbg.match_genres(["DJ"], GENRES, lookup({"DJ": ["house", "techno"]}))
    assert hits == {"EDM", "Electronic"}
    assert seen == ["house", "techno"]


def test_main_artist_wins_and_featured_not_looked_up():
    tl = lookup({"Main": ["pop"], "Feat": ["edm"]})
    hits, _ = sbg.match_genres(["Main", "Feat"], GENRES, tl)
    assert hits == {"Pop"}
    assert tl.calls == ["Main"]


def test_featured_artist_is_fallback():
    tl = lookup({"Main": ["obscure"], "Feat": ["edm"]})
    hits, seen = sbg.match_genres(["Main", "Feat"], GENRES, tl)
    assert hits == {"EDM"}
    assert seen == ["obscure", "edm"]


def test_no_match_reports_tags_in_order_without_duplicates():
    tl = lookup({"A": ["folk", "indie"], "B": ["indie", "lo fi"]})
    hits, seen = sbg.match_genres(["A", "B"], GENRES, tl)
    assert hits == set()
    assert seen == ["folk", "indie", "lo fi"]


def test_whole_tag_match_only():
    hits, _ = sbg.match_genres(["X"], GENRES, lookup({"X": ["k pop"]}))
    assert hits == set()


def test_no_artists():
    assert sbg.match_genres([], GENRES, lookup({})) == (set(), [])


# ---------------------------------------------------------------- dedupe / ownership / genre map

def summary(uri, name="Song", artist="Artist", isrc=None, duration_ms=200_000):
    return sbg.track_summary({
        "uri": f"spotify:track:{uri}", "name": name, "artists": [{"name": artist}],
        "external_ids": {"isrc": isrc} if isrc else {}, "duration_ms": duration_ms,
    })


def test_new_tracks_drops_repeats_quietly():
    tracks = [summary("a", "A"), summary("b", "B"), summary("a", "A"), summary("c", "C")]
    fresh, same = sbg.new_tracks(tracks, existing=[summary("c", "C")])
    assert [t["uri"] for t in fresh] == ["spotify:track:a", "spotify:track:b"]
    assert same == 0


def test_new_tracks_same_recording_by_isrc():
    single = summary("single", "Totally Different Title", isrc="usabc1234567")
    album = summary("album", "Song", isrc="USABC1234567")
    fresh, same = sbg.new_tracks([single], existing=[album])
    assert fresh == [] and same == 1


def test_new_tracks_same_title_artist_and_length():
    existing = [summary("album", "Song (feat. Someone)", duration_ms=200_000)]
    remaster = summary("remaster", "Song - 2011 Remaster", duration_ms=201_500)
    live = summary("live", "Song - Live", duration_ms=200_000)
    other_intro = summary("intro2", "Song", duration_ms=95_000)  # same title, clearly a different track
    fresh, same = sbg.new_tracks([remaster, live, other_intro], existing)
    assert [t["uri"].split(":")[-1] for t in fresh] == ["live", "intro2"]
    assert same == 1


@pytest.mark.parametrize("title, expected", [
    ("Song (feat. X)", "song|artist"),
    ("Song [Remastered 2009]", "song|artist"),
    ("Song - 2011 Remaster", "song|artist"),
    ("Song (with Halsey)", "song|artist"),
    ("Song - Radio Edit", "song|artist"),
    ("Song - Live", "song live|artist"),
    ("Song (Remix)", "song remix|artist"),
    ("!!!", "!!!|artist"),
])
def test_song_key(title, expected):
    assert sbg.song_key(title, ["Artist"]) == expected


def test_song_key_needs_an_artist():
    assert sbg.song_key("Song", []) == ""


# ---------------------------------------------------------------- date filters

@pytest.mark.parametrize("value, end, expected", [
    ("2019", False, "2019-01-01"),
    ("2019", True, "2019-12-31"),
    ("2019-05", True, "2019-05-31"),
    ("2019-05-03", True, "2019-05-03"),
    ("", False, ""),
])
def test_date_bound(value, end, expected):
    assert sbg.date_bound(value, end) == expected


@pytest.mark.parametrize("bad", ["19", "2019/05/01", "May 2019"])
def test_date_bound_rejects(bad):
    with pytest.raises(sbg.GenrefyError):
        sbg.date_bound(bad)


def test_parse_date_filters():
    assert sbg.parse_date_filters({"added_after": "2024", "released_before": "2010", "released_after": ""}) == {
        "added_after": "2024-01-01", "released_before": "2010-12-31"}
    with pytest.raises(sbg.GenrefyError, match="ends before it starts"):
        sbg.parse_date_filters({"added_after": "2024-06-01", "added_before": "2024-01-01"})


def test_passes_date_filters():
    t = {"added_at": "2024-03-15T10:00:00Z", "release_date": "2019"}
    f = sbg.parse_date_filters
    assert sbg.passes_date_filters(t, {})
    assert sbg.passes_date_filters(t, f({"added_after": "2024-03-15", "added_before": "2024-03"}))
    assert not sbg.passes_date_filters(t, f({"added_after": "2024-03-16"}))
    assert sbg.passes_date_filters(t, f({"released_after": "2019-06"}))      # year-only 2019 overlaps
    assert not sbg.passes_date_filters(t, f({"released_after": "2020"}))
    assert not sbg.passes_date_filters(t, f({"released_before": "2018"}))
    assert not sbg.passes_date_filters({"added_at": None}, f({"added_after": "2000"}))  # unknown date


def test_index_by_name_prefers_editable_playlist():
    followed = {"id": "1", "name": "Pop", "owner": {"id": "someone"}}
    mine = {"id": "2", "name": "pop ", "owner": {"id": "me"}}
    collab = {"id": "3", "name": "EDM", "owner": {"id": "x"}, "collaborative": True}
    by_name = sbg.index_by_name([followed, mine, collab, {"id": "4", "name": None}], "me")
    assert by_name["pop"]["id"] == "2"
    assert sbg.can_edit(by_name["edm"], "me")
    assert set(by_name) == {"pop", "edm"}


def test_load_genre_map_old_format(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({
        "_help": "ignored",
        "playlists": {"Pop": ["K-Pop", "Pop"], "Jazz": "jazz"},
        "unsorted_playlist": "  ",
    }))
    genres, unsorted = sbg.load_genre_map(path)
    assert genres == {
        "Pop": {"keywords": {"k pop", "pop"}, "target": {"playlist_id": None, "playlist": "Pop"}},
        "Jazz": {"keywords": {"jazz"}, "target": {"playlist_id": None, "playlist": "Jazz"}},
    }
    assert unsorted is None


def test_parse_genre_map_custom_playlists():
    genres, unsorted = sbg.parse_genre_map({
        "genres": {
            "EDM": {"keywords": ["house"], "playlist": "Club Bangers", "playlist_id": "abc"},
            "Future Bass": {"keywords": ["future bass"], "playlist": "Club Bangers"},
            "Rock": {"keywords": ["rock"]},   # no playlist given: named after the genre
        },
        "playlists": {"Ignored": ["x"]},    # "genres" wins over the old key
        "unsorted_playlist": {"playlist": "Misc", "playlist_id": "zzz"},
    })
    assert genres["EDM"]["target"] == {"playlist_id": "abc", "playlist": "Club Bangers"}
    assert genres["Future Bass"]["target"] == {"playlist_id": None, "playlist": "Club Bangers"}
    assert genres["Rock"]["target"] == {"playlist_id": None, "playlist": "Rock"}
    assert "Ignored" not in genres
    assert unsorted == {"playlist_id": "zzz", "playlist": "Misc"}


@pytest.mark.parametrize("config", [
    {"genres": {"Pop": []}},
    {"genres": {"Pop": {"keywords": []}}},
    {"genres": {}},
    {"genres": ["pop"]},
    [],
])
def test_parse_genre_map_rejects(config):
    with pytest.raises(sbg.GenrefyError):
        sbg.parse_genre_map(config)


def test_bundled_genre_map_loads():
    genres, _ = sbg.load_genre_map(sbg.MAP_FILE)
    assert "EDM" in genres
