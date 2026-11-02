#!/usr/bin/env bash
# Circle-Design aus dem Fundus-Portal übernehmen (tokens.css, app.css, Schriften) und die Review-App veröffentlichen.
#   ./build.sh            nur bauen      ./build.sh deploy    bauen und hosting + rules veröffentlichen
set -euo pipefail
cd "$(dirname "$0")"
rm -rf public/vendor && mkdir -p public/vendor/css public/vendor/fonts
cp ../portal/css/tokens.css ../portal/css/app.css public/vendor/css/
cp ../portal/fonts/*.woff2 public/vendor/fonts/
if [ "${1:-}" = deploy ]; then
  firebase deploy --project fundus-review --only hosting,firestore:rules,storage
fi
