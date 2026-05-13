#!/usr/bin/env python3
"""
Download tracks from Spotify URLs with full metadata tagging.

Uses the Spotify API for metadata, yt-dlp for audio, and mutagen for tagging.
Produces properly tagged ALAC files that import cleanly into Apple Music.

Usage:
    python scripts/spotify_download.py <spotify-url>
    python scripts/spotify_download.py "https://open.spotify.com/album/..."
    python scripts/spotify_download.py "https://open.spotify.com/track/..."
    python scripts/spotify_download.py "https://open.spotify.com/playlist/..."

Options:
    --codec CODEC   Audio codec (default: alac)
    --dry-run       Show what would be downloaded without downloading
    --yes           Skip confirmation prompt
    --verbose       Show detailed progress
    --force         Re-download even if file already exists
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import spotipy
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover
from spotipy.oauth2 import SpotifyOAuth

from utils import MUSIC_LIBRARY, PROJECT_ROOT, sanitize_filename

CACHE_DIR = PROJECT_ROOT / ".cache"
SPOTIFY_CONFIG = PROJECT_ROOT / "config" / "spotify.json"

SEARCH_COUNT = 10
DURATION_DELTA = 5  # seconds tolerance for YouTube duration matching

# yt-dlp codec mapping
CODEC_MAP = {
    "alac": {"codec": "alac", "ext": "m4a"},
    "aac": {"codec": "aac", "ext": "m4a"},
    "flac": {"codec": "flac", "ext": "flac"},
    "mp3": {"codec": "mp3", "ext": "mp3"},
}


def get_spotify_client():
    """Create authenticated Spotipy client using cached token."""
    if not SPOTIFY_CONFIG.exists():
        print(f"Error: {SPOTIFY_CONFIG} not found.")
        print("Copy config/spotify.json.example and fill in your credentials.")
        sys.exit(1)

    with open(SPOTIFY_CONFIG) as f:
        config = json.load(f)

    for key in ("client_id", "client_secret"):
        if not config.get(key) or config[key].startswith("your_"):
            print(f"Error: '{key}' not set in {SPOTIFY_CONFIG}")
            sys.exit(1)

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

    token_info = auth_manager.cache_handler.get_cached_token()
    if token_info:
        # Let spotipy handle refresh automatically
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
        return spotipy.Spotify(auth_manager=auth_manager)

    # No cached token — direct user to sync_all_playlists.py for initial auth
    print("No cached Spotify token found.")
    print("Run sync_all_playlists.py first to authenticate:")
    print("  python scripts/sync_all_playlists.py --auth-url <url>")
    sys.exit(1)


def parse_spotify_url(url):
    """Parse a Spotify URL and return (type, id). Type is 'track', 'album', or 'playlist'."""
    parsed = urlparse(url)

    # Handle open.spotify.com URLs
    if parsed.hostname and "spotify.com" in parsed.hostname:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[-2] in ("track", "album", "playlist"):
            return parts[-2], parts[-1].split("?")[0]

    # Handle spotify:type:id URIs
    if url.startswith("spotify:"):
        parts = url.split(":")
        if len(parts) >= 3 and parts[1] in ("track", "album", "playlist"):
            return parts[1], parts[2]

    print(f"Error: Could not parse Spotify URL: {url}")
    sys.exit(1)


def fetch_tracks(sp, url_type, url_id):
    """Fetch track metadata from Spotify API. Returns list of track dicts."""
    tracks = []

    if url_type == "track":
        t = sp.track(url_id)
        tracks.append(_extract_track(t, t["album"]))

    elif url_type == "album":
        album = sp.album(url_id)
        album_info = album
        # Paginate through album tracks
        results = album["tracks"]
        while True:
            for t in results["items"]:
                # Album track items don't include album info, so we pass it in
                tracks.append(_extract_track(t, album_info, is_album_track=True))
            if results["next"]:
                results = sp.next(results)
            else:
                break

    elif url_type == "playlist":
        results = sp.playlist_tracks(url_id)
        while True:
            for item in results["items"]:
                t = item.get("track")
                if not t or item.get("is_local"):
                    continue
                tracks.append(_extract_track(t, t.get("album")))
            if results["next"]:
                results = sp.next(results)
            else:
                break

    return tracks


def _extract_track(track, album_info, is_album_track=False):
    """Extract metadata dict from a Spotify track object."""
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    album_artists = ", ".join(a["name"] for a in album_info.get("artists", []))

    # Get cover art URL (largest available)
    images = album_info.get("images", [])
    cover_url = images[0]["url"] if images else None

    # Get total tracks from album
    total_tracks = album_info.get("total_tracks", 0)

    # Duration — album track items have duration_ms too
    duration_s = track.get("duration_ms", 0) / 1000

    return {
        "title": track.get("name", "Unknown"),
        "artists": artists,
        "album": album_info.get("name", "Unknown Album"),
        "album_artist": album_artists,
        "track_number": track.get("track_number", 1),
        "disc_number": track.get("disc_number", 1),
        "total_tracks": total_tracks,
        "release_date": album_info.get("release_date", ""),
        "cover_url": cover_url,
        "duration_s": duration_s,
        "artist_ids": [a["id"] for a in track.get("artists", []) if a.get("id")],
        "spotify_id": track.get("id", ""),
    }


def fetch_genres(sp, tracks):
    """Batch-fetch genres from artist endpoints and attach to tracks."""
    # Collect unique artist IDs
    all_artist_ids = set()
    for t in tracks:
        all_artist_ids.update(t["artist_ids"])

    # Fetch in batches of 50 (API limit)
    artist_genres = {}
    artist_ids = list(all_artist_ids)
    for i in range(0, len(artist_ids), 50):
        batch = artist_ids[i : i + 50]
        try:
            results = sp.artists(batch)
            for a in results.get("artists", []):
                if a:
                    artist_genres[a["id"]] = a.get("genres", [])
        except Exception:
            pass

    # Attach genres to tracks (use first artist's genres)
    for t in tracks:
        genres = []
        for aid in t["artist_ids"]:
            genres.extend(artist_genres.get(aid, []))
        # Deduplicate, title-case
        seen = set()
        unique = []
        for g in genres:
            gl = g.lower()
            if gl not in seen:
                seen.add(gl)
                unique.append(g.title())
        t["genre"] = ", ".join(unique[:3]) if unique else ""


def search_youtube(track, codec_info, verbose=False):
    """Search YouTube for a matching track and return the best video URL.

    Uses yt-dlp's duration filtering to find a video within ±5s of the Spotify duration.
    """
    query = f"{track['title']} {track['artists']} {track['album']}"
    search_url = f"ytsearch{SEARCH_COUNT}:{query}"

    duration = track["duration_s"]
    match_filter = None
    if duration > 0:
        match_filter = yt_dlp.utils.match_filter_func(
            f"duration>={duration - DURATION_DELTA} & duration<={duration + DURATION_DELTA}"
        )

    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "extract_flat": False,
        "match_filter": match_filter,
        "playlistend": SEARCH_COUNT,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get("entries", [])
            if entries:
                return entries[0]
    except Exception:
        pass

    # Fallback: search without duration filter
    if match_filter:
        opts["match_filter"] = None
        if verbose:
            print("    Retrying without duration filter...")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", [])
                if entries:
                    return entries[0]
        except Exception:
            pass

    return None


def download_audio(video_info, output_path, codec_info, verbose=False):
    """Download audio from a YouTube video using yt-dlp."""
    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "outtmpl": str(output_path) + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec_info["codec"],
                "preferredquality": "0",  # best quality
            }
        ],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_info["webpage_url"]])

    # Find the output file (yt-dlp appends the extension)
    expected = Path(str(output_path) + f".{codec_info['ext']}")
    if expected.exists():
        return expected

    # Sometimes yt-dlp uses a different extension, find it
    parent = output_path.parent
    stem = output_path.name
    for f in parent.iterdir():
        if f.name.startswith(stem) and f.suffix in (".m4a", ".mp3", ".flac", ".opus"):
            return f

    return None


def tag_file(filepath, track, cover_data=None):
    """Write MP4 metadata tags to an m4a file using mutagen."""
    audio = MP4(str(filepath))

    audio["\xa9nam"] = [track["title"]]
    audio["\xa9ART"] = [track["artists"]]
    audio["aART"] = [track["album_artist"]]
    audio["\xa9alb"] = [track["album"]]
    audio["trkn"] = [(track["track_number"], track["total_tracks"])]
    audio["disk"] = [(track["disc_number"], 0)]

    if track.get("release_date"):
        audio["\xa9day"] = [track["release_date"]]

    if track.get("genre"):
        audio["\xa9gen"] = [track["genre"]]

    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def build_final_path(track, codec_info):
    """Build the final file path matching gamdl convention."""
    album_artist = sanitize_filename(track["album_artist"])
    album = sanitize_filename(track["album"])
    title = sanitize_filename(track["title"])
    track_num = track["track_number"]
    disc_num = track["disc_number"]

    # Multi-disc: use disc-track format
    if disc_num > 1:
        filename = f"{disc_num}-{track_num:02d} {title}.{codec_info['ext']}"
    else:
        filename = f"{track_num:02d} {title}.{codec_info['ext']}"

    return MUSIC_LIBRARY / album_artist / album / filename


def download_cover(url):
    """Download cover art from URL and return bytes."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def process_track(track, codec_info, cover_data, args):
    """Download, tag, and move a single track. Returns True on success."""
    final_path = build_final_path(track, codec_info)

    label = f"{track['track_number']:02d}. {track['artists']} - {track['title']}"

    if final_path.exists() and not args.force:
        print(f"  SKIP  {label} (already exists)")
        return True

    if args.dry_run:
        print(f"  WOULD {label}")
        print(f"        -> {final_path}")
        return True

    print(f"  GET   {label}")

    # Search YouTube
    video = search_youtube(track, codec_info, verbose=args.verbose)
    if not video:
        print(f"  FAIL  {label} (no YouTube match)")
        return False

    if args.verbose:
        print(
            f"        Found: {video.get('title', '?')} [{video.get('duration', '?')}s]"
        )

    # Download to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "audio"
        try:
            downloaded = download_audio(
                video, tmp_path, codec_info, verbose=args.verbose
            )
        except Exception as e:
            print(f"  FAIL  {label} (download error: {e})")
            return False

        if not downloaded:
            print(f"  FAIL  {label} (no output file)")
            return False

        # Tag the file
        try:
            tag_file(downloaded, track, cover_data)
        except Exception as e:
            print(f"  WARN  {label} (tagging error: {e}, file still saved)")

        # Move to final location
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded), str(final_path))

    print(f"  OK    {label}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Download Spotify tracks with full metadata tagging."
    )
    parser.add_argument("url", help="Spotify URL (track, album, or playlist)")
    parser.add_argument(
        "--codec",
        default="alac",
        choices=CODEC_MAP.keys(),
        help="Audio codec (default: alac)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed progress"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if file already exists"
    )
    args = parser.parse_args()

    codec_info = CODEC_MAP[args.codec]

    # Parse URL
    url_type, url_id = parse_spotify_url(args.url)
    print(f"Fetching {url_type} metadata from Spotify...")

    # Auth & fetch
    sp = get_spotify_client()
    tracks = fetch_tracks(sp, url_type, url_id)

    if not tracks:
        print("No tracks found.")
        sys.exit(1)

    # Fetch genres
    fetch_genres(sp, tracks)

    # Preview
    album_label = tracks[0]["album"] if url_type == "album" else url_type
    print(f"\n{album_label} — {len(tracks)} track(s)\n")

    # Check how many already exist
    existing = sum(1 for t in tracks if build_final_path(t, codec_info).exists())
    if existing and not args.force:
        print(f"({existing} already in library, will be skipped)\n")

    if not args.yes and not args.dry_run:
        for t in tracks:
            path = build_final_path(t, codec_info)
            status = "EXISTS" if path.exists() else "NEW"
            print(
                f"  [{status}] {t['track_number']:02d}. {t['artists']} - {t['title']}"
            )
        print()
        try:
            answer = input(
                f"Download {len(tracks)} track(s) as {args.codec.upper()}? [Y/n] "
            )
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer.strip().lower() in ("n", "no"):
            print("Aborted.")
            sys.exit(0)

    # Download cover art once (shared across album)
    cover_data = None
    cover_url = tracks[0].get("cover_url")
    if cover_url:
        cover_data = download_cover(cover_url)
        if args.verbose:
            print(
                f"Downloaded cover art ({len(cover_data)} bytes)"
                if cover_data
                else "No cover art"
            )

    # Process tracks
    print()
    success = 0
    failed = 0
    skipped = 0

    for track in tracks:
        final_path = build_final_path(track, codec_info)
        if final_path.exists() and not args.force:
            skipped += 1
            result = True
        else:
            result = process_track(track, codec_info, cover_data, args)

        if result:
            success += 1
        else:
            failed += 1

    # Summary
    print(f"\nDone: {success} OK, {failed} failed, {skipped} skipped")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
