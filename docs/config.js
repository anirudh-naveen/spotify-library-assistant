// Optional defaults for this copy of Spotassist.
//
// Leave these empty and every visitor enters their own Spotify Client ID and
// Last.fm API key on the page (stored only in their browser).
//
// Or fill them in to skip that step. Both values end up public on your site, which
// is fine for these two: the Spotify Client ID is meant to be public when logging in
// with PKCE, and the Last.fm key can only read public data. Keep in mind:
// - Spotify apps in Development mode only work for accounts you add under
//   User Management in the Spotify dashboard. Everyone else needs their own app.
// - Everyone using your Last.fm key shares its rate limit.
export const DEFAULTS = {
  spotifyClientId: "",
  lastfmApiKey: "",
};
