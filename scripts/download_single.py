#!/usr/bin/env python3
"""
Unified download entry point for MusicDrop app.
Detects link type, downloads in ALAC, outputs JSON progress lines.
Supports --preview mode for fetching metadata without downloading.

Usage:
    python3 download_single.py "https://music.apple.com/us/album/..."
    python3 download_single.py "https://youtube.com/watch?v=..." --artist "X" --album "Y"
    python3 download_single.py --preview "https://music.apple.com/us/album/..."
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
AMD_DIR = PROJECT_ROOT / "AppleMusicDecrypt"


def emit(status, **kwargs):
    """Print a JSON progress line for the Swift app to parse."""
    msg = {"status": status, **kwargs}
    print(json.dumps(msg), flush=True)


def is_apple_music(url):
    return "music.apple.com" in url or "itunes.apple.com" in url


def is_spotify(url):
    return "open.spotify.com" in url


def is_youtube(url):
    return any(d in url for d in ["youtube.com", "youtu.be", "soundcloud.com"])


def check_drive(drive_path):
    if not Path(drive_path).exists():
        emit("error", message="Drive not connected")
        sys.exit(1)
    stat = os.statvfs(drive_path)
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
    if free_gb < 1:
        emit("error", message=f"Low disk space: {free_gb:.1f} GB remaining")
        sys.exit(1)


def _find_bin(name):
    """Find a binary by checking common paths."""
    for prefix in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]:
        path = os.path.join(prefix, name)
        if os.path.isfile(path):
            return path
    return name  # fall back to bare name (rely on PATH)


def check_docker():
    docker = _find_bin("docker")
    colima = _find_bin("colima")
    try:
        result = subprocess.run(
            [
                docker,
                "ps",
                "--filter",
                "name=wrapper-manager",
                "--format",
                "{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "Up" in result.stdout:
            return True
    except Exception:
        pass
    emit("info", message="Starting download service...")
    try:
        subprocess.run(
            [colima, "start", "--cpu", "2", "--memory", "4"],
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            [docker, "start", "wrapper-manager"], capture_output=True, timeout=15
        )
        return True
    except Exception as e:
        emit("error", message=f"Could not start download service: {e}")
        return False


# MARK: - Preview


def _scrape_playlist_preview(url):
    """Scrape Apple Music playlist page for metadata via og/music meta tags."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Parse meta tags
    def meta(prop):
        m = re.search(rf'<meta property="{re.escape(prop)}" content="([^"]*)"', html)
        return m.group(1) if m else None

    def meta_all(prop):
        return re.findall(rf'<meta property="{re.escape(prop)}" content="([^"]*)"', html)

    og_title = meta("og:title") or ""
    # "Unmatched 🤭 by ishi on Apple Music" -> name = "Unmatched 🤭", artist = "ishi"
    title_match = re.match(r"(.+?) by (.+?) on Apple Music", og_title)
    if title_match:
        name, artist = title_match.group(1), title_match.group(2)
    else:
        name, artist = og_title.replace(" on Apple Music", ""), "Apple Music"

    artwork = meta("og:image")
    if artwork:
        artwork = artwork.replace("1200x630wp-60.jpg", "600x600bb.jpg")

    # Parse tracks from music:song meta tags
    song_urls = meta_all("music:song")
    durations = meta_all("music:song:duration")
    track_nums = meta_all("music:song:track")

    tracks = []
    for i, song_url in enumerate(song_urls):
        # Extract song name from URL slug: /song/song-name-here/123 -> "Song Name Here"
        slug_match = re.search(r"/song/([^/]+)/\d+", song_url)
        title = slug_match.group(1).replace("-", " ").title() if slug_match else f"Track {i+1}"

        # Parse ISO 8601 duration: PT2M56S -> "2:56"
        dur = durations[i] if i < len(durations) else ""
        dur_match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur)
        if dur_match:
            h, m, s = (int(x or 0) for x in dur_match.groups())
            mins = h * 60 + m
            duration = f"{mins}:{s:02d}"
        else:
            duration = "0:00"

        number = int(track_nums[i]) if i < len(track_nums) else i + 1
        tracks.append({"number": number, "title": title, "duration": duration})

    return name, artist, artwork, tracks


def preview_apple_music(url):
    """Fetch album/playlist metadata from iTunes API without downloading."""
    # Check if this is a playlist URL
    if "/playlist/" in url:
        try:
            name, artist, artwork, tracks = _scrape_playlist_preview(url)
            emit(
                "preview",
                artist=artist,
                album=name,
                artwork_url=artwork,
                tracks=tracks,
                explicit=False,
            )
        except Exception as e:
            emit("error", message=f"Could not fetch playlist info: {e}")
        return

    # Extract album ID from URL
    match = re.search(r"/album/[^/]*/(\d+)", url) or re.search(r"/album/(\d+)", url)
    if not match:
        emit("error", message="Could not parse album ID from URL")
        return

    album_id = match.group(1)
    api_url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song&limit=200"

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        emit("error", message=f"iTunes API error: {e}")
        return

    results = data.get("results", [])
    if not results:
        emit("error", message="Album not found on Apple Music")
        return

    # First result is the album/collection, rest are tracks
    album_info = results[0]
    tracks = [r for r in results[1:] if r.get("wrapperType") == "track"]

    artwork = album_info.get("artworkUrl100", "").replace("100x100", "600x600")

    preview_tracks = []
    for t in tracks:
        duration_ms = t.get("trackTimeMillis", 0)
        mins = duration_ms // 60000
        secs = (duration_ms % 60000) // 1000
        preview_tracks.append(
            {
                "number": t.get("trackNumber", 0),
                "title": t.get("trackName", ""),
                "duration": f"{mins}:{secs:02d}",
            }
        )

    emit(
        "preview",
        artist=album_info.get("artistName", ""),
        album=album_info.get("collectionName", ""),
        artwork_url=artwork,
        tracks=preview_tracks,
        explicit=album_info.get("collectionExplicitness") == "explicit",
    )


def preview_youtube(url):
    """Fetch video info from YouTube without downloading."""
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        chapters = None
        if info.get("chapters"):
            chapters = [
                {"title": ch["title"], "start_time": ch["start_time"]}
                for ch in info["chapters"]
            ]

        emit(
            "preview",
            title=info.get("title", ""),
            chapters=chapters,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
        )
    except Exception as e:
        emit("error", message=f"Could not fetch video info: {e}")


def preview_spotify(url):
    """Fetch album/track/playlist metadata from Spotify API."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        config_path = SCRIPT_DIR.parent / "config" / "spotify.json"
        if not config_path.exists():
            emit("error", message="Spotify not configured. Set up config/spotify.json")
            return

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
        token_info = auth_manager.cache_handler.get_cached_token()
        if not token_info:
            emit(
                "error",
                message="Spotify token expired. Re-authenticate by running: python3 scripts/sync_spotify_to_vinyl.py --auth",
            )
            return
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
        sp = spotipy.Spotify(auth=token_info["access_token"])

        # Parse URL to determine type
        # e.g. open.spotify.com/album/xxx, /track/xxx, /playlist/xxx
        # Strip query params from URL for cleaner ID parsing
        clean_url = url.split("?")[0]

        if "/album/" in url:
            match = re.search(r"/album/([a-zA-Z0-9]+)", clean_url)
            if not match:
                emit("error", message="Could not parse Spotify album ID")
                return
            data = sp.album(match.group(1))
            tracks = []
            for i, t in enumerate(data["tracks"]["items"], 1):
                dur_ms = t.get("duration_ms", 0)
                mins = dur_ms // 60000
                secs = (dur_ms % 60000) // 1000
                tracks.append(
                    {"number": i, "title": t["name"], "duration": f"{mins}:{secs:02d}"}
                )
            artwork = data["images"][0]["url"] if data.get("images") else None
            emit(
                "preview",
                type="album",
                name=data["name"],
                artist=data["artists"][0]["name"],
                artwork_url=artwork,
                tracks=tracks,
                spotify_url=url,
                explicit=any(t.get("explicit", False) for t in data["tracks"]["items"]),
            )

        elif "/track/" in url:
            match = re.search(r"/track/([a-zA-Z0-9]+)", clean_url)
            if not match:
                emit("error", message="Could not parse Spotify track ID")
                return
            data = sp.track(match.group(1))
            dur_ms = data.get("duration_ms", 0)
            mins = dur_ms // 60000
            secs = (dur_ms % 60000) // 1000
            emit(
                "preview",
                type="track",
                name=data["name"],
                artist=data["artists"][0]["name"],
                artwork_url=data["album"]["images"][0]["url"]
                if data["album"].get("images")
                else None,
                tracks=[
                    {
                        "number": 1,
                        "title": data["name"],
                        "duration": f"{mins}:{secs:02d}",
                    }
                ],
                spotify_url=url,
                explicit=data.get("explicit", False),
            )

        elif "/playlist/" in url:
            match = re.search(r"/playlist/([a-zA-Z0-9]+)", clean_url)
            if not match:
                emit("error", message="Could not parse Spotify playlist ID")
                return
            playlist_id = match.group(1)
            data = sp.playlist(playlist_id)

            # Try API response first, fall back to embed scrape
            tracks_obj = data.get("tracks") or {}
            raw_items = tracks_obj.get("items", []) if isinstance(tracks_obj, dict) else []
            total_tracks = tracks_obj.get("total", 0) if isinstance(tracks_obj, dict) else 0

            tracks = []
            if raw_items:
                for i, item in enumerate(raw_items[:50], 1):
                    t = item.get("track")
                    if not t or not isinstance(t, dict):
                        continue
                    name = t.get("name", "")
                    dur_ms = t.get("duration_ms", 0)
                    mins = dur_ms // 60000
                    secs = (dur_ms % 60000) // 1000
                    tracks.append(
                        {"number": i, "title": name, "duration": f"{mins}:{secs:02d}"}
                    )
            else:
                # Spotify API restricted — fetch via embed page
                from utils import fetch_playlist_tracks_from_embed

                embed_tracks = fetch_playlist_tracks_from_embed(playlist_id)
                total_tracks = len(embed_tracks)
                for i, et in enumerate(embed_tracks[:50], 1):
                    dur_ms = et.get("duration_ms", 0)
                    mins = dur_ms // 60000
                    secs = (dur_ms % 60000) // 1000
                    tracks.append(
                        {"number": i, "title": et["title"], "duration": f"{mins}:{secs:02d}"}
                    )

            artwork = (
                data.get("images", [{}])[0].get("url") if data.get("images") else None
            )
            emit(
                "preview",
                type="playlist",
                name=data["name"],
                artist=data["owner"]["display_name"],
                artwork_url=artwork,
                tracks=tracks,
                total_tracks=total_tracks,
                spotify_url=url,
                explicit=False,
            )
        else:
            emit("error", message="Unsupported Spotify URL type")

    except ImportError:
        emit("error", message="spotipy not installed. Run: pip3 install spotipy")
    except Exception as e:
        emit("error", message=f"Spotify API error: {e}")


def download_spotify(
    url, artist=None, album=None, drive_path="/Volumes/One Touch /music library"
):
    """Download from Spotify via spotify_download.py (YouTube audio source)."""
    emit("info", message="Downloading via Spotify (YouTube audio source)...")

    cmd = [
        "python3",
        str(SCRIPT_DIR / "spotify_download.py"),
        url,
        "--output",
        drive_path,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    saved_files = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        # Try to parse JSON progress from spotify_download.py
        try:
            msg = json.loads(line)
            if msg.get("status"):
                print(line, flush=True)  # Forward JSON to Swift
                if msg["status"] == "saved":
                    saved_files.append(msg.get("track", ""))
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: parse human-readable output
        if "Downloaded" in line or "Saved" in line:
            emit("saved", track=line[:100])
            saved_files.append(line)
        elif "Downloading" in line or "Searching" in line:
            emit("downloading", message=line[:100])
        elif "Error" in line or "error" in line.lower():
            emit("warning", message=line[:200])

    proc.wait()
    return saved_files


# MARK: - Download


def download_apple_music(url):
    emit("info", message="Downloading from Apple Music (ALAC lossless)...")

    cmd = [_find_bin("poetry"), "run", "python", "batch_download.py", url]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(AMD_DIR),
    )

    saved_files = []
    for line in proc.stdout:
        line = line.strip()
        if "Start ripping" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Start ripping", line)
            if match:
                emit("downloading", track=match.group(1))
            match = re.search(r"ALBUM \| (.+?) \| INFO - Start ripping", line)
            if match:
                emit("downloading", album=match.group(1))
        elif "Selected codec" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Selected codec: (.+)", line)
            if match:
                emit("decrypting", track=match.group(1), codec=match.group(2))
        elif "Song saved" in line:
            match = re.search(r"SONG \| (.+?) \| SUCCESS", line)
            if match:
                emit("saved", track=match.group(1))
                saved_files.append(match.group(1))
        elif "already exist" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Song already", line)
            if match:
                emit("skipped", track=match.group(1))
        elif "Error" in line or "error" in line.lower():
            if "fork_posix" not in line and "WARNING" not in line:
                emit("warning", message=line[:200])

    proc.wait()
    return saved_files


def download_youtube(url, artist, album):
    emit("info", message=f"Downloading from YouTube: {artist} - {album}")

    cmd = [
        "python3",
        str(SCRIPT_DIR / "yt_download.py"),
        url,
        "--artist",
        artist,
        "--album",
        album,
        "--yes",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    saved_files = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("  OK"):
            emit("saved", track=line.replace("  OK    ", ""))
            saved_files.append(line)
        elif line.startswith("  GET"):
            emit("downloading", track=line.replace("  GET   ", ""))
        elif line.startswith("  FAIL"):
            emit("warning", message=line)

    proc.wait()
    return saved_files


# MARK: - Main


def main():
    parser = argparse.ArgumentParser(description="MusicDrop download bridge")
    parser.add_argument("url", help="Apple Music or YouTube URL")
    parser.add_argument("--artist", help="Artist name (for YouTube)")
    parser.add_argument("--album", help="Album name (for YouTube)")
    parser.add_argument(
        "--preview", action="store_true", help="Fetch metadata only, don't download"
    )
    parser.add_argument(
        "--drive-path",
        default="/Volumes/One Touch /music library",
        help="Download destination",
    )
    parser.add_argument("--codec", default="alac", help="Audio codec")
    args = parser.parse_args()

    if args.preview:
        if is_apple_music(args.url):
            preview_apple_music(args.url)
        elif is_spotify(args.url):
            preview_spotify(args.url)
        elif is_youtube(args.url):
            preview_youtube(args.url)
        else:
            emit("error", message="Unsupported URL")
        return

    # Pre-checks
    check_drive(args.drive_path)
    if is_apple_music(args.url):
        if not check_docker():
            sys.exit(1)

    # Download
    if is_apple_music(args.url):
        saved = download_apple_music(args.url)
        emit("complete", source="apple_music", tracks_saved=len(saved))
    elif is_spotify(args.url):
        saved = download_spotify(args.url, args.artist, args.album, args.drive_path)
        emit("complete", source="spotify", tracks_saved=len(saved))
    elif is_youtube(args.url):
        if not args.artist or not args.album:
            emit("error", message="YouTube downloads require --artist and --album")
            sys.exit(1)
        saved = download_youtube(args.url, args.artist, args.album)
        emit("complete", source="youtube", tracks_saved=len(saved))
    else:
        emit(
            "error",
            message="Unsupported URL. Use Apple Music, Spotify, or YouTube links.",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
