"""build_plan / apply_plan against fake Spotify and Last.fm clients (no network)."""

import pytest

import sort_by_genre as sbg

ME = "me"


def track(uri, name, *artists):
    return {"uri": f"spotify:track:{uri}", "name": name, "artists": [{"name": a} for a in artists]}


class FakeSpotify:
    def __init__(self, playlists, contents):
        self.playlists = playlists      # list of playlist objects
        self.contents = contents        # playlist id -> list of tracks
        self.created, self.added = [], []

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
    genre_map = {"EDM": {"house"}, "Pop": {"pop"}}
    return sp, lastfm, genre_map


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
    plan = sbg.build_plan(sp, lastfm, "Big Mix", genre_map, "Unsorted")
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
