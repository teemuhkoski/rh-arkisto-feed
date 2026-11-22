#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

REPO_NAME="${1:-rh-arkisto-feed}"

command -v gh >/dev/null 2>&1 || { echo "Asenna ensin GitHub CLI: brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Kirjaudu ensin: gh auth login"; exit 1; }

OWNER=$(gh api user --jq .login)

python3 scripts/clean_transcripts.py
python3 scripts/build_feed.py

if [[ ! -d .git ]]; then
  git init -b main
fi

git add .gitignore GEMINI.md feed.xml index.html cover.jpg chapters/ transcripts/ analyysit/ data/ scripts/ docs/ .agent/

if ! git diff --cached --quiet; then
  git commit -m "Publish RSS feed, chapters, transcripts, and archive site"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  gh repo create "$OWNER/$REPO_NAME" --public --source=. --remote=origin --push
else
  git push -u origin main
fi

gh api -X POST "repos/$OWNER/$REPO_NAME/pages" \
  -f build_type="legacy" \
  -f "source[branch]=main" \
  -f "source[path]=/" || true

echo ""
echo "Valmis! Syötteen osoite (muutaman minuutin viiveellä Pagesin rakentuessa):"
echo "  RSS:     https://${OWNER}.github.io/${REPO_NAME}/feed.xml"
echo "  Arkisto: https://${OWNER}.github.io/${REPO_NAME}/"
