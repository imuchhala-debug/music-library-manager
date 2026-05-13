#!/usr/bin/env python3
"""
Sync all Spotify playlists to local M3U8 files.

Authenticates with Spotify via Spotipy, fetches all playlists and their tracks,
fuzzy-matches against the local music library, and writes M3U8 playlist files
that can be imported into Apple Music (File > Library > Import Playlist).

Usage:
    python scripts/sync_all_playlists.py                    # full sync
    python scripts/sync_all_playlists.py --dry-run          # match only, no M3U8 output
    python scripts/sync_all_playlists.py --playlist "name"  # sync one playlist
    python scripts/sync_all_playlists.py --cache-only       # skip Spotify API, use cached data
    python scripts/sync_all_playlists.py --download-missing # download unmatched tracks via gamdl
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from utils import (
    PROJECT_ROOT,
    get_local_tracks,
    match_track,
    create_m3u8,
)

CACHE_DIR = PROJECT_ROOT / ".cache"
PLAYLIST_CACHE = CACHE_DIR / "spotify_playlists.json"
CACHE_TTL = 24 * 60 * 60  # 24 hours
SPOTIFY_CONFIG = PROJECT_ROOT / "config" / "spotify.json"


def load_spotify_config():
    """Load Spotify API credentials from config/spotify.json."""
    if not SPOTIFY_CONFIG.exists():
        print(f"Error: {SPOTIFY_CONFIG} not found.")
        print(
            "Copy config/spotify.json.example to config/spotify.json and fill in your credentials."
        )
        print("Create a Spotify app at https://developer.spotify.com/dashboard")
        sys.exit(1)

    with open(SPOTIFY_CONFIG) as f:
        config = json.load(f)

    for key in ("client_id", "client_secret"):
        if not config.get(key) or config[key].startswith("your_"):
            print(f"Error: '{key}' not set in {SPOTIFY_CONFIG}")
            sys.exit(1)

    return config


def get_spotify_client(auth_url=None):
    """Create authenticated Spotipy client.

    If no cached token exists, prints an auth URL for the user to visit.
    Pass auth_url= the redirect URL (from the browser) to complete auth.
    """
    config = load_spotify_config()
    redirect_uri = config.get("redirect_uri", "https://example.com/callback")

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = str(CACHE_DIR / ".spotify_token")

    auth_manager = SpotifyOAuth(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=cache_path,
        open_browser=False,
    )

    # Check for cached token first
    token_info = auth_manager.cache_handler.get_cached_token()
    if token_info and not auth_manager.is_token_expired(token_info):
        return spotipy.Spotify(auth_manager=auth_manager)

    # If we have an auth URL response, exchange it for a token
    if auth_url:
        code = auth_manager.parse_response_code(auth_url)
        auth_manager.get_access_token(code)
        return spotipy.Spotify(auth_manager=auth_manager)

    # No token and no auth URL — print instructions and exit
    auth_link = auth_manager.get_authorize_url()
    print("Authorization required. Open this URL in your browser:\n")
    print(f"  {auth_link}\n")
    print("After authorizing, you'll be redirected to a URL starting with")
    print(f"  {redirect_uri}?code=...")
    print("\nCopy that full URL and re-run with:")
    print('  python scripts/sync_all_playlists.py --auth-url "PASTE_URL_HERE"')
    sys.exit(0)


def fetch_all_playlists(sp):
    """Fetch all user playlists with their tracks from Spotify. Returns list of playlist dicts."""
    playlists = []
    results = sp.current_user_playlists(limit=50)

    while results:
        for entry in results["items"]:
            if entry is None:
                continue
            # Spotify API uses "items" instead of "tracks" for playlist track info
            items_info = entry.get("items") or entry.get("tracks") or {}
            playlist = {
                "id": entry["id"],
                "name": entry.get("name", "Untitled"),
                "owner": (entry.get("owner") or {}).get("display_name", "Unknown"),
                "total_tracks": items_info.get("total", 0),
                "tracks": [],
            }

            # Fetch full playlist via sp.playlist() (sp.playlist_tracks() returns 403)
            try:
                full = sp.playlist(entry["id"])
            except Exception as e:
                print(f"  Skipped: {playlist['name']} (error: {e})")
                continue

            # Paginate through tracks — field is "items" not "tracks"
            page = full.get("items") or full.get("tracks") or {}
            while page:
                for t in page.get("items", []):
                    # Track data is under "item" or "track" key
                    track = t.get("item") or t.get("track")
                    if not track or t.get("is_local"):
                        continue
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    playlist["tracks"].append(
                        {
                            "artist": artists,
                            "title": track.get("name", "Unknown"),
                            "album": (track.get("album") or {}).get("name", ""),
                            "uri": track.get("uri", ""),
                        }
                    )
                # Follow pagination
                if page.get("next"):
                    page = sp.next(page)
                else:
                    page = None

            playlists.append(playlist)
            print(f"  Fetched: {playlist['name']} ({len(playlist['tracks'])} tracks)")

        results = sp.next(results)

    return playlists


def load_cache():
    """Load cached playlist data if it exists and is fresh."""
    if not PLAYLIST_CACHE.exists():
        return None
    try:
        with open(PLAYLIST_CACHE) as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_TTL:
            print(f"Cache expired ({age / 3600:.1f}h old). Re-fetching from Spotify.")
            return None
        print(f"Using cached data ({age / 3600:.1f}h old).")
        return data["playlists"]
    except (json.JSONDecodeError, KeyError):
        return None


def save_cache(playlists):
    """Save playlist data to local JSON cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PLAYLIST_CACHE, "w") as f:
        json.dump({"timestamp": time.time(), "playlists": playlists}, f, indent=2)


def search_itunes(artist, title):
    """Search iTunes API for a track. Returns the iTunes URL if found."""
    query = f"{artist} {title}"
    params = urllib.parse.urlencode({"term": query, "media": "music", "limit": 1})
    url = f"https://itunes.apple.com/search?{params}"
    try:
        import requests

        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("trackViewUrl")
    except Exception:
        pass
    return None


def download_track(itunes_url):
    """Download a track via gamdl using an iTunes URL."""
    try:
        result = subprocess.run(
            [
                "gamdl",
                itunes_url,
                "--config-path",
                str(PROJECT_ROOT / "config" / "gamdl.toml"),
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        return result.returncode == 0
    except FileNotFoundError:
        print("    gamdl not found. Install it or check your PATH.")
        return False


def sync_playlist(playlist, local_tracks, dry_run=False, download_missing=False):
    """Match a single playlist against local tracks, optionally write M3U8.

    Returns (matched_list, missing_list).
    """
    matched = []
    missing = []

    for sp_track in playlist["tracks"]:
        local_match, score = match_track(sp_track, local_tracks)
        if local_match:
            matched.append(
                {
                    "artist": sp_track["artist"],
                    "title": sp_track["title"],
                    "local_path": local_match["path"],
                    "score": score,
                }
            )
        else:
            missing.append(sp_track)

    total = len(playlist["tracks"])
    match_pct = (len(matched) / total * 100) if total else 0
    status = "DRY RUN" if dry_run else "OK"
    print(
        f"  [{status}] {playlist['name']}: {len(matched)}/{total} matched ({match_pct:.0f}%)"
    )

    if not dry_run and matched:
        path = create_m3u8(playlist["name"], matched)
        print(f"    -> {path}")

    if download_missing and missing:
        print(f"    Attempting to download {len(missing)} missing tracks...")
        for track in missing:
            itunes_url = search_itunes(track["artist"], track["title"])
            if itunes_url:
                print(f"    Downloading: {track['artist']} - {track['title']}")
                if download_track(itunes_url):
                    print("      Downloaded successfully")
                else:
                    print("      Download failed")
            else:
                print(f"    Not on iTunes: {track['artist']} - {track['title']}")

    return matched, missing


def sync_to_apple_music(playlist_name, matched_tracks):
    """Sync matched tracks into an Apple Music playlist.

    Creates the playlist if it doesn't exist, then adds only tracks
    that aren't already in the playlist. Uses a temp file for paths
    to avoid AppleScript escaping issues.
    """
    import tempfile

    track_paths = [t["local_path"] for t in matched_tracks if t.get("local_path")]
    if not track_paths:
        return 0

    # Strip leading/trailing whitespace from playlist name for Apple Music
    playlist_name = playlist_name.strip()

    # Write paths to a temp file (one per line) to avoid escaping issues
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    try:
        for p in track_paths:
            tmp.write(p + "\n")
        tmp.close()

        script = """
            use AppleScript version "2.4"
            use scripting additions

            on run argv
                set playlistName to item 1 of argv
                set pathFile to item 2 of argv

                -- Read paths from temp file
                set trackPaths to paragraphs of (do shell script "cat " & quoted form of pathFile)

                tell application "Music"
                    -- Find or create the playlist
                    set targetPlaylist to missing value
                    repeat with p in (get every user playlist)
                        if name of p is playlistName then
                            set targetPlaylist to p
                            exit repeat
                        end if
                    end repeat

                    if targetPlaylist is missing value then
                        set targetPlaylist to (make new user playlist with properties {name:playlistName})
                    end if

                    -- Get existing track locations in the playlist
                    set existingPaths to {}
                    try
                        repeat with t in (get every track of targetPlaylist)
                            try
                                set end of existingPaths to POSIX path of (get location of t)
                            end try
                        end repeat
                    end try

                    -- Add only tracks not already present
                    set addedCount to 0
                    repeat with trackPath in trackPaths
                        set trackPath to trackPath as text
                        if trackPath is not "" and trackPath is not in existingPaths then
                            try
                                set fileRef to (POSIX file trackPath) as alias
                                add fileRef to targetPlaylist
                                set addedCount to addedCount + 1
                            end try
                        end if
                    end repeat

                    return addedCount
                end tell
            end run
        """

        result = subprocess.run(
            ["osascript", "-e", script, playlist_name, tmp.name],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            added = result.stdout.strip()
            return int(added) if added.isdigit() else 0
        else:
            print(f"    Apple Music error: {result.stderr.strip()}")
            return -1
    except subprocess.TimeoutExpired:
        print(f"    Apple Music sync timed out for '{playlist_name}'")
        return -1
    finally:
        os.unlink(tmp.name)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Spotify playlists to local M3U8 files for Apple Music import."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Match only, don't write M3U8 files"
    )
    parser.add_argument(
        "--playlist",
        type=str,
        help="Sync only the playlist matching this name (case-insensitive)",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Skip Spotify API, use cached data only",
    )
    parser.add_argument(
        "--download-missing",
        action="store_true",
        help="Search iTunes and download unmatched tracks via gamdl",
    )
    parser.add_argument(
        "--auth-url",
        type=str,
        help="Spotify redirect URL from browser (for first-time auth)",
    )
    parser.add_argument(
        "--apple-music",
        action="store_true",
        help="Sync playlists into Apple Music app (add new tracks to existing playlists)",
    )
    args = parser.parse_args()

    # Load playlists (from cache or Spotify API)
    playlists = None
    if args.cache_only:
        playlists = load_cache()
        if playlists is None:
            print("Error: No cached data found. Run without --cache-only first.")
            sys.exit(1)
    else:
        playlists = load_cache()
        if playlists is None:
            print("Authenticating with Spotify...")
            sp = get_spotify_client(auth_url=args.auth_url)
            user = sp.current_user()
            print(f"Logged in as: {user['display_name']}")
            print("\nFetching playlists...")
            playlists = fetch_all_playlists(sp)
            save_cache(playlists)
            print(f"\nCached {len(playlists)} playlists.")

    # Filter to single playlist if requested
    if args.playlist:
        target = args.playlist.lower()
        playlists = [p for p in playlists if target in p["name"].lower()]
        if not playlists:
            print(f"No playlist matching '{args.playlist}' found.")
            sys.exit(1)

    print("\nScanning local library...")
    local_tracks = get_local_tracks()
    print(f"Found {len(local_tracks)} local tracks.\n")

    # Sync each playlist
    all_missing = {}
    total_matched = 0
    total_tracks = 0
    all_matched = {}  # playlist_name -> matched_tracks

    for playlist in playlists:
        matched, missing = sync_playlist(
            playlist,
            local_tracks,
            dry_run=args.dry_run,
            download_missing=args.download_missing,
        )
        total_matched += len(matched)
        total_tracks += len(playlist["tracks"])
        all_matched[playlist["name"]] = matched
        for track in missing:
            key = f"{track['artist']} - {track['title']}"
            all_missing[key] = track

    # If downloading, re-scan and re-match to pick up new files
    if args.download_missing and all_missing:
        print("\nRe-scanning library after downloads...")
        local_tracks = get_local_tracks()
        print(f"Found {len(local_tracks)} local tracks.\n")
        all_missing = {}
        total_matched = 0
        total_tracks = 0
        for playlist in playlists:
            matched, missing = sync_playlist(
                playlist, local_tracks, dry_run=args.dry_run
            )
            total_matched += len(matched)
            total_tracks += len(playlist["tracks"])
            all_matched[playlist["name"]] = matched
            for track in missing:
                key = f"{track['artist']} - {track['title']}"
                all_missing[key] = track

    # Sync to Apple Music app
    if args.apple_music and not args.dry_run:
        print("\nSyncing to Apple Music...")
        for name, matched in all_matched.items():
            if not matched:
                continue
            added = sync_to_apple_music(name, matched)
            if added > 0:
                print(f"  {name}: added {added} new tracks")
            elif added == 0:
                print(f"  {name}: up to date")
            # added == -1 means error, already printed

    # Summary
    overall_pct = (total_matched / total_tracks * 100) if total_tracks else 0
    print(f"\n{'=' * 60}")
    print(
        f"Summary: {total_matched}/{total_tracks} tracks matched ({overall_pct:.0f}%)"
    )
    print(f"Playlists processed: {len(playlists)}")

    if all_missing:
        print(f"\nMissing tracks (deduplicated): {len(all_missing)}")
        for i, key in enumerate(sorted(all_missing)):
            if i >= 25:
                print(f"  ... and {len(all_missing) - 25} more")
                break
            print(f"  - {key}")

    if args.dry_run:
        print("\n(Dry run - no changes were made.)")


if __name__ == "__main__":
    main()
