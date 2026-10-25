"""build_plan / apply_plan against fake Spotify and Last.fm clients (no network)."""

import pytest

import sort_by_genre as sbg

ME = "me"


def track(uri, name, *artists, added="2024-01-10T00:00:00Z", released="2020-05-01", isrc=None, length=200_000):
    return {
        "uri": f"spotify:track:{uri}", "name": name, "artists": [{"name": a} for a in artists],
        "added_at": added, "album": {"release_date": released}, "duration_ms": length,
        "external_ids": {"isrc": isrc} if isrc else {},
    }


class FakeSpotify:
    def __init__(self, playlists, contents):
        self.playlists = playlists      # list of playlist objects
        self.contents = contents        # playlist id -> list of tracks
        self.created, self.added, self.removed = [], [], []

    def me(self):
        return {"id": ME, "display_name": "Me"}

    def my_playlists(self):
        return self.playlists

    def playlist_tracks(self, playlist_id):
        if playlist_id not in self.contents:
            raise sbg.SpotifyError(404, "GET", playlist_id, "not found")
        return iter(self.contents[playlist_id])

    def create_playlist(self, name):
        pid = f"new-{name}"
        self.created.append(name)
        self.contents[pid] = []
        return {"id": pid}

    def add_tracks(self, playlist_id, uris):
        self.added.append((playlist_id, list(uris)))
        self.contents[playlist_id].extend({"uri": u} for u in uris)

    def remove_tracks(self, playlist_id, uris):
        self.removed.append((playlist_id, list(uris)))
        self.contents[playlist_id] = [t for t in self.contents[playlist_id] if t["uri"] not in uris]


class FakeLastFM:
    def __init__(self, tags):
        self.tags_by_artist = tags

    def tags(self, artist):
        return self.tags_by_artist.get(artist, [])

    def save(self):
        pass


def playlist(pid, name, owner=ME, collaborative=False):
    return {"id": pid, "name": name, "owner": {"id": owner}, "collaborative": collaborative}


@pytest.fixture
def world():
    source = [
        track("a", "Song A", "DJ"),
        track("b", "Song B", "Singer"),
        track("c", "Song C", "Nobody"),
        track("a", "Song A again", "DJ"),   # duplicate in the source
        track("d", "Song D", "DJ"),
    ]
    sp = FakeSpotify(
        [playlist("src", "Big Mix"), playlist("edm", "EDM")],
        {"src": source, "edm": [{"uri": "spotify:track:d"}]},  # D is already in EDM
    )
    lastfm = FakeLastFM({"DJ": ["house"], "Singer": ["pop"], "Nobody": ["polka"]})
    genres, _ = sbg.parse_genre_map({"genres": {"EDM": ["house"], "Pop": ["pop"]}})
    return sp, lastfm, genres


def uris(target):
    return [t["uri"].split(":")[-1] for t in target["tracks"]]


def test_build_plan_dedupes_and_skips_existing(world):
    sp, lastfm, genre_map = world
    plan = sbg.build_plan(sp, lastfm, "big mix", genre_map, None)
    targets = {t["name"]: t for t in plan["targets"]}

    assert plan["source_name"] == "Big Mix"
    assert plan["total"] == 5
    assert uris(targets["EDM"]) == ["a"]           # duplicate A once, D already there
    assert targets["EDM"]["playlist_id"] == "edm"
    assert uris(targets["Pop"]) == ["b"]
    assert targets["Pop"]["playlist_id"] is None   # needs creating
    assert [u["name"] for u in plan["unmatched"]] == ["Song C"]
    assert plan["unmatched"][0]["tags"] == ["polka"]
    assert sp.created == [] and sp.added == []     # planning changes nothing


def test_build_plan_unsorted_playlist(world):
    sp, lastfm, genre_map = world
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genre_map, sbg.parse_target("Unsorted"))
    unsorted = next(t for t in plan["targets"] if t["name"] == "Unsorted")
    assert uris(unsorted) == ["c"]


def test_build_plan_progress_reports_each_track(world):
    sp, lastfm, genre_map = world
    calls = []
    sbg.build_plan(sp, lastfm, "Big Mix", genre_map, None, lambda *a: calls.append(a))
    lookups = [c for c in calls if c[0].startswith("Looking up")]
    assert lookups[-1][1:] == (5, 5)


def test_build_plan_refuses_playlists_you_dont_own(world):
    sp, lastfm, genre_map = world
    sp.playlists.append(playlist("theirs", "Pop", owner="someone"))
    with pytest.raises(sbg.GenrefyError, match="isn't your playlist"):
        sbg.build_plan(sp, lastfm, "Big Mix", genre_map, None)


def test_build_plan_unreadable_source(world):
    sp, lastfm, genre_map = world
    sp.playlists.append(playlist("gone", "Gone"))
    with pytest.raises(sbg.GenrefyError, match="Couldn't read the source"):
        sbg.build_plan(sp, lastfm, "Gone", genre_map, None)


def test_apply_plan_creates_adds_and_rechecks(world):
    sp, lastfm, genre_map = world
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genre_map, None)
    sp.contents["edm"].append({"uri": "spotify:track:a"})  # added elsewhere after the preview
    log = []
    sbg.apply_plan(sp, plan, report=log.append)

    assert sp.created == ["Pop"]
    assert sp.added == [("new-Pop", ["spotify:track:b"])]
    assert log == ["EDM: nothing new", 'Created playlist "Pop"', "Pop: added 1 songs"]


def test_resolve_source_fills_name_for_ids():
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    by_name = sbg.index_by_name([playlist(pid, "Chill")], ME)
    assert sbg.resolve_source(pid, by_name) == (pid, "Chill")
    with pytest.raises(sbg.GenrefyError):
        sbg.resolve_source("nope", by_name)


def test_genres_can_share_a_chosen_playlist(world):
    sp, lastfm, _ = world
    sp.playlists.append(playlist("mix", "Everything Nice"))
    sp.contents["mix"] = []
    genres, _ = sbg.parse_genre_map({"genres": {
        "EDM": {"keywords": ["house"], "playlist": "old name", "playlist_id": "mix"},
        "Pop": {"keywords": ["pop"], "playlist": "Everything Nice"},
    }})
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genres)
    assert len(plan["targets"]) == 1
    target = plan["targets"][0]
    assert target["name"] == "Everything Nice"        # the playlist's current name, not the stored one
    assert target["genres"] == ["EDM", "Pop"]
    assert sorted(uris(target)) == ["a", "b", "d"]


def test_new_playlist_with_custom_name(world):
    sp, lastfm, _ = world
    genres, _ = sbg.parse_genre_map({"genres": {"Pop": {"keywords": ["pop"], "playlist": "Bops"}}})
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genres)
    sbg.apply_plan(sp, plan, report=lambda m: None)
    assert sp.created == ["Bops"]


def test_chosen_playlist_missing(world):
    sp, lastfm, _ = world
    genres, _ = sbg.parse_genre_map({"genres": {"Pop": {"keywords": ["pop"], "playlist_id": "deleted"}}})
    with pytest.raises(sbg.GenrefyError, match="isn't in your library anymore"):
        sbg.build_plan(sp, lastfm, "Big Mix", genres)


def test_same_song_under_another_id_is_skipped(world):
    sp, lastfm, genres = world
    sp.contents["edm"].append(track("album-a", "Song A", "DJ", isrc="X1"))
    sp.contents["src"].append(track("single-a", "Song A - Radio Edit", "DJ", isrc="X1"))
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genres)
    edm = next(t for t in plan["targets"] if t["name"] == "EDM")
    # "single-a" matches by ISRC; the source's plain "Song A" matches by title, artist and length
    assert uris(edm) == []
    assert edm["same_song_skipped"] == 2


def test_date_filters_limit_the_source(world):
    sp, lastfm, genres = world
    sp.contents["src"] = [
        track("old", "Old", "DJ", added="2020-01-01T00:00:00Z", released="2001"),
        track("new", "New", "DJ", added="2025-06-01T00:00:00Z", released="2024-02-02"),
    ]
    filters = sbg.parse_date_filters({"added_after": "2025"})
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genres, filters=filters)
    assert (plan["source_total"], plan["total"]) == (2, 1)
    assert uris(next(t for t in plan["targets"] if t["name"] == "EDM")) == ["new"]
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genres, filters=sbg.parse_date_filters({"released_before": "2010"}))
    assert uris(next(t for t in plan["targets"] if t["name"] == "EDM")) == ["old"]


# ---------------------------------------------------------------- clean up

@pytest.fixture
def messy():
    sp = FakeSpotify(
        [playlist("edm", "EDM"), playlist("theirs", "Theirs", owner="someone")],
        {"edm": [
            track("dj", "Banger", "DJ"),
            track("pop", "Ballad", "Singer"),
            track("pop", "Ballad", "Singer"),        # repeated: listed once
            track("mystery", "Demo", "Nobody"),       # no tags at all
            track("collab", "Collab", "Singer", "DJ"),  # featured artist matches
            track("synth", "Glow", "Synth Kid"),
        ], "theirs": []},
    )
    lastfm = FakeLastFM({"DJ": ["house"], "Singer": ["pop", "dance"], "Synth Kid": ["electronic"]})
    genres, _ = sbg.parse_genre_map({"genres": {
        "EDM": {"keywords": ["house"], "playlist_id": "edm"},
        "Electronic": {"keywords": ["electronic"], "playlist_id": "edm"},
        "Pop": ["pop"],
    }})
    return sp, lastfm, genres


def test_cleanup_lists_songs_that_dont_match(messy):
    sp, lastfm, genres = messy
    c = sbg.build_cleanup(sp, lastfm, "EDM", "EDM", genres)
    assert c["playlist_id"] == "edm" and c["total"] == 5
    off = {t["name"]: t for t in c["off"]}
    assert set(off) == {"Ballad", "Demo", "Glow"}
    assert off["Ballad"]["tags"] == ["pop", "dance"] and not off["Ballad"]["unknown"]
    assert off["Demo"]["unknown"]
    assert sp.removed == []  # finding changes nothing


def test_cleanup_with_several_genres(messy):
    sp, lastfm, genres = messy
    c = sbg.build_cleanup(sp, lastfm, "edm", ["EDM", "Electronic"], genres)
    assert {t["name"] for t in c["off"]} == {"Ballad", "Demo"}
    assert c["keywords"] == ["electronic", "house"]


@pytest.mark.parametrize("playlist, genres_arg, message", [
    ("Theirs", "EDM", "only clean up playlists you own"),
    ("EDM", "Jazz", "no genre called"),
    ("EDM", [], "Pick the genre"),
])
def test_cleanup_refuses(messy, playlist, genres_arg, message):
    sp, lastfm, genres = messy
    with pytest.raises(sbg.GenrefyError, match=message):
        sbg.build_cleanup(sp, lastfm, playlist, genres_arg, genres)


def test_apply_cleanup_removes_only_listed_songs(messy):
    sp, lastfm, genres = messy
    c = sbg.build_cleanup(sp, lastfm, "EDM", "EDM", genres)
    log = []
    n = sbg.apply_cleanup(sp, c, ["spotify:track:pop", "spotify:track:dj", "spotify:track:pop"], report=log.append)
    assert n == 1
    assert sp.removed == [("edm", ["spotify:track:pop"])]  # "dj" matched EDM, so it isn't removable
    assert [t["uri"] for t in sp.contents["edm"]].count("spotify:track:pop") == 0
    assert log == ["EDM: removed 1 songs"]


def test_remove_tracks_falls_back_to_old_body(monkeypatch):
    calls = []

    def fake_call(self, method, url, **kwargs):
        calls.append(kwargs["json"])
        if "items" in kwargs["json"]:
            raise sbg.SpotifyError(400, method, url, "bad request")
        return {}

    monkeypatch.setattr(sbg.Spotify, "call", fake_call)
    sbg.Spotify(auth=None).remove_tracks("p", [f"spotify:track:{i}" for i in range(150)])
    assert [list(c) for c in calls] == [["items"], ["tracks"], ["items"], ["tracks"]]
    assert len(calls[1]["tracks"]) == 100 and len(calls[3]["tracks"]) == 50
