#!/usr/bin/env python3
"""
Shared utilities for playlist sync scripts.
Provides normalize/similarity helpers, local library scanning, fuzzy matching, and M3U8 creation.
"""

import json
import os
import re
from pathlib import Path
from difflib import SequenceMatcher

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MUSIC_LIBRARY = PROJECT_ROOT / "music library"
PLAYLIST_DIR = MUSIC_LIBRARY / "Playlists"


def sanitize_filename(name):
    """Remove or replace characters that are unsafe for filenames."""
    # Replace path separators and other problematic chars
    name = re.sub(r"[/\\:]", "-", name)
    # Remove characters that are invalid on macOS/Windows
    name = re.sub(r'[<>"|?*]', "", name)
    # Collapse multiple spaces/dashes
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"-{2,}", "-", name)
    return name.strip().rstrip(".")


def normalize(text):
    """Normalize text for matching - lowercase, remove special chars."""
    if not text:
        return ""
    # Remove feat., ft., parentheses content, brackets
    text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text)
    text = re.sub(r"\s*(feat\.?|ft\.?|featuring)\s+.*$", "", text, flags=re.IGNORECASE)
    # Remove special characters, keep alphanumeric and spaces
    text = re.sub(r"[^\w\s]", "", text)
    return text.lower().strip()


def similarity(a, b):
    """Calculate similarity ratio between two strings."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def get_local_tracks(library_path=None):
    """Scan local library and return list of track dicts with artist, title, path."""
    library = library_path or MUSIC_LIBRARY
    tracks = []
    for root, _, files in os.walk(library):
        for file in files:
            if file.endswith((".m4a", ".mp3")):
                path = os.path.join(root, file)
                # Artist folder is the first directory inside the library
                rel = os.path.relpath(path, library)
                rel_parts = Path(rel).parts
                if len(rel_parts) >= 2:
                    artist = rel_parts[0]  # Artist folder
                else:
                    artist = "Unknown"
                # Extract title from filename (remove track number and extension)
                title = re.sub(r"^\d+[-\s]*", "", Path(file).stem)
                tracks.append({"artist": artist, "title": title, "path": path})
    return tracks


def _raw_similarity(a, b):
    """Similarity on the original strings (no normalization)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def match_track(spotify_track, local_tracks, threshold=0.75):
    """Find best matching local track for a Spotify track.

    Uses weighted similarity: 40% artist + 60% title.
    Requires a minimum artist similarity of 0.4 to avoid cross-artist
    false positives.  When normalized scores tie, raw (un-normalized)
    title similarity is used as a tiebreaker so "All of the Lights"
    beats "All of the Lights (Interlude)".
    Returns (best_match_dict, score) or (None, 0).
    """
    best_match = None
    best_score = 0
    best_raw_title = 0

    sp_artist = spotify_track["artist"]
    sp_title = spotify_track["title"]

    for local in local_tracks:
        artist_sim = similarity(sp_artist, local["artist"])
        if artist_sim < 0.4:
            continue
        title_sim = similarity(sp_title, local["title"])
        if title_sim < 0.7:
            continue
        score = (artist_sim * 0.4) + (title_sim * 0.6)

        if score < threshold:
            continue

        raw_title = _raw_similarity(sp_title, local["title"])
        if score > best_score or (score == best_score and raw_title > best_raw_title):
            best_score = score
            best_match = local
            best_raw_title = raw_title

    return best_match, best_score


def fetch_playlist_tracks_from_embed(playlist_id):
    """Fetch playlist tracks via Spotify's embed page.

    The Spotify Web API playlist-tracks endpoint now returns 403 for apps in
    Development Mode.  The embed page still serves full track data as JSON
    embedded in the HTML, so we scrape that instead.

    Returns a list of dicts: {title, artist, duration_ms, uri}
    """
    import urllib.request

    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8")

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html
    )
    if not match:
        return []

    data = json.loads(match.group(1))
    entity = (
        data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    track_list = entity.get("trackList", [])

    print(json.dumps({
        "status": "progress",
        "message": f"Embed scraper: got {len(track_list)} tracks from embed page"
    }), flush=True)

    tracks = []
    for item in track_list:
        if not item.get("isPlayable", True):
            continue
        tracks.append(
            {
                "title": item.get("title", ""),
                "artist": item.get("subtitle", ""),
                "duration_ms": item.get("duration", 0),
                "uri": item.get("uri", ""),
            }
        )
    return tracks


_COVER_KEYWORDS = {"cover", "karaoke", "tribute", "lullaby",
                   "style of", "made famous", "originally performed",
                   "dj mix", "remix"}

# Cache artist IDs and album lists across calls within one sync session
_artist_id_cache = {}    # artist_name_lower -> artist_id
_artist_albums_cache = {} # artist_id -> [(album_name, album_id), ...]


def _itunes_fetch(url, max_retries=3):
    """Fetch a URL from the iTunes API with retry/backoff. Returns parsed JSON or None."""
    import time
    import urllib.error
    import urllib.request

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Vinyl/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            return None
        except urllib.error.URLError:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            return None
    return None


def _clean_track_url(track_url):
    """Strip affiliate params from an iTunes track URL, keeping only ?i= (track ID)."""
    import urllib.parse
    if not track_url:
        return None
    parsed = urllib.parse.urlparse(track_url)
    params = urllib.parse.parse_qs(parsed.query)
    clean_params = {k: v[0] for k, v in params.items() if k == "i"}
    clean_query = urllib.parse.urlencode(clean_params) if clean_params else ""
    return urllib.parse.urlunparse(parsed._replace(query=clean_query))


def _artist_similarity(spotify_artist, itunes_artist):
    """Compare artists, handling Spotify's multi-artist format.
    Tries full string similarity first, then checks if the iTunes artist
    name appears within the Spotify artist string (substring match)."""
    full_sim = similarity(spotify_artist, itunes_artist)
    if full_sim >= 0.6:
        return full_sim
    # Check if iTunes artist is a substring of the Spotify artist string
    # Handles "Kali Uchis" in "Kali Uchis, Tyler, The Creator, Bootsy Collins"
    norm_spotify = normalize(spotify_artist)
    norm_itunes = normalize(itunes_artist)
    if len(norm_itunes) >= 3 and norm_itunes in norm_spotify:
        return max(full_sim, 0.85)
    return full_sim


def _pick_best_song(results, artist, title):
    """From a list of iTunes song results, pick the best artist+title match.
    Returns the result dict or None."""
    norm_title = normalize(title)
    best_result = None
    best_score = 0

    for result in results:
        track_name = result.get("trackName", "")
        album_name = result.get("collectionName", "")
        combined = (track_name + " " + album_name).lower()
        if any(kw in combined for kw in _COVER_KEYWORDS):
            continue
        result_artist = result.get("artistName", "")
        artist_sim = _artist_similarity(artist, result_artist)
        if artist_sim < 0.6:
            continue
        result_title = normalize(track_name)
        title_sim = SequenceMatcher(None, norm_title, result_title).ratio()
        if title_sim < 0.7:
            continue
        score = (artist_sim * 0.4) + (title_sim * 0.6)
        if score > best_score:
            best_score = score
            best_result = result

    return best_result


def resolve_apple_music_url(artist, title, album_hint=""):
    """Search the iTunes API and return the best Apple Music track URL.

    Uses a two-stage strategy:
      1. Direct song search by "artist title"
      2. Fallback: find artist → find album → find track within album

    Returns (track_url, resolved_artist, resolved_album) or (None, None, None).
    """
    import time
    import urllib.parse

    # For multi-artist strings ("Kali Uchis, Tyler, The Creator"), extract primary.
    # Can't naively split on comma — "Tyler, The Creator" has a comma in the name.
    # Strategy: try progressively shorter comma-separated prefixes until one
    # matches an iTunes artist.
    primary_artist = artist

    # --- Stage 1: Direct song search ---
    query = f"{primary_artist} {title}"
    search_url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": query, "media": "music", "entity": "song", "limit": 15}
    )
    data = _itunes_fetch(search_url)
    if data:
        best = _pick_best_song(data.get("results", []), artist, title)
        if best:
            return (
                _clean_track_url(best.get("trackViewUrl", "")),
                best.get("artistName", artist),
                best.get("collectionName", ""),
            )

    # --- Stage 2: Artist → album → track lookup fallback ---
    # 2a. Get artist ID — try progressively shorter comma prefixes
    # "Tyler, The Creator, Frank Ocean" → try full, then "Tyler, The Creator", then "Tyler"
    artist_candidates = [artist]
    if "," in artist:
        parts = artist.split(",")
        for i in range(len(parts) - 1, 0, -1):
            artist_candidates.append(",".join(parts[:i]).strip())

    artist_id = None
    for candidate in artist_candidates:
        candidate_lower = candidate.lower().strip()
        if candidate_lower in _artist_id_cache:
            artist_id = _artist_id_cache[candidate_lower]
            break
        time.sleep(0.3)
        artist_url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": candidate, "media": "music", "entity": "musicArtist", "limit": 5}
        )
        artist_data = _itunes_fetch(artist_url)
        if artist_data:
            for r in artist_data.get("results", []):
                if similarity(candidate, r.get("artistName", "")) >= 0.8:
                    _artist_id_cache[candidate_lower] = r["artistId"]
                    artist_id = r["artistId"]
                    break
        if artist_id:
            break

    if not artist_id:
        return None, None, None

    # 2b. Get artist's albums (cached)
    if artist_id not in _artist_albums_cache:
        time.sleep(0.3)
        albums_url = f"https://itunes.apple.com/lookup?id={artist_id}&entity=album&limit=100"
        albums_data = _itunes_fetch(albums_url)
        if albums_data:
            albums = []
            for r in albums_data.get("results", []):
                if r.get("wrapperType") == "collection":
                    albums.append((r.get("collectionName", ""), r.get("collectionId")))
            _artist_albums_cache[artist_id] = albums

    albums = _artist_albums_cache.get(artist_id, [])
    if not albums:
        return None, None, None

    # 2c. Find matching album — try album_hint first, then scan all albums
    target_album_id = None
    if album_hint:
        best_album_sim = 0
        for aname, aid in albums:
            s = similarity(album_hint, aname)
            if s >= 0.5 and s > best_album_sim:
                best_album_sim = s
                target_album_id = aid

    # Build list of albums to check: album_hint match first, then title-matched
    # albums (singles are often named "TRACK TITLE - Single")
    album_ids_to_check = []
    seen = set()
    if target_album_id:
        album_ids_to_check.append(target_album_id)
        seen.add(target_album_id)
    for aname, aid in albums:
        if aid not in seen and similarity(title, aname) >= 0.5:
            album_ids_to_check.append(aid)
            seen.add(aid)
    # Fall back to first 5 albums if still nothing
    if not album_ids_to_check:
        album_ids_to_check = [aid for _, aid in albums[:5]]

    # 2d. Lookup tracks in each candidate album
    norm_title = normalize(title)
    for album_id in album_ids_to_check:
        time.sleep(0.3)
        tracks_url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song"
        tracks_data = _itunes_fetch(tracks_url)
        if not tracks_data:
            continue
        for r in tracks_data.get("results", []):
            if r.get("wrapperType") != "track":
                continue
            result_title = normalize(r.get("trackName", ""))
            if SequenceMatcher(None, norm_title, result_title).ratio() >= 0.7:
                return (
                    _clean_track_url(r.get("trackViewUrl", "")),
                    r.get("artistName", artist),
                    r.get("collectionName", ""),
                )

    return None, None, None


def create_m3u8(playlist_name, matched_tracks, output_dir=None):
    """Create M3U8 playlist file with absolute paths.

    matched_tracks: list of dicts with 'title' and 'local_path' keys.
    Returns the path to the created file.
    """
    output_dir = output_dir or PLAYLIST_DIR
    os.makedirs(output_dir, exist_ok=True)

    safe_name = re.sub(r"[^\w\s-]", "", playlist_name).replace(" ", "_")
    filepath = os.path.join(output_dir, f"{safe_name}.m3u8")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"#PLAYLIST:{playlist_name}\n")

        for track in matched_tracks:
            if track.get("local_path"):
                title = track.get("title", "Unknown")
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(f"{track['local_path']}\n")

    return filepath
