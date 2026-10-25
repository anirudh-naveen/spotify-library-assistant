#!/usr/bin/env python3
"""
Genrefy

Reads a source Spotify playlist, looks up each track's artist on Last.fm to get
genre tags, and appends the track to every genre playlist whose keywords match
(see genre_map.json). Songs already in a genre playlist are skipped, so it's
safe to re-run whenever you add new songs to the source playlist.

Usage:
    python sort_by_genre.py "My Big Playlist" --dry-run   # preview, changes nothing
    python sort_by_genre.py "My Big Playlist"             # actually add songs
    python sort_by_genre.py https://open.spotify.com/playlist/XXXX

For a browser UI instead, run: python genrefy_web.py
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

APP_NAME = "Genrefy"

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


class GenrefyError(Exception):
    """A problem to show the user as a plain message, without a traceback."""


class NotLoggedIn(GenrefyError):
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
        raise GenrefyError(f"Missing {name}. Copy .env.example to .env and fill it in.")
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
            raise GenrefyError(f"Spotify login failed: {r.status_code} {r.text[:300]}")
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
            raise GenrefyError(f"Couldn't listen on {parsed.hostname}:{parsed.port} for the Spotify login "
                               f"({e}). Is something else (like genrefy_web.py) using that port?")
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
            raise GenrefyError("Spotify login failed: no response within 5 minutes.")
        if "error" in result:
            raise GenrefyError(f"Spotify login failed: {result['error']}")
        if result.get("state") != state or "code" not in result:
            raise GenrefyError("Spotify login failed: the response didn't match this login attempt. Try again.")
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

class SpotifyError(GenrefyError):
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
                    raise GenrefyError(f"Spotify rate-limited this app for {int(wait)} seconds. Try again later.")
                time.sleep(wait + 1)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if not r.ok:
                raise SpotifyError(r.status_code, method, url, r.text)
            return r.json() if r.content else {}
        raise GenrefyError(f"Spotify kept failing on {method} {url}; try again later.")

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
            yield track

    def create_playlist(self, name):
        return self.call("POST", "me/playlists", json={
            "name": name,
            "public": False,
            "description": f"Auto-sorted by genre with {APP_NAME}",
        })

    def add_tracks(self, playlist_id, uris):
        for batch in chunks(uris, 100):
            self.call("POST", f"playlists/{playlist_id}/items", json={"uris": batch})


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
                raise GenrefyError("Last.fm rejected the API key. Check LASTFM_API_KEY in .env.")
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

def parse_genre_map(config, source="genre map"):
    """Validate a genre map config dict. Returns ({playlist: {keywords}}, unsorted name or None)."""
    if not isinstance(config, dict):
        raise GenrefyError(f"{source} must be a JSON object.")
    playlists = {}
    for name, keywords in (config.get("playlists") or {}).items():
        if isinstance(keywords, str):  # "Pop": "pop" instead of ["pop"]
            keywords = [keywords]
        kws = {norm(k) for k in keywords or []} - {""}
        if not name.strip() or not kws:
            raise GenrefyError(f'{source}: playlist "{name}" needs a name and at least one keyword.')
        playlists[name.strip()] = kws
    if not playlists:
        raise GenrefyError(f"{source} has no playlists defined.")
    unsorted = (config.get("unsorted_playlist") or "").strip() or None
    return playlists, unsorted


def read_genre_config(path=MAP_FILE):
    if not path.exists():
        raise GenrefyError(f"Missing {path.name}. It should sit next to this script.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise GenrefyError(f"{path.name} isn't valid JSON: {e}")


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


def new_uris(tracks, existing):
    """URIs from tracks not already in `existing`, in order, without duplicates."""
    seen = set(existing)
    out = []
    for t in tracks:
        uri = t["uri"]
        if uri not in seen:
            seen.add(uri)
            out.append(uri)
    return out


def track_summary(track):
    return {
        "uri": track["uri"],
        "name": track.get("name") or "(untitled)",
        "artists": [a["name"] for a in track.get("artists") or [] if a and a.get("name")],
    }


# ---------------------------------------------------------------- plan and apply

def resolve_source(source, by_name):
    """Return (playlist id, name or None). An exact name in your library wins over ID parsing."""
    source = source.strip()
    match = by_name.get(source.lower()) or next((p for p in by_name.values() if p.get("id") == source), None)
    if match:
        return match["id"], match.get("name")
    source_id = parse_playlist_id(source)
    if source_id is None:
        raise GenrefyError(f'No playlist named "{source}" in your library.')
    name = next((p.get("name") for p in by_name.values() if p.get("id") == source_id), None)
    return source_id, name


def build_plan(sp, lastfm, source, genre_map, unsorted_name, progress=None):
    """Work out which songs would go where, without changing anything.

    progress(stage, done, total) is called as work proceeds, if given.
    The returned dict is JSON-serializable.
    """
    progress = progress or (lambda stage, done, total: None)
    progress("Reading your playlists", 0, 0)
    me = sp.me()
    my_id = me["id"]
    by_name = index_by_name(sp.my_playlists(), my_id)
    source_id, source_name = resolve_source(source, by_name)

    # --- find (or plan to create) each target playlist
    target_names = list(dict.fromkeys(list(genre_map) + ([unsorted_name] if unsorted_name else [])))
    targets = {}
    for name in target_names:
        p = by_name.get(name.lower())
        if p and not can_edit(p, my_id):
            raise GenrefyError(f'"{name}" isn\'t your playlist, so songs can\'t be added to it. '
                               "Rename it in the genre map.")
        targets[name] = p  # None means it needs creating

    # --- read source and work out where each track goes
    progress("Reading the source playlist", 0, 0)
    try:
        tracks = [track_summary(t) for t in sp.playlist_tracks(source_id)]
    except SpotifyError as e:
        if e.status in (403, 404):
            raise GenrefyError(
                "Couldn't read the source playlist. It may not exist, or Spotify may not let "
                "this app read it (playlists you don't own or collaborate on are often blocked). "
                "Copy its songs into a playlist of your own and use that instead.")
        raise

    assigned = {name: [] for name in target_names}
    unmatched = []
    try:
        for i, track in enumerate(tracks, 1):
            hits, seen_tags = match_genres(track["artists"], genre_map, lastfm.tags)
            for name in hits:
                assigned[name].append(track)
            if not hits:
                unmatched.append({**track, "tags": seen_tags})
                if unsorted_name:
                    assigned[unsorted_name].append(track)
            progress("Looking up genres on Last.fm", i, len(tracks))
            if i % 25 == 0:
                lastfm.save()
    finally:
        lastfm.save()  # keep lookups even if interrupted

    # --- skip songs already in each target
    results = []
    for n, name in enumerate(target_names, 1):
        progress("Checking your genre playlists", n, len(target_names))
        playlist = targets[name]
        existing = {t["uri"] for t in sp.playlist_tracks(playlist["id"])} if playlist else set()
        by_uri = {t["uri"]: t for t in assigned[name]}
        results.append({
            "name": name,
            "playlist_id": playlist["id"] if playlist else None,
            "matched": len(by_uri),
            "tracks": [by_uri[u] for u in new_uris(assigned[name], existing)],
        })

    return {
        "user": me.get("display_name") or my_id,
        "source_id": source_id,
        "source_name": source_name,
        "total": len(tracks),
        "targets": results,
        "unmatched": unmatched,
    }


def apply_plan(sp, plan, report=print):
    """Create missing playlists and add the planned songs. Re-checks each target first,
    so songs added since the plan was built aren't added twice."""
    for target in plan["targets"]:
        name, playlist_id = target["name"], target["playlist_id"]
        existing = {t["uri"] for t in sp.playlist_tracks(playlist_id)} if playlist_id else set()
        uris = new_uris(target["tracks"], existing)
        if not uris:
            report(f"{name}: nothing new")
            continue
        if not playlist_id:
            playlist_id = target["playlist_id"] = sp.create_playlist(name)["id"]
            report(f'Created playlist "{name}"')
        sp.add_tracks(playlist_id, uris)
        report(f"{name}: added {len(uris)} songs")


def write_unmatched(unmatched, path=UNMATCHED_FILE):
    with path.open("w", encoding="utf-8") as f:
        for t in unmatched:
            f.write(f"{t['name']} - {', '.join(t['artists'])}  |  tags: {', '.join(t['tags']) or '(none)'}\n")


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(
        prog="sort_by_genre.py",
        description=f"{APP_NAME}: sort a Spotify playlist into genre playlists using Last.fm tags.",
        epilog="For a browser UI, run: python genrefy_web.py",
    )
    parser.add_argument("source", help="Source playlist name, link, or ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing anything")
    parser.add_argument("--map", type=Path, default=MAP_FILE, help="Path to the genre map JSON")
    args = parser.parse_args()

    load_env()
    try:
        genre_map, unsorted_name = load_genre_map(args.map)
        sp = Spotify(SpotifyAuth())
        lastfm = LastFM()
        run(sp, lastfm, args, genre_map, unsorted_name)
    except GenrefyError as e:
        sys.exit(str(e))


def run(sp, lastfm, args, genre_map, unsorted_name):
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
    plan = build_plan(sp, lastfm, args.source, genre_map, unsorted_name, progress)
    print(f"Source playlist has {plan['total']} songs.")

    print("\nResults:")
    if args.dry_run:
        for target in plan["targets"]:
            count = len(target["tracks"])
            if not count:
                print(f"  {target['name']}: nothing new")
                continue
            note = " (playlist would be created)" if not target["playlist_id"] else ""
            print(f"  {target['name']}: would add {count} songs{note}")
    else:
        apply_plan(sp, plan, report=lambda msg: print(f"  {msg}"))

    # --- report songs no rule matched, with their tags so you can add keywords
    write_unmatched(plan["unmatched"])
    print(f"\n{len(plan['unmatched'])} songs matched no genre playlist; see {UNMATCHED_FILE.name}")
    if args.dry_run:
        print("Dry run: nothing was changed. Run again without --dry-run to apply.")


if __name__ == "__main__":
    main()
