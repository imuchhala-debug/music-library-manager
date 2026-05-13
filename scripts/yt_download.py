#!/usr/bin/env python3
"""
Download audio from YouTube/SoundCloud with full metadata tagging.

For unreleased, leaked, or non-streaming music. Downloads audio via yt-dlp,
tags with mutagen, and places files in the music library.

Supports:
  - YouTube playlists (each video = one track)
  - Single YouTube/SoundCloud videos
  - Videos with chapters (auto-split into separate tracks)
  - Custom tracklist files for manual chapter splitting

Usage:
    # YouTube playlist → album
    python scripts/yt_download.py "https://youtube.com/playlist?list=..." \\
        --artist "Frank Ocean" --album "Unreleased, Vol. 1"

    # Single video with chapters → auto-split into tracks
    python scripts/yt_download.py "https://youtu.be/..." \\
        --artist "Kanye West" --album "Yandhi"

    # Single video + tracklist file → split by timestamps
    python scripts/yt_download.py "https://youtu.be/..." \\
        --artist "Kanye West" --album "Yandhi" \\
        --tracklist tracklist.txt

    # With custom cover art
    python scripts/yt_download.py "https://youtu.be/..." \\
        --artist "Frank Ocean" --album "Nostalgia, Ultra" \\
        --art "https://i.imgur.com/abc123.jpg"

Tracklist file format (one per line):
    0:00 Track Title
    3:45 Another Track
    7:12 Third Track

Options:
    --artist ARTIST     Artist name (required)
    --album ALBUM       Album name (required)
    --art URL/PATH      Cover art URL or local file path
    --codec CODEC       Audio codec: alac, aac, mp3, flac (default: alac)
    --tracklist FILE    Tracklist file for splitting a single video by timestamps
    --dry-run           Show what would be downloaded
    --yes               Skip confirmation prompt
    --verbose           Show detailed progress
    --force             Re-download even if files exist
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

from utils import MUSIC_LIBRARY, sanitize_filename

CODEC_MAP = {
    "alac": {"codec": "alac", "ext": "m4a"},
    "aac": {"codec": "aac", "ext": "m4a"},
    "flac": {"codec": "flac", "ext": "flac"},
    "mp3": {"codec": "mp3", "ext": "mp3"},
}

# Patterns to strip from video titles to get clean track names
TITLE_NOISE = [
    r"^\s*\d+[\.\)\-]\s*",  # leading track numbers: "01. ", "1) ", "1- "
    r"\s*\[.*?\]\s*",  # [LEAK], [UNRELEASED], [HQ], etc.
    r"\s*\((?:official|audio|lyric|visualizer|hq|hd|4k).*?\)\s*",  # (Official Audio), etc.
    r"\s*【.*?】\s*",  # Japanese-style brackets
]


def clean_title(title, artist=None):
    """Clean a video title into a track name."""
    # Remove artist prefix patterns: "Artist - Title", "Artist | Title"
    if artist:
        # Try removing "Artist - " or "Artist | " prefix (case-insensitive)
        pattern = re.escape(artist) + r"\s*[-–—|]\s*"
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Apply noise patterns
    for pattern in TITLE_NOISE:
        title = re.sub(pattern, " ", title, flags=re.IGNORECASE)

    # Clean up whitespace
    title = re.sub(r"\s+", " ", title).strip()

    return title


def parse_tracklist(filepath):
    """Parse a tracklist file into list of (start_seconds, title) tuples."""
    tracks = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Match timestamp formats: 0:00, 00:00, 1:23:45
            m = re.match(r"(\d{1,2}:)?(\d{1,2}):(\d{2})\s+(.*)", line)
            if m:
                hours = int(m.group(1).rstrip(":")) if m.group(1) else 0
                minutes = int(m.group(2))
                seconds = int(m.group(3))
                total_seconds = hours * 3600 + minutes * 60 + seconds
                title = m.group(4).strip()
                tracks.append((total_seconds, title))
    return tracks


def fetch_playlist_entries(url, verbose=False):
    """Extract video entries from a YouTube playlist or single video."""
    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "extract_flat": False,
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return [], False

    # Playlist
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        return entries, False

    # Single video — check for chapters
    has_chapters = bool(info.get("chapters"))
    return [info], has_chapters


def download_full_audio(url, output_path, codec_info, verbose=False):
    """Download a single video's full audio to output_path."""
    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "outtmpl": str(output_path) + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec_info["codec"],
                "preferredquality": "0",
            }
        ],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    # Find output file
    expected = Path(str(output_path) + f".{codec_info['ext']}")
    if expected.exists():
        return expected

    parent = output_path.parent
    stem = output_path.name
    for f in parent.iterdir():
        if f.name.startswith(stem) and f.suffix in (
            ".m4a",
            ".mp3",
            ".flac",
            ".opus",
            ".webm",
        ):
            return f
    return None


def split_audio(input_file, segments, codec_info, output_dir):
    """Split an audio file into segments using ffmpeg.

    segments: list of (start_seconds, end_seconds, track_number, title)
    Returns list of (track_number, title, filepath).
    """
    results = []
    ext = codec_info["ext"]

    for start, end, track_num, title in segments:
        safe_title = sanitize_filename(title)
        out_file = output_dir / f"{track_num:02d} {safe_title}.{ext}"

        cmd = ["ffmpeg", "-y", "-i", str(input_file), "-ss", str(start)]
        if end is not None:
            cmd += ["-to", str(end)]

        if codec_info["codec"] == "alac":
            cmd += ["-acodec", "alac"]
        elif codec_info["codec"] == "aac":
            cmd += ["-acodec", "aac", "-b:a", "256k"]
        elif codec_info["codec"] == "flac":
            cmd += ["-acodec", "flac"]
        elif codec_info["codec"] == "mp3":
            cmd += ["-acodec", "libmp3lame", "-b:a", "320k"]

        cmd += ["-vn", str(out_file)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and out_file.exists():
            results.append((track_num, title, out_file))

    return results


def get_cover_art(art_source):
    """Load cover art from a URL or local file path. Returns bytes or None."""
    if not art_source:
        return None

    # Local file
    if os.path.isfile(art_source):
        with open(art_source, "rb") as f:
            return f.read()

    # URL
    try:
        resp = requests.get(art_source, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"  Warning: could not fetch cover art: {e}")
        return None


def tag_m4a(filepath, title, artist, album, track_num, total_tracks, cover_data=None):
    """Write MP4 metadata tags."""
    audio = MP4(str(filepath))
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9alb"] = [album]
    audio["trkn"] = [(track_num, total_tracks)]
    audio["disk"] = [(1, 0)]

    if cover_data:
        # Detect format from magic bytes
        if cover_data[:3] == b"\xff\xd8\xff":
            fmt = MP4Cover.FORMAT_JPEG
        elif cover_data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = MP4Cover.FORMAT_PNG
        else:
            fmt = MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]

    audio.save()


def build_final_path(artist, album, track_num, title, codec_info):
    """Build final library path."""
    safe_artist = sanitize_filename(artist)
    safe_album = sanitize_filename(album)
    safe_title = sanitize_filename(title)
    ext = codec_info["ext"]
    filename = f"{track_num:02d} {safe_title}.{ext}"
    return MUSIC_LIBRARY / safe_artist / safe_album / filename


def download_video_thumbnail(video_info):
    """Try to download the video/playlist thumbnail as fallback cover art."""
    thumb_url = video_info.get("thumbnail")
    if not thumb_url:
        thumbnails = video_info.get("thumbnails", [])
        if thumbnails:
            # Pick the largest
            thumb_url = sorted(
                thumbnails, key=lambda t: t.get("width", 0), reverse=True
            )[0].get("url")
    if thumb_url:
        try:
            resp = requests.get(thumb_url, timeout=15)
            resp.raise_for_status()
            return resp.content
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Download audio from YouTube/SoundCloud with metadata tagging."
    )
    parser.add_argument("url", help="YouTube or SoundCloud URL (video, playlist, etc.)")
    parser.add_argument("--artist", required=True, help="Artist name")
    parser.add_argument("--album", required=True, help="Album name")
    parser.add_argument("--art", help="Cover art URL or local file path")
    parser.add_argument(
        "--codec",
        default="alac",
        choices=CODEC_MAP.keys(),
        help="Audio codec (default: alac)",
    )
    parser.add_argument(
        "--tracklist", help="Tracklist file for splitting single video by timestamps"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be downloaded"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed progress"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if files exist"
    )
    args = parser.parse_args()

    codec_info = CODEC_MAP[args.codec]
    artist = args.artist
    album = args.album

    print("Fetching info from URL...")
    entries, has_chapters = fetch_playlist_entries(args.url, verbose=args.verbose)

    if not entries:
        print("No videos found.")
        sys.exit(1)

    # Determine track list
    tracks_to_download = []

    if args.tracklist:
        # Single video + tracklist file → split mode
        tracklist = parse_tracklist(args.tracklist)
        if not tracklist:
            print(f"Error: no tracks found in {args.tracklist}")
            sys.exit(1)
        for i, (start, title) in enumerate(tracklist):
            tracks_to_download.append(
                {
                    "track_num": i + 1,
                    "title": clean_title(title, artist),
                    "source": "split",
                }
            )
        print(f"Will split into {len(tracklist)} tracks from tracklist file")

    elif has_chapters and len(entries) == 1:
        # Single video with chapters → split by chapters
        chapters = entries[0].get("chapters", [])
        for i, ch in enumerate(chapters):
            tracks_to_download.append(
                {
                    "track_num": i + 1,
                    "title": clean_title(ch.get("title", f"Track {i + 1}"), artist),
                    "source": "chapter",
                }
            )
        print(f"Found {len(chapters)} chapters — will split into individual tracks")

    else:
        # Playlist or single video → each entry is a track
        for i, entry in enumerate(entries):
            title = clean_title(entry.get("title", f"Track {i + 1}"), artist)
            tracks_to_download.append(
                {
                    "track_num": i + 1,
                    "title": title,
                    "entry": entry,
                    "source": "video",
                }
            )

    total_tracks = len(tracks_to_download)

    # Preview
    print(f"\n{artist} — {album} ({total_tracks} tracks)\n")
    existing = 0
    for t in tracks_to_download:
        path = build_final_path(artist, album, t["track_num"], t["title"], codec_info)
        status = "EXISTS" if path.exists() else "NEW"
        if path.exists():
            existing += 1
        print(f"  [{status}] {t['track_num']:02d}. {t['title']}")

    if existing and not args.force:
        print(f"\n({existing} already in library, will be skipped)")

    if args.dry_run:
        print("\n(Dry run — no files downloaded)")
        return

    if not args.yes:
        print()
        try:
            answer = input(
                f"Download {total_tracks} track(s) as {args.codec.upper()}? [Y/n] "
            )
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer.strip().lower() in ("n", "no"):
            print("Aborted.")
            sys.exit(0)

    # Get cover art
    cover_data = get_cover_art(args.art)
    if not cover_data and entries:
        # Fallback: use first video's thumbnail
        cover_data = download_video_thumbnail(entries[0])
        if cover_data:
            print("Using video thumbnail as cover art")

    print()
    success = 0
    failed = 0

    if tracks_to_download[0]["source"] == "video":
        # Download each video as a separate track
        for t in tracks_to_download:
            final_path = build_final_path(
                artist, album, t["track_num"], t["title"], codec_info
            )
            label = f"{t['track_num']:02d}. {t['title']}"

            if final_path.exists() and not args.force:
                print(f"  SKIP  {label}")
                success += 1
                continue

            print(f"  GET   {label}")
            entry = t["entry"]
            video_url = entry.get("webpage_url") or entry.get("url") or entry.get("id")

            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir) / "audio"
                try:
                    downloaded = download_full_audio(
                        video_url, tmp_path, codec_info, verbose=args.verbose
                    )
                except Exception as e:
                    print(f"  FAIL  {label} ({e})")
                    failed += 1
                    continue

                if not downloaded:
                    print(f"  FAIL  {label} (no output file)")
                    failed += 1
                    continue

                # Tag
                try:
                    tag_m4a(
                        downloaded,
                        t["title"],
                        artist,
                        album,
                        t["track_num"],
                        total_tracks,
                        cover_data,
                    )
                except Exception as e:
                    print(f"  WARN  {label} (tagging error: {e})")

                # Move to final location
                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(downloaded), str(final_path))

            print(f"  OK    {label}")
            success += 1

    else:
        # Split mode: download full video first, then split
        video_url = entries[0].get("webpage_url") or entries[0].get("url")
        print("  Downloading full audio...")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "full_audio"
            try:
                full_audio = download_full_audio(
                    video_url, tmp_path, codec_info, verbose=args.verbose
                )
            except Exception as e:
                print(f"  FAIL  Could not download audio: {e}")
                sys.exit(1)

            if not full_audio:
                print("  FAIL  No output file")
                sys.exit(1)

            print(f"  Splitting into {total_tracks} tracks...")

            # Build segments with start/end times
            if args.tracklist:
                tracklist = parse_tracklist(args.tracklist)
                segments = []
                for i, (start, title) in enumerate(tracklist):
                    end = tracklist[i + 1][0] if i + 1 < len(tracklist) else None
                    clean = clean_title(title, artist)
                    segments.append((start, end, i + 1, clean))
            else:
                # Chapters
                chapters = entries[0].get("chapters", [])
                segments = []
                for i, ch in enumerate(chapters):
                    start = ch.get("start_time", 0)
                    end = ch.get("end_time")
                    clean = clean_title(ch.get("title", f"Track {i + 1}"), artist)
                    segments.append((start, end, i + 1, clean))

            split_dir = Path(tmpdir) / "split"
            split_dir.mkdir()
            split_results = split_audio(full_audio, segments, codec_info, split_dir)

            for track_num, title, split_file in split_results:
                final_path = build_final_path(
                    artist, album, track_num, title, codec_info
                )
                label = f"{track_num:02d}. {title}"

                if final_path.exists() and not args.force:
                    print(f"  SKIP  {label}")
                    success += 1
                    continue

                # Tag
                try:
                    tag_m4a(
                        split_file,
                        title,
                        artist,
                        album,
                        track_num,
                        total_tracks,
                        cover_data,
                    )
                except Exception as e:
                    print(f"  WARN  {label} (tagging error: {e})")

                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(split_file), str(final_path))
                print(f"  OK    {label}")
                success += 1

            # Check for any that failed to split
            split_nums = {r[0] for r in split_results}
            for t in tracks_to_download:
                if t["track_num"] not in split_nums:
                    print(f"  FAIL  {t['track_num']:02d}. {t['title']} (split failed)")
                    failed += 1

    print(f"\nDone: {success} OK, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
