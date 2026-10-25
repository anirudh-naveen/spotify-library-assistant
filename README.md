# Spotassist

Spotassist sorts a big Spotify playlist into smaller genre playlists, and cleans
out songs that don't fit a playlist.

You point it at a source playlist. For each song it looks up the artist's top
tags on [Last.fm](https://www.last.fm/api/show/artist.getTopTags), then adds the
song to every genre playlist whose keywords match. You decide the genres, their
keywords, and which playlist each one goes to. A song can land in more than one
playlist.

Spotassist only adds songs when sorting: anything already in a target playlist is
skipped (including the same song from a different release), so re-running it
after you add songs to the source picks up just the new ones.

**Use it in your browser:** https://anirudh-naveen.github.io/spotify-library-assistant/

There's also a [command-line version](#command-line-version) in this repo.

## Why Last.fm?

Spotify's Web API no longer reliably returns artist genres. The field is
deprecated and often empty. Last.fm's community tags cover far more artists and
are free to query with an API key.

## Using the website

The website runs entirely in your browser. There's no Spotassist server: the page
talks straight to Spotify and Last.fm, and everything it saves (your keys, your
Spotify login, your genre map and a cache of Last.fm tags) stays in that
browser's storage.

### First visit: connect your accounts

You need two free keys. The page walks you through this, but in short:

1. **Spotify Client ID.** In the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   click **Create app**. Under **Redirect URIs**, add the page's address exactly as
   the setup panel shows it (for the site above, that's
   `https://anirudh-naveen.github.io/spotify-library-assistant/`). Tick **Web API**,
   save, and copy the **Client ID** from the app's Settings.
2. **Last.fm API key.** Create one at [last.fm/api/account/create](https://www.last.fm/api/account/create).
   Any app name works, and the callback URL can stay blank. Copy the **API key**.

Paste both into the setup panel, click **Save keys**, then **Log in with Spotify**.

Why your own Spotify app? Spotify keeps apps in Development mode unless a company
applies for more, and Development mode only allows a short list of accounts that
the app's owner adds by hand. With your own app you're the owner, so it always
works for you. To share yours with friends, add their Spotify emails under
**User Management** in the dashboard.

### The three tabs

- **Sort** adds songs to genre playlists. Pick a source playlist (or paste a
  link), optionally filter by the date songs were added or released, and click
  **Preview** to see exactly what would go where. Nothing changes until you click
  **Add songs**. Songs that matched nothing are listed with their Last.fm tags;
  click a tag to add it as a keyword for a genre.
- **Clean** removes songs that don't fit one of your playlists (see below).
- **Map** is where you edit genres: each one's keywords and the playlist it goes
  to, which can be one you already own or a new one with any name. Several genres
  can share a playlist. Use **Export** to back up your genre map or move it to
  another browser, and **Import** to load one, including a `genre_map.json` from
  the command-line version.

#### Cleaning up a playlist

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

### Privacy

The page sends your playlists' data only to Spotify, and artist names only to
Last.fm. The fonts come from Google Fonts. Logging out removes your Spotify login
from the browser; your keys and genre map stay until you clear the site's data.

## Publishing your own copy

The website is the `docs/` folder: plain HTML, CSS and JavaScript with no build
step, so GitHub Pages can serve it as is.

1. Push the repo to GitHub.
2. In the repo, open **Settings → Pages**. Under **Build and deployment**, choose
   **Deploy from a branch**, pick your main branch and the `/docs` folder, and save.
3. After a minute the site is live at `https://<your-username>.github.io/<repo-name>/`.
   Add that exact address to your Spotify app's **Redirect URIs**.

Optionally, put your own Spotify Client ID and Last.fm API key in `docs/config.js`
so visitors can skip the setup step. Both end up public on your site, which is
fine for these two keys, but your Spotify app still only works for the accounts
you add under **User Management**, and everyone shares your Last.fm rate limit.

### Running the website locally

```bash
python3 -m http.server 8888 --bind 127.0.0.1 --directory docs
```

Then open http://127.0.0.1:8888/ and add `http://127.0.0.1:8888/` to your Spotify
app's Redirect URIs. Opening `docs/index.html` straight from disk won't work,
because browsers don't allow its JavaScript modules or the Spotify login from
`file://` pages.

## Command-line version

`sort_by_genre.py` does the sorting from a terminal, using `genre_map.json` in
this folder for its genres.

### Requirements

- Python 3.9+
- A Spotify account
- A Spotify developer app (free; you only need its Client ID)
- A Last.fm API key (free)

The only dependency is `requests`.

### Setup

#### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

#### 2. Create a Spotify developer app

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

#### 3. Get a Last.fm API key

1. Go to [Create API account](https://www.last.fm/api/account/create). Log in or
   sign up first if asked.
2. Fill in an application name. The callback URL and homepage can stay blank.
3. Copy the **API key**. You don't need the shared secret.

#### 4. Configure

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

### Running it

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

This is the command-line version's genre map. (The website keeps its own copy in
your browser, edited in the **Map** tab; Export and Import move it between the two.)
The file looks like this:

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
  playlist. The website also stores the playlist's ID (`"playlist_id"`), so
  renaming the playlist in Spotify doesn't break anything.
- Playlist names are matched case-insensitively against playlists you own. If
  there's no match, Spotassist creates a private playlist with that name.
- Keywords are Last.fm tags. A song joins a genre when one of its artist's top
  tags equals a keyword. Tags are compared whole, so `pop` doesn't match `k pop`.
- Older files that use `"playlists"` instead of `"genres"` still work.
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
| `docs/index.html` | The website's page: HTML, CSS and the UI code, no build step |
| `docs/spotassist.js` | The website's logic: Spotify login, Last.fm, sorting and clean-up |
| `docs/config.js` | Optional default keys for your copy of the site |
| `sort_by_genre.py` | The command-line version |
| `genre_map.json` | The command-line version's genres and keywords |
| `tests/` | pytest tests for the command line, and `tests/js/` for the website |

## Running tests

```bash
pip install pytest
python -m pytest
```

```bash
node --test tests/js/*.test.mjs
```

The JavaScript tests need Node 22 or newer and nothing else. Both suites use
fake Spotify and Last.fm clients and make no network calls.

## License

[MIT](LICENSE)

Spotassist is an independent project. It isn't affiliated with, endorsed by or sponsored by Spotify.
