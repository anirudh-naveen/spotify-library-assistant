# Spotassist

Spotassist sorts a big Spotify playlist into smaller genre playlists.

You point it at a source playlist. For each song it looks up the artist's top
tags on [Last.fm](https://www.last.fm/api/show/artist.getTopTags), then appends
the song to every genre playlist whose keywords match. The keywords and
playlists come from `genre_map.json`, which you edit. A song can land in more
than one playlist.

Spotassist only adds songs: anything already in a target playlist is skipped, so
re-running it after you add songs to the source picks up just the new ones. Songs
that matched nothing are listed in `unmatched.txt` along with their Last.fm tags,
so you can see which keywords to add.

## Why Last.fm?

Spotify's Web API no longer reliably returns artist genres. The field is
deprecated and often empty. Last.fm's community tags cover far more artists and
are free to query with an API key.

## Requirements

- Python 3.9+
- A Spotify account
- A Spotify developer app (free; you only need its Client ID)
- A Last.fm API key (free)

The only dependency is `requests`.

## Setup

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create a Spotify developer app

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create app**. Any name and description will do.
3. Under **Redirect URIs**, add exactly `http://127.0.0.1:8888/callback`.
   Spotify doesn't accept `localhost`; use the loopback IP.
4. Under **Which API/SDKs are you planning to use?**, tick **Web API**, then save.
5. Open the app's **Settings** and copy the **Client ID**. You don't need the
   client secret: Spotassist logs in with PKCE.
6. New apps start in Development mode, where only allowlisted accounts can log
   in. If someone other than the app owner will use it, add their Spotify email
   under **User Management**.

### 3. Get a Last.fm API key

1. Go to [Create API account](https://www.last.fm/api/account/create). Log in or
   sign up first if asked.
2. Fill in an application name. The callback URL and homepage can stay blank.
3. Copy the **API key**. You don't need the shared secret.

### 4. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Required | Notes |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | yes | From step 2 |
| `LASTFM_API_KEY` | yes | From step 3 |
| `SPOTIFY_REDIRECT_URI` | no | Defaults to `http://127.0.0.1:8888/callback`. If you change it, make it match the dashboard exactly. |

Values already set in your environment take precedence over `.env`.

## Usage

### Web UI

```bash
python spotassist_web.py
```

This starts a small web server on your own computer and opens
http://127.0.0.1:8888 in your browser. From there you can:

- log in to Spotify
- pick a source playlist from your library, or paste a link
- optionally filter the source by date: when songs were added to it, or when
  they were released
- preview which songs would go to which playlist, with live progress
- click **Add songs** to apply the preview
- see the songs that matched nothing along with their Last.fm tags. Click a tag
  to add it as a keyword for a genre.
- edit your genres in the **Genre map** tab, and pick the playlist each genre goes
  to: one you already own, or a new one with any name
- remove songs that don't fit from one of your playlists in the **Clean up** tab
  (see below)

#### Cleaning up a playlist

The **Clean up** tab removes songs that don't fit a playlist:

1. Pick one of your playlists and the genre(s) it should match. If several genres
   go to that playlist, "Any genre that goes here" is picked for you, so songs
   from all of them count as fitting.
2. Click **Find songs**. Spotassist lists every song whose artists match none of
   those genres' keywords, along with their Last.fm tags so you can see why.
   Nothing changes yet.
3. Every listed song starts ticked, except ones whose artists have no Last.fm
   tags at all, since there's no evidence either way. Untick anything you want to
   keep.
4. Click **Remove** and confirm. Spotify has no undo for this, so you'd have to
   add songs back yourself.

The check is per artist, the same as sorting. Fixing your keywords first (for
example removing broad tags like `dance`) makes the list more accurate.

The server only answers requests from that page on your own computer. It uses
the same address as the Spotify redirect URI, so stop it (Ctrl+C) before using
the command line version's login. Use `--no-browser` to skip opening a tab, and
`--map` to use a different genre map. Saving from the Genre map tab rewrites
`genre_map.json` with standard JSON formatting.

### Command line

Preview first. `--dry-run` reads everything and shows what would be added, but
doesn't create or change any Spotify playlists:

```bash
python sort_by_genre.py "My Big Playlist" --dry-run
```

Then run it for real:

```bash
python sort_by_genre.py "My Big Playlist"
```

The source can be a playlist name from your library (case-insensitive), a share
link, a `spotify:playlist:` URI or a bare playlist ID:

```bash
python sort_by_genre.py https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
```

Use a different genre map with `--map path/to/map.json`.

Only sort some of the source's songs with the date filters. Each takes `YYYY`,
`YYYY-MM` or `YYYY-MM-DD`, and ranges include both ends:

```bash
python sort_by_genre.py "My Big Playlist" --added-after 2025-01-01 --dry-run
```

```bash
python sort_by_genre.py "My Big Playlist" --released-after 2010 --released-before 2019
```

`--added-after` and `--added-before` use the date a song was added to the source
playlist. `--released-after` and `--released-before` use its album's release date.
A song with no known date is left out when that date is filtered.

The first run opens your browser to log in to Spotify. The token is cached in
`.spotify_token.json` and refreshed automatically after that. Last.fm lookups are
cached in `lastfm_cache.json`, so later runs only query new artists. Delete that
file to fetch fresh tags.

## Editing `genre_map.json`

The easiest way to edit it is the web UI's **Genre map** tab. The file looks
like this:

```json
{
  "genres": {
    "Pop": ["pop", "dance pop", "k pop"],
    "EDM": { "keywords": ["edm", "house", "dubstep"], "playlist": "Club Bangers" },
    "Future Bass": { "keywords": ["future bass"], "playlist": "Club Bangers" }
  },
  "unsorted_playlist": null
}
```

- Each key under `genres` is a genre name. It's only a label.
- A genre written as a plain list of keywords goes to the playlist with the same
  name, like `Pop` above.
- To send a genre to a playlist with a different name, use an object with
  `keywords` and `playlist`, like `EDM` above. Several genres can share a
  playlist. The web UI also stores the playlist's ID (`"playlist_id"`), so
  renaming the playlist in Spotify doesn't break anything.
- Playlist names are matched case-insensitively against playlists you own. If
  there's no match, Spotassist creates a private playlist with that name.
- Keywords are Last.fm tags. A song joins a genre when one of its artist's top
  tags equals a keyword. Tags are compared whole, so `pop` doesn't match `k pop`.
- Older files that use `"playlists"` instead of `"genres"` still work. Saving
  from the web UI converts them.
- Tags and keywords are normalized before they're compared: lowercase,
  `-`/`_` become spaces, `&` becomes `and`. That makes `K-Pop` equal `k pop` and
  `Drum & Bass` equal `drum and bass`.
- Set `unsorted_playlist` to a playlist name, for example `"Unsorted"`, or a
  `{"playlist": ..., "playlist_id": ...}` object, to collect songs that matched
  nothing. Leave it `null` to skip that.
- Any top-level key starting with `_` (like `_help`) is ignored, so you can use
  it for notes.

The main artist's tags are tried first. Featured artists are checked only if the
main artist matched nothing. Only an artist's top 8 tags with a Last.fm weight of
at least 10 are used. Change `MAX_TAGS` and `MIN_TAG_WEIGHT` at the top of the
script to adjust that.

After a run, check `unmatched.txt` for tags you could add as keywords.

## Limitations

- **Artist-level genres.** Tags describe the artist, not the individual song, so
  an artist who spans genres puts all their songs in the same playlists.
- **Tag quality.** Last.fm tags are crowd-sourced. Some are noisy (`seen live`,
  `favorites`), and lesser-known artists may have none.
- **Name collisions.** Last.fm looks artists up by name, so two artists who share
  a name get the same tags.
- **Same-song detection is best effort.** Besides exact track IDs, Spotassist treats
  two tracks as the same song if they share an ISRC (the recording's industry
  ID), or if they have the same title and main artist and lengths within 4
  seconds. Title extras like "(feat. X)", "- 2011 Remaster" or "- Radio Edit" are
  ignored for this. Live, remix and acoustic versions count as different songs,
  and a radio edit that's much shorter than the album version can still be added
  next to it.
- **Add-only.** Removing a song from the source doesn't remove it from the genre
  playlists, and editing `genre_map.json` doesn't move songs that were already
  sorted.
- **Local files and podcast episodes are skipped.** The Web API can't add them to
  playlists.
- **Your own playlists only.** Spotassist can only add to playlists you own or that
  are collaborative. If a playlist you only follow has the same name as a genre's
  playlist, Spotassist uses your own playlist when you have one, and otherwise stops
  so you can pick a different playlist. Spotify may also refuse to let the app read a source playlist
  you don't own; copy its songs into your own playlist first.
- **API changes.** Spotassist targets Spotify's February 2026 Web API
  (`/playlists/{id}/items`, `POST /me/playlists`). Older or future API versions
  may need changes.

## Project layout

| File | What it is |
| --- | --- |
| `sort_by_genre.py` | Core logic and the command line tool |
| `spotassist_web.py` | Local web server for the web UI |
| `web/index.html` | The web page: plain HTML, CSS and JS, no build step |
| `genre_map.json` | Your genre playlists and their keywords |
| `tests/` | pytest tests, no network needed |

## Running tests

```bash
pip install pytest
python -m pytest
```

The tests cover the pure logic only and make no network calls.

## License

[MIT](LICENSE)

Spotassist is an independent project. It isn't affiliated with, endorsed by or sponsored by Spotify.
