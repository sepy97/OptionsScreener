#!/usr/bin/env bash
# Fetch the private fundamental-analysis engine into vendor/, for the image build to install.
#
# The engine is NOT a dependency of this project (see README): it lives in a private repo, so it
# is absent from pyproject/uv.lock and this repository stays installable by anyone. It is pulled
# in HERE, at deploy time, outside the Docker build -- which is why the build needs no registry
# credentials, no BuildKit secret and no git binary.
#
# Without a token this is a deliberate NO-OP: the image builds fine and the Fundamentals tab
# explains that it isn't deployed. Nothing else in the app is affected.
#
# Run from the deploy checkout:  bash deploy/fetch_fundcore.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="${FUNDCORE_REPO:-sepy97/StockAnalysis}"
REF="$(tr -d '[:space:]' < deploy/fundcore.version)"

mkdir -p vendor
rm -f vendor/*.whl   # never install a stale version left by an earlier deploy

# The token normally lives in the deploy directory's .env alongside the other secrets.
if [ -z "${GH_TOKEN:-}" ] && [ -f .env ]; then
  GH_TOKEN="$(sed -n 's/^GH_TOKEN=//p' .env | tail -1 | tr -d '"'"'"'[:space:]')"
fi

if [ -z "${GH_TOKEN:-}" ]; then
  echo "GH_TOKEN not set -- skipping the analysis engine; the Fundamentals tab will be disabled."
  exit 0
fi

auth=(-H "Authorization: Bearer $GH_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28")
release="https://api.github.com/repos/$REPO/releases/tags/$REF"

# Private release assets are only downloadable through the asset API by id, so resolve it first.
# Keep the asset's OWN filename: a wheel's name encodes its version and compatibility tags, and
# pip/uv reject a renamed file ("Must have a Python tag").
read -r asset_id asset_name <<<"$(curl -fsSL "${auth[@]}" "$release" | python3 -c '
import json, sys
wheels = [a for a in json.load(sys.stdin).get("assets", []) if a["name"].endswith(".whl")]
print(wheels[0]["id"], wheels[0]["name"]) if wheels else print("", "")
')"

if [ -z "$asset_id" ]; then
  echo "ERROR: no .whl asset on $REPO release $REF (is the release-wheel workflow green?)" >&2
  exit 1
fi

curl -fsSL "${auth[@]}" -H "Accept: application/octet-stream" \
  -o "vendor/$asset_name" \
  "https://api.github.com/repos/$REPO/releases/assets/$asset_id"

echo "fetched analysis engine $REF -> vendor/$asset_name ($(wc -c < "vendor/$asset_name") bytes)"
