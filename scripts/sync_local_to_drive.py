#!/usr/bin/env python3
"""
Sync local music library content to the external drive.
Copies albums that exist locally but not on the drive (unreleased, demos, compilations).
Skips albums that already exist on the drive (those are ALAC from Apple Music).

Usage:
    python3 scripts/sync_local_to_drive.py           # dry run
    python3 scripts/sync_local_to_drive.py --go       # actually copy
"""

import argparse
import shutil
from pathlib import Path

LOCAL_LIBRARY = Path.home() / "Documents/ishaan/media/music/music library"
DRIVE_LIBRARY = Path("/Volumes/One Touch /music library")

# Scan these subdirectories of the local library
SCAN_DIRS = [
    LOCAL_LIBRARY / "Music",  # Main artist/album structure
    LOCAL_LIBRARY / "Compilations",  # Compilations
    LOCAL_LIBRARY / "Kanye West",  # Top-level Kanye (unreleased)
]


def find_missing_albums():
    """Find albums that exist locally but not on the drive."""
    missing = []

    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue

        # Determine the relative base for path mapping
        if scan_dir == LOCAL_LIBRARY / "Music":
            # Music/Artist/Album → drive/Artist/Album
            for artist_dir in sorted(scan_dir.iterdir()):
                if not artist_dir.is_dir() or artist_dir.name.startswith("."):
                    continue
                for album_dir in sorted(artist_dir.iterdir()):
                    if not album_dir.is_dir():
                        continue
                    # Check if any .m4a files exist
                    tracks = list(album_dir.glob("*.m4a"))
                    if not tracks:
                        continue
                    drive_path = DRIVE_LIBRARY / artist_dir.name / album_dir.name
                    if not drive_path.exists():
                        missing.append(
                            (
                                album_dir,
                                drive_path,
                                artist_dir.name,
                                album_dir.name,
                                len(tracks),
                            )
                        )

        elif scan_dir == LOCAL_LIBRARY / "Compilations":
            # Compilations/Album → drive/Compilations/Album
            for album_dir in sorted(scan_dir.iterdir()):
                if not album_dir.is_dir():
                    continue
                tracks = list(album_dir.glob("*.m4a"))
                if not tracks:
                    continue
                drive_path = DRIVE_LIBRARY / "Compilations" / album_dir.name
                if not drive_path.exists():
                    missing.append(
                        (
                            album_dir,
                            drive_path,
                            "Compilations",
                            album_dir.name,
                            len(tracks),
                        )
                    )

        else:
            # Top-level artist dir (e.g., Kanye West/[Uncategorized])
            artist_name = scan_dir.name
            for album_dir in sorted(scan_dir.iterdir()):
                if not album_dir.is_dir():
                    continue
                tracks = list(album_dir.glob("*.m4a"))
                if not tracks:
                    continue
                drive_path = DRIVE_LIBRARY / artist_name / album_dir.name
                if not drive_path.exists():
                    missing.append(
                        (
                            album_dir,
                            drive_path,
                            artist_name,
                            album_dir.name,
                            len(tracks),
                        )
                    )

    return missing


def copy_album(src, dst):
    """Copy an album folder to the drive, preserving .m4a, .lrc, cover files."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in (".m4a", ".lrc", ".jpg", ".png"):
            shutil.copy2(str(f), str(dst / f.name))


def main():
    parser = argparse.ArgumentParser(description="Sync local music to external drive")
    parser.add_argument(
        "--go", action="store_true", help="Actually copy (default is dry run)"
    )
    args = parser.parse_args()

    if not DRIVE_LIBRARY.exists():
        print("Error: Drive not connected at", DRIVE_LIBRARY)
        return

    print("Scanning for albums not on drive...")
    missing = find_missing_albums()

    if not missing:
        print("Everything is already synced!")
        return

    total_tracks = sum(m[4] for m in missing)
    print(f"\n{len(missing)} albums ({total_tracks} tracks) to sync:\n")

    for src, dst, artist, album, count in missing:
        status = "COPY" if args.go else "WOULD COPY"
        print(f"  [{status}] {artist} / {album} ({count} tracks)")
        if args.go:
            copy_album(src, dst)

    if args.go:
        print(f"\nDone! Copied {len(missing)} albums ({total_tracks} tracks) to drive.")
        print("Relaunch Vinyl or wait for library rescan to see them.")
    else:
        print("\nDry run. Use --go to actually copy.")


if __name__ == "__main__":
    main()
