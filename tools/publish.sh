#!/bin/zsh
# Publish a scrubbed copy of Arthur to its public GitHub repo. Aborts if anything personal remains.
# Needs tools/scrub_map.json (gitignored) — copy tools/scrub_map.example.json and fill in your own names.
# Usage: tools/publish.sh "commit message" [--shots]   (set ARTHUR_REPO=owner/name to override)
set -e
cd "${0:A:h}/.."
REPO=${ARTHUR_REPO:-thecozycod3r/arthur}
MAP=tools/scrub_map.json
[ -f $MAP ] || { echo "missing $MAP (see tools/scrub_map.example.json)"; exit 1; }
W=${TMPDIR:-/tmp}/arthur-publish; rm -rf $W
git clone -q https://github.com/$REPO $W
rsync -a --exclude '__pycache__' --exclude '*.pyc' arthur ui $W/
rsync -a --exclude 'results_*.json' tests/ $W/tests/
rsync -a --exclude 'scrub_map.json' tools/ $W/tools/
cp tools/README.public.md $W/README.md
cp SETUP.md $W/SETUP.md
.venv/bin/python tools/scrub.py $W
NAMES=$(.venv/bin/python -c "import json;print(json.load(open('$MAP'))['leak_names'])")
SECRETS=$(.venv/bin/python -c "import json;print(json.load(open('$MAP'))['leak_secrets'])")
if grep -rIlqiE "$NAMES" --exclude-dir=.git --exclude=scrub_map.example.json $W || grep -rIlqE "$SECRETS" --exclude-dir=.git --exclude=scrub_map.example.json $W; then
  echo "ABORT: personal data or secrets found:"; grep -rIniE "$NAMES|$SECRETS" --exclude-dir=.git --exclude=scrub_map.example.json $W | head; exit 1
fi
[ "$2" = "--shots" ] && node tests/demo_shots.mjs $W/docs >/dev/null
cd $W; git add -A
git diff --cached --quiet && { echo "nothing to publish"; exit 0; }
OWNER=${REPO%%/*}; ID=$(gh api users/$OWNER --jq .id); PREV=$(gh api user --jq .login)
gh auth switch -h github.com -u $OWNER >/dev/null
git -c user.name=$OWNER -c user.email="${ID}+${OWNER}@users.noreply.github.com" commit -q -m "${1:-Update Arthur}"
git push -q; gh auth switch -h github.com -u $PREV >/dev/null
echo "published: $(git log --oneline -1)"
