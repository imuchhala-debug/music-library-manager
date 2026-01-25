#!/usr/bin/env python3
"""
Sync Spotify playlists to local M3U8 files.
Matches Spotify tracks against local library and creates playlist files.
"""

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from difflib import SequenceMatcher

# Get the project root (parent of scripts folder)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MUSIC_LIBRARY = PROJECT_ROOT / "music library"
PLAYLIST_DIR = MUSIC_LIBRARY / "Playlists"


def normalize(text):
    """Normalize text for matching - lowercase, remove special chars."""
    if not text:
        return ""
    # Remove feat., ft., parentheses content, brackets
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    text = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*$', '', text, flags=re.IGNORECASE)
    # Remove special characters, keep alphanumeric and spaces
    text = re.sub(r'[^\w\s]', '', text)
    return text.lower().strip()


def similarity(a, b):
    """Calculate similarity ratio between two strings."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def get_local_tracks():
    """Scan local library and return list of (artist, title, path) tuples."""
    tracks = []
    for root, _, files in os.walk(MUSIC_LIBRARY):
        for file in files:
            if file.endswith(('.m4a', '.mp3')):
                path = os.path.join(root, file)
                # Extract artist from path (parent of parent is usually artist)
                parts = Path(path).parts
                if len(parts) >= 3:
                    artist = parts[-3]  # Artist folder
                    # Extract title from filename (remove track number and extension)
                    title = re.sub(r'^\d+[-\s]*', '', Path(file).stem)
                    tracks.append({
                        'artist': artist,
                        'title': title,
                        'path': path
                    })
    return tracks


def fetch_spotify_playlist(url):
    """Fetch playlist tracks using spotdl's metadata fetching."""
    try:
        # Use spotdl to get playlist info
        result = subprocess.run(
            ['spotdl', 'save', url, '--save-file', '/tmp/spotify_playlist.json'],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"Error fetching playlist: {result.stderr}")
            return None, []
        
        # Read the saved JSON
        with open('/tmp/spotify_playlist.json', 'r') as f:
            data = json.load(f)
        
        # Extract playlist name and tracks
        playlist_name = "Spotify Playlist"
        tracks = []
        
        for item in data:
            tracks.append({
                'artist': item.get('artist', item.get('artists', ['Unknown'])[0] if isinstance(item.get('artists'), list) else 'Unknown'),
                'title': item.get('name', item.get('title', 'Unknown'))
            })
            # Try to get playlist name from first item
            if 'playlist' in item:
                playlist_name = item['playlist']
        
        return playlist_name, tracks
        
    except Exception as e:
        print(f"Error: {e}")
        return None, []


def match_track(spotify_track, local_tracks, threshold=0.6):
    """Find best matching local track for a Spotify track."""
    best_match = None
    best_score = 0
    
    sp_artist = spotify_track['artist']
    sp_title = spotify_track['title']
    
    for local in local_tracks:
        # Calculate combined similarity score
        artist_sim = similarity(sp_artist, local['artist'])
        title_sim = similarity(sp_title, local['title'])
        
        # Weight title higher than artist
        score = (artist_sim * 0.3) + (title_sim * 0.7)
        
        if score > best_score and score >= threshold:
            best_score = score
            best_match = local
    
    return best_match, best_score


def create_m3u8(playlist_name, matched_tracks, output_dir):
    """Create M3U8 playlist file."""
    os.makedirs(output_dir, exist_ok=True)
    
    # Sanitize playlist name for filename
    safe_name = re.sub(r'[^\w\s-]', '', playlist_name).replace(' ', '_')
    filepath = os.path.join(output_dir, f"{safe_name}.m3u8")
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        f.write(f"#PLAYLIST:{playlist_name}\n")
        
        for track in matched_tracks:
            if track.get('local_path'):
                title = track.get('title', 'Unknown')
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(f"{track['local_path']}\n")
    
    return filepath


def main():
    if len(sys.argv) < 2:
        print("Usage: playlist_sync.py <spotify-playlist-url>")
        print("\nThis tool:")
        print("  1. Fetches tracks from a Spotify playlist")
        print("  2. Matches them against your local library")
        print("  3. Creates an M3U8 playlist file")
        print("  4. Reports any missing tracks")
        sys.exit(1)
    
    url = sys.argv[1]
    
    if 'spotify.com' not in url:
        print("Error: Please provide a Spotify playlist URL")
        sys.exit(1)
    
    print("🎵 Fetching Spotify playlist...")
    playlist_name, spotify_tracks = fetch_spotify_playlist(url)
    
    if not spotify_tracks:
        print("Failed to fetch playlist tracks")
        sys.exit(1)
    
    print(f"📋 Found {len(spotify_tracks)} tracks in '{playlist_name}'")
    
    print("🔍 Scanning local library...")
    local_tracks = get_local_tracks()
    print(f"📁 Found {len(local_tracks)} local tracks")
    
    print("\n🔗 Matching tracks...")
    matched = []
    missing = []
    
    for sp_track in spotify_tracks:
        local_match, score = match_track(sp_track, local_tracks)
        
        if local_match:
            matched.append({
                'artist': sp_track['artist'],
                'title': sp_track['title'],
                'local_path': local_match['path'],
                'score': score
            })
            print(f"  ✓ {sp_track['artist']} - {sp_track['title']}")
        else:
            missing.append(sp_track)
            print(f"  ✗ {sp_track['artist']} - {sp_track['title']}")
    
    # Create playlist file
    if matched:
        playlist_path = create_m3u8(playlist_name, matched, PLAYLIST_DIR)
        print(f"\n✅ Created playlist: {playlist_path}")
    
    # Summary
    print(f"\n📊 Summary:")
    print(f"   Matched: {len(matched)}/{len(spotify_tracks)}")
    print(f"   Missing: {len(missing)}")
    
    if missing:
        print(f"\n❌ Missing tracks (download with 'music add <spotify-link>'):")
        for track in missing[:10]:  # Show first 10
            print(f"   - {track['artist']} - {track['title']}")
        if len(missing) > 10:
            print(f"   ... and {len(missing) - 10} more")


if __name__ == "__main__":
    main()
