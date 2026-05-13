#!/usr/bin/env python3
"""
Edit metadata on .m4a files. Called by Vinyl app.

Usage:
    python3 edit_metadata.py <file.m4a> --title "X" --artist "X" --album "X" --year "2024"
    python3 edit_metadata.py <file.m4a> --cover <image_path>
    python3 edit_metadata.py --delete-album <album_path>
    python3 edit_metadata.py --rename-album <album_path> --artist "New" --album "New"
"""

import argparse
import json
import os
import shutil
from pathlib import Path


def edit_track(
    filepath,
    title=None,
    artist=None,
    album=None,
    album_artist=None,
    year=None,
    track_number=None,
    genre=None,
    cover_path=None,
):
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(filepath)

    if title is not None:
        audio["\xa9nam"] = [title]
    if artist is not None:
        audio["\xa9ART"] = [artist]
    if album is not None:
        audio["\xa9alb"] = [album]
    if album_artist is not None:
        audio["aART"] = [album_artist]
    if year is not None:
        audio["\xa9day"] = [year]
    if genre is not None:
        audio["\xa9gen"] = [genre]
    if track_number is not None:
        total = audio.get("trkn", [(1, 0)])[0][1]
        audio["trkn"] = [(track_number, total)]
    if cover_path is not None:
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        if cover_path.lower().endswith(".png"):
            fmt = MP4Cover.FORMAT_PNG
        else:
            fmt = MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]

    audio.save()
    print(json.dumps({"status": "saved", "path": filepath}))


def delete_album(album_path):
    if os.path.isdir(album_path):
        shutil.rmtree(album_path)
        # Remove empty artist dir
        parent = os.path.dirname(album_path)
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
        print(json.dumps({"status": "deleted", "path": album_path}))
    else:
        print(json.dumps({"status": "error", "message": f"Not found: {album_path}"}))


def rename_album(album_path, new_artist=None, new_album=None):
    from mutagen.mp4 import MP4

    p = Path(album_path)
    if not p.is_dir():
        print(json.dumps({"status": "error", "message": f"Not found: {album_path}"}))
        return

    drive_root = p.parent.parent  # e.g., /Volumes/One Touch /music library

    old_artist = p.parent.name
    old_album = p.name
    target_artist = new_artist or old_artist
    target_album = new_album or old_album

    # Update metadata in all tracks
    for f in p.glob("*.m4a"):
        try:
            audio = MP4(str(f))
            if new_artist:
                audio["\xa9ART"] = [new_artist]
                audio["aART"] = [new_artist]
            if new_album:
                audio["\xa9alb"] = [new_album]
            audio.save()
        except Exception:
            pass

    # Move folder if artist or album name changed
    new_path = drive_root / target_artist / target_album
    if new_path != p:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(new_path))
        # Clean empty old artist dir
        if p.parent.exists() and not list(p.parent.iterdir()):
            p.parent.rmdir()

    print(json.dumps({"status": "renamed", "old": album_path, "new": str(new_path)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", help="Path to .m4a file")
    parser.add_argument("--title")
    parser.add_argument("--artist")
    parser.add_argument("--album")
    parser.add_argument("--album-artist")
    parser.add_argument("--year")
    parser.add_argument("--track-number", type=int)
    parser.add_argument("--genre")
    parser.add_argument("--cover")
    parser.add_argument("--delete-album")
    parser.add_argument("--rename-album")
    args = parser.parse_args()

    if args.delete_album:
        delete_album(args.delete_album)
    elif args.rename_album:
        rename_album(args.rename_album, new_artist=args.artist, new_album=args.album)
    elif args.file:
        edit_track(
            args.file,
            title=args.title,
            artist=args.artist,
            album=args.album,
            album_artist=args.album_artist,
            year=args.year,
            track_number=args.track_number,
            genre=args.genre,
            cover_path=args.cover,
        )
    else:
        print(json.dumps({"status": "error", "message": "No file or action specified"}))


if __name__ == "__main__":
    main()
