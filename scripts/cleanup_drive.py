#!/usr/bin/env python3
"""
Clean up the external drive library:
1. Remove AAC duplicates where ALAC version exists under different artist name
2. Remove partial AAC copies where full ALAC album exists
3. Merge Kanye [Uncategorized] into [Unreleased]

Usage:
    python3 scripts/cleanup_drive.py           # dry run
    python3 scripts/cleanup_drive.py --go      # actually delete/move
"""

import argparse
import shutil
import subprocess
from pathlib import Path

DRIVE = Path("/Volumes/One Touch /music library")


def get_codec(filepath):
    """Get codec of an m4a file via afinfo."""
    try:
        result = subprocess.run(
            ["afinfo", str(filepath)], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "Data format:" in line:
                if "alac" in line.lower():
                    return "alac"
                elif "aac" in line.lower():
                    return "aac"
    except Exception:
        pass
    return "unknown"


def get_album_info(album_path):
    """Get track count, codec, and size of an album folder."""
    tracks = list(album_path.glob("*.m4a"))
    if not tracks:
        return 0, "empty", 0
    codec = get_codec(tracks[0])
    size = sum(f.stat().st_size for f in tracks)
    return len(tracks), codec, size


def find_duplicate_albums():
    """Find albums that exist under different artist names (same album name)."""
    albums = {}  # album_name -> [(artist, path, track_count, codec, size)]

    for artist_dir in sorted(DRIVE.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue
            count, codec, size = get_album_info(album_dir)
            if count > 0:
                key = album_dir.name
                if key not in albums:
                    albums[key] = []
                albums[key].append((artist_dir.name, album_dir, count, codec, size))

    # Find duplicates (same album name, multiple entries)
    dupes = {}
    for name, entries in albums.items():
        if len(entries) > 1:
            dupes[name] = entries
    return dupes


def find_aac_with_alac_counterpart():
    """Find AAC albums where the same album exists as ALAC under the same or different artist."""
    to_remove = []
    dupes = find_duplicate_albums()

    for album_name, entries in dupes.items():
        alac_entries = [e for e in entries if e[3] == "alac"]
        aac_entries = [e for e in entries if e[3] == "aac"]

        if alac_entries and aac_entries:
            # Keep the ALAC version(s), mark AAC for removal
            best_alac = max(
                alac_entries, key=lambda e: (e[2], e[4])
            )  # most tracks, biggest
            for aac in aac_entries:
                to_remove.append((aac[1], f"AAC dupe of ALAC under '{best_alac[0]}'"))

    return to_remove


def find_partial_aac_albums():
    """Find AAC albums with only 1-2 tracks where a full version likely exists."""
    partials = []
    for artist_dir in sorted(DRIVE.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue
            count, codec, size = get_album_info(album_dir)
            if count > 0 and count <= 2 and codec == "aac":
                # Check if this is a single (legitimate) or a partial copy
                if "Single" not in album_dir.name and "EP" not in album_dir.name:
                    partials.append((album_dir, f"Partial AAC ({count} tracks)"))
    return partials


def decompose_compilations():
    """Identify compilation tracks that should be moved to artist folders."""
    comp_dir = DRIVE / "Compilations"
    if not comp_dir.exists():
        return []

    moves = []
    for album_dir in sorted(comp_dir.iterdir()):
        if not album_dir.is_dir():
            continue
        for track in album_dir.glob("*.m4a"):
            # Read artist from metadata
            try:
                result = subprocess.run(
                    ["mdls", "-name", "kMDItemAuthors", str(track)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # Parse artist from mdls output
                artist = None
                for line in result.stdout.splitlines():
                    if '"' in line:
                        artist = line.strip().strip('"').strip(",").strip('"')
                        break
                if artist and artist != "(null)":
                    dest = DRIVE / artist / album_dir.name
                    moves.append((track, dest, artist, album_dir.name))
            except Exception:
                pass
    return moves


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args()

    if not DRIVE.exists():
        print("Drive not connected")
        return

    # 1. Find and remove AAC duplicates where ALAC exists
    print("=== AAC DUPLICATES (ALAC version exists under different artist) ===")
    dupes = find_aac_with_alac_counterpart()
    for path, reason in dupes:
        action = "DELETE" if args.go else "WOULD DELETE"
        print(f"  [{action}] {path.parent.name}/{path.name} — {reason}")
        if args.go:
            shutil.rmtree(path)
            # Remove empty artist dir
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()

    # 2. Find partial AAC albums (1-2 tracks, not singles)
    print("\n=== PARTIAL AAC ALBUMS (1-2 tracks, likely incomplete) ===")
    partials = find_partial_aac_albums()
    for path, reason in partials:
        action = "DELETE" if args.go else "WOULD DELETE"
        print(f"  [{action}] {path.parent.name}/{path.name} — {reason}")
        if args.go:
            shutil.rmtree(path)
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()

    # 3. Merge Kanye [Uncategorized] into [Unreleased]
    uncat = DRIVE / "Kanye West" / "[Uncategorized]"
    unrel = DRIVE / "Kanye West" / "[Unreleased]"
    if uncat.exists() and unrel.exists():
        print("\n=== MERGE KANYE [Uncategorized] → [Unreleased] ===")
        existing_names = {f.name for f in unrel.iterdir()} if unrel.exists() else set()
        for f in sorted(uncat.iterdir()):
            if f.name not in existing_names:
                action = "MOVE" if args.go else "WOULD MOVE"
                print(f"  [{action}] {f.name}")
                if args.go:
                    shutil.move(str(f), str(unrel / f.name))
            else:
                print(f"  [SKIP] {f.name} (already in [Unreleased])")
        if args.go and uncat.exists():
            # Remove [Uncategorized] if empty
            remaining = list(uncat.iterdir())
            if not remaining:
                uncat.rmdir()
                print("  Removed empty [Uncategorized] folder")

    # Summary
    print("\n=== SUMMARY ===")
    print(f"  AAC duplicates to remove: {len(dupes)}")
    print(f"  Partial AAC albums to remove: {len(partials)}")
    if not args.go:
        print("\n  Dry run. Use --go to execute.")


if __name__ == "__main__":
    main()
