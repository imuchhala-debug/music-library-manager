#!/bin/bash
# Live monitor for Vinyl ALAC redownload
# Usage: bash scripts/monitor.sh

DRIVE="/Volumes/One Touch /music library"
LOG="/tmp/redownload_log.txt"
START_TRACKS=$(find "$DRIVE" -name "*.m4a" 2>/dev/null | wc -l | tr -d ' ')
START_TIME=$(date +%s)
W=60

hline() { printf "  +"; for i in $(seq 1 $W); do printf "%s" "$1"; done; printf "+\n"; }
row() {
    local text="$1"
    local vlen=${#text}
    local pad=$((W - vlen))
    if [ $pad -lt 0 ]; then pad=0; text="${text:0:$W}"; fi
    printf "  |%s%*s|\n" "$text" "$pad" ""
}
blank() { printf "  |%*s|\n" $W ""; }
twoCol() {
    local left="$1" right="$2"
    local mid=$((W - 4 - ${#left} - ${#right}))
    if [ $mid -lt 1 ]; then mid=1; fi
    row "  ${left}$(printf '%*s' $mid '')${right}  "
}

while true; do
    clear
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    TRACKS=$(find "$DRIVE" -name "*.m4a" 2>/dev/null | wc -l | tr -d ' ')
    SIZE=$(du -sh "$DRIVE" 2>/dev/null | cut -f1 | tr -d ' ')
    ARTISTS=$(ls "$DRIVE" 2>/dev/null | wc -l | tr -d ' ')
    ALAC=$(find "$DRIVE" -name "*.m4a" -size +20M 2>/dev/null | wc -l | tr -d ' ')

    # Get current URL progress from log
    CURRENT_URL=$(grep -o '\[.*/.* \]' "$LOG" 2>/dev/null | tail -1 | tr -d '[]' | tr -d ' ')
    if [ -z "$CURRENT_URL" ]; then CURRENT_URL="--"; fi

    # Speed & ETA
    NEW_TRACKS=$((TRACKS - START_TRACKS))
    if [ $NEW_TRACKS -gt 0 ] && [ $ELAPSED -gt 0 ]; then
        SPT=$((ELAPSED / NEW_TRACKS))
        SPEED="~${SPT}s/track"
    else
        SPEED="--"
    fi

    E_H=$((ELAPSED / 3600)); E_M=$(( (ELAPSED % 3600) / 60 )); E_S=$((ELAPSED % 60))
    if [ $E_H -gt 0 ]; then EFMT="${E_H}h ${E_M}m ${E_S}s"
    elif [ $E_M -gt 0 ]; then EFMT="${E_M}m ${E_S}s"
    else EFMT="${E_S}s"; fi

    # Status
    if pgrep -f resume_download > /dev/null 2>&1; then
        STATUS="DOWNLOADING"
        STATICON=">>"
    else
        STATUS="STOPPED"
        STATICON="[]"
    fi

    TITLE="Vinyl - Library Download"
    TPAD=$(( (W - ${#TITLE}) / 2 ))

    echo ""
    hline "="
    blank
    row "$(printf '%*s%s' $TPAD '' "$TITLE")"
    blank
    twoCol "Tracks: $TRACKS" "New: +$NEW_TRACKS"
    twoCol "Artists: $ARTISTS" "Speed: $SPEED"
    twoCol "Size: $SIZE" "Elapsed: $EFMT"
    twoCol "Progress: $CURRENT_URL" "Status: $STATUS"
    blank
    hline "="
    blank
    row "  Recent downloads:"
    blank

    grep "Song saved" "$LOG" 2>/dev/null | tail -10 | while read -r line; do
        SONG=$(echo "$line" | sed 's/.*| SONG | //' | sed 's/ | SUCCESS.*//')
        row "    + ${SONG:0:52}"
    done

    blank
    row "  $STATICON $STATUS"
    blank
    hline "="
    echo ""
    echo "    Ctrl+C to exit monitor (downloads continue)"

    sleep 5
done
