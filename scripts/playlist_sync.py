#!/usr/bin/env python3
"""
Sync a single Spotify playlist (by URL) to a local M3U8 file.
Matches Spotify tracks against local library and creates a playlist file.

Usage: playlist_sync.py <spotify-playlist-url>
"""

import json
import subprocess
import sys

from utils import (
    get_local_tracks,
    match_track,
    create_m3u8,
)


def fetch_spotify_playlist(url):
    """Fetch playlist tracks using spotdl's metadata fetching."""
    try:
        result = subprocess.run(
            ["spotdl", "save", url, "--save-file", "/tmp/spotify_playlist.json"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"Error fetching playlist: {result.stderr}")
            return None, []

        with open("/tmp/spotify_playlist.json", "r") as f:
            data = json.load(f)

        playlist_name = "Spotify Playlist"
        tracks = []

        for item in data:
            tracks.append(
                {
                    "artist": item.get(
                        "artist",
                        item.get("artists", ["Unknown"])[0]
                        if isinstance(item.get("artists"), list)
                        else "Unknown",
                    ),
                    "title": item.get("name", item.get("title", "Unknown")),
                }
            )
            if "playlist" in item:
                playlist_name = item["playlist"]

        return playlist_name, tracks

    except Exception as e:
        print(f"Error: {e}")
        return None, []


def main():
    if len(sys.argv) < 2:
        print("Usage: playlist_sync.py <spotify-playlist-url>")
        print("\nThis tool:")
        print("  1. Fetches tracks from a Spotify playlist")
        print("  2. Matches them against your local library")
        print("  3. Creates an M3U8 playlist file")
        print("  4. Reports any missing tracks")
        sys.exit(1)

    url = sys.argv[1]

    if "spotify.com" not in url:
        print("Error: Please provide a Spotify playlist URL")
        sys.exit(1)

    print("Fetching Spotify playlist...")
    playlist_name, spotify_tracks = fetch_spotify_playlist(url)

    if not spotify_tracks:
        print("Failed to fetch playlist tracks")
        sys.exit(1)

    print(f"Found {len(spotify_tracks)} tracks in '{playlist_name}'")

    print("Scanning local library...")
    local_tracks = get_local_tracks()
    print(f"Found {len(local_tracks)} local tracks")

    print("\nMatching tracks...")
    matched = []
    missing = []

    for sp_track in spotify_tracks:
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
            print(f"  + {sp_track['artist']} - {sp_track['title']}")
        else:
            missing.append(sp_track)
            print(f"  - {sp_track['artist']} - {sp_track['title']}")

    if matched:
        playlist_path = create_m3u8(playlist_name, matched)
        print(f"\nCreated playlist: {playlist_path}")

    print("\nSummary:")
    print(f"   Matched: {len(matched)}/{len(spotify_tracks)}")
    print(f"   Missing: {len(missing)}")

    if missing:
        print("\nMissing tracks (download with 'music add <spotify-link>'):")
        for track in missing[:10]:
            print(f"   - {track['artist']} - {track['title']}")
        if len(missing) > 10:
            print(f"   ... and {len(missing) - 10} more")


if __name__ == "__main__":
    main()
