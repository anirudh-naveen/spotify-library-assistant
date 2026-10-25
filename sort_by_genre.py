#!/usr/bin/env python3
"""
Spotassist

Reads a source Spotify playlist, looks up each track's artist on Last.fm to get
genre tags, and appends the track to every genre playlist whose keywords match
(see genre_map.json). Songs already in a genre playlist are skipped, so it's
safe to re-run whenever you add new songs to the source playlist.

Usage:
    python sort_by_genre.py "My Big Playlist" --dry-run   # preview, changes nothing
    python sort_by_genre.py "My Big Playlist"             # actually add songs
    python sort_by_genre.py https://open.spotify.com/playlist/XXXX

For the browser version, open https://anirudh-naveen.github.io/spotify-library-assistant/
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

APP_NAME = "Spotassist"

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
MAP_FILE = HERE / "genre_map.json"
CACHE_FILE = HERE / "lastfm_cache.json"
TOKEN_FILE = HERE / ".spotify_token.json"
UNMATCHED_FILE = HERE / "unmatched.txt"

SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_AUTH = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)

MIN_TAG_WEIGHT = 10   # Last.fm tag weights run 0-100; ignore weak, noisy tags
MAX_TAGS = 8          # only consider an artist's top N tags
MAX_RETRY_WAIT = 300  # give up rather than sleep longer than this on a 429
LOGIN_TIMEOUT = 300


class SpotassistError(Exception):
    """A problem to show the user as a plain message, without a traceback."""


class NotLoggedIn(SpotassistError):
    pass


# ---------------------------------------------------------------- helpers

def load_env():
    """Read KEY=VALUE lines from .env into the environment (without overriding)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name):
    value = os.environ.get(name, "")
    if not value or value.startswith("paste_"):
        raise SpotassistError(f"Missing {name}. Copy .env.example to .env and fill it in.")
    return value


def redirect_uri():
    return os.environ.get("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT


def norm(tag):
    """Normalize a genre tag: lowercase, '&' to 'and', hyphens/underscores to spaces."""
    tag = str(tag).lower().replace("&", " and ").replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", tag).strip()


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def retry_after_seconds(headers, default=2.0):
    """Parse a Retry-After header (seconds, possibly fractional)."""
    try:
        return max(float(headers.get("Retry-After", default)), 0.0)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- Spotify auth (PKCE, no client secret)

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    path_expected = "/callback"
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.path_expected:
            self.send_response(404)  # e.g. /favicon.ico; keep waiting for the real callback
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Logged in. You can close this tab." if "code" in query else "Login failed. Check the terminal."
        self.wfile.write(f"<p style='font-family:sans-serif'>{msg}</p>".encode())

    def log_message(self, *args):
        pass  # keep the terminal quiet


class SpotifyAuth:
    """Holds the Spotify token. With interactive=False it never opens a browser itself;
    callers (the web UI) handle login through authorize_url() and exchange_code()."""

    def __init__(self, interactive=True):
        self.client_id = require_env("SPOTIFY_CLIENT_ID")
        self.redirect_uri = redirect_uri()
        self.interactive = interactive
        self._lock = threading.Lock()
        self.token = None
        if TOKEN_FILE.exists():
            try:
                token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                if isinstance(token, dict) and token.get("access_token"):
                    self.token = token
            except json.JSONDecodeError:
                pass

    def _save(self, data):
        if "refresh_token" not in data and self.token:
            data["refresh_token"] = self.token.get("refresh_token")
        data["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
        self.token = data
        TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
        try:
            TOKEN_FILE.chmod(0o600)
        except OSError:
            pass

    def authorize_url(self):
        """Return (url, state, verifier) for a new PKCE login."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        url = SPOTIFY_AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SCOPES,
            "state": state,
        })
        return url, state, verifier

    def exchange_code(self, code, verifier):
        r = requests.post(SPOTIFY_TOKEN, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }, timeout=30)
        if not r.ok:
            raise SpotassistError(f"Spotify login failed: {r.status_code} {r.text[:300]}")
        with self._lock:
            self._save(r.json())

    def _login(self):
        url, state, verifier = self.authorize_url()
        parsed = urllib.parse.urlparse(self.redirect_uri)
        _CallbackHandler.path_expected = parsed.path or "/"
        _CallbackHandler.result = {}
        try:
            server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), _CallbackHandler)
        except OSError as e:
            raise SpotassistError(f"Couldn't listen on {parsed.hostname}:{parsed.port} for the Spotify login "
                               f"({e}). Is something else (like a local copy of the web page) using that port?")
        server.timeout = 1

        print("Opening your browser to log in to Spotify...")
        print(f"If it doesn't open, visit this link:\n{url}\n")
        webbrowser.open(url)
        deadline = time.time() + LOGIN_TIMEOUT
        try:
            while not _CallbackHandler.result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()

        result = _CallbackHandler.result
        if not result:
            raise SpotassistError("Spotify login failed: no response within 5 minutes.")
        if "error" in result:
            raise SpotassistError(f"Spotify login failed: {result['error']}")
        if result.get("state") != state or "code" not in result:
            raise SpotassistError("Spotify login failed: the response didn't match this login attempt. Try again.")
        self.exchange_code(result["code"], verifier)

    def _refresh(self):
        try:
            r = requests.post(SPOTIFY_TOKEN, data={
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
                "client_id": self.client_id,
            }, timeout=30)
        except requests.RequestException:
            return False
        if not r.ok:
            return False
        self._save(r.json())
        return True

    def logged_in(self):
        return self.token is not None

    def logout(self):
        with self._lock:
            self.token = None
            TOKEN_FILE.unlink(missing_ok=True)

    def invalidate(self):
        """Force a refresh on the next access_token() call (e.g. after a 401)."""
        if self.token:
            self.token["expires_at"] = 0

    def access_token(self):
        with self._lock:
            if self.token and time.time() < self.token.get("expires_at", 0):
                return self.token["access_token"]
            if self.token and self.token.get("refresh_token") and self._refresh():
                return self.token["access_token"]
            if not self.interactive:
                self.token = None
                raise NotLoggedIn("Log in to Spotify first.")
            self._login()
            return self.token["access_token"]


# ---------------------------------------------------------------- Spotify API

class SpotifyError(SpotassistError):
    def __init__(self, status, method, url, text):
        super().__init__(f"Spotify error {status} on {method} {url}:\n{text[:400]}")
        self.status = status


class Spotify:
    def __init__(self, auth):
        self.auth = auth
        self.session = requests.Session()

    def call(self, method, url, **kwargs):
        if not url.startswith("http"):
            url = f"{SPOTIFY_API}/{url}"
        reauthed = False
        for attempt in range(6):
            try:
                r = self.session.request(
                    method, url,
                    headers={"Authorization": f"Bearer {self.auth.access_token()}"},
                    timeout=30, **kwargs,
                )
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))  # network hiccup: back off and retry
                continue
            if r.status_code == 401 and not reauthed:  # token expired/revoked early
                self.auth.invalidate()
                reauthed = True
                continue
            if r.status_code == 429:  # rate limited: wait as told
                wait = retry_after_seconds(r.headers)
                if wait > MAX_RETRY_WAIT:
                    raise SpotassistError(f"Spotify rate-limited this app for {int(wait)} seconds. Try again later.")
                time.sleep(wait + 1)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if not r.ok:
                raise SpotifyError(r.status_code, method, url, r.text)
            return r.json() if r.content else {}
        raise SpotassistError(f"Spotify kept failing on {method} {url}; try again later.")

    def paged(self, url, params=None):
        data = self.call("GET", url, params=params)
        while True:
            yield from (x for x in data.get("items") or [] if x)  # items can contain nulls
            if not data.get("next"):
                break
            data = self.call("GET", data["next"])

    def me(self):
        return self.call("GET", "me")

    def my_playlists(self):
        return list(self.paged("me/playlists", {"limit": 50}))

    def playlist_tracks(self, playlist_id):
        """Yield track objects. Handles the 2026 'item' key and the older 'track' key."""
        for entry in self.paged(f"playlists/{playlist_id}/items", {"limit": 50}):
            track = entry.get("item") or entry.get("track")
            if not track or track.get("type", "track") != "track":
                continue  # skip podcast episodes / removed tracks
            uri = track.get("uri") or ""
            if track.get("is_local") or not uri.startswith("spotify:track:"):
                continue  # local files can't be added via the API
            yield {**track, "added_at": entry.get("added_at")}

    def create_playlist(self, name):
        return self.call("POST", "me/playlists", json={
            "name": name,
            "public": False,
            "description": f"Auto-sorted by genre with {APP_NAME}",
        })

    def add_tracks(self, playlist_id, uris):
        for batch in chunks(uris, 100):
            self.call("POST", f"playlists/{playlist_id}/items", json={"uris": batch})

    def remove_tracks(self, playlist_id, uris):
        """Remove every occurrence of these tracks from the playlist."""
        for batch in chunks(uris, 100):
            items = [{"uri": u} for u in batch]
            try:
                self.call("DELETE", f"playlists/{playlist_id}/items", json={"items": items})
            except SpotifyError as e:
                if e.status != 400:
                    raise
                # older API versions call the list "tracks"
                self.call("DELETE", f"playlists/{playlist_id}/items", json={"tracks": items})


# ---------------------------------------------------------------- Last.fm

class LastFM:
    def __init__(self):
        self.key = require_env("LASTFM_API_KEY")
        self.cache = {}
        if CACHE_FILE.exists():
            try:
                self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}

    def save(self):
        CACHE_FILE.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")

    def _request(self, artist):
        """Return parsed JSON, or None on a transient failure. Retries Last.fm rate limits."""
        for attempt in range(4):
            try:
                r = requests.get(LASTFM_API, params={
                    "method": "artist.gettoptags",
                    "artist": artist,
                    "autocorrect": 1,
                    "api_key": self.key,
                    "format": "json",
                }, timeout=20)
            except requests.RequestException:
                return None
            time.sleep(0.25)  # stay well under Last.fm's rate limit
            try:
                data = r.json()
            except ValueError:
                data = {}
            # Last.fm reports errors in the JSON body, sometimes with a 200, sometimes not
            error = data.get("error")
            if error in (10, 26) or (r.status_code == 403 and error is None):
                raise SpotassistError("Last.fm rejected the API key. Check LASTFM_API_KEY in .env.")
            if error == 29 or r.status_code == 429:  # rate limit exceeded
                time.sleep(5 * (attempt + 1))
                continue
            if error == 6:  # artist not found: a real answer, worth caching
                return {}
            if error or not r.ok:
                return None
            return data
        return None

    def tags(self, artist):
        cache_key = artist.lower()
        if cache_key in self.cache:
            return self.cache[cache_key]
        data = self._request(artist)
        if data is None:
            return []  # transient failure: don't cache, try again next run
        found = parse_toptags(data)
        self.cache[cache_key] = found
        return found


def parse_toptags(data, min_weight=MIN_TAG_WEIGHT, max_tags=MAX_TAGS):
    """Pull normalized, de-duplicated tag names out of an artist.getTopTags response."""
    raw = (data.get("toptags") or {}).get("tag") or []
    if isinstance(raw, dict):  # a single tag comes back as an object, not a list
        raw = [raw]
    found = []
    for t in raw:
        try:
            weight = int(t.get("count", 0))
        except (TypeError, ValueError):
            weight = 0
        name = norm(t.get("name", ""))
        if weight >= min_weight and name and name not in found:
            found.append(name)
        if len(found) >= max_tags:
            break
    return found


# ---------------------------------------------------------------- genre map

def parse_target(value, default_name=""):
    """A destination playlist: a name string, or {"playlist": name, "playlist_id": id}."""
    if isinstance(value, dict):
        name = str(value.get("playlist") or "").strip()
        playlist_id = str(value.get("playlist_id") or "").strip() or None
    else:
        name, playlist_id = str(value or "").strip(), None
    return {"playlist_id": playlist_id, "playlist": name or default_name}


def parse_genre_map(config, source="genre map"):
    """Validate a genre map config dict.

    Returns ({genre: {"keywords": {...}, "target": {"playlist_id", "playlist"}}}, unsorted target or None).
    Each genre is either a keyword list (songs go to a playlist named after the genre) or
    {"keywords": [...], "playlist": "Name", "playlist_id": "optional Spotify ID"}.
    The older top-level key "playlists" is read the same way as "genres".
    """
    if not isinstance(config, dict):
        raise SpotassistError(f"{source} must be a JSON object.")
    raw = config.get("genres") if "genres" in config else config.get("playlists")
    if raw is not None and not isinstance(raw, dict):
        raise SpotassistError(f'{source}: "genres" must be an object.')
    genres = {}
    for genre, value in (raw or {}).items():
        genre = genre.strip()
        if isinstance(value, dict):
            keywords, target = value.get("keywords"), parse_target(value, genre)
        else:
            keywords, target = value, parse_target(genre)
        if isinstance(keywords, str):  # "Pop": "pop" instead of ["pop"]
            keywords = [keywords]
        kws = {norm(k) for k in keywords or []} - {""}
        if not genre or not kws:
            raise SpotassistError(f'{source}: genre "{genre}" needs a name and at least one keyword.')
        if not target["playlist"] and not target["playlist_id"]:
            raise SpotassistError(f'{source}: genre "{genre}" needs a playlist to go to.')
        genres[genre] = {"keywords": kws, "target": target}
    if not genres:
        raise SpotassistError(f"{source} has no genres defined.")
    unsorted = parse_target(config.get("unsorted_playlist"))
    if not unsorted["playlist"] and not unsorted["playlist_id"]:
        unsorted = None
    return genres, unsorted


def read_genre_config(path=MAP_FILE):
    if not path.exists():
        raise SpotassistError(f"Missing {path.name}. It should sit next to this script.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpotassistError(f"{path.name} isn't valid JSON: {e}")


def load_genre_map(path=MAP_FILE):
    return parse_genre_map(read_genre_config(path), path.name)


# ---------------------------------------------------------------- pure helpers

def parse_playlist_id(text):
    """Extract a playlist ID from a link, a spotify: URI, or a bare 22-character ID."""
    text = text.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})(?![A-Za-z0-9])", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return text
    return None


def can_edit(playlist, my_id):
    return (playlist.get("owner") or {}).get("id") == my_id or bool(playlist.get("collaborative"))


def index_by_name(playlists, my_id):
    """Map lowercase name -> playlist. Playlists you can edit win over ones you only follow."""
    by_name = {}
    for p in playlists:
        key = (p.get("name") or "").strip().lower()
        if not key:
            continue
        current = by_name.get(key)
        if current is None or (can_edit(p, my_id) and not can_edit(current, my_id)):
            by_name[key] = p
    return by_name


def match_genres(artists, genre_map, tag_lookup):
    """Return (matched playlist names, tags seen). Main artist first; featured artists are a fallback."""
    hits, seen = set(), []
    for artist in artists:
        tags = list(dict.fromkeys(tag_lookup(artist)))
        seen.extend(t for t in tags if t not in seen)
        hits = {name for name, kws in genre_map.items() if kws.intersection(tags)}
        if hits:
            break
    return hits, seen


# Title decorations that don't make it a different song. Live, remix, acoustic etc. are kept.
_TITLE_NOISE = (r"featuring|feat\.?|ft\.?|with|remaster(?:ed)?|\d{4} remaster(?:ed)?|single version|"
                r"album version|radio edit|explicit|clean|mono|stereo|original mix")
SAME_SONG_MAX_LENGTH_DIFF_MS = 4000


def song_key(name, artists):
    """A loose identity for a song: cleaned-up title plus main artist. '' if there's no artist."""
    if not artists:
        return ""
    title = (name or "").lower()
    title = re.sub(rf"\s*[(\[]\s*(?:{_TITLE_NOISE})\b[^)\]]*[)\]]", "", title)  # "(feat. X)", "[Remastered]"
    title = re.sub(rf"\s+-\s+(?:{_TITLE_NOISE})\b.*$", "", title)                # " - 2011 Remaster"
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title)).strip()
    return f"{cleaned or title.strip()}|{artists[0].strip().lower()}"


def track_summary(track):
    artists = [a["name"] for a in track.get("artists") or [] if a and a.get("name")]
    name = track.get("name") or "(untitled)"
    return {
        "uri": track["uri"],
        "name": name,
        "artists": artists,
        "isrc": ((track.get("external_ids") or {}).get("isrc") or "").upper() or None,
        "duration_ms": track.get("duration_ms"),
        "key": song_key(name, artists),
        "added_at": track.get("added_at"),                                  # "2024-05-01T12:00:00Z"
        "release_date": (track.get("album") or {}).get("release_date"),    # "2019", "2019-05" or "2019-05-03"
    }


# ---------------------------------------------------------------- date filters

DATE_FILTERS = ("added_after", "added_before", "released_after", "released_before")


def date_bound(value, end=False):
    """Turn "2019", "2019-05" or "2019-05-03" into a full YYYY-MM-DD bound ('' if empty).
    end=True gives the last day of a year or month, so "before 2019" includes all of 2019."""
    value = (value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", value):
        raise SpotassistError(f'"{value}" isn\'t a date. Use YYYY, YYYY-MM or YYYY-MM-DD.')
    if len(value) == 4:
        return value + ("-12-31" if end else "-01-01")
    if len(value) == 7:
        return value + ("-31" if end else "-01")  # only compared as text, so -31 works for any month
    return value


def parse_date_filters(raw):
    """Normalize {"added_after": "2024", ...} into full-date bounds, dropping empty ones."""
    out = {}
    for name in DATE_FILTERS:
        bound = date_bound((raw or {}).get(name), end=name.endswith("_before"))
        if bound:
            out[name] = bound
    for kind in ("added", "released"):
        after, before = out.get(f"{kind}_after"), out.get(f"{kind}_before")
        if after and before and after > before:
            raise SpotassistError(f"The {kind} date range ends before it starts.")
    return out


def passes_date_filters(track, filters):
    """True if the track's dates are inside every range in `filters` (inclusive).
    A song with no known date is left out when that date is being filtered."""
    if not filters:
        return True
    added = (track.get("added_at") or "")[:10]
    if filters.get("added_after") and not (added and added >= filters["added_after"]):
        return False
    if filters.get("added_before") and not (added and added <= filters["added_before"]):
        return False
    released = track.get("release_date") or ""
    if filters.get("released_after") or filters.get("released_before"):
        if not released:
            return False
        # a partial date like "2019" counts if any part of that year is inside the range
        if filters.get("released_after") and date_bound(released, end=True) < filters["released_after"]:
            return False
        if filters.get("released_before") and date_bound(released) > filters["released_before"]:
            return False
    return True


def new_tracks(tracks, existing=()):
    """Tracks (summaries) not already in `existing` or earlier in `tracks`.

    Exact repeats (same Spotify URI) are dropped quietly. The same song under a different
    URI (a single vs. the album version, a remaster) is recognised by ISRC, or by title and
    main artist with nearly the same length, and counted. Returns (new tracks, same-song count).
    """
    uris, isrcs, lengths_by_key = set(), set(), {}

    def remember(t):
        uris.add(t["uri"])
        if t.get("isrc"):
            isrcs.add(t["isrc"])
        if t.get("key"):
            lengths_by_key.setdefault(t["key"], []).append(t.get("duration_ms"))

    def same_song_seen(t):
        if t.get("isrc") and t["isrc"] in isrcs:
            return True
        for length in lengths_by_key.get(t.get("key") or None, []):
            if length is None or t.get("duration_ms") is None:
                return True
            if abs(length - t["duration_ms"]) <= SAME_SONG_MAX_LENGTH_DIFF_MS:
                return True
        return False

    for t in existing:
        remember(t)
    out, same_song = [], 0
    for t in tracks:
        if t["uri"] in uris:
            continue
        if same_song_seen(t):
            uris.add(t["uri"])  # so a later repeat of this exact track is dropped too
            same_song += 1
            continue
        remember(t)
        out.append(t)
    return out, same_song


# ---------------------------------------------------------------- plan and apply

def resolve_source(source, by_name):
    """Return (playlist id, name or None). An exact name in your library wins over ID parsing."""
    source = source.strip()
    match = by_name.get(source.lower()) or next((p for p in by_name.values() if p.get("id") == source), None)
    if match:
        return match["id"], match.get("name")
    source_id = parse_playlist_id(source)
    if source_id is None:
        raise SpotassistError(f'No playlist named "{source}" in your library.')
    name = next((p.get("name") for p in by_name.values() if p.get("id") == source_id), None)
    return source_id, name


UNMATCHED_LABEL = "Unmatched songs"


def build_plan(sp, lastfm, source, genres, unsorted=None, progress=None, filters=None):
    """Work out which songs would go where, without changing anything.

    `genres` and `unsorted` come from parse_genre_map(). Several genres may share a playlist.
    `filters` (from parse_date_filters) limits which source songs are considered.
    progress(stage, done, total) is called as work proceeds, if given.
    The returned dict is JSON-serializable.
    """
    progress = progress or (lambda stage, done, total: None)
    progress("Reading your playlists", 0, 0)
    me = sp.me()
    my_id = me["id"]
    mine = sp.my_playlists()
    by_name = index_by_name(mine, my_id)
    by_id = {p["id"]: p for p in mine if p.get("id")}
    source_id, source_name = resolve_source(source, by_name)

    # --- find (or plan to create) each destination playlist; genres can share one
    targets = {}  # key -> {"name", "playlist", "genres"}

    def add_target(label, target):
        p = by_id.get(target["playlist_id"]) if target["playlist_id"] else by_name.get(target["playlist"].lower())
        if target["playlist_id"] and not p:
            raise SpotassistError(f'The playlist chosen for "{label}" isn\'t in your library anymore. '
                               "Pick another one in the genre map.")
        if p and not can_edit(p, my_id):
            raise SpotassistError(f'"{p.get("name")}" (for "{label}") isn\'t your playlist, so songs can\'t be '
                               "added to it. Pick a different playlist in the genre map.")
        key = p["id"] if p else "new:" + target["playlist"].lower()
        entry = targets.setdefault(key, {"name": p.get("name") if p else target["playlist"],
                                         "playlist": p, "genres": []})
        entry["genres"].append(label)
        return key

    genre_target = {genre: add_target(genre, spec["target"]) for genre, spec in genres.items()}
    unsorted_key = add_target(UNMATCHED_LABEL, unsorted) if unsorted else None
    keywords = {genre: spec["keywords"] for genre, spec in genres.items()}

    # --- read source and work out where each track goes
    progress("Reading the source playlist", 0, 0)
    try:
        all_tracks = [track_summary(t) for t in sp.playlist_tracks(source_id)]
    except SpotifyError as e:
        if e.status in (403, 404):
            raise SpotassistError(
                "Couldn't read the source playlist. It may not exist, or Spotify may not let "
                "this app read it (playlists you don't own or collaborate on are often blocked). "
                "Copy its songs into a playlist of your own and use that instead.")
        raise
    tracks = [t for t in all_tracks if passes_date_filters(t, filters)]

    assigned = {key: [] for key in targets}
    unmatched = []
    try:
        for i, track in enumerate(tracks, 1):
            hits, seen_tags = match_genres(track["artists"], keywords, lastfm.tags)
            for genre in hits:
                assigned[genre_target[genre]].append(track)
            if not hits:
                unmatched.append({**track, "tags": seen_tags})
                if unsorted_key:
                    assigned[unsorted_key].append(track)
            progress("Looking up genres on Last.fm", i, len(tracks))
            if i % 25 == 0:
                lastfm.save()
    finally:
        lastfm.save()  # keep lookups even if interrupted

    # --- skip songs already in each destination, including the same song under another ID
    results = []
    for n, (key, target) in enumerate(targets.items(), 1):
        progress("Checking your genre playlists", n, len(targets))
        playlist = target["playlist"]
        existing = [track_summary(t) for t in sp.playlist_tracks(playlist["id"])] if playlist else []
        fresh, same_song = new_tracks(assigned[key], existing)
        results.append({
            "name": target["name"],
            "playlist_id": playlist["id"] if playlist else None,
            "genres": target["genres"],
            "matched": len({t["uri"] for t in assigned[key]}),
            "tracks": fresh,
            "same_song_skipped": same_song,
        })

    return {
        "user": me.get("display_name") or my_id,
        "source_id": source_id,
        "source_name": source_name,
        "source_total": len(all_tracks),
        "total": len(tracks),
        "filters": filters or {},
        "targets": results,
        "unmatched": unmatched,
    }


def apply_plan(sp, plan, report=print):
    """Create missing playlists and add the planned songs. Re-checks each target first,
    so songs added since the plan was built aren't added twice."""
    for target in plan["targets"]:
        name, playlist_id = target["name"], target["playlist_id"]
        existing = [track_summary(t) for t in sp.playlist_tracks(playlist_id)] if playlist_id else []
        uris = [t["uri"] for t in new_tracks(target["tracks"], existing)[0]]
        if not uris:
            report(f"{name}: nothing new")
            continue
        if not playlist_id:
            playlist_id = target["playlist_id"] = sp.create_playlist(name)["id"]
            report(f'Created playlist "{name}"')
        sp.add_tracks(playlist_id, uris)
        report(f"{name}: added {len(uris)} songs")


# ---------------------------------------------------------------- clean up

def build_cleanup(sp, lastfm, playlist, genre_names, genres, progress=None):
    """Find songs in one of your playlists whose artists match none of the given genres' keywords.

    `genre_names` is one genre name or a list of them (e.g. every genre that feeds this playlist).
    Changes nothing. Songs whose artists have no Last.fm tags at all are marked "unknown",
    since there's no evidence either way. The returned dict is JSON-serializable.
    """
    progress = progress or (lambda stage, done, total: None)
    names = [genre_names] if isinstance(genre_names, str) else list(dict.fromkeys(genre_names or []))
    if not names:
        raise SpotassistError("Pick the genre the playlist should match.")
    missing = [g for g in names if g not in genres]
    if missing:
        raise SpotassistError(f'There\'s no genre called "{missing[0]}" in the genre map.')
    keywords = {g: genres[g]["keywords"] for g in names}

    progress("Reading your playlists", 0, 0)
    my_id = sp.me()["id"]
    mine = sp.my_playlists()
    playlist_id, _ = resolve_source(playlist, index_by_name(mine, my_id))
    p = next((x for x in mine if x.get("id") == playlist_id), None)
    if not p or not can_edit(p, my_id):
        raise SpotassistError("You can only clean up playlists you own or collaborate on.")

    progress("Reading the playlist", 0, 0)
    tracks, seen = [], set()
    for t in sp.playlist_tracks(playlist_id):
        if t["uri"] not in seen:  # a repeated song only needs checking once
            seen.add(t["uri"])
            tracks.append(track_summary(t))

    off = []
    try:
        for i, track in enumerate(tracks, 1):
            hits, tags = match_genres(track["artists"], keywords, lastfm.tags)
            if not hits:
                off.append({**track, "tags": tags, "unknown": not tags})
            progress("Looking up genres on Last.fm", i, len(tracks))
            if i % 25 == 0:
                lastfm.save()
    finally:
        lastfm.save()

    return {
        "playlist_id": playlist_id,
        "playlist_name": p.get("name") or "(untitled)",
        "genres": names,
        "keywords": sorted(set().union(*keywords.values())),
        "total": len(tracks),
        "off": off,
    }


def apply_cleanup(sp, cleanup, uris, report=print):
    """Remove the chosen songs (which must come from cleanup["off"]) from the playlist."""
    allowed = {t["uri"] for t in cleanup["off"]}
    uris = list(dict.fromkeys(u for u in uris if u in allowed))
    if not uris:
        report("Nothing selected to remove")
        return 0
    sp.remove_tracks(cleanup["playlist_id"], uris)
    report(f'{cleanup["playlist_name"]}: removed {len(uris)} songs')
    return len(uris)


def write_unmatched(unmatched, path=UNMATCHED_FILE):
    with path.open("w", encoding="utf-8") as f:
        for t in unmatched:
            f.write(f"{t['name']} - {', '.join(t['artists'])}  |  tags: {', '.join(t['tags']) or '(none)'}\n")


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(
        prog="sort_by_genre.py",
        description=f"{APP_NAME}: sort a Spotify playlist into genre playlists using Last.fm tags.",
        epilog="Browser version: https://anirudh-naveen.github.io/spotify-library-assistant/",
    )
    parser.add_argument("source", help="Source playlist name, link, or ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing anything")
    parser.add_argument("--map", type=Path, default=MAP_FILE, help="Path to the genre map JSON")
    dates = parser.add_argument_group("date filters (YYYY, YYYY-MM or YYYY-MM-DD; ranges are inclusive)")
    dates.add_argument("--added-after", metavar="DATE", help="Only songs added to the source on/after DATE")
    dates.add_argument("--added-before", metavar="DATE", help="Only songs added to the source on/before DATE")
    dates.add_argument("--released-after", metavar="DATE", help="Only songs released on/after DATE")
    dates.add_argument("--released-before", metavar="DATE", help="Only songs released on/before DATE")
    args = parser.parse_args()

    load_env()
    try:
        args.filters = parse_date_filters({name: getattr(args, name) for name in DATE_FILTERS})
        genres, unsorted = load_genre_map(args.map)
        sp = Spotify(SpotifyAuth())
        lastfm = LastFM()
        run(sp, lastfm, args, genres, unsorted)
    except SpotassistError as e:
        sys.exit(str(e))


def run(sp, lastfm, args, genres, unsorted):
    last_stage = None

    def progress(stage, done, total):
        nonlocal last_stage
        if stage != last_stage:
            print(f"{stage}...")
            last_stage = stage
        if stage.startswith("Looking up") and done % 25 == 0:
            print(f"  {done}/{total}")

    me = sp.me()
    print(f"Signed in as {me.get('display_name') or me['id']}")
    plan = build_plan(sp, lastfm, args.source, genres, unsorted, progress, args.filters)
    print(f"Source playlist has {plan['source_total']} songs.")
    if args.filters:
        print(f"{plan['total']} of them pass the date filters.")

    print("\nResults:")
    for target in plan["targets"]:
        if target["same_song_skipped"]:
            print(f"  {target['name']}: skipping {target['same_song_skipped']} songs already there under another version")
    if args.dry_run:
        for target in plan["targets"]:
            count = len(target["tracks"])
            label = target["name"]
            if target["genres"] != [label]:
                label += f" ({', '.join(target['genres'])})"
            if not count:
                print(f"  {label}: nothing new")
                continue
            note = " (playlist would be created)" if not target["playlist_id"] else ""
            print(f"  {label}: would add {count} songs{note}")
    else:
        apply_plan(sp, plan, report=lambda msg: print(f"  {msg}"))

    # --- report songs no rule matched, with their tags so you can add keywords
    write_unmatched(plan["unmatched"])
    print(f"\n{len(plan['unmatched'])} songs matched no genre playlist; see {UNMATCHED_FILE.name}")
    if args.dry_run:
        print("Dry run: nothing was changed. Run again without --dry-run to apply.")


if __name__ == "__main__":
    main()
