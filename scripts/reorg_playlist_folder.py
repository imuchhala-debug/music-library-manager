"""Reorganize a `Playlists/{name}/` folder into the canonical `{artist}/{album}/` layout.

Why: Vinyl's LibraryManager treats folder structure as canonical (folderArtist/folderAlbum).
Tracks dumped into `Playlists/scaping/` end up with artist="Playlists", album="scaping"
in the library view, and the playlist row shows them with weird combined "NN. Artist - Title"
filenames. Moving them into `{artist}/{album}/` fixes display and matches how
`"a sword sliced the air"` works.

For each `.m4a` in the playlist folder:
  1. Read embedded MP4 metadata (artist, album, album_artist, title, trkn, disk).
  2. Compute the canonical destination: `{drive}/{album_artist}/{album}/{disk}-{NN} {title}.m4a`
     (matching batch_download's current `songNameFormat`).
  3. If a file already exists at that path (either this format OR the older `{NN} {title}.m4a`
     variant), skip the move — the track is already in the library. Delete the duplicate.
  4. Otherwise, move the `.m4a` + sibling `.lrc` to the destination.
  5. Rewrite `playlists.json` so the playlist's trackPaths point to the new locations,
     preserving playlist order.
  6. Remove the now-empty `Playlists/{name}/` folder.

Usage:
    # Kill Vinyl first (we're rewriting playlists.json)
    killall Vinyl
    poetry run python scripts/reorg_playlist_folder.py scaping

Safe to re-run. Dry-runs with --dry-run.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

try:
    from mutagen.mp4 import MP4
except ImportError:
    sys.exit("mutagen not installed — run inside the AppleMusicDecrypt poetry env")

DRIVE = Path("/Volumes/One Touch/music library")
PLAYLISTS_JSON = Path.home() / "Library/Application Support/com.ishaan.vinyl/playlists.json"


def sanitize(name: str) -> str:
    """Strip filesystem-illegal chars. Matches batch_download's get_valid_filename loosely."""
    # Only forward slash is truly illegal on macOS; colon also problematic historically.
    return name.replace("/", "_").replace(":", "_").strip()


def read_track_meta(m4a: Path) -> dict:
    tags = MP4(m4a).tags or {}
    title = (tags.get("\xa9nam") or [m4a.stem])[0]
    artist = (tags.get("\xa9ART") or ["Unknown Artist"])[0]
    album = (tags.get("\xa9alb") or ["Unknown Album"])[0]
    album_artist = (tags.get("aART") or [artist])[0]
    trkn = tags.get("trkn") or [(0, 0)]
    disk = tags.get("disk") or [(1, 1)]
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "trkn": trkn[0][0] if trkn and trkn[0] else 0,
        "disk": disk[0][0] if disk and disk[0] else 1,
    }


def canonical_dest(meta: dict) -> Path:
    """Match current `songNameFormat = "{disk}-{tracknum:02d} {title}"` + `dirPathFormat`."""
    artist_dir = sanitize(meta["album_artist"])
    album_dir = sanitize(meta["album"])
    name = f"{meta['disk']}-{meta['trkn']:02d} {sanitize(meta['title'])}.m4a"
    return DRIVE / artist_dir / album_dir / name


def legacy_dest(meta: dict) -> Path:
    """Older filename convention without disk prefix."""
    artist_dir = sanitize(meta["album_artist"])
    album_dir = sanitize(meta["album"])
    name = f"{meta['trkn']:02d} {sanitize(meta['title'])}.m4a"
    return DRIVE / artist_dir / album_dir / name


def find_existing(meta: dict) -> Path | None:
    for candidate in (canonical_dest(meta), legacy_dest(meta)):
        if candidate.exists():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist_name", help="Folder name under Playlists/ to reorganize")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without moving")
    args = parser.parse_args()

    src_dir = DRIVE / "Playlists" / args.playlist_name
    if not src_dir.is_dir():
        sys.exit(f"Not a directory: {src_dir}")

    with PLAYLISTS_JSON.open() as f:
        playlists = json.load(f)

    pl_idx = next((i for i, p in enumerate(playlists) if p["name"] == args.playlist_name), None)
    if pl_idx is None:
        sys.exit(f"No playlist named {args.playlist_name!r} in playlists.json")
    pl = playlists[pl_idx]

    # Iterate in playlist order (from trackPaths), not disk order
    old_paths = [Path(p) for p in pl["trackPaths"]]

    plan = []  # list of (old_path, new_path, action) where action ∈ {move, already_at_dest, dedupe}
    for old in old_paths:
        if not old.exists():
            print(f"MISSING: {old} — leaving trackPath as-is")
            plan.append((old, old, "missing"))
            continue
        try:
            meta = read_track_meta(old)
        except Exception as e:
            print(f"ERR reading {old.name}: {e} — skipping")
            plan.append((old, old, "error"))
            continue

        existing = find_existing(meta)
        if existing and existing.resolve() != old.resolve():
            # Duplicate of a track already in the library
            plan.append((old, existing, "dedupe"))
        elif existing and existing.resolve() == old.resolve():
            # Already in place (idempotency)
            plan.append((old, existing, "already_at_dest"))
        else:
            dest = canonical_dest(meta)
            plan.append((old, dest, "move"))

    # Summarize
    counts = {}
    for _, _, action in plan:
        counts[action] = counts.get(action, 0) + 1
    print(f"\nPlan for {args.playlist_name} ({len(plan)} tracks):")
    for action, n in counts.items():
        print(f"  {action}: {n}")
    print()
    for old, new, action in plan[:8]:
        print(f"  [{action}] {old.name}\n           -> {new}")
    if len(plan) > 8:
        print(f"  ... and {len(plan) - 8} more")

    if args.dry_run:
        print("\n(dry-run; no changes made)")
        return

    # Execute
    new_paths = []
    for old, new, action in plan:
        if action in ("missing", "error"):
            new_paths.append(str(old))
            continue
        if action == "already_at_dest":
            new_paths.append(str(new))
            continue
        if action == "dedupe":
            # Delete duplicate .m4a + sibling .lrc, point playlist at existing
            try:
                old.unlink()
                sib_lrc = old.with_suffix(".lrc")
                if sib_lrc.exists():
                    sib_lrc.unlink()
            except Exception as e:
                print(f"WARN: couldn't delete {old}: {e}")
            new_paths.append(str(new))
            continue
        # action == "move"
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            sib_lrc = old.with_suffix(".lrc")
            if sib_lrc.exists():
                shutil.move(str(sib_lrc), str(new.with_suffix(".lrc")))
            new_paths.append(str(new))
        except Exception as e:
            print(f"ERR moving {old} -> {new}: {e}")
            new_paths.append(str(old))

    # Persist updated trackPaths
    playlists[pl_idx]["trackPaths"] = new_paths
    with PLAYLISTS_JSON.open("w") as f:
        json.dump(playlists, f, indent=2)
    print(f"\nUpdated {PLAYLISTS_JSON}")

    # Orphan pass: any .m4a left in src_dir that wasn't in trackPaths is an unreferenced
    # file from an older playlist download. If it duplicates a track already in the main
    # library, delete. Otherwise move it into its canonical `{artist}/{album}/` slot so
    # it becomes part of the main library and the Playlists/ folder empties out.
    orphan_dedup = 0
    orphan_moved = 0
    for m4a in list(src_dir.glob("*.m4a")):
        try:
            meta = read_track_meta(m4a)
        except Exception:
            continue
        existing = find_existing(meta)
        if existing and existing.resolve() != m4a.resolve():
            try:
                m4a.unlink()
                sib_lrc = m4a.with_suffix(".lrc")
                if sib_lrc.exists():
                    sib_lrc.unlink()
                orphan_dedup += 1
            except Exception as e:
                print(f"WARN: couldn't delete orphan {m4a}: {e}")
        elif not existing:
            # Move the orphan to its canonical home
            dest = canonical_dest(meta)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m4a), str(dest))
                sib_lrc = m4a.with_suffix(".lrc")
                if sib_lrc.exists():
                    shutil.move(str(sib_lrc), str(dest.with_suffix(".lrc")))
                orphan_moved += 1
            except Exception as e:
                print(f"WARN: couldn't move orphan {m4a} -> {dest}: {e}")
    if orphan_dedup or orphan_moved:
        print(f"\nOrphan pass: {orphan_dedup} duplicates deleted, {orphan_moved} moved to canonical locations")

    # Clean up empty playlist folder
    remaining = list(src_dir.iterdir())
    if not remaining:
        src_dir.rmdir()
        print(f"Removed empty {src_dir}")
    else:
        print(f"\n{src_dir} still contains {len(remaining)} items — left in place:")
        for r in remaining[:5]:
            print(f"  {r.name}")


if __name__ == "__main__":
    main()
