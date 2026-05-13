#!/usr/bin/env python3
"""
Fetch artist images from Spotify (primary) with Deezer fallback.

Usage:
    echo '["Drake", "Kendrick Lamar"]' | python3 fetch_artist_images.py --cache-dir /path/to/cache

Reads JSON array of artist names from stdin.
Downloads artist images to cache-dir as {normalized_name}.jpg.

On Spotify 429 (rate limit), Spotify is disabled for the remainder of the
batch and Deezer is used for the rest. Deezer is unauthenticated.
"""

import json
import sys
import argparse
import re
import requests
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR.parent / "config"

DEEZER_PLACEHOLDER_MARKER = "/images/artist//"
HTTP_TIMEOUT = 10


def normalize_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name.strip()


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def init_spotify():
    config_path = CONFIG_DIR / "spotify.json"
    if not config_path.exists():
        emit({"status": "warning", "message": "Spotify config missing; using Deezer only"})
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        config = json.loads(config_path.read_text())
        auth = SpotifyClientCredentials(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
        )
        return spotipy.Spotify(auth_manager=auth)
    except ImportError:
        emit({"status": "warning", "message": "spotipy not installed; using Deezer only"})
        return None
    except Exception as e:
        emit({"status": "warning", "message": f"Spotify auth failed; using Deezer only: {e}"})
        return None


def try_spotify(sp, name: str):
    """Return (image_url, error_kind) — error_kind is 'rate_limit', 'other', or None."""
    try:
        import spotipy
    except ImportError:
        return None, "other"
    try:
        result = sp.search(q=f'artist:"{name}"', type="artist", limit=1)
        items = result.get("artists", {}).get("items", [])
        if not items:
            return None, None
        images = items[0].get("images", [])
        if not images:
            return None, None
        return (images[1]["url"] if len(images) > 1 else images[0]["url"]), None
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 429:
            return None, "rate_limit"
        return None, "other"
    except Exception:
        return None, "other"


def try_deezer(name: str):
    """Return image URL or None. Skips Deezer's generic placeholder."""
    try:
        resp = requests.get(
            "https://api.deezer.com/search/artist",
            params={"q": name, "limit": 1},
            headers={"User-Agent": "Vinyl/1.0"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            return None
        pic = items[0].get("picture_xl") or items[0].get("picture_big")
        if not pic or DEEZER_PLACEHOLDER_MARKER in pic:
            return None
        return pic
    except Exception as e:
        emit({"status": "warning", "artist": name, "source": "deezer", "message": str(e)})
        return None


def download(url: str, dest: Path) -> bool:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Vinyl/1.0"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except Exception as e:
        emit({"status": "warning", "url": url, "message": f"download failed: {e}"})
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        names = json.loads(sys.stdin.read())
    except Exception as e:
        emit({"status": "error", "message": f"Invalid input: {e}"})
        sys.exit(1)

    if not names:
        emit({"status": "done", "fetched": 0})
        return

    sp = init_spotify()
    spotify_disabled = sp is None

    fetched = 0
    spotify_hits = 0
    deezer_hits = 0
    for name in names:
        normalized = normalize_name(name)
        dest = cache_dir / f"{normalized}.jpg"
        if dest.exists():
            continue

        img_url = None
        source = None

        if not spotify_disabled:
            img_url, err = try_spotify(sp, name)
            if err == "rate_limit":
                spotify_disabled = True
                emit({"status": "warning", "message": "Spotify rate-limited; Deezer fallback for remainder"})
                img_url = None
            elif img_url:
                source = "spotify"

        if not img_url:
            img_url = try_deezer(name)
            if img_url:
                source = "deezer"

        if not img_url:
            continue

        if download(img_url, dest):
            fetched += 1
            if source == "spotify":
                spotify_hits += 1
            else:
                deezer_hits += 1

    emit({
        "status": "done",
        "fetched": fetched,
        "spotify": spotify_hits,
        "deezer": deezer_hits,
        "spotify_disabled": spotify_disabled,
    })


if __name__ == "__main__":
    main()
