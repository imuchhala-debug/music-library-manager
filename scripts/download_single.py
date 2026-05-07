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
            [docker, "ps", "--filter", "name=wrapper-manager", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        if "Up" in result.stdout:
            return True
    except Exception:
        pass
    emit("info", message="Starting download service...")
    try:
        subprocess.run([colima, "start", "--cpu", "2", "--memory", "4"], capture_output=True, timeout=60)
        subprocess.run([docker, "start", "wrapper-manager"], capture_output=True, timeout=15)
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

            # Spotify dev mode often returns tracks=None; scrape real total from page
            if not total_tracks:
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    og_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(og_req, timeout=10, context=ctx) as og_resp:
                        og_html = og_resp.read().decode("utf-8", errors="replace")
                    og_match = re.search(r'<meta property="og:description" content="[^"]*?(\d+)\s+items', og_html)
                    if og_match:
                        total_tracks = int(og_match.group(1))
                except Exception:
                    pass

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
                if not total_tracks:
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


def _fetch_track_metadata(track_url):
    """Look up (title, artist) for an Apple Music song URL via iTunes API.
    Song URLs contain `?i=<adam_id>`; album URLs don't. Returns None if not a song URL or if
    lookup fails.
    """
    m = re.search(r"[?&]i=(\d+)", track_url)
    if not m:
        return None
    song_id = m.group(1)
    lookup_url = f"https://itunes.apple.com/lookup?id={song_id}"
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(lookup_url, headers={"User-Agent": "Vinyl/1.0"})
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read())
        if data.get("resultCount", 0) > 0:
            r = data["results"][0]
            return {
                "url": track_url,
                "title": r.get("trackName", ""),
                "artist": r.get("artistName", ""),
            }
    except Exception:
        pass
    return None


def _itunes_lookup(lookup_id):
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            f"https://itunes.apple.com/lookup?id={lookup_id}",
            headers={"User-Agent": "Vinyl/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if data.get("resultCount", 0) == 0:
        return None
    return data["results"][0]


def _fetch_artwork(url):
    """Resolve album cover art via iTunes API for either a song URL (?i=<id>) or an
    album URL (/album/.../<id>). Returns {"artwork_url", "artist", "album"} or None.
    Used to populate the download row thumbnail before any track downloads.
    Song-ID lookup is tried first; falls back to album-ID from the URL if empty.
    """
    candidate_ids = []
    song_m = re.search(r"[?&]i=(\d+)", url)
    if song_m:
        candidate_ids.append(song_m.group(1))
    album_m = re.search(r"/album/[^/]*/(\d+)", url) or re.search(r"/album/(\d+)", url)
    if album_m:
        candidate_ids.append(album_m.group(1))
    for lookup_id in candidate_ids:
        r = _itunes_lookup(lookup_id)
        if not r:
            continue
        art = r.get("artworkUrl100") or r.get("artworkUrl60")
        if not art:
            continue
        art = art.replace("100x100bb", "600x600bb").replace("100x100", "600x600")
        return {
            "artwork_url": art,
            "artist": r.get("artistName") or r.get("collectionArtistName"),
            "album": r.get("collectionName") or r.get("trackName"),
        }
    return None


def _normalize_failure_reason(raw):
    """Map a batch_download log excerpt to a normalized failure-reason token.
    Swift maps this to human-readable text. Raw message is preserved in `detail`.
    """
    low = raw.lower()
    if "not found" in low or "not available" in low or "no such song" in low:
        return "not_found"
    if "integrity" in low:
        return "integrity_check"
    if "no active subscription" in low or "no subscription" in low:
        return "no_subscription"
    if "region" in low and ("locked" in low or "not available" in low):
        return "region_locked"
    if "no available instance" in low or "wrapper" in low:
        return "no_wrapper"
    if "decrypt" in low or "m3u8" in low or "stream" in low:
        return "decrypt_failed"
    return "other"


def download_apple_music(urls, drive_path=None):
    """Download one or more Apple Music URLs. Accepts a string or list of strings."""
    if isinstance(urls, str):
        urls = [urls]
    emit("info", message=f"Downloading {len(urls)} item(s) from Apple Music (ALAC lossless)...")

    # Emit artwork early so the Swift download row can show real cover art instead of
    # a placeholder. First URL that resolves wins (playlists fall back to first track's album).
    for u in urls:
        art = _fetch_artwork(u)
        if art:
            emit(
                "album_metadata",
                url=u,
                artwork_url=art["artwork_url"],
                artist=art.get("artist"),
                album=art.get("album"),
            )
            break

    # Update config.toml output path if a custom drive path is provided.
    # Playlist tracks go into the same {album_artist}/{album} folders as regular album
    # downloads so Vinyl's library scanner sees them with correct artist/album (folder
    # structure is canonical). Vinyl tracks playlist membership via playlists.json.
    if drive_path:
        config_path = AMD_DIR / "config.toml"
        try:
            lines = config_path.read_text().splitlines()
            new_lines = []
            for line in lines:
                if line.strip().startswith("dirPathFormat") and "playlist" not in line.lower():
                    new_lines.append(f'dirPathFormat = "{drive_path}/{{album_artist}}/{{album}}"')
                elif line.strip().startswith("playlistDirPathFormat"):
                    new_lines.append(f'playlistDirPathFormat = "{drive_path}/{{album_artist}}/{{album}}"')
                elif line.strip().startswith("playlistSongNameFormat"):
                    new_lines.append('playlistSongNameFormat = "{disk}-{tracknum:02d} {title}"')
                else:
                    new_lines.append(line)
            config_path.write_text("\n".join(new_lines) + "\n")
        except Exception as e:
            emit("warning", message=f"Could not update config path: {e}")

    # Detect playlist URL → remember name + start time so we can emit all saved paths
    # at batch end. Vinyl uses this to create a Playlist record pointing at the saved tracks.
    import time
    batch_start = time.time()
    playlist_name = None
    for u in urls:
        m = re.search(r"/playlist/([^/]+)/pl\.", u)
        if m:
            slug = urllib.parse.unquote(m.group(1))
            # Convert URL slug "my-chill-vibes" → "my chill vibes"; leave all-lowercase since
            # real names often are (e.g. "scaping"). Vinyl can rename later.
            playlist_name = slug.replace("-", " ")
            break

    # Pre-fetch per-track metadata so we can emit `track_queued` up-front and maintain a mapping
    # from the "Artist - Title" key emitted in batch_download logs back to the source URL. Album
    # URLs (no `?i=` param) are skipped here; their songs surface via log events without URLs.
    meta_by_key = {}
    meta_by_url = {}
    for u in urls:
        meta = _fetch_track_metadata(u)
        if meta:
            key = f"{meta['artist']} - {meta['title']}".lower()
            meta_by_key[key] = meta
            meta_by_url[u] = meta
            emit("track_queued", url=meta["url"], track=meta["title"], artist=meta["artist"])

    def _url_for(song_tag):
        """song_tag is the "Artist - Title" string captured from batch_download logs."""
        m = meta_by_key.get(song_tag.lower())
        return (m or {}).get("url", "")

    cmd = [_find_bin("poetry"), "run", "python", "batch_download.py"] + urls
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(AMD_DIR),
    )

    saved_files = []
    started_keys = set()
    terminal_keys = set()  # saved, skipped, or failed

    # Stall watchdog: if no stdout activity for STALL_TIMEOUT seconds, kill the batch.
    # Protects against wrapper-manager m3u8→decrypt hangs where batch_download.py blocks
    # on gRPC forever and never exits — leaving Vinyl's queue deadlocked.
    import threading
    STALL_TIMEOUT = 120.0
    last_event = [time.time()]
    stall_triggered = [False]

    def watchdog():
        while proc.poll() is None:
            time.sleep(5)
            if time.time() - last_event[0] > STALL_TIMEOUT:
                stall_triggered[0] = True
                emit("warning", message=f"Download stalled — no progress for {int(STALL_TIMEOUT)}s. Killing batch.")
                try:
                    proc.terminate()
                    time.sleep(5)
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                return
    threading.Thread(target=watchdog, daemon=True).start()

    for line in proc.stdout:
        last_event[0] = time.time()
        line = line.strip()
        if "Start ripping" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Start ripping", line)
            if match:
                tag = match.group(1)
                started_keys.add(tag.lower())
                emit("downloading", track=tag)
                emit("track_downloading", url=_url_for(tag), track=tag)
            match = re.search(r"ALBUM \| (.+?) \| INFO - Start ripping", line)
            if match:
                emit("downloading", album=match.group(1))
        elif "Selected codec" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Selected codec: (.+)", line)
            if match:
                tag = match.group(1)
                emit("decrypting", track=tag, codec=match.group(2))
        elif "Song saved" in line:
            match = re.search(r"SONG \| (.+?) \| SUCCESS", line)
            if match:
                tag = match.group(1)
                terminal_keys.add(tag.lower())
                emit("saved", track=tag)
                emit("track_saved", url=_url_for(tag), track=tag)
                saved_files.append(tag)
        elif "already exist" in line:
            match = re.search(r"SONG \| (.+?) \| INFO - Song already", line)
            if match:
                tag = match.group(1)
                terminal_keys.add(tag.lower())
                emit("skipped", track=tag)
                emit("track_skipped", url=_url_for(tag), track=tag)
        elif "Error" in line or "error" in line.lower():
            if "fork_posix" in line or "WARNING" in line:
                continue
            # Per-song error: "SONG | Artist - Title | ERROR - message"
            per_song = re.search(r"SONG \| (.+?) \| ERROR - (.+)", line)
            if per_song:
                tag = per_song.group(1)
                raw_reason = per_song.group(2)
                terminal_keys.add(tag.lower())
                reason = _normalize_failure_reason(raw_reason)
                emit("warning", message=f"Error processing song | {tag} | {raw_reason}"[:200])
                emit("track_failed", url=_url_for(tag), track=tag,
                     reason=reason, detail=raw_reason[:200])
            else:
                emit("warning", message=line[:200])

    proc.wait()

    # Surface tracks that were pre-queued but never terminated. Three buckets:
    #   stall-triggered           → decrypt_stall  (watchdog killed the subprocess)
    #   started-but-not-finished  → decrypt_failed (mid-stream crash)
    #   never-started             → no_wrapper     (queue never reached them)
    stall_detail = ("Download stalled for over 2 minutes and was terminated. "
                    "Usually means the wrapper subprocess crashed or the decrypt hung.")
    for key, meta in meta_by_key.items():
        if key in terminal_keys:
            continue
        if stall_triggered[0] and key in started_keys:
            emit("track_failed", url=meta["url"], track=meta["title"],
                 reason="decrypt_stall", detail=stall_detail)
        elif key in started_keys:
            emit("track_failed", url=meta["url"], track=meta["title"],
                 reason="decrypt_failed",
                 detail="Track started but did not complete before batch exited.")
        else:
            emit("track_failed", url=meta["url"], track=meta["title"],
                 reason="no_wrapper",
                 detail="Track was queued but the batch never processed it.")

    # Playlist URL? Emit every .m4a written to drive_path during this batch so Vinyl can
    # create a Playlist record pointing at them. Ordered by mtime (≈ playlist order since
    # batch_download processes tracks sequentially). Skips files that pre-existed the batch.
    if playlist_name and drive_path:
        drive_root = Path(drive_path)
        saved_paths = []
        if drive_root.is_dir():
            for p in drive_root.rglob("*.m4a"):
                try:
                    if p.stat().st_mtime >= batch_start:
                        saved_paths.append((p.stat().st_mtime, str(p)))
                except OSError:
                    continue
        saved_paths.sort(key=lambda x: x[0])
        emit("playlist_complete", name=playlist_name,
             paths=[p for _, p in saved_paths])

    # `any_started` lets caller distinguish wrapper-down (zero "Start ripping" lines) from
    # wrapper-side decrypt failure (started but didn't save). Vinyl maps the former to the
    # re-auth sheet and the latter to a retry-this-album hint.
    return saved_files, bool(started_keys)


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
    parser.add_argument("urls", nargs="+", help="Apple Music, Spotify, or YouTube URL(s)")
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
        url = args.urls[0]
        if is_apple_music(url):
            preview_apple_music(url)
        elif is_spotify(url):
            preview_spotify(url)
        elif is_youtube(url):
            preview_youtube(url)
        else:
            emit("error", message="Unsupported URL")
        return

    # Pre-checks
    check_drive(args.drive_path)

    # Separate URLs by type
    am_urls = [u for u in args.urls if is_apple_music(u)]
    spotify_urls = [u for u in args.urls if is_spotify(u)]
    youtube_urls = [u for u in args.urls if is_youtube(u)]
    unknown_urls = [u for u in args.urls if u not in am_urls + spotify_urls + youtube_urls]

    if unknown_urls:
        emit("error", message=f"Unsupported URL(s): {', '.join(unknown_urls[:3])}")
        sys.exit(1)

    # Apple Music batch
    if am_urls:
        if not check_docker():
            sys.exit(1)
        saved, any_started = download_apple_music(am_urls, args.drive_path)
        if saved:
            emit("complete", source="apple_music", tracks_saved=len(saved))
        else:
            if not any_started:
                emit("error", reason="no_wrapper",
                     message="Wrapper service unavailable — re-authenticate Apple Music")
            else:
                emit("error", reason="decrypt_failed",
                     message="Decrypt failed for all tracks — try again or restart Docker")
            sys.exit(1)

    # Spotify (single URL)
    for url in spotify_urls:
        saved = download_spotify(url, args.artist, args.album, args.drive_path)
        if saved:
            emit("complete", source="spotify", tracks_saved=len(saved))
        else:
            emit("error", message="No tracks downloaded")

    # YouTube (single URL)
    for url in youtube_urls:
        if not args.artist or not args.album:
            emit("error", message="YouTube downloads require --artist and --album")
            sys.exit(1)
        saved = download_youtube(url, args.artist, args.album)
        if saved:
            emit("complete", source="youtube", tracks_saved=len(saved))
        else:
            emit("error", message="No tracks downloaded")


if __name__ == "__main__":
    main()
