# Music Library Manager

A collection of Python scripts to manage a local music library, sync playlists from Spotify, and download music from Apple Music.

## Features

- **Find Incomplete Albums**: Scan your library for albums with missing tracks
- **Complete Albums**: Search Apple Music and download missing tracks automatically
- **Playlist Sync**: Convert Spotify playlists to local M3U8 files by matching against your library

## Requirements

### Dependencies

```bash
# Apple Music downloads
pip install gamdl

# Spotify playlist sync
pip install spotdl

# YouTube/SoundCloud downloads
pip install yt-dlp
```

### Setup

1. Clone this repository
2. Copy your cookies to `config/cookies.txt` (for Apple Music authentication)
3. Update paths in config files if needed

## Folder Structure

```
music/
├── config/           # Configuration files
│   ├── gamdl.toml    # Apple Music downloader config
│   └── yt-dlp.conf   # YouTube/SoundCloud config
├── scripts/          # Python scripts
│   ├── find_incomplete_albums.py
│   ├── complete_albums.py
│   └── playlist_sync.py
├── docs/             # Usage instructions
│   ├── applemusic.txt
│   ├── soundcloud.txt
│   └── youtube.txt
└── music library/    # Your music files (not tracked in git)
    └── [Artist]/[Album]/[Track].m4a
```

## Usage

### Find Incomplete Albums

Scans your library and identifies albums with missing tracks:

```bash
python scripts/find_incomplete_albums.py
```

### Complete Albums

Finds incomplete albums, searches Apple Music for full versions, and downloads them:

```bash
python scripts/complete_albums.py
```

**Note**: Albums must be added to your Apple Music library before downloading.

### Sync Spotify Playlist

Converts a Spotify playlist to a local M3U8 file:

```bash
python scripts/playlist_sync.py "https://open.spotify.com/playlist/..."
```

### Manual Downloads

**Apple Music:**
```bash
cd ~/Documents/ish/media/music
gamdl --config-path config/gamdl.toml "APPLE_MUSIC_URL"
```

**YouTube/SoundCloud:**
```bash
cd ~/Documents/ish/media/music
yt-dlp --config-location config/yt-dlp.conf "URL"
```

## Configuration

### gamdl.toml

Key settings:
- `output_path`: Where to save downloaded music
- `final_path`: File naming template (`{album_artist}/{album}/{track:02d} {title}`)
- `song_codec`: Audio codec (`aac-legacy`)
- `synced_lyrics`: Download synced lyrics (`.lrc` files)

### yt-dlp.conf

Key settings:
- Downloads as MP3 with embedded metadata and artwork
- Output path matches gamdl structure

## How It Works

### Album Completion Detection

The scripts detect incomplete albums by:
1. Scanning track numbers in filenames (e.g., `01 Song.m4a`)
2. Finding gaps in the sequence (e.g., tracks 1,2,4,5 = missing track 3)
3. Supporting multi-disc formats (`1-01 Song.m4a`)

### Playlist Matching

The playlist sync uses fuzzy matching:
1. Normalizes artist/title (removes feat., parentheses, special chars)
2. Calculates similarity using SequenceMatcher
3. Weights title (70%) higher than artist (30%)
4. Uses 0.6 threshold for matches

## License

MIT
