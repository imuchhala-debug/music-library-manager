#!/usr/bin/env python3
"""
Fetch Spotify listening profile: top tracks, top artists, recently played.

Usage:
    python3 fetch_spotify_stats.py [--after TIMESTAMP_MS]

Emits JSON lines:
    {"status": "progress", "message": "..."}
    {"status": "result", "top_tracks": {...}, "top_artists": {...}, "recently_played": [...]}
"""

import json
import sys
import argparse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR.parent / "config"


def emit(status, **kwargs):
    msg = {"status": status, **kwargs}
    print(json.dumps(msg), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--after",
        type=int,
        default=0,
        help="Only return recently-played after this Unix timestamp (ms)",
    )
    args = parser.parse_args()

    # Load Spotify credentials
    config_path = CONFIG_DIR / "spotify.json"
    if not config_path.exists():
        emit("error", message="Spotify not configured. Set up config/spotify.json")
        sys.exit(1)

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        config = json.loads(config_path.read_text())
        cache_path = str(CONFIG_DIR / ".spotify_token")

        auth_manager = SpotifyOAuth(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            redirect_uri=config.get("redirect_uri", "http://127.0.0.1:8888/callback"),
            scope="playlist-read-private playlist-read-collaborative user-library-read user-top-read user-read-recently-played",
            cache_path=cache_path,
            open_browser=False,
        )

        token_info = auth_manager.cache_handler.get_cached_token()
        if not token_info:
            emit(
                "error",
                message="Spotify token expired. Re-authenticate: cd scripts && python3 -c \"import spotipy; from spotipy.oauth2 import SpotifyOAuth; SpotifyOAuth(client_id='...', scope='user-top-read user-read-recently-played', redirect_uri='http://127.0.0.1:8888/callback', cache_path='../config/.spotify_token').get_access_token(as_dict=False)\"",
            )
            sys.exit(1)

        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])

        sp = spotipy.Spotify(auth=token_info["access_token"])

    except ImportError:
        emit("error", message="spotipy not installed. Run: pip3 install spotipy")
        sys.exit(1)
    except Exception as e:
        emit("error", message=f"Spotify auth error: {e}")
        sys.exit(1)

    emit("progress", message="Fetching top tracks...")

    # Fetch top tracks (3 time ranges)
    top_tracks = {}
    for time_range in ["short_term", "medium_term", "long_term"]:
        try:
            result = sp.current_user_top_tracks(limit=50, time_range=time_range)
            items = []
            for i, t in enumerate(result.get("items", [])):
                artists = ", ".join(a["name"] for a in t.get("artists", []))
                album = t.get("album", {})
                items.append(
                    {
                        "rank": i + 1,
                        "name": t.get("name", ""),
                        "artist": artists,
                        "album": album.get("name", ""),
                        "duration_ms": t.get("duration_ms", 0),
                        "uri": t.get("uri", ""),
                        "artwork_url": album.get("images", [{}])[0].get("url", "")
                        if album.get("images")
                        else "",
                    }
                )
            top_tracks[time_range] = items
        except Exception as e:
            emit(
                "progress",
                message=f"Warning: could not fetch top tracks ({time_range}): {e}",
            )
            top_tracks[time_range] = []

    emit("progress", message="Fetching top artists...")

    # Fetch top artists (3 time ranges)
    top_artists = {}
    for time_range in ["short_term", "medium_term", "long_term"]:
        try:
            result = sp.current_user_top_artists(limit=50, time_range=time_range)
            items = []
            for i, a in enumerate(result.get("items", [])):
                items.append(
                    {
                        "rank": i + 1,
                        "name": a.get("name", ""),
                        "genres": a.get("genres", []),
                        "uri": a.get("uri", ""),
                        "artwork_url": a.get("images", [{}])[0].get("url", "")
                        if a.get("images")
                        else "",
                    }
                )
            top_artists[time_range] = items
        except Exception as e:
            emit(
                "progress",
                message=f"Warning: could not fetch top artists ({time_range}): {e}",
            )
            top_artists[time_range] = []

    emit("progress", message="Fetching recently played...")

    # Fetch recently played
    recently_played = []
    try:
        kwargs = {"limit": 50}
        if args.after > 0:
            kwargs["after"] = args.after

        result = sp.current_user_recently_played(**kwargs)
        for item in result.get("items", []):
            t = item.get("track", {})
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            album = t.get("album", {})
            recently_played.append(
                {
                    "title": t.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "duration_ms": t.get("duration_ms", 0),
                    "played_at": item.get("played_at", ""),
                    "uri": t.get("uri", ""),
                }
            )
    except Exception as e:
        emit("progress", message=f"Warning: could not fetch recently played: {e}")

    emit(
        "result",
        top_tracks=top_tracks,
        top_artists=top_artists,
        recently_played=recently_played,
    )


if __name__ == "__main__":
    main()
