#!/usr/bin/env python3
"""Find incomplete albums in the music library."""

import os
import re
from pathlib import Path
from collections import defaultdict

# Get the project root (parent of scripts folder)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MUSIC_LIBRARY = PROJECT_ROOT / "music library"

def get_track_numbers(album_path):
    """Extract track numbers from .m4a files in an album directory."""
    track_numbers = []
    for file in Path(album_path).glob("*.m4a"):
        # Try to extract track number from filename (e.g., "01 Song Name.m4a")
        match = re.match(r'^(\d+)', file.name)
        if match:
            track_numbers.append(int(match.group(1)))
        # Also check for disc-track format (e.g., "1-01 Song Name.m4a")
        match2 = re.match(r'^(\d+)-(\d+)', file.name)
        if match2:
            disc = int(match2.group(1))
            track = int(match2.group(2))
            track_numbers.append(disc * 100 + track)  # Encode disc-track
    return sorted(track_numbers)

def find_incomplete_albums():
    """Find albums that appear to be incomplete."""
    incomplete = []
    
    for artist_dir in Path(MUSIC_LIBRARY).iterdir():
        if not artist_dir.is_dir():
            continue
        if artist_dir.name in ['Automatically Add to Music.localized', 'Compilations', 'Playlists', 'Downloads-Music']:
            continue
            
        for album_dir in artist_dir.iterdir():
            if not album_dir.is_dir():
                continue
                
            m4a_files = list(album_dir.glob("*.m4a"))
            track_count = len(m4a_files)
            
            if track_count == 0:
                continue
            
            track_numbers = get_track_numbers(album_dir)
            
            # Check for gaps in track numbers (not flagging low track counts as EPs are valid)
            is_incomplete = False
            reason = ""

            if track_numbers:
                # Check for gaps in track numbers
                expected = list(range(min(track_numbers), max(track_numbers) + 1))
                missing = set(expected) - set(track_numbers)
                if missing and len(missing) <= 10:  # Don't flag if too many missing (might be intentional)
                    is_incomplete = True
                    reason = f"Missing tracks: {sorted(missing)}"
            
            if is_incomplete:
                incomplete.append({
                    'artist': artist_dir.name,
                    'album': album_dir.name,
                    'track_count': track_count,
                    'reason': reason,
                    'path': str(album_dir)
                })
    
    return incomplete

if __name__ == "__main__":
    print("Scanning for incomplete albums...")
    print("=" * 80)
    
    incomplete = find_incomplete_albums()
    
    # Sort by artist then album
    incomplete.sort(key=lambda x: (x['artist'], x['album']))
    
    for album in incomplete:
        print(f"{album['artist']} / {album['album']}")
        print(f"  Tracks: {album['track_count']}, Reason: {album['reason']}")
        print()
    
    print("=" * 80)
    print(f"Total incomplete albums found: {len(incomplete)}")
