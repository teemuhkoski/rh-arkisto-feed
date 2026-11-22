#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
BUILD_ARGS=()
COMMIT_MSG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -m|--message)
      COMMIT_MSG="${2:?Anna commit-viesti}"
      shift 2
      ;;
    *)
      BUILD_ARGS+=("$1")
      shift
      ;;
  esac
done

echo "Siivotaan tekstitykset (.vtt)..."
python3 scripts/clean_transcripts.py

echo "Generoidaan feed.xml ja index.html..."
python3 scripts/build_feed.py "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  echo "[DRY RUN] feed.xml, index.html ja tekstitykset päivitetty paikallisesti."
  git status -s
  exit 0
fi

git branch -M main

# Poista vanha jaksonumeroton chapters.json, jos se on vielä seurannassa
if git ls-files --error-unmatch chapters.json >/dev/null 2>&1; then
  git rm -f chapters.json
fi

# Lisää julkaistavat tiedostot, analyysit, data ja skriptit (.gitignore suojaa MP3- ja raaka-.srt-tiedostot)
git add .gitignore GEMINI.md feed.xml index.html cover.jpg chapters/ transcripts/ analyysit/ data/ scripts/ docs/ .agent/

EP_COUNT=$(grep -c '<item>' feed.xml || true)
if [[ -z "$COMMIT_MSG" ]]; then
  COMMIT_MSG="Update RSS feed and archive (${EP_COUNT} episodes)"
fi

if git diff --cached --quiet; then
  echo "Ei uusia julkaistavia muutoksia."
else
  git commit -m "$COMMIT_MSG"
  git push -u origin main
fi

if command -v gh >/dev/null 2>&1; then
  gh repo edit teemuhkoski/rh-arkisto-feed --default-branch main || true
fi

git push origin --delete master 2>/dev/null || true

echo ""
echo "Valmis! Syöte päivitetty (${EP_COUNT} jaksoa):"
echo "  RSS:     https://teemuhkoski.github.io/rh-arkisto-feed/feed.xml"
echo "  Arkisto: https://teemuhkoski.github.io/rh-arkisto-feed/"
