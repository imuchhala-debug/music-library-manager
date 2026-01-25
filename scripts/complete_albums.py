#!/usr/bin/env python3
"""
Find incomplete albums and download full versions from Apple Music.
Searches Apple Music for album URLs, then downloads with gamdl.
"""

import os
import re
import subprocess
import json
import urllib.request
import urllib.parse
from pathlib import Path

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# Get the project root (parent of scripts folder)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MUSIC_LIBRARY = PROJECT_ROOT / "music library"
MUSIC_DIR = PROJECT_ROOT


def get_track_numbers(album_path):
    """Extract track numbers from .m4a files in an album directory."""
    track_numbers = []
    for file in Path(album_path).glob("*.m4a"):
        match = re.match(r'^(\d+)', file.name)
        if match:
            track_numbers.append(int(match.group(1)))
        match2 = re.match(r'^(\d+)-(\d+)', file.name)
        if match2:
            disc = int(match2.group(1))
            track = int(match2.group(2))
            track_numbers.append(disc * 100 + track)
    return sorted(track_numbers)


def find_incomplete_albums():
    """Find albums that have missing tracks (not just singles)."""
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
            
            # Only flag albums with gaps in track numbers (not singles)
            if track_numbers and track_count >= 5:
                expected = list(range(min(track_numbers), max(track_numbers) + 1))
                missing = set(expected) - set(track_numbers)
                if missing and len(missing) <= 10:
                    incomplete.append({
                        'artist': artist_dir.name,
                        'album': album_dir.name,
                        'track_count': track_count,
                        'missing': sorted(missing),
                        'path': str(album_dir)
                    })
    
    return incomplete


def do_itunes_search(query):
    """Perform iTunes API search and return results."""
    encoded_query = urllib.parse.quote(query)
    search_url = f"https://itunes.apple.com/search?term={encoded_query}&entity=album&limit=15"
    
    try:
        if REQUESTS_AVAILABLE:
            response = requests.get(search_url, timeout=10)
            return response.json()
        else:
            with urllib.request.urlopen(search_url, timeout=10) as resp:
                return json.loads(resp.read().decode())
    except Exception:
        return None


def normalize_for_search(text):
    """Normalize text for search - remove special chars, parentheses, etc."""
    # Remove parentheses and brackets content
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    # Remove edition/version suffixes
    text = re.sub(r'\s*(Deluxe|Remaster|Edition|Version|Bonus|Extended|Explicit).*$', '', text, flags=re.IGNORECASE)
    # Replace $ with S (for A$AP)
    text = text.replace('$', 'S')
    # Remove underscores
    text = text.replace('_', ' ')
    # Remove special characters but keep spaces
    text = re.sub(r'[^\w\s]', '', text)
    return text.strip()


def search_apple_music(artist, album):
    """Search Apple Music for an album and return its URL."""
    artist_clean = normalize_for_search(artist)
    album_clean = normalize_for_search(album)

    # Try multiple search strategies
    search_queries = [
        f"{artist_clean} {album_clean}",           # Full search
        f'"{album_clean}" {artist_clean}',         # Album in quotes
    ]

    for query in search_queries:
        if not query.strip():
            continue

        data = do_itunes_search(query)
        if not data or data.get('resultCount', 0) == 0:
            continue

        # Score each result
        best_match = None
        best_score = 0
        best_album_score = 0

        for result in data['results']:
            result_artist = result.get('artistName', '').lower()
            result_album = result.get('collectionName', '').lower()
            result_album_clean = normalize_for_search(result_album).lower()

            # Calculate match score
            artist_score = 0
            album_score = 0

            # Artist matching
            artist_lower = artist_clean.lower()
            if artist_lower == result_artist:
                artist_score = 50
            elif artist_lower in result_artist or result_artist in artist_lower:
                artist_score = 30
            elif any(word in result_artist for word in artist_lower.split() if len(word) > 2):
                artist_score = 15

            # Album matching - must have some album match
            album_lower = album_clean.lower()
            if album_lower == result_album_clean:
                album_score = 50
            elif album_lower in result_album_clean or result_album_clean in album_lower:
                album_score = 40
            else:
                # Check word overlap
                album_words = set(w for w in album_lower.split() if len(w) > 2)
                result_words = set(w for w in result_album_clean.split() if len(w) > 2)
                if album_words and result_words:
                    overlap = len(album_words & result_words) / len(album_words)
                    if overlap >= 0.5:
                        album_score = int(30 * overlap)

            total_score = artist_score + album_score

            # Require minimum album score to avoid wrong albums
            if album_score >= 20 and total_score > best_score:
                best_score = total_score
                best_album_score = album_score
                best_match = result

        # Accept if we have good artist match AND album match
        if best_match and best_score >= 50 and best_album_score >= 20:
            collection_id = best_match.get('collectionId')
            if collection_id:
                return f"https://music.apple.com/us/album/{collection_id}"

    return None


def download_apple_music_album(url):
    """Download album from Apple Music URL using gamdl."""
    try:
        result = subprocess.run(
            [
                'gamdl',
                '--config-path', 'config/gamdl.toml',
                '--overwrite',
                url
            ],
            capture_output=True,
            text=True,
            cwd=MUSIC_DIR
        )
        
        output = result.stdout + result.stderr
        
        # Check for success indicators
        # Note: "Finished with 0 error(s)" is a success message
        if 'Finished' in output and '0 error(s)' in output:
            return True, None
        elif 'No downloadable media found' in output:
            return False, "Not in your Apple Music library"
        elif 'Finished' in output and 'error(s)' in output:
            # Has errors but finished
            return False, "Download had errors"
        elif 'error' in output.lower() and '0 error' not in output.lower():
            # Extract error message
            lines = output.split('\n')
            for line in lines:
                if 'error' in line.lower() and '0 error' not in line.lower():
                    return False, line[:100]
            return False, "Unknown error"
        else:
            return True, None  # Assume success if no error
        
    except FileNotFoundError:
        return False, "gamdl not found"
    except Exception as e:
        return False, str(e)


def main():
    print("🔍 Scanning for incomplete albums...")
    incomplete = find_incomplete_albums()
    
    if not incomplete:
        print("✅ No incomplete albums found!")
        return
    
    print(f"\n📋 Found {len(incomplete)} albums with missing tracks:\n")
    
    for i, album in enumerate(incomplete, 1):
        print(f"{i}. {album['artist']} - {album['album']}")
        print(f"   Has {album['track_count']} tracks, missing: {album['missing']}")
    
    print(f"\n" + "="*60)
    
    # Search for Apple Music URLs
    print("\n🍎 Searching Apple Music for album URLs...\n")
    
    albums_with_urls = []
    not_found = []
    
    for album in incomplete:
        print(f"  {album['artist']} - {album['album']}...", end=" ", flush=True)
        url = search_apple_music(album['artist'], album['album'])
        if url:
            print(f"✓")
            albums_with_urls.append({**album, 'apple_music_url': url})
        else:
            print(f"✗ Not found")
            not_found.append(album)
    
    print(f"\n📋 Found {len(albums_with_urls)}/{len(incomplete)} albums on Apple Music")
    
    if not albums_with_urls:
        print("\n❌ No albums found on Apple Music")
        return
    
    if not_found:
        print(f"\n⚠️  {len(not_found)} albums not found:")
        for album in not_found[:5]:
            print(f"   - {album['artist']} - {album['album']}")
        if len(not_found) > 5:
            print(f"   ... and {len(not_found) - 5} more")
    
    response = input(f"\nDownload {len(albums_with_urls)} albums from Apple Music? [y/N]: ").strip().lower()
    
    if response != 'y':
        print("Cancelled.")
        return
    
    print("\n🎵 Downloading albums from Apple Music...\n")
    print("Note: Albums must be in your Apple Music library to download.\n")
    
    success = 0
    failed = 0
    not_in_library = []
    
    for album in albums_with_urls:
        print(f"📀 {album['artist']} - {album['album']}")
        
        ok, error = download_apple_music_album(album['apple_music_url'])
        if ok:
            print(f"   ✅ Downloaded")
            success += 1
        else:
            if "Not in your Apple Music library" in str(error):
                print(f"   ⚠️ Not in library - skipped")
                not_in_library.append(album)
            else:
                print(f"   ❌ {error}")
            failed += 1
    
    print(f"\n" + "="*60)
    print(f"✅ Results: {success} downloaded, {failed} skipped/failed")
    
    if not_in_library:
        print(f"\n💡 To download the {len(not_in_library)} missing albums:")
        print("   1. Open Apple Music app")
        print("   2. Search for and add these albums to your library:")
        for album in not_in_library[:5]:
            print(f"      - {album['artist']} - {album['album']}")
        if len(not_in_library) > 5:
            print(f"      ... and {len(not_in_library) - 5} more")
        print("   3. Run 'music complete' again")


if __name__ == "__main__":
    main()
