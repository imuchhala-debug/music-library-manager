#!/usr/bin/env python3
"""
Sync a Spotify playlist to Vinyl: match tracks against local library,
report matched file paths and missing tracks as JSON.

Usage:
    python3 sync_spotify_to_vinyl.py <spotify_playlist_url> [--library-path /path/to/library]

Emits JSON lines:
    {"status": "progress", "message": "Matching tracks..."}
    {"status": "result", "name": "...", "matched": [...], "missing": [...], "artwork_url": "..."}
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from utils import get_local_tracks, match_track


def emit(status, **kwargs):
    msg = {"status": status, **kwargs}
    print(json.dumps(msg), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Spotify playlist URL")
    parser.add_argument("--library-path", default="/Volumes/One Touch /music library")
    args = parser.parse_args()

    # Parse playlist ID (strip query params like ?si=...)
    import re

    clean_url = args.url.split("?")[0]
    match = re.search(r"/playlist/([a-zA-Z0-9]+)", clean_url)
    if not match:
        emit("error", message="Could not parse Spotify playlist ID from URL")
        sys.exit(1)

    playlist_id = match.group(1)

    # Load Spotify credentials
    config_path = SCRIPT_DIR.parent / "config" / "spotify.json"
    if not config_path.exists():
        emit("error", message="Spotify not configured. Set up config/spotify.json")
        sys.exit(1)

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        config = json.loads(config_path.read_text())
        cache_dir = SCRIPT_DIR.parent / "config"
        cache_path = str(cache_dir / ".spotify_token")

        auth_manager = SpotifyOAuth(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            redirect_uri=config.get("redirect_uri", "http://127.0.0.1:8888/callback"),
            scope="playlist-read-private playlist-read-collaborative user-library-read",
            cache_path=cache_path,
            open_browser=False,
        )
        # Check for valid cached token — don't try interactive auth from subprocess
        token_info = auth_manager.cache_handler.get_cached_token()
        if not token_info:
            emit(
                "error",
                message="Spotify token expired. Re-authenticate in the download panel first.",
            )
            sys.exit(1)
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
        access_token = token_info["access_token"]
        sp = spotipy.Spotify(auth=access_token)
    except ImportError:
        emit("error", message="spotipy not installed. Run: pip3 install spotipy")
        sys.exit(1)
    except Exception as e:
        emit("error", message=f"Spotify auth error: {e}")
        sys.exit(1)

    # Fetch playlist
    emit("progress", message="Fetching Spotify playlist...")
    try:
        data = sp.playlist(playlist_id)
    except Exception as e:
        emit("error", message=f"Could not fetch playlist: {e}")
        sys.exit(1)

    playlist_name = data["name"]
    artwork_url = data["images"][0]["url"] if data.get("images") else None

    # Fetch tracks from playlist using spotipy's built-in pagination
    sp_tracks = []

    def parse_items(items):
        parsed = []
        for item in items:
            t = item.get("track")
            if not t or not isinstance(t, dict):
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            parsed.append(
                {
                    "artist": artists,
                    "title": t.get("name", ""),
                    "album": t.get("album", {}).get("name", ""),
                    "duration_ms": t.get("duration_ms", 0),
                }
            )
        return parsed

    # Tier 1: Try Spotify API with per-page error isolation
    try:
        results = sp.playlist_tracks(playlist_id)
        sp_tracks.extend(parse_items(results.get("items", [])))
        while results.get("next"):
            try:
                results = sp.next(results)
                sp_tracks.extend(parse_items(results.get("items", [])))
            except Exception as page_err:
                emit("progress", message=f"Pagination error after {len(sp_tracks)} tracks: {page_err}")
                break
    except Exception as e:
        emit("progress", message=f"Spotify API error: {e}. Trying fallbacks...")
        # Tier 1 failed entirely — try inline tracks from sp.playlist() response
        tracks_obj = data.get("tracks") or {}
        if isinstance(tracks_obj, dict):
            sp_tracks.extend(parse_items(tracks_obj.get("items", [])))

    # Tier 2: Detect truncation and try embed scraper for the full list
    expected_total = (data.get("tracks") or {}).get("total", 0)
    if expected_total and len(sp_tracks) < expected_total:
        emit("progress", message=f"Got {len(sp_tracks)}/{expected_total} from API. Trying embed scraper...")
        from utils import fetch_playlist_tracks_from_embed

        embed_tracks = fetch_playlist_tracks_from_embed(playlist_id)
        if len(embed_tracks) > len(sp_tracks):
            sp_tracks = [
                {
                    "artist": et.get("artist", ""),
                    "title": et.get("title", ""),
                    "album": "",
                    "duration_ms": et.get("duration_ms", 0),
                }
                for et in embed_tracks
            ]
            emit("progress", message=f"Embed scraper returned {len(sp_tracks)} tracks")

    # Tier 3: Last resort — embed scraper if nothing else worked
    if not sp_tracks:
        from utils import fetch_playlist_tracks_from_embed

        embed_tracks = fetch_playlist_tracks_from_embed(playlist_id)
        for et in embed_tracks:
            sp_tracks.append(
                {
                    "artist": et.get("artist", ""),
                    "title": et.get("title", ""),
                    "album": "",
                    "duration_ms": et.get("duration_ms", 0),
                }
            )

    if not sp_tracks:
        emit("error", message="No tracks found in playlist")
        sys.exit(1)

    emit(
        "progress",
        message=f"Fetched {len(sp_tracks)} tracks. Scanning local library...",
    )

    # Scan local library
    local_tracks = get_local_tracks(args.library_path)
    emit("progress", message=f"Found {len(local_tracks)} local tracks. Matching...")

    # Match each Spotify track to local library
    matched = []
    missing = []
    total = len(sp_tracks)
    for i, sp_track in enumerate(sp_tracks, 1):
        if i % 5 == 1 or i == total:
            pct = int(i / total * 100)
            emit("progress", message=f"Matching {i}/{total}...", percent=pct)
        local_match, score = match_track(sp_track, local_tracks)
        if local_match:
            matched.append(
                {
                    "title": sp_track["title"],
                    "artist": sp_track["artist"],
                    "local_path": local_match["path"],
                    "score": round(score, 2),
                }
            )
        else:
            dur_ms = sp_track.get("duration_ms", 0)
            mins = dur_ms // 60000
            secs = (dur_ms % 60000) // 1000
            missing.append(
                {
                    "title": sp_track["title"],
                    "artist": sp_track["artist"],
                    "album": sp_track.get("album", ""),
                    "duration": f"{mins}:{secs:02d}",
                }
            )

    # Resolve Apple Music URLs for missing tracks
    if missing:
        import time
        from utils import resolve_apple_music_url

        emit("progress", message=f"Resolving {len(missing)} missing tracks on Apple Music...")
        failed_resolve = 0
        for i, m in enumerate(missing, 1):
            if i % 5 == 1 or i == len(missing):
                emit("progress", message=f"Resolving {i}/{len(missing)}...")
            am_url, am_artist, am_album = resolve_apple_music_url(m["artist"], m["title"], m.get("album", ""))
            if am_url:
                m["apple_music_url"] = am_url
                if am_album:
                    m["album"] = am_album
            else:
                failed_resolve += 1
            if i < len(missing):
                time.sleep(0.4)

        if failed_resolve > 0:
            emit("progress", message=f"{failed_resolve}/{len(missing)} tracks could not be found on Apple Music")

    emit(
        "result",
        name=playlist_name,
        artwork_url=artwork_url,
        total=len(sp_tracks),
        matched=matched,
        missing=missing,
    )


if __name__ == "__main__":
    main()
