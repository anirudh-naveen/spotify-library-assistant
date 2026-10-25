// Tests for the browser version (docs/spotassist.js), with fake Spotify and Last.fm clients.
// Run with: node --test tests/js/*.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

// just enough of a browser for the module
const memory = new Map();
globalThis.localStorage = {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => memory.set(k, String(v)),
  removeItem: (k) => memory.delete(k),
};
globalThis.location = new URL("https://someone.github.io/spotify-library-assistant/index.html");

const sa = await import("../../docs/spotassist.js");

const ME = "me";
const track = (uri, name, artists, { added = "2024-01-10T00:00:00Z", released = "2020-05-01", isrc = null, length = 200000 } = {}) => ({
  uri: `spotify:track:${uri}`, name, artists: [].concat(artists).map((a) => ({ name: a })),
  added_at: added, album: { release_date: released }, duration_ms: length, external_ids: isrc ? { isrc } : {},
});
const playlist = (id, name, owner = ME, collaborative = false) => ({ id, name, owner: { id: owner }, collaborative });

class FakeSpotify {
  constructor(playlists, contents) {
    this.playlists = playlists;
    this.contents = contents;
    this.created = [];
    this.added = [];
    this.removed = [];
  }
  async me() { return { id: ME, display_name: "Me" }; }
  async myPlaylists() { return this.playlists; }
  async *playlistTracks(id) {
    if (!(id in this.contents)) throw new sa.SpotifyError(404, "GET", id, "not found");
    yield* this.contents[id];
  }
  async createPlaylist(name) {
    const id = `new-${name}`;
    this.created.push(name);
    this.contents[id] = [];
    return { id };
  }
  async addTracks(id, uris) {
    this.added.push([id, uris]);
    this.contents[id].push(...uris.map((uri) => ({ uri })));
  }
  async removeTracks(id, uris) {
    this.removed.push([id, uris]);
    this.contents[id] = this.contents[id].filter((t) => !uris.includes(t.uri));
  }
}

const fakeLastFM = (tags) => ({ tags: async (a) => tags[a] || [], save() {} });
const ids = (target) => target.tracks.map((t) => t.uri.split(":").pop());

function world() {
  const sp = new FakeSpotify([playlist("src", "Big Mix"), playlist("edm", "EDM")], {
    src: [track("a", "Song A", "DJ"), track("b", "Song B", "Singer"), track("c", "Song C", "Nobody"),
          track("a", "Song A again", "DJ"), track("d", "Song D", "DJ")],
    edm: [{ uri: "spotify:track:d" }],
  });
  const lastfm = fakeLastFM({ DJ: ["house"], Singer: ["pop"], Nobody: ["polka"] });
  const parsed = sa.parseGenreMap({ genres: { EDM: ["house"], Pop: ["pop"] } });
  return { sp, lastfm, parsed };
}

// ---------------------------------------------------------------- pure helpers

test("norm", () => {
  assert.equal(sa.norm("K-Pop"), "k pop");
  assert.equal(sa.norm("Drum & Bass"), "drum and bass");
  assert.equal(sa.norm("  Hip_Hop "), "hip hop");
});

test("parseToptags filters, normalizes, dedupes and handles a single tag", () => {
  const data = { toptags: { tag: [{ name: "Hip-Hop", count: 100 }, { name: "hip hop", count: 80 }, { name: "", count: 50 }, { name: "rap", count: "40" }, { name: "seen live", count: 3 }] } };
  assert.deepEqual(sa.parseToptags(data), ["hip hop", "rap"]);
  assert.deepEqual(sa.parseToptags({ toptags: { tag: { name: "Jazz", count: 100 } } }), ["jazz"]);
  assert.deepEqual(sa.parseToptags({}), []);
});

test("songKey matches the Python version", () => {
  const cases = {
    "Song (feat. X)": "song|artist", "Song [Remastered 2009]": "song|artist", "Song - 2011 Remaster": "song|artist",
    "Song (with Halsey)": "song|artist", "Song - Radio Edit": "song|artist", "Song - Live": "song live|artist",
    "Song (Remix)": "song remix|artist", "!!!": "!!!|artist",
  };
  for (const [title, key] of Object.entries(cases)) assert.equal(sa.songKey(title, ["Artist"]), key, title);
  assert.equal(sa.songKey("Song", []), "");
});

test("newTracks: repeats, ISRC, title + length", () => {
  const s = (uri, name, extra = {}) => sa.trackSummary(track(uri, name, "Artist", extra));
  assert.deepEqual(sa.newTracks([s("a", "A"), s("b", "B"), s("a", "A")], [s("c", "C")]).map((x) => (Array.isArray(x) ? x.map((t) => t.uri) : x)),
    [["spotify:track:a", "spotify:track:b"], 0]);
  assert.equal(sa.newTracks([s("single", "Other Title", { isrc: "usx1" })], [s("album", "Song", { isrc: "USX1" })])[1], 1);
  const [fresh, same] = sa.newTracks(
    [s("remaster", "Song - 2011 Remaster", { length: 201500 }), s("live", "Song - Live"), s("intro2", "Song", { length: 95000 })],
    [s("album", "Song (feat. Someone)")]);
  assert.deepEqual(fresh.map((t) => t.uri.split(":").pop()), ["live", "intro2"]);
  assert.equal(same, 1);
});

test("date filters", () => {
  assert.equal(sa.dateBound("2019", true), "2019-12-31");
  assert.equal(sa.dateBound("2019-05", true), "2019-05-31");
  assert.throws(() => sa.dateBound("May 2019"), /isn't a date/);
  assert.throws(() => sa.parseDateFilters({ added_after: "2024-06-01", added_before: "2024-01-01" }), /ends before it starts/);
  const t = { added_at: "2024-03-15T10:00:00Z", release_date: "2019" };
  const f = sa.parseDateFilters;
  assert.ok(sa.passesDateFilters(t, {}));
  assert.ok(sa.passesDateFilters(t, f({ added_after: "2024-03-15", added_before: "2024-03" })));
  assert.ok(!sa.passesDateFilters(t, f({ added_after: "2024-03-16" })));
  assert.ok(sa.passesDateFilters(t, f({ released_after: "2019-06" })));
  assert.ok(!sa.passesDateFilters(t, f({ released_before: "2018" })));
  assert.ok(!sa.passesDateFilters({ added_at: null }, f({ added_after: "2000" })));
});

test("parseGenreMap: both formats, custom playlists, validation", () => {
  const old = sa.parseGenreMap({ playlists: { Pop: ["K-Pop", "Pop"], Jazz: "jazz" }, unsorted_playlist: "  " });
  assert.deepEqual([...old.genres.Pop.keywords].sort(), ["k pop", "pop"]);
  assert.deepEqual(old.genres.Jazz.target, { playlist_id: null, playlist: "Jazz" });
  assert.equal(old.unsorted, null);
  const custom = sa.parseGenreMap({ genres: { EDM: { keywords: ["house"], playlist: "Club", playlist_id: "abc" } }, unsorted_playlist: { playlist: "Misc" } });
  assert.deepEqual(custom.genres.EDM.target, { playlist_id: "abc", playlist: "Club" });
  assert.deepEqual(custom.unsorted, { playlist_id: null, playlist: "Misc" });
  for (const bad of [{ genres: { Pop: [] } }, { genres: {} }, { genres: ["pop"] }, []]) assert.throws(() => sa.parseGenreMap(bad));
  sa.parseGenreMap(sa.DEFAULT_GENRE_MAP);  // the built-in map is valid
});

test("parsePlaylistId", () => {
  const id = "37i9dQZF1DXcBWIGoYBM5M";
  for (const ok of [`https://open.spotify.com/playlist/${id}?si=x`, `spotify:playlist:${id}`, ` ${id} `]) assert.equal(sa.parsePlaylistId(ok), id);
  for (const bad of ["My Mix", `${id}X`, `https://open.spotify.com/album/${id}`]) assert.equal(sa.parsePlaylistId(bad), null);
});

test("pageRedirectUri drops index.html and the query", () => {
  assert.equal(sa.pageRedirectUri("https://a.github.io/repo/index.html?code=1"), "https://a.github.io/repo/");
  assert.equal(sa.pageRedirectUri("http://127.0.0.1:8888/"), "http://127.0.0.1:8888/");
});

// ---------------------------------------------------------------- plan and apply

test("buildPlan dedupes, skips existing and plans new playlists", async () => {
  const { sp, lastfm, parsed } = world();
  const plan = await sa.buildPlan(sp, lastfm, "big mix", parsed);
  const t = Object.fromEntries(plan.targets.map((x) => [x.name, x]));
  assert.equal(plan.source_name, "Big Mix");
  assert.deepEqual(ids(t.EDM), ["a"]);
  assert.deepEqual(ids(t.Pop), ["b"]);
  assert.equal(t.Pop.playlist_id, null);
  assert.deepEqual(plan.unmatched.map((u) => u.name), ["Song C"]);
  assert.deepEqual(sp.created, []);
});

test("genres can share a chosen playlist; filters limit the source", async () => {
  const { sp, lastfm } = world();
  sp.playlists.push(playlist("mix", "Everything"));
  sp.contents.mix = [];
  const parsed = sa.parseGenreMap({ genres: { EDM: { keywords: ["house"], playlist_id: "mix" }, Pop: { keywords: ["pop"], playlist: "Everything" } } });
  const plan = await sa.buildPlan(sp, lastfm, "Big Mix", parsed);
  assert.equal(plan.targets.length, 1);
  assert.deepEqual(plan.targets[0].genres, ["EDM", "Pop"]);

  sp.contents.src = [track("old", "Old", "DJ", { added: "2020-01-01T00:00:00Z" }), track("new", "New", "DJ", { added: "2025-06-01T00:00:00Z" })];
  const filtered = await sa.buildPlan(sp, lastfm, "Big Mix", parsed, { filters: sa.parseDateFilters({ added_after: "2025" }) });
  assert.deepEqual([filtered.source_total, filtered.total], [2, 1]);
});

test("buildPlan refuses playlists you don't own and unreadable sources", async () => {
  const { sp, lastfm, parsed } = world();
  sp.playlists.push(playlist("theirs", "Pop", "someone"));
  await assert.rejects(sa.buildPlan(sp, lastfm, "Big Mix", parsed), /isn't your playlist/);
  const w = world();
  w.sp.playlists.push(playlist("gone", "Gone"));
  await assert.rejects(sa.buildPlan(w.sp, w.lastfm, "Gone", w.parsed), /Couldn't read the source/);
});

test("applyPlan creates, adds and re-checks", async () => {
  const { sp, lastfm, parsed } = world();
  const plan = await sa.buildPlan(sp, lastfm, "Big Mix", parsed);
  sp.contents.edm.push({ uri: "spotify:track:a" });  // added elsewhere after the preview
  const log = [];
  await sa.applyPlan(sp, plan, (m) => log.push(m));
  assert.deepEqual(sp.created, ["Pop"]);
  assert.deepEqual(log, ["EDM: nothing new", 'Created playlist "Pop"', "Pop: added 1 songs"]);
});

// ---------------------------------------------------------------- clean up

function messy() {
  const sp = new FakeSpotify([playlist("edm", "EDM"), playlist("theirs", "Theirs", "someone")], {
    edm: [track("dj", "Banger", "DJ"), track("pop", "Ballad", "Singer"), track("pop", "Ballad", "Singer"),
          track("mystery", "Demo", "Nobody"), track("collab", "Collab", ["Singer", "DJ"]), track("synth", "Glow", "Synth Kid")],
    theirs: [],
  });
  const lastfm = fakeLastFM({ DJ: ["house"], Singer: ["pop", "dance"], "Synth Kid": ["electronic"] });
  const parsed = sa.parseGenreMap({ genres: { EDM: { keywords: ["house"], playlist_id: "edm" }, Electronic: { keywords: ["electronic"], playlist_id: "edm" } } });
  return { sp, lastfm, parsed };
}

test("buildCleanup lists songs that don't match, one or several genres", async () => {
  const { sp, lastfm, parsed } = messy();
  const one = await sa.buildCleanup(sp, lastfm, "EDM", "EDM", parsed);
  assert.equal(one.total, 5);
  assert.deepEqual(one.off.map((t) => t.name).sort(), ["Ballad", "Demo", "Glow"]);
  assert.ok(one.off.find((t) => t.name === "Demo").unknown);
  const both = await sa.buildCleanup(sp, lastfm, "edm", ["EDM", "Electronic"], parsed);
  assert.deepEqual(both.off.map((t) => t.name).sort(), ["Ballad", "Demo"]);
  await assert.rejects(sa.buildCleanup(sp, lastfm, "Theirs", "EDM", parsed), /only clean up playlists you own/);
  await assert.rejects(sa.buildCleanup(sp, lastfm, "EDM", "Jazz", parsed), /no genre called/);
});

test("applyCleanup removes only listed songs", async () => {
  const { sp, lastfm, parsed } = messy();
  const c = await sa.buildCleanup(sp, lastfm, "EDM", "EDM", parsed);
  const n = await sa.applyCleanup(sp, c, ["spotify:track:pop", "spotify:track:dj", "spotify:track:pop"]);
  assert.equal(n, 1);
  assert.deepEqual(sp.removed, [["edm", ["spotify:track:pop"]]]);
});

test("removeTracks falls back to the older request body", async () => {
  const calls = [];
  const sp = new sa.Spotify(null);
  sp.call = async (method, url, { json }) => {
    calls.push(Object.keys(json)[0]);
    if (json.items) throw new sa.SpotifyError(400, method, url, "bad request");
    return {};
  };
  await sp.removeTracks("p", Array.from({ length: 150 }, (_, i) => `spotify:track:${i}`));
  assert.deepEqual(calls, ["items", "tracks", "items", "tracks"]);
});

// ---------------------------------------------------------------- the in-browser backend

const settle = () => new Promise((r) => setTimeout(r, 0));

test("settings: validation and readiness", () => {
  memory.clear();
  const b = new sa.Backend();
  assert.equal(b.ready(), false);
  assert.equal(b.settings().redirectUri, "https://someone.github.io/spotify-library-assistant/");
  assert.throws(() => b.saveSettings({ clientId: "nope", lastfmKey: "" }), /Client ID/);
  b.saveSettings({ clientId: "a".repeat(32), lastfmKey: "b".repeat(32) });
  assert.equal(b.ready(), true);
  assert.equal(new sa.Backend().ready(), true);  // remembered
});

test("genre map is stored and validated", async () => {
  memory.clear();
  const b = new sa.Backend();
  assert.deepEqual(await b.request("GET", "/api/genre-map"), sa.DEFAULT_GENRE_MAP);
  await assert.rejects(b.request("PUT", "/api/genre-map", { genres: { Pop: [] } }));
  await b.request("PUT", "/api/genre-map", { genres: { Rock: ["rock"] } });
  assert.deepEqual(new sa.Backend().genreConfig(), { genres: { Rock: ["rock"] } });
});

test("apply only once, and only the latest preview", async () => {
  const b = new sa.Backend();
  const applied = [];
  b.sp = { addTracks() {}, createPlaylist() {}, async *playlistTracks() {} };
  b.plan = { id: "p1", applied: false, targets: [{ name: "X", playlist_id: "x", tracks: [{ uri: "u", key: "" }] }] };
  b.sp.addTracks = async (id, uris) => applied.push([id, uris]);
  assert.throws(() => b.startApply("old"), /out of date/);
  b.startApply("p1");
  await settle(); await settle();
  assert.throws(() => b.startApply("p1"), /already applied/);
  assert.deepEqual(applied, [["x", ["u"]]]);
});

test("remove guards and repeat batches", async () => {
  const b = new sa.Backend();
  const removed = [];
  b.sp = { removeTracks: async (id, uris) => removed.push(uris) };
  b.cleanup = { id: "c1", removed: [], playlist_id: "p", playlist_name: "P", off: [{ uri: "a" }, { uri: "b" }] };
  assert.throws(() => b.startRemove("c1", []), /Tick at least one/);
  assert.throws(() => b.startRemove("old", ["a"]), /out of date/);
  b.startRemove("c1", ["a"]);
  assert.throws(() => b.startRemove("c1", ["b"]), /still working/);
  await settle(); await settle();
  assert.throws(() => b.startRemove("c1", ["a"]), /already removed/);
  b.startRemove("c1", ["a", "b"]);
  await settle(); await settle();
  assert.deepEqual(removed, [["a"], ["b"]]);
});
