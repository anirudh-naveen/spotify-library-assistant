<<<<<<< Updated upstream
# genrefy
Spotify third-party application that helps organize playlists and other useful features.
=======
# Genrefy

Genrefy sorts a big Spotify playlist into smaller genre playlists.

You point it at a source playlist. For each song it looks up the artist's top
tags on [Last.fm](https://www.last.fm/api/show/artist.getTopTags), then appends
the song to every genre playlist whose keywords match. The keywords and
playlists come from `genre_map.json`, which you edit. A song can land in more
than one playlist.

Genrefy only adds songs: anything already in a target playlist is skipped, so
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
   client secret: Genrefy logs in with PKCE.
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

The first run opens your browser to log in to Spotify. The token is cached in
`.spotify_token.json` and refreshed automatically after that. Last.fm lookups are
cached in `lastfm_cache.json`, so later runs only query new artists. Delete that
file to fetch fresh tags.

## Editing `genre_map.json`

```json
{
  "playlists": {
    "EDM": ["edm", "house", "dubstep", "drum and bass"],
    "Pop": ["pop", "dance pop", "k pop"]
  },
  "unsorted_playlist": null
}
```

- Each key under `playlists` is the exact name of a playlist in your library.
  The match is case-insensitive. If a playlist doesn't exist yet, Genrefy creates
  it as a private playlist.
- Each value is a list of Last.fm tags that send a song to that playlist. A song
  matches when one of its artist's top tags equals a keyword. Tags are compared
  whole, so `pop` doesn't match `k pop`.
- Tags and keywords are normalized before they're compared: lowercase,
  `-`/`_` become spaces, `&` becomes `and`. That makes `K-Pop` equal `k pop` and
  `Drum & Bass` equal `drum and bass`.
- Set `unsorted_playlist` to a playlist name, for example `"Unsorted"`, to collect
  songs that matched nothing. Leave it `null` to skip that.
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
- **Duplicates are matched by Spotify track ID.** The same song from a different
  release (single, album, deluxe edition) has a different ID and can be added
  twice.
- **Add-only.** Removing a song from the source doesn't remove it from the genre
  playlists, and editing `genre_map.json` doesn't move songs that were already
  sorted.
- **Local files and podcast episodes are skipped.** The Web API can't add them to
  playlists.
- **Your own playlists only.** Genrefy can only add to playlists you own or that
  are collaborative. If a playlist you only follow has the same name as a genre,
  Genrefy uses your own playlist when you have one, and otherwise stops so you can
  rename the genre. Spotify may also refuse to let the app read a source playlist
  you don't own; copy its songs into your own playlist first.
- **API changes.** Genrefy targets Spotify's February 2026 Web API
  (`/playlists/{id}/items`, `POST /me/playlists`). Older or future API versions
  may need changes.

## Running tests

```bash
pip install pytest
python -m pytest
```

The tests cover the pure logic only and make no network calls.

## License

[MIT](LICENSE)
>>>>>>> Stashed changes
