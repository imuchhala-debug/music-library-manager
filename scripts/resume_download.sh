#!/bin/bash
# Resume ALAC downloads from saved URL list — no scanning/searching
cd /Users/ishaanmuchhala/Documents/ishaan/media/music/AppleMusicDecrypt

URL_FILE="${1:-/Users/ishaanmuchhala/Documents/ishaan/media/music/config/album_urls.txt}"
total=$(wc -l < "$URL_FILE")
i=0

while IFS= read -r url; do
    i=$((i + 1))
    echo "[$i/$total] $url"
    poetry run python batch_download.py "$url" 2>&1 | grep -E "Song saved|already exist|Start ripping|ALBUM|Error|error|failed"
    echo ""
done < "$URL_FILE"

echo "ALL DONE"
