// Spotassist in the browser: the same sorting and clean-up logic as sort_by_genre.py,
// talking to Spotify and Last.fm directly. Nothing goes through a server; tokens,
// settings, the genre map and the Last.fm cache live in this browser's localStorage.

import { DEFAULTS } from "./config.js";

export const APP_NAME = "Spotassist";

const SPOTIFY_API = "https://api.spotify.com/v1";
const SPOTIFY_AUTH = "https://accounts.spotify.com/authorize";
const SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token";
const LASTFM_API = "https://ws.audioscrobbler.com/2.0/";
const SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public";

export const MIN_TAG_WEIGHT = 10;   // Last.fm tag weights run 0-100; ignore weak, noisy tags
export const MAX_TAGS = 8;          // only consider an artist's top N tags
const MAX_RETRY_WAIT = 300;         // give up rather than wait longer than this on a 429
export const SAME_SONG_MAX_LENGTH_DIFF_MS = 4000;
export const UNMATCHED_LABEL = "Unmatched songs";

export const DEFAULT_GENRE_MAP = {
  genres: {
    "EDM": ["edm", "electronic dance music", "house", "progressive house", "electro house", "big room", "dubstep", "trance", "future bass", "drum and bass"],
    "Pop": ["pop", "dance pop", "electropop", "synthpop", "teen pop", "k pop"],
    "Soft Pop": ["soft pop", "acoustic", "singer songwriter", "soft rock", "ballad", "chill"],
    "Art Pop": ["art pop", "indie pop", "dream pop", "experimental pop", "alt pop"],
    "Electronic": ["electronic", "electronica", "synthwave", "idm", "ambient", "downtempo"],
    "Alternative Metal": ["alternative metal", "nu metal", "metalcore", "metal", "hard rock", "post hardcore"],
    "Emo": ["emo", "pop punk", "emo rap", "midwest emo"],
  },
  unsorted_playlist: null,
};

export class SpotassistError extends Error {}
export class NotLoggedIn extends SpotassistError {}
export class SpotifyError extends SpotassistError {
  constructor(status, method, url, text) {
    super(`Spotify error ${status} on ${method} ${url}:\n${String(text || "").slice(0, 400)}`);
    this.status = status;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- storage (fails soft: private windows etc.)

export const store = {
  get(key, fallback) {
    try {
      const value = localStorage.getItem("spotassist." + key);
      return value == null ? fallback : JSON.parse(value);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem("spotassist." + key, JSON.stringify(value)); } catch { /* storage full or blocked */ }
  },
  remove(key) {
    try { localStorage.removeItem("spotassist." + key); } catch { /* blocked */ }
  },
};

// ---------------------------------------------------------------- pure helpers

export function norm(tag) {
  return String(tag).toLowerCase().replace(/&/g, " and ").replace(/[-_]/g, " ").replace(/\s+/g, " ").trim();
}

export function retryAfterSeconds(value, fallback = 2) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? Math.max(n, 0) : fallback;
}

export function parseToptags(data, minWeight = MIN_TAG_WEIGHT, maxTags = MAX_TAGS) {
  let raw = ((data || {}).toptags || {}).tag || [];
  if (!Array.isArray(raw)) raw = [raw];  // a single tag comes back as an object, not a list
  const found = [];
  for (const t of raw) {
    let weight = parseInt(t.count ?? 0, 10);
    if (Number.isNaN(weight)) weight = 0;
    const name = norm(t.name ?? "");
    if (weight >= minWeight && name && !found.includes(name)) found.push(name);
    if (found.length >= maxTags) break;
  }
  return found;
}

// Title decorations that don't make it a different song. Live, remix, acoustic etc. are kept.
const TITLE_NOISE = String.raw`featuring|feat\.?|ft\.?|with|remaster(?:ed)?|\d{4} remaster(?:ed)?|single version|album version|radio edit|explicit|clean|mono|stereo|original mix`;
const NOISE_BRACKETS = new RegExp(String.raw`\s*[(\[]\s*(?:${TITLE_NOISE})\b[^)\]]*[)\]]`, "g");
const NOISE_SUFFIX = new RegExp(String.raw`\s+-\s+(?:${TITLE_NOISE})\b.*$`);

/** A loose identity for a song: cleaned-up title plus main artist. "" if there's no artist. */
export function songKey(name, artists) {
  if (!artists || !artists.length) return "";
  let title = (name || "").toLowerCase();
  title = title.replace(NOISE_BRACKETS, "").replace(NOISE_SUFFIX, "");
  const cleaned = title.replace(/[^\p{L}\p{N}_\s]/gu, "").replace(/\s+/g, " ").trim();
  return `${cleaned || title.trim()}|${artists[0].trim().toLowerCase()}`;
}

export function trackSummary(track) {
  const artists = (track.artists || []).filter((a) => a && a.name).map((a) => a.name);
  const name = track.name || "(untitled)";
  return {
    uri: track.uri,
    name,
    artists,
    isrc: ((track.external_ids || {}).isrc || "").toUpperCase() || null,
    duration_ms: track.duration_ms ?? null,
    key: songKey(name, artists),
    added_at: track.added_at ?? null,                          // "2024-05-01T12:00:00Z"
    release_date: (track.album || {}).release_date ?? null,    // "2019", "2019-05" or "2019-05-03"
  };
}

/**
 * Tracks (summaries) not already in `existing` or earlier in `tracks`.
 * Exact repeats (same Spotify URI) are dropped quietly. The same song under a different URI
 * (a single vs. the album version, a remaster) is recognised by ISRC, or by title and main
 * artist with nearly the same length, and counted. Returns [new tracks, same-song count].
 */
export function newTracks(tracks, existing = []) {
  const uris = new Set(), isrcs = new Set(), lengthsByKey = new Map();
  const remember = (t) => {
    uris.add(t.uri);
    if (t.isrc) isrcs.add(t.isrc);
    if (t.key) {
      if (!lengthsByKey.has(t.key)) lengthsByKey.set(t.key, []);
      lengthsByKey.get(t.key).push(t.duration_ms ?? null);
    }
  };
  const sameSongSeen = (t) => {
    if (t.isrc && isrcs.has(t.isrc)) return true;
    for (const length of (t.key && lengthsByKey.get(t.key)) || []) {
      if (length == null || t.duration_ms == null) return true;
      if (Math.abs(length - t.duration_ms) <= SAME_SONG_MAX_LENGTH_DIFF_MS) return true;
    }
    return false;
  };
  existing.forEach(remember);
  const out = [];
  let sameSong = 0;
  for (const t of tracks) {
    if (uris.has(t.uri)) continue;
    if (sameSongSeen(t)) {
      uris.add(t.uri);  // so a later repeat of this exact track is dropped too
      sameSong++;
      continue;
    }
    remember(t);
    out.push(t);
  }
  return [out, sameSong];
}

// ---------------------------------------------------------------- date filters

export const DATE_FILTERS = ["added_after", "added_before", "released_after", "released_before"];

/** "2019", "2019-05" or "2019-05-03" as a full YYYY-MM-DD bound ("" if empty).
 *  end=true gives the last day of a year or month, so "before 2019" includes all of 2019. */
export function dateBound(value, end = false) {
  value = String(value || "").trim();
  if (!value) return "";
  if (!/^\d{4}(-\d{2}(-\d{2})?)?$/.test(value)) {
    throw new SpotassistError(`"${value}" isn't a date. Use YYYY, YYYY-MM or YYYY-MM-DD.`);
  }
  if (value.length === 4) return value + (end ? "-12-31" : "-01-01");
  if (value.length === 7) return value + (end ? "-31" : "-01");  // only compared as text, so -31 works for any month
  return value;
}

export function parseDateFilters(raw) {
  const out = {};
  for (const name of DATE_FILTERS) {
    const bound = dateBound((raw || {})[name], name.endsWith("_before"));
    if (bound) out[name] = bound;
  }
  for (const kind of ["added", "released"]) {
    const after = out[`${kind}_after`], before = out[`${kind}_before`];
    if (after && before && after > before) throw new SpotassistError(`The ${kind} date range ends before it starts.`);
  }
  return out;
}

/** True if the track's dates are inside every range in `filters` (inclusive).
 *  A song with no known date is left out when that date is being filtered. */
export function passesDateFilters(track, filters) {
  if (!filters || !Object.keys(filters).length) return true;
  const added = (track.added_at || "").slice(0, 10);
  if (filters.added_after && !(added && added >= filters.added_after)) return false;
  if (filters.added_before && !(added && added <= filters.added_before)) return false;
  const released = track.release_date || "";
  if (filters.released_after || filters.released_before) {
    if (!released) return false;
    // a partial date like "2019" counts if any part of that year is inside the range
    if (filters.released_after && dateBound(released, true) < filters.released_after) return false;
    if (filters.released_before && dateBound(released) > filters.released_before) return false;
  }
  return true;
}

// ---------------------------------------------------------------- genre map

/** A destination playlist: a name string, or {playlist: name, playlist_id: id}. */
export function parseTarget(value, defaultName = "") {
  let name, playlistId;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    name = String(value.playlist || "").trim();
    playlistId = String(value.playlist_id || "").trim() || null;
  } else {
    name = String(value || "").trim();
    playlistId = null;
  }
  return { playlist_id: playlistId, playlist: name || defaultName };
}

/**
 * Validate a genre map. Returns {genres: {genre: {keywords: Set, target}}, unsorted: target|null}.
 * Each genre is a keyword list (songs go to a playlist named after the genre) or
 * {keywords: [...], playlist: "Name", playlist_id: "optional Spotify ID"}.
 * The older top-level key "playlists" is read the same way as "genres".
 */
export function parseGenreMap(config, source = "genre map") {
  if (!config || typeof config !== "object" || Array.isArray(config)) throw new SpotassistError(`${source} must be a JSON object.`);
  const raw = "genres" in config ? config.genres : config.playlists;
  if (raw != null && (typeof raw !== "object" || Array.isArray(raw))) throw new SpotassistError(`${source}: "genres" must be an object.`);
  const genres = {};
  for (let [genre, value] of Object.entries(raw || {})) {
    genre = genre.trim();
    let keywords, target;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      keywords = value.keywords;
      target = parseTarget(value, genre);
    } else {
      keywords = value;
      target = parseTarget(genre);
    }
    if (typeof keywords === "string") keywords = [keywords];
    const kws = new Set((Array.isArray(keywords) ? keywords : []).map(norm).filter(Boolean));
    if (!genre || !kws.size) throw new SpotassistError(`${source}: genre "${genre}" needs a name and at least one keyword.`);
    if (!target.playlist && !target.playlist_id) throw new SpotassistError(`${source}: genre "${genre}" needs a playlist to go to.`);
    genres[genre] = { keywords: kws, target };
  }
  if (!Object.keys(genres).length) throw new SpotassistError(`${source} has no genres defined.`);
  let unsorted = parseTarget(config.unsorted_playlist);
  if (!unsorted.playlist && !unsorted.playlist_id) unsorted = null;
  return { genres, unsorted };
}

// ---------------------------------------------------------------- playlists & matching

/** Extract a playlist ID from a link, a spotify: URI, or a bare 22-character ID. */
export function parsePlaylistId(text) {
  text = String(text).trim();
  const m = text.match(/playlist[/:]([A-Za-z0-9]{22})(?![A-Za-z0-9])/);
  if (m) return m[1];
  return /^[A-Za-z0-9]{22}$/.test(text) ? text : null;
}

export function canEdit(playlist, myId) {
  return ((playlist.owner || {}).id === myId) || Boolean(playlist.collaborative);
}

/** Map lowercase name -> playlist. Playlists you can edit win over ones you only follow. */
export function indexByName(playlists, myId) {
  const byName = new Map();
  for (const p of playlists) {
    const key = (p.name || "").trim().toLowerCase();
    if (!key) continue;
    const current = byName.get(key);
    if (!current || (canEdit(p, myId) && !canEdit(current, myId))) byName.set(key, p);
  }
  return byName;
}

/** Return [matched genre names, tags seen]. Main artist first; featured artists are a fallback. */
export async function matchGenres(artists, keywordMap, tagLookup) {
  let hits = new Set();
  const seen = [];
  for (const artist of artists) {
    const tags = [...new Set(await tagLookup(artist))];
    for (const t of tags) if (!seen.includes(t)) seen.push(t);
    hits = new Set(Object.entries(keywordMap).filter(([, kws]) => tags.some((t) => kws.has(t))).map(([name]) => name));
    if (hits.size) break;
  }
  return [hits, seen];
}

/** Return [playlist id, name or null]. An exact name in your library wins over ID parsing. */
export function resolveSource(source, byName) {
  source = String(source).trim();
  const match = byName.get(source.toLowerCase()) || [...byName.values()].find((p) => p.id === source);
  if (match) return [match.id, match.name ?? null];
  const id = parsePlaylistId(source);
  if (!id) throw new SpotassistError(`No playlist named "${source}" in your library.`);
  const named = [...byName.values()].find((p) => p.id === id);
  return [id, named ? named.name : null];
}

async function collect(iterable) {
  const out = [];
  for await (const x of iterable) out.push(x);
  return out;
}

// ---------------------------------------------------------------- plan and apply

/**
 * Work out which songs would go where, without changing anything.
 * `parsed` comes from parseGenreMap(). progress(stage, done, total) is called as work proceeds.
 */
export async function buildPlan(sp, lastfm, source, parsed, { progress = () => {}, filters = {} } = {}) {
  const { genres, unsorted } = parsed;
  progress("Reading your playlists", 0, 0);
  const me = await sp.me();
  const myId = me.id;
  const mine = await sp.myPlaylists();
  const byName = indexByName(mine, myId);
  const byId = new Map(mine.filter((p) => p.id).map((p) => [p.id, p]));
  const [sourceId, sourceName] = resolveSource(source, byName);

  // find (or plan to create) each destination playlist; genres can share one
  const targets = new Map();  // key -> {name, playlist, genres}
  const addTarget = (label, target) => {
    const p = target.playlist_id ? byId.get(target.playlist_id) : byName.get(target.playlist.toLowerCase());
    if (target.playlist_id && !p) {
      throw new SpotassistError(`The playlist chosen for "${label}" isn't in your library anymore. Pick another one in the genre map.`);
    }
    if (p && !canEdit(p, myId)) {
      throw new SpotassistError(`"${p.name}" (for "${label}") isn't your playlist, so songs can't be added to it. Pick a different playlist in the genre map.`);
    }
    const key = p ? p.id : "new:" + target.playlist.toLowerCase();
    if (!targets.has(key)) targets.set(key, { name: p ? p.name : target.playlist, playlist: p || null, genres: [] });
    targets.get(key).genres.push(label);
    return key;
  };
  const genreTarget = {};
  for (const [genre, spec] of Object.entries(genres)) genreTarget[genre] = addTarget(genre, spec.target);
  const unsortedKey = unsorted ? addTarget(UNMATCHED_LABEL, unsorted) : null;
  const keywords = Object.fromEntries(Object.entries(genres).map(([g, spec]) => [g, spec.keywords]));

  progress("Reading the source playlist", 0, 0);
  let allTracks;
  try {
    allTracks = (await collect(sp.playlistTracks(sourceId))).map(trackSummary);
  } catch (e) {
    if (e instanceof SpotifyError && (e.status === 403 || e.status === 404)) {
      throw new SpotassistError("Couldn't read the source playlist. It may not exist, or Spotify may not let this app read it (playlists you don't own or collaborate on are often blocked). Copy its songs into a playlist of your own and use that instead.");
    }
    throw e;
  }
  const tracks = allTracks.filter((t) => passesDateFilters(t, filters));

  const assigned = new Map([...targets.keys()].map((k) => [k, []]));
  const unmatched = [];
  try {
    for (let i = 0; i < tracks.length; i++) {
      const track = tracks[i];
      const [hits, seenTags] = await matchGenres(track.artists, keywords, (a) => lastfm.tags(a));
      for (const genre of hits) assigned.get(genreTarget[genre]).push(track);
      if (!hits.size) {
        unmatched.push({ ...track, tags: seenTags });
        if (unsortedKey) assigned.get(unsortedKey).push(track);
      }
      progress("Looking up genres on Last.fm", i + 1, tracks.length);
      if ((i + 1) % 25 === 0) lastfm.save();
    }
  } finally {
    lastfm.save();  // keep lookups even if something fails
  }

  // skip songs already in each destination, including the same song under another ID
  const results = [];
  let n = 0;
  for (const [key, target] of targets) {
    progress("Checking your genre playlists", ++n, targets.size);
    const existing = target.playlist ? (await collect(sp.playlistTracks(target.playlist.id))).map(trackSummary) : [];
    const [fresh, sameSong] = newTracks(assigned.get(key), existing);
    results.push({
      name: target.name,
      playlist_id: target.playlist ? target.playlist.id : null,
      genres: target.genres,
      matched: new Set(assigned.get(key).map((t) => t.uri)).size,
      tracks: fresh,
      same_song_skipped: sameSong,
    });
  }

  return {
    user: me.display_name || myId,
    source_id: sourceId,
    source_name: sourceName,
    source_total: allTracks.length,
    total: tracks.length,
    filters: filters || {},
    targets: results,
    unmatched,
  };
}

/** Create missing playlists and add the planned songs. Re-checks each target first,
 *  so songs added since the plan was built aren't added twice. */
export async function applyPlan(sp, plan, report = () => {}) {
  for (const target of plan.targets) {
    let playlistId = target.playlist_id;
    const existing = playlistId ? (await collect(sp.playlistTracks(playlistId))).map(trackSummary) : [];
    const uris = newTracks(target.tracks, existing)[0].map((t) => t.uri);
    if (!uris.length) {
      report(`${target.name}: nothing new`);
      continue;
    }
    if (!playlistId) {
      playlistId = target.playlist_id = (await sp.createPlaylist(target.name)).id;
      report(`Created playlist "${target.name}"`);
    }
    await sp.addTracks(playlistId, uris);
    report(`${target.name}: added ${uris.length} songs`);
  }
}

// ---------------------------------------------------------------- clean up

/**
 * Find songs in one of your playlists whose artists match none of the given genres' keywords.
 * Changes nothing. Songs whose artists have no Last.fm tags at all are marked "unknown".
 */
export async function buildCleanup(sp, lastfm, playlist, genreNames, parsed, { progress = () => {} } = {}) {
  const names = typeof genreNames === "string" ? [genreNames] : [...new Set(genreNames || [])];
  if (!names.length) throw new SpotassistError("Pick the genre the playlist should match.");
  const missing = names.find((g) => !(g in parsed.genres));
  if (missing) throw new SpotassistError(`There's no genre called "${missing}" in the genre map.`);
  const keywords = Object.fromEntries(names.map((g) => [g, parsed.genres[g].keywords]));

  progress("Reading your playlists", 0, 0);
  const myId = (await sp.me()).id;
  const mine = await sp.myPlaylists();
  const [playlistId] = resolveSource(playlist, indexByName(mine, myId));
  const p = mine.find((x) => x.id === playlistId);
  if (!p || !canEdit(p, myId)) throw new SpotassistError("You can only clean up playlists you own or collaborate on.");

  progress("Reading the playlist", 0, 0);
  const tracks = [];
  const seen = new Set();
  for await (const t of sp.playlistTracks(playlistId)) {
    if (!seen.has(t.uri)) {  // a repeated song only needs checking once
      seen.add(t.uri);
      tracks.push(trackSummary(t));
    }
  }

  const off = [];
  try {
    for (let i = 0; i < tracks.length; i++) {
      const [hits, tags] = await matchGenres(tracks[i].artists, keywords, (a) => lastfm.tags(a));
      if (!hits.size) off.push({ ...tracks[i], tags, unknown: !tags.length });
      progress("Looking up genres on Last.fm", i + 1, tracks.length);
      if ((i + 1) % 25 === 0) lastfm.save();
    }
  } finally {
    lastfm.save();
  }

  const allKeywords = new Set();
  for (const kws of Object.values(keywords)) for (const k of kws) allKeywords.add(k);
  return {
    playlist_id: playlistId,
    playlist_name: p.name || "(untitled)",
    genres: names,
    keywords: [...allKeywords].sort(),
    total: tracks.length,
    off,
  };
}

/** Remove the chosen songs (which must come from cleanup.off) from the playlist. */
export async function applyCleanup(sp, cleanup, uris, report = () => {}) {
  const allowed = new Set(cleanup.off.map((t) => t.uri));
  const chosen = [...new Set(uris.filter((u) => allowed.has(u)))];
  if (!chosen.length) {
    report("Nothing selected to remove");
    return 0;
  }
  await sp.removeTracks(cleanup.playlist_id, chosen);
  report(`${cleanup.playlist_name}: removed ${chosen.length} songs`);
  return chosen.length;
}

// ---------------------------------------------------------------- Spotify login (PKCE, no client secret)

function base64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(bytes = 48) {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** This page's address, which is what Spotify sends the login back to. */
export function pageRedirectUri(href = location.href) {
  const url = new URL(href);
  return url.origin + url.pathname.replace(/index\.html$/, "");
}

export class SpotifyAuth {
  constructor(clientId, redirectUri) {
    this.clientId = clientId;
    this.redirectUri = redirectUri;
    const token = store.get("token", null);
    // a token from a different Spotify app (Client ID changed) is useless
    this.token = token && token.access_token && token.client_id === clientId ? token : null;
    this.refreshing = null;
  }

  async startLogin() {
    const verifier = randomString(64);
    const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
    const state = randomString(16);
    try { sessionStorage.setItem("spotassist.pkce", JSON.stringify({ state, verifier })); } catch { /* blocked */ }
    const params = new URLSearchParams({
      client_id: this.clientId,
      response_type: "code",
      redirect_uri: this.redirectUri,
      code_challenge_method: "S256",
      code_challenge: challenge,
      scope: SCOPES,
      state,
    });
    location.assign(`${SPOTIFY_AUTH}?${params}`);
  }

  /** If Spotify just sent the user back here, finish logging in. Returns an error message or null. */
  async handleRedirect() {
    const url = new URL(location.href);
    const code = url.searchParams.get("code"), error = url.searchParams.get("error"), state = url.searchParams.get("state");
    if (!code && !error) return null;
    history.replaceState(null, "", url.pathname);  // drop ?code=... from the address bar
    let pending = null;
    try {
      pending = JSON.parse(sessionStorage.getItem("spotassist.pkce") || "null");
      sessionStorage.removeItem("spotassist.pkce");
    } catch { /* blocked */ }
    if (error) return `Spotify login failed: ${error}`;
    if (!pending || pending.state !== state) return "Spotify login didn't match this browser tab. Try again.";
    const r = await fetch(SPOTIFY_TOKEN, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: this.redirectUri,
        client_id: this.clientId,
        code_verifier: pending.verifier,
      }),
    });
    if (!r.ok) return `Spotify login failed: ${r.status} ${(await r.text()).slice(0, 300)}`;
    this.save(await r.json());
    return null;
  }

  save(data) {
    if (!data.refresh_token && this.token) data.refresh_token = this.token.refresh_token;
    data.expires_at = Date.now() + (Number(data.expires_in) || 3600) * 1000 - 60_000;
    data.client_id = this.clientId;
    this.token = data;
    store.set("token", data);
  }

  async refresh() {
    try {
      const r = await fetch(SPOTIFY_TOKEN, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: this.token.refresh_token, client_id: this.clientId }),
      });
      if (!r.ok) return false;
      this.save(await r.json());
      return true;
    } catch {
      return false;
    }
  }

  loggedIn() { return this.token !== null; }

  logout() {
    this.token = null;
    store.remove("token");
  }

  invalidate() { if (this.token) this.token.expires_at = 0; }

  async accessToken() {
    if (this.token && Date.now() < this.token.expires_at) return this.token.access_token;
    if (this.token && this.token.refresh_token) {
      this.refreshing ||= this.refresh().finally(() => { this.refreshing = null; });  // one refresh at a time
      if (await this.refreshing) return this.token.access_token;
    }
    this.logout();
    throw new NotLoggedIn("Log in to Spotify first.");
  }
}

// ---------------------------------------------------------------- Spotify API

export class Spotify {
  constructor(auth) { this.auth = auth; }

  async call(method, url, { params, json } = {}) {
    if (!url.startsWith("http")) url = `${SPOTIFY_API}/${url}`;
    if (params) {
      const u = new URL(url);
      for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
      url = u.toString();
    }
    let reauthed = false;
    for (let attempt = 0; attempt < 6; attempt++) {
      const headers = { Authorization: `Bearer ${await this.auth.accessToken()}` };
      if (json !== undefined) headers["Content-Type"] = "application/json";
      let r;
      try {
        r = await fetch(url, { method, headers, body: json !== undefined ? JSON.stringify(json) : undefined });
      } catch {
        await sleep(2000 * (attempt + 1));  // network hiccup: back off and retry
        continue;
      }
      if (r.status === 401 && !reauthed) {  // token expired or revoked early
        this.auth.invalidate();
        reauthed = true;
        continue;
      }
      if (r.status === 429) {  // rate limited: wait as told
        const wait = retryAfterSeconds(r.headers.get("Retry-After"));
        if (wait > MAX_RETRY_WAIT) throw new SpotassistError(`Spotify rate-limited this app for ${Math.round(wait)} seconds. Try again later.`);
        await sleep((wait + 1) * 1000);
        continue;
      }
      if (r.status >= 500) {
        await sleep(2000 * (attempt + 1));
        continue;
      }
      const text = await r.text();
      if (!r.ok) throw new SpotifyError(r.status, method, url, text);
      return text ? JSON.parse(text) : {};
    }
    throw new SpotassistError(`Spotify kept failing on ${method} ${url}; try again later.`);
  }

  async *paged(url, params) {
    let data = await this.call("GET", url, { params });
    while (true) {
      for (const item of data.items || []) if (item) yield item;  // items can contain nulls
      if (!data.next) break;
      data = await this.call("GET", data.next);
    }
  }

  me() { return this.call("GET", "me"); }

  myPlaylists() { return collect(this.paged("me/playlists", { limit: 50 })); }

  /** Track objects. Handles the 2026 "item" key and the older "track" key. */
  async *playlistTracks(playlistId) {
    for await (const entry of this.paged(`playlists/${playlistId}/items`, { limit: 50 })) {
      const track = entry.item || entry.track;
      if (!track || (track.type || "track") !== "track") continue;  // podcast episodes, removed tracks
      const uri = track.uri || "";
      if (track.is_local || !uri.startsWith("spotify:track:")) continue;  // local files can't be added via the API
      yield { ...track, added_at: entry.added_at };
    }
  }

  createPlaylist(name) {
    return this.call("POST", "me/playlists", { json: { name, public: false, description: `Auto-sorted by genre with ${APP_NAME}` } });
  }

  async addTracks(playlistId, uris) {
    for (let i = 0; i < uris.length; i += 100) {
      await this.call("POST", `playlists/${playlistId}/items`, { json: { uris: uris.slice(i, i + 100) } });
    }
  }

  /** Remove every occurrence of these tracks from the playlist. */
  async removeTracks(playlistId, uris) {
    for (let i = 0; i < uris.length; i += 100) {
      const items = uris.slice(i, i + 100).map((uri) => ({ uri }));
      try {
        await this.call("DELETE", `playlists/${playlistId}/items`, { json: { items } });
      } catch (e) {
        if (!(e instanceof SpotifyError) || e.status !== 400) throw e;
        await this.call("DELETE", `playlists/${playlistId}/items`, { json: { tracks: items } });  // older API name
      }
    }
  }
}

// ---------------------------------------------------------------- Last.fm

export class LastFM {
  constructor(key) {
    this.key = key;
    this.cache = store.get("lastfmCache", {});
  }

  save() { store.set("lastfmCache", this.cache); }

  /** Parsed JSON, {} for "artist not found", or null on a transient failure. */
  async request(artist) {
    for (let attempt = 0; attempt < 4; attempt++) {
      const url = new URL(LASTFM_API);
      for (const [k, v] of Object.entries({ method: "artist.gettoptags", artist, autocorrect: 1, api_key: this.key, format: "json" })) {
        url.searchParams.set(k, v);
      }
      let r;
      try {
        r = await fetch(url);
      } catch {
        return null;
      }
      await sleep(250);  // stay well under Last.fm's rate limit
      let data = {};
      try { data = await r.json(); } catch { /* not JSON */ }
      const error = data.error;
      if (error === 10 || error === 26 || (r.status === 403 && error == null)) {
        throw new SpotassistError("Last.fm rejected the API key. Check it in Settings.");
      }
      if (error === 29 || r.status === 429) {  // rate limit exceeded
        await sleep(5000 * (attempt + 1));
        continue;
      }
      if (error === 6) return {};  // artist not found: a real answer, worth caching
      if (error || !r.ok) return null;
      return data;
    }
    return null;
  }

  async tags(artist) {
    const cacheKey = artist.toLowerCase();
    if (cacheKey in this.cache) return this.cache[cacheKey];
    const data = await this.request(artist);
    if (data === null) return [];  // transient failure: don't cache, try again next time
    const found = parseToptags(data);
    this.cache[cacheKey] = found;
    return found;
  }
}

// ---------------------------------------------------------------- the app's "backend", in the browser

const randomId = () => randomString(8);

/**
 * Everything the page asks for, answered locally. request(method, path, body) mirrors the
 * API the old Python web server had, so the page code stays the same.
 */
export class Backend {
  constructor() {
    this.redirectUri = pageRedirectUri();
    this.job = null;
    this.plan = null;
    this.cleanup = null;
    this.user = null;
    this.configure();
  }

  // --- settings (your own keys, or the site's defaults from config.js)

  settings() {
    const saved = store.get("settings", {});
    return {
      clientId: saved.clientId || DEFAULTS.spotifyClientId || "",
      lastfmKey: saved.lastfmKey || DEFAULTS.lastfmApiKey || "",
      savedClientId: saved.clientId || "",
      savedLastfmKey: saved.lastfmKey || "",
      hasDefaults: Boolean(DEFAULTS.spotifyClientId && DEFAULTS.lastfmApiKey),
      redirectUri: this.redirectUri,
    };
  }

  saveSettings({ clientId, lastfmKey }) {
    clientId = String(clientId || "").trim();
    lastfmKey = String(lastfmKey || "").trim();
    if (clientId && !/^[0-9a-f]{32}$/i.test(clientId)) throw new SpotassistError("That doesn't look like a Spotify Client ID (32 letters and numbers).");
    if (lastfmKey && !/^[0-9a-f]{32}$/i.test(lastfmKey)) throw new SpotassistError("That doesn't look like a Last.fm API key (32 letters and numbers).");
    store.set("settings", { clientId, lastfmKey });
    this.configure();
  }

  ready() {
    const s = this.settings();
    return Boolean(s.clientId && s.lastfmKey);
  }

  configure() {
    const s = this.settings();
    this.auth = new SpotifyAuth(s.clientId, this.redirectUri);
    this.sp = new Spotify(this.auth);
    this.lastfm = new LastFM(s.lastfmKey);
    this.user = null;
  }

  // --- login

  async finishLogin() { return this.ready() ? this.auth.handleRedirect() : null; }

  login() {
    if (!this.ready()) throw new SpotassistError("Add your Spotify Client ID and Last.fm API key first.");
    return this.auth.startLogin();
  }

  async currentUser() {
    if (!this.ready() || !this.auth.loggedIn()) return null;
    if (!this.user) {
      try {
        const me = await this.sp.me();
        this.user = { id: me.id, name: me.display_name || me.id };
      } catch (e) {
        if (e instanceof NotLoggedIn) return null;
        throw e;
      }
    }
    return this.user;
  }

  logout() {
    this.auth.logout();
    this.user = this.plan = this.cleanup = null;
  }

  // --- data

  async playlists() {
    const myId = (await this.currentUser()).id;
    const out = (await this.sp.myPlaylists()).map((p) => {
      // the Feb 2026 API calls the count "items"; older responses call it "tracks"
      const counts = p.items && !Array.isArray(p.items) ? p.items : p.tracks;
      return { id: p.id, name: p.name || "(untitled)", count: (counts || {}).total ?? null, editable: canEdit(p, myId) };
    });
    return out.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  }

  genreConfig() { return store.get("genreMap", DEFAULT_GENRE_MAP); }

  saveGenreConfig(config) {
    parseGenreMap(config);  // throws if invalid
    store.set("genreMap", config);
  }

  parsedGenreMap() { return parseGenreMap(this.genreConfig()); }

  // --- background jobs (one at a time)

  jobState() {
    const j = this.job;
    if (!j) return null;
    return { id: j.id, kind: j.kind, status: j.status, stage: j.stage, done: j.done, total: j.total, log: [...j.log], error: j.error, result: j.result };
  }

  start(kind, work) {
    if (this.job && this.job.status === "running") throw new SpotassistError("Spotassist is still working on the last request.");
    const job = { id: randomId(), kind, status: "running", stage: "Starting", done: 0, total: 0, log: [], error: null, result: null };
    this.job = job;
    const progress = (stage, done, total) => Object.assign(job, { stage, done, total });
    (async () => {
      try {
        job.result = await work(job, progress);
        job.status = "done";
      } catch (e) {
        if (!(e instanceof SpotassistError)) console.error(e);
        job.error = e instanceof SpotassistError ? e.message : `Unexpected error: ${e.message}`;
        job.status = "error";
      }
    })();
    return this.jobState();
  }

  startPreview(source, filters) {
    if (!String(source || "").trim()) throw new SpotassistError("Pick a source playlist first.");
    const parsedFilters = parseDateFilters(filters && typeof filters === "object" ? filters : {});
    const parsed = this.parsedGenreMap();
    return this.start("preview", async (job, progress) => {
      const plan = await buildPlan(this.sp, this.lastfm, source, parsed, { progress, filters: parsedFilters });
      Object.assign(plan, { id: randomId(), applied: false });
      this.plan = plan;
      return plan;
    });
  }

  startApply(planId) {
    const plan = this.plan;
    if (!plan || plan.id !== planId) throw new SpotassistError("That preview is out of date. Run the preview again.");
    if (plan.applied) throw new SpotassistError("That preview was already applied. Run the preview again to add newer songs.");
    plan.applied = true;  // set before starting, so a double click can't apply twice
    return this.start("apply", async (job) => {
      job.stage = "Adding songs";
      await applyPlan(this.sp, plan, (msg) => job.log.push(msg));
      return { plan_id: planId };
    });
  }

  startCleanup(playlist, genreNames) {
    if (!String(playlist || "").trim()) throw new SpotassistError("Pick a playlist to clean up.");
    if (!Array.isArray(genreNames) || !genreNames.every((g) => typeof g === "string")) {
      throw new SpotassistError("Pick the genre the playlist should match.");
    }
    const parsed = this.parsedGenreMap();
    return this.start("cleanup", async (job, progress) => {
      const cleanup = await buildCleanup(this.sp, this.lastfm, playlist, genreNames, parsed, { progress });
      Object.assign(cleanup, { id: randomId(), removed: [] });
      this.cleanup = cleanup;
      return cleanup;
    });
  }

  startRemove(cleanupId, uris) {
    if (!Array.isArray(uris) || !uris.length) throw new SpotassistError("Tick at least one song to remove.");
    const cleanup = this.cleanup;
    if (!cleanup || cleanup.id !== cleanupId) throw new SpotassistError("That list is out of date. Find the songs again.");
    if (this.job && this.job.status === "running") throw new SpotassistError("Spotassist is still working on the last request.");
    const already = new Set(cleanup.removed);
    const chosen = uris.map(String).filter((u) => !already.has(u));
    if (!chosen.length) throw new SpotassistError("Those songs were already removed.");
    cleanup.removed.push(...chosen);  // before starting, so a double click can't remove twice
    return this.start("remove", async (job) => {
      job.stage = "Removing songs";
      const removed = await applyCleanup(this.sp, cleanup, chosen, (msg) => job.log.push(msg));
      return { cleanup_id: cleanupId, removed, uris: chosen };
    });
  }

  // --- the request router the page uses

  async request(method, path, body = {}) {
    const route = `${method} ${path}`;
    switch (route) {
      case "GET /api/status": {
        const user = await this.currentUser();
        return { user, job: this.jobState(), plan: user ? this.plan : null, cleanup: user ? this.cleanup : null };
      }
      case "GET /api/playlists": return this.playlists();
      case "GET /api/genre-map": return this.genreConfig();
      case "GET /api/job": return this.jobState();
      case "PUT /api/genre-map": this.saveGenreConfig(body); return { ok: true };
      case "POST /api/preview": return this.startPreview(String(body.source || ""), body.filters);
      case "POST /api/apply": return this.startApply(String(body.plan_id || ""));
      case "POST /api/cleanup": return this.startCleanup(String(body.playlist || ""), body.genres);
      case "POST /api/remove": return this.startRemove(String(body.cleanup_id || ""), body.uris);
      case "POST /api/logout": this.logout(); return { ok: true };
      default: throw new SpotassistError(`Unknown request ${route}`);
    }
  }
}
