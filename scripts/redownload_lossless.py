#!/usr/bin/env python3
"""
Re-download the music library in lossless ALAC from Apple Music.

Scans the existing library for Artist/Album pairs, searches the iTunes API
for matching albums, and re-downloads them via gamdl in ALAC quality.

Usage:
    # Dry run — see what would be re-downloaded
    python scripts/redownload_lossless.py --dry-run

    # Re-download everything available on Apple Music
    python scripts/redownload_lossless.py

    # Re-download a specific artist only
    python scripts/redownload_lossless.py --artist "Kanye West"

    # Re-download a specific album only
    python scripts/redownload_lossless.py --artist "Radiohead" --album "OK Computer"

    # Show what's already lossless vs what needs re-downloading
    python scripts/redownload_lossless.py --status
"""

import argparse
import json
import re
import subprocess
import sys
import time
import ssl
import urllib.parse
import urllib.request

from utils import MUSIC_LIBRARY, PROJECT_ROOT

AMD_DIR = PROJECT_ROOT / "AppleMusicDecrypt"

# Audio files live inside music library/Music/ (gamdl's output structure)
AUDIO_LIBRARY = MUSIC_LIBRARY / "Music"

# Folders to skip (not real artist folders)
SKIP_FOLDERS = {
    "Automatically Add to Music.localized",
    "Compilations",
    "Playlists",
    ".DS_Store",
}


def get_library_albums(library_path=None, artist_filter=None, album_filter=None):
    """Scan library and return list of (artist, album, path) tuples."""
    library = library_path or AUDIO_LIBRARY
    albums = []

    for artist_dir in sorted(library.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name in SKIP_FOLDERS:
            continue
        if artist_filter and artist_dir.name != artist_filter:
            continue

        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue
            if album_dir.name == "[Uncategorized]":
                continue
            if album_filter and album_dir.name != album_filter:
                continue

            # Check it has audio files
            has_audio = any(
                f.suffix in (".m4a", ".mp3", ".flac")
                for f in album_dir.iterdir()
                if f.is_file()
            )
            if has_audio:
                albums.append((artist_dir.name, album_dir.name, album_dir))

    return albums


def check_codec(filepath):
    """Check audio codec of a file using afinfo. Returns codec name string."""
    try:
        result = subprocess.run(
            ["afinfo", str(filepath)], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if "Data format:" in line:
                if "alac" in line.lower() or "Apple Lossless" in line:
                    return "alac"
                elif "aac" in line.lower():
                    return "aac"
                elif "flac" in line.lower():
                    return "flac"
                elif "mp3" in line.lower() or "mpeg" in line.lower():
                    return "mp3"
                return line.split("Data format:")[-1].strip()
    except Exception:
        pass
    return "unknown"


def check_album_codec(album_path):
    """Check codec of the first audio file in an album folder."""
    for f in sorted(album_path.iterdir()):
        if f.suffix in (".m4a", ".mp3", ".flac") and f.is_file():
            return check_codec(f)
    return "unknown"


def _normalize(text):
    """Normalize text for fuzzy comparison."""
    text = text.lower()
    # Remove common suffixes/annotations
    for s in [
        "(deluxe)",
        "(deluxe edition)",
        "(deluxe version)",
        "(remastered)",
        "(remaster)",
        "(bonus track)",
        "(bonus track version)",
        "(expanded edition)",
        "(special edition)",
        "(2011 remaster)",
        "(2012 remastered)",
        "(2015 remaster)",
        "(2017 remaster)",
        "(2019 mix)",
        "(2022 mix)",
        "(2007 remaster)",
        "(remastered 2003)",
        "(live)",
        "(live acoustic)",
        "- single",
        "- ep",
        "(mono & stereo)",
    ]:
        text = text.replace(s, "")
    # Strip punctuation and extra whitespace
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _get_url(r):
    """Extract Apple Music URL from an iTunes API result."""
    collection_url = r.get("collectionViewUrl", "")
    if collection_url:
        return collection_url
    cid = r.get("collectionId")
    if cid:
        return f"https://music.apple.com/album/{cid}"
    return None


def _itunes_request(url):
    """Make a request to the iTunes API. Returns parsed JSON or None."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"    API error: {e}")
        return None


def _find_artist_id(artist):
    """Search for an artist and return their iTunes artist ID."""
    params = urllib.parse.urlencode(
        {
            "term": artist,
            "media": "music",
            "entity": "musicArtist",
            "limit": 5,
        }
    )
    data = _itunes_request(f"https://itunes.apple.com/search?{params}")
    if not data:
        return None

    artist_norm = _normalize(artist)
    for r in data.get("results", []):
        r_name = _normalize(r.get("artistName", ""))
        if _similarity(artist_norm, r_name) > 0.8:
            return r.get("artistId")

    # Fallback: first result if close enough
    results = data.get("results", [])
    if results:
        r_name = _normalize(results[0].get("artistName", ""))
        if _similarity(artist_norm, r_name) > 0.5:
            return results[0].get("artistId")

    return None


def _get_artist_albums(artist_id):
    """Look up all albums by an artist using the iTunes lookup API."""
    params = urllib.parse.urlencode(
        {
            "id": artist_id,
            "entity": "album",
            "limit": 200,
        }
    )
    data = _itunes_request(f"https://itunes.apple.com/lookup?{params}")
    if not data:
        return []

    # Filter to just albums (first result is the artist itself)
    return [
        r
        for r in data.get("results", [])
        if r.get("wrapperType") == "collection" and r.get("collectionType") == "Album"
    ]


def search_itunes(artist, album):
    """Search iTunes API for an album. Returns Apple Music URL or None.

    Two-step approach:
    1. Find the artist by name → get their artistId
    2. Look up all albums by that artist → match by album name
    Prefers explicit versions.
    """
    # Step 1: Find artist
    artist_id = _find_artist_id(artist)
    if not artist_id:
        return None

    # Step 2: Get all albums by this artist
    time.sleep(1)  # Rate limit
    albums = _get_artist_albums(artist_id)
    if not albums:
        return None

    album_norm = _normalize(album)

    # Score each album
    scored = []
    for r in albums:
        r_album = _normalize(r.get("collectionName", ""))
        score = _similarity(album_norm, r_album)
        if score < 0.45:
            continue

        is_explicit = r.get("collectionExplicitness") == "explicit"
        # Boost explicit versions
        final_score = score + (0.05 if is_explicit else 0)
        scored.append((final_score, is_explicit, r))

    if not scored:
        return None

    scored.sort(key=lambda x: (-x[0], not x[1]))
    best_score, best_explicit, best = scored[0]

    return _get_url(best)


def _similarity(a, b):
    """Simple similarity ratio between two strings."""
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def run_amd(apple_music_url, verbose=False, max_retries=3):
    """Run AppleMusicDecrypt to download an album in ALAC. Returns True on success."""
    cmd = [
        "poetry",
        "run",
        "python",
        "batch_download.py",
        apple_music_url,
    ]

    for attempt in range(1, max_retries + 1):
        if verbose:
            print(f"    Attempt {attempt}/{max_retries}: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=not verbose,
                text=True,
                timeout=1800,  # 30 min per album
                cwd=str(AMD_DIR),
            )
            if result.returncode == 0:
                return True
            if attempt < max_retries:
                print(f"    Retrying ({attempt}/{max_retries})...")
                time.sleep(5)
        except subprocess.TimeoutExpired:
            print("    Timeout (30 min)")
            if attempt < max_retries:
                print(f"    Retrying ({attempt}/{max_retries})...")
        except Exception as e:
            print(f"    Download error: {e}")
            if attempt < max_retries:
                time.sleep(5)

    return False


def show_status(albums):
    """Show codec status for all albums."""
    stats = {"alac": 0, "aac": 0, "flac": 0, "mp3": 0, "unknown": 0}

    for artist, album, path in albums:
        codec = check_album_codec(path)
        stats[codec] = stats.get(codec, 0) + 1
        marker = "OK" if codec in ("alac", "flac") else "LOSSY"
        print(f"  [{marker:5s}] {codec:7s}  {artist} — {album}")

    print(f"\nSummary: {len(albums)} albums")
    for codec, count in sorted(stats.items(), key=lambda x: -x[1]):
        if count:
            print(f"  {codec}: {count}")


def main():
    parser = argparse.ArgumentParser(
        description="Re-download music library in lossless ALAC from Apple Music."
    )
    parser.add_argument("--artist", help="Only process this artist")
    parser.add_argument("--album", help="Only process this album (requires --artist)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Search Apple Music but don't download"
    )
    parser.add_argument(
        "--status", action="store_true", help="Show codec status of all albums"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed progress"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    if args.album and not args.artist:
        print("Error: --album requires --artist")
        sys.exit(1)

    print("Scanning library...")
    albums = get_library_albums(
        artist_filter=args.artist,
        album_filter=args.album,
    )
    print(f"Found {len(albums)} albums\n")

    if not albums:
        print("No albums found.")
        sys.exit(0)

    if args.status:
        show_status(albums)
        return

    # Filter to only lossy albums
    print("Checking codecs...")
    to_redownload = []
    already_lossless = 0

    for artist, album, path in albums:
        codec = check_album_codec(path)
        if codec in ("alac", "flac"):
            already_lossless += 1
            if args.verbose:
                print(f"  SKIP  {artist} — {album} (already {codec})")
        else:
            to_redownload.append((artist, album, path))

    print(f"\n{already_lossless} albums already lossless")
    print(f"{len(to_redownload)} albums need re-downloading\n")

    if not to_redownload:
        print("Nothing to do!")
        return

    # Search Apple Music for each album
    print("Searching Apple Music...")
    found = []
    not_found = []

    for artist, album, path in to_redownload:
        # Rate limit: iTunes API allows ~20 requests/minute
        time.sleep(3)
        print(f"  Searching: {artist} — {album}...", end=" ", flush=True)
        url = search_itunes(artist, album)
        if url:
            print("FOUND")
            found.append((artist, album, path, url))
        else:
            print("NOT FOUND")
            not_found.append((artist, album, path))

    print(f"\nResults: {len(found)} found, {len(not_found)} not found on Apple Music\n")

    if not_found:
        print("Not found (will keep existing files):")
        for artist, album, _ in not_found:
            print(f"  {artist} — {album}")
        print()

    if not found:
        print("Nothing to download.")
        return

    print("Will re-download:")
    for artist, album, _, url in found:
        print(f"  {artist} — {album}")
        if args.verbose:
            print(f"    {url}")

    if args.dry_run:
        print("\n(Dry run — no files downloaded)")
        return

    if not args.yes:
        print()
        try:
            answer = input(f"Re-download {len(found)} album(s) as ALAC? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer.strip().lower() in ("n", "no"):
            print("Aborted.")
            sys.exit(0)

    # Download
    print()
    success = 0
    failed = 0

    for artist, album, path, url in found:
        print(f"  Downloading: {artist} — {album}")
        ok = run_amd(url, verbose=args.verbose)
        if ok:
            print(f"  OK    {artist} — {album}")
            success += 1
        else:
            print(f"  FAIL  {artist} — {album}")
            failed += 1

    print(
        f"\nDone: {success} downloaded, {failed} failed, {len(not_found)} not on Apple Music"
    )

    if not_found:
        print("\nAlbums not on Apple Music (kept as-is):")
        for artist, album, _ in not_found:
            print(f"  {artist} — {album}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
