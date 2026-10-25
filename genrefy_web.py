#!/usr/bin/env python3
"""
Genrefy web UI

A local web page for Genrefy: log in to Spotify, preview where songs would go,
apply it, and edit the genre map. It runs on your own computer only, at the
Spotify redirect address (http://127.0.0.1:8888 by default).

Usage:
    python genrefy_web.py              # starts the server and opens your browser
    python genrefy_web.py --no-browser
"""

import argparse
import http.server
import json
import secrets
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path

import sort_by_genre as core

INDEX_FILE = core.HERE / "web" / "index.html"
MAX_BODY = 1_000_000


class App:
    """State shared by all requests: Spotify/Last.fm clients, the current job and the last preview."""

    def __init__(self, map_path):
        self.map_path = map_path
        self.auth = core.SpotifyAuth(interactive=False)
        self.sp = core.Spotify(self.auth)
        self.lastfm = core.LastFM()
        self.pending_logins = {}  # state -> PKCE verifier
        self.user = None
        self.lock = threading.Lock()
        self.job = None
        self.plan = None
        self.cleanup = None

    # --- account

    def current_user(self):
        if not self.auth.logged_in():
            self.user = None
            return None
        if self.user is None:
            try:
                me = self.sp.me()
                self.user = {"id": me["id"], "name": me.get("display_name") or me["id"]}
            except core.NotLoggedIn:
                return None
        return self.user

    def logout(self):
        self.auth.logout()
        with self.lock:
            self.user = None
            self.plan = None
            self.cleanup = None

    def playlists(self):
        my_id = self.current_user()["id"]
        out = []
        for p in self.sp.my_playlists():
            # the Feb 2026 API calls the count "items"; older responses call it "tracks"
            counts = p.get("items") if isinstance(p.get("items"), dict) else p.get("tracks")
            out.append({
                "id": p["id"],
                "name": p.get("name") or "(untitled)",
                "count": (counts or {}).get("total"),
                "editable": core.can_edit(p, my_id),
            })
        out.sort(key=lambda p: p["name"].lower())
        return out

    # --- genre map

    def genre_config(self):
        return core.read_genre_config(self.map_path)

    def save_genre_config(self, config):
        core.parse_genre_map(config)  # raises GenrefyError if invalid
        self.map_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # --- background jobs (one at a time)

    def job_state(self):
        job = self.job
        if not job:
            return None
        return {k: job[k] for k in ("id", "kind", "status", "stage", "done", "total", "log", "error", "result")}

    def _start(self, kind, work):
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise core.GenrefyError("Genrefy is still working on the last request.")
            job = {"id": secrets.token_hex(8), "kind": kind, "status": "running", "stage": "Starting",
                   "done": 0, "total": 0, "log": [], "error": None, "result": None}
            self.job = job

        def target():
            try:
                job["result"] = work(job)
                job["status"] = "done"
            except core.GenrefyError as e:
                job["error"] = str(e)
                job["status"] = "error"
            except Exception as e:  # show something useful instead of a stuck spinner
                traceback.print_exc()
                job["error"] = f"Unexpected error: {e}"
                job["status"] = "error"

        threading.Thread(target=target, daemon=True).start()
        return self.job_state()

    def start_preview(self, source, filters=None):
        if not source.strip():
            raise core.GenrefyError("Pick a source playlist first.")
        filters = core.parse_date_filters(filters if isinstance(filters, dict) else {})
        genres, unsorted = core.load_genre_map(self.map_path)

        def work(job):
            def progress(stage, done, total):
                job["stage"], job["done"], job["total"] = stage, done, total

            plan = core.build_plan(self.sp, self.lastfm, source, genres, unsorted, progress, filters)
            plan["id"] = secrets.token_hex(8)
            plan["applied"] = False
            core.write_unmatched(plan["unmatched"])
            self.plan = plan
            return plan

        return self._start("preview", work)

    def start_apply(self, plan_id):
        with self.lock:
            plan = self.plan
            if not plan or plan["id"] != plan_id:
                raise core.GenrefyError("That preview is out of date. Run the preview again.")
            if plan["applied"]:
                raise core.GenrefyError("That preview was already applied. Run the preview again to add newer songs.")
            plan["applied"] = True  # set before starting, so a double click can't apply twice

        def work(job):
            job["stage"] = "Adding songs"
            core.apply_plan(self.sp, plan, report=job["log"].append)
            return {"plan_id": plan_id}

        return self._start("apply", work)

    def start_cleanup(self, playlist, genre_names):
        if not playlist.strip():
            raise core.GenrefyError("Pick a playlist to clean up.")
        if not isinstance(genre_names, list) or not all(isinstance(g, str) for g in genre_names):
            raise core.GenrefyError("Pick the genre the playlist should match.")
        genres, _ = core.load_genre_map(self.map_path)

        def work(job):
            def progress(stage, done, total):
                job["stage"], job["done"], job["total"] = stage, done, total

            cleanup = core.build_cleanup(self.sp, self.lastfm, playlist, genre_names, genres, progress)
            cleanup["id"] = secrets.token_hex(8)
            cleanup["removed"] = []
            self.cleanup = cleanup
            return cleanup

        return self._start("cleanup", work)

    def start_remove(self, cleanup_id, uris):
        if not isinstance(uris, list) or not uris:
            raise core.GenrefyError("Tick at least one song to remove.")
        with self.lock:
            cleanup = self.cleanup
            if not cleanup or cleanup["id"] != cleanup_id:
                raise core.GenrefyError("That list is out of date. Find the songs again.")
            already = set(cleanup["removed"])
            uris = [str(u) for u in uris if str(u) not in already]
            if not uris:
                raise core.GenrefyError("Those songs were already removed.")
            cleanup["removed"].extend(uris)  # before starting, so a double click can't remove twice

        def work(job):
            job["stage"] = "Removing songs"
            removed = core.apply_cleanup(self.sp, cleanup, uris, report=job["log"].append)
            return {"cleanup_id": cleanup_id, "removed": removed, "uris": uris}

        try:
            return self._start("remove", work)
        except core.GenrefyError:
            with self.lock:  # never started, so those songs weren't removed after all
                cleanup["removed"] = [u for u in cleanup["removed"] if u not in uris]
            raise


class Handler(http.server.BaseHTTPRequestHandler):
    app = None              # set in main()
    allowed_hosts = set()   # set in main()
    callback_path = "/callback"

    def log_message(self, *args):
        pass

    # --- plumbing

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _redirect(self, location):
        self._send(302, headers={"Location": location})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise core.GenrefyError("Request too large.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise core.GenrefyError("Request wasn't valid JSON.")

    def _trusted(self, write):
        """Only answer pages served from this server. The Host check blocks DNS-rebinding;
        requiring a custom header on writes means other websites can't post here."""
        if self.headers.get("Host") not in self.allowed_hosts:
            return False
        return not write or self.headers.get("X-Genrefy") == "1"

    def _handle(self, routes, write):
        if not self._trusted(write):
            return self._send(403, b"Forbidden")
        parsed = urllib.parse.urlparse(self.path)
        route = routes.get(parsed.path)
        if not route:
            return self._send(404, b"Not found")
        try:
            route(urllib.parse.parse_qs(parsed.query))
        except core.NotLoggedIn as e:
            self._error(str(e), 401)
        except core.GenrefyError as e:
            self._error(str(e))

    def do_GET(self):
        self._handle({
            "/": self.page,
            "/login": self.login,
            self.callback_path: self.callback,
            "/api/status": lambda q: self._json(self.status()),
            "/api/playlists": lambda q: self._json(self.app.playlists()),
            "/api/genre-map": lambda q: self._json(self.app.genre_config()),
            "/api/job": lambda q: self._json(self.app.job_state()),
        }, write=False)

    def do_POST(self):
        self._handle({
            "/api/preview": lambda q: self._json(self._preview(self._body())),
            "/api/apply": lambda q: self._json(self.app.start_apply(str(self._body().get("plan_id", "")))),
            "/api/cleanup": lambda q: self._json(self._cleanup(self._body())),
            "/api/remove": lambda q: self._json(self._remove(self._body())),
            "/api/logout": lambda q: (self.app.logout(), self._json({"ok": True})),
        }, write=True)

    def do_PUT(self):
        self._handle({
            "/api/genre-map": lambda q: (self.app.save_genre_config(self._body()), self._json({"ok": True})),
        }, write=True)

    # --- routes

    def _preview(self, body):
        return self.app.start_preview(str(body.get("source", "")), body.get("filters"))

    def _cleanup(self, body):
        return self.app.start_cleanup(str(body.get("playlist", "")), body.get("genres"))

    def _remove(self, body):
        return self.app.start_remove(str(body.get("cleanup_id", "")), body.get("uris"))

    def status(self):
        user = self.app.current_user()
        plan = self.app.plan
        return {
            "user": user,
            "job": self.app.job_state(),
            "plan": plan if user else None,
            "cleanup": self.app.cleanup if user else None,
        }

    def page(self, query):
        self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                       "frame-ancestors 'none'",
        })

    def login(self, query):
        url, state, verifier = self.app.auth.authorize_url()
        self.app.pending_logins[state] = verifier
        self._redirect(url)

    def callback(self, query):
        q = {k: v[0] for k, v in query.items()}
        verifier = self.app.pending_logins.pop(q.get("state", ""), None)
        if "error" in q:
            return self._redirect("/?login_error=" + urllib.parse.quote(q["error"]))
        if not verifier or "code" not in q:
            return self._redirect("/?login_error=" + urllib.parse.quote("login didn't match; try again"))
        try:
            self.app.auth.exchange_code(q["code"], verifier)
        except core.GenrefyError as e:
            return self._redirect("/?login_error=" + urllib.parse.quote(str(e)))
        self.app.user = None
        self._redirect("/")


def main():
    parser = argparse.ArgumentParser(description=f"{core.APP_NAME} web UI (runs locally).")
    parser.add_argument("--map", type=Path, default=core.MAP_FILE, help="Path to the genre map JSON")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    args = parser.parse_args()

    core.load_env()
    try:
        app = App(args.map)
    except core.GenrefyError as e:
        sys.exit(str(e))

    # Serve on the Spotify redirect address, so Spotify sends the login straight back here.
    redirect = urllib.parse.urlparse(core.redirect_uri())
    host, port = redirect.hostname, redirect.port or 80
    if host not in ("127.0.0.1", "localhost"):
        sys.exit(f"SPOTIFY_REDIRECT_URI must point at this computer (127.0.0.1), not {host}.")
    Handler.app = app
    Handler.callback_path = redirect.path or "/callback"
    Handler.allowed_hosts = {f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"}

    try:
        server = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        sys.exit(f"Couldn't start on {host}:{port} ({e}). Is Genrefy already running?")

    url = f"http://{host}:{port}/"
    print(f"{core.APP_NAME} is running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        app.lastfm.save()


if __name__ == "__main__":
    main()
