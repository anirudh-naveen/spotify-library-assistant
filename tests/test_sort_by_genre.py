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

def test_new_uris_skips_existing_and_duplicates():
    tracks = [{"uri": u} for u in ["a", "b", "a", "c", "b"]]
    assert sbg.new_uris(tracks, existing={"c"}) == ["a", "b"]


def test_index_by_name_prefers_editable_playlist():
    followed = {"id": "1", "name": "Pop", "owner": {"id": "someone"}}
    mine = {"id": "2", "name": "pop ", "owner": {"id": "me"}}
    collab = {"id": "3", "name": "EDM", "owner": {"id": "x"}, "collaborative": True}
    by_name = sbg.index_by_name([followed, mine, collab, {"id": "4", "name": None}], "me")
    assert by_name["pop"]["id"] == "2"
    assert sbg.can_edit(by_name["edm"], "me")
    assert set(by_name) == {"pop", "edm"}


def test_load_genre_map_normalizes(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({
        "_help": "ignored",
        "playlists": {"Pop": ["K-Pop", "Pop"], "Jazz": "jazz"},
        "unsorted_playlist": "  ",
    }))
    playlists, unsorted = sbg.load_genre_map(path)
    assert playlists == {"Pop": {"k pop", "pop"}, "Jazz": {"jazz"}}
    assert unsorted is None


def test_load_genre_map_rejects_empty_keywords(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"playlists": {"Pop": []}}))
    with pytest.raises(SystemExit):
        sbg.load_genre_map(path)


def test_bundled_genre_map_loads():
    playlists, _ = sbg.load_genre_map(sbg.MAP_FILE)
    assert "EDM" in playlists
