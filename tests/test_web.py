"""Web app guards, with the Spotify/Last.fm clients replaced by fakes (no network)."""

import json
import time

import pytest

import genrefy_web as web
import sort_by_genre as sbg


class FakeHandler(web.Handler):
    """A Handler that can be built without a socket, to test the request checks."""

    def __init__(self, headers):
        self.headers = headers


def make_app(tmp_path):
    app = web.App.__new__(web.App)  # skip __init__: no .env, token or network needed
    app.map_path = tmp_path / "genre_map.json"
    app.map_path.write_text(json.dumps({"_help": "keep me", "playlists": {"Pop": ["pop"]}}))
    app.lock = web.threading.Lock()
    app.job = None
    app.plan = None
    return app


@pytest.fixture(autouse=True)
def hosts():
    web.Handler.allowed_hosts = {"127.0.0.1:8888", "localhost:8888"}


@pytest.mark.parametrize("headers, write, ok", [
    ({"Host": "127.0.0.1:8888"}, False, True),
    ({"Host": "localhost:8888"}, False, True),
    ({"Host": "evil.example:8888"}, False, False),          # DNS rebinding
    ({"Host": "127.0.0.1:8888"}, True, False),              # write without the custom header
    ({"Host": "127.0.0.1:8888", "X-Genrefy": "1"}, True, True),
])
def test_trusted_requests(headers, write, ok):
    assert FakeHandler(headers)._trusted(write) is ok


def test_save_genre_config_validates_and_keeps_extra_keys(tmp_path):
    app = make_app(tmp_path)
    with pytest.raises(sbg.GenrefyError):
        app.save_genre_config({"playlists": {"Pop": []}})
    config = {"_help": "keep me", "genres": {"Rock": {"keywords": ["rock"], "playlist": "Loud", "playlist_id": "x"}},
              "unsorted_playlist": None}
    app.save_genre_config(config)
    assert json.loads(app.map_path.read_text()) == config


def wait(app):
    for _ in range(100):
        if app.job["status"] != "running":
            return app.job
        time.sleep(0.01)
    raise AssertionError("job didn't finish")


def test_apply_only_once_and_only_latest_preview(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app.sp = object()
    calls = []
    monkeypatch.setattr(sbg, "apply_plan", lambda sp, plan, report: calls.append(plan["id"]))
    app.plan = {"id": "p1", "applied": False, "targets": []}

    with pytest.raises(sbg.GenrefyError, match="out of date"):
        app.start_apply("old")
    app.start_apply("p1")
    assert wait(app)["status"] == "done"
    with pytest.raises(sbg.GenrefyError, match="already applied"):
        app.start_apply("p1")
    assert calls == ["p1"]


def test_job_errors_are_reported(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    def boom(job):
        raise sbg.GenrefyError("Last.fm rejected the API key.")

    app._start("preview", boom)
    job = wait(app)
    assert job["status"] == "error" and "Last.fm" in job["error"]
