#!/usr/bin/env sh
set -eu

mkdir -p "${DATA_DIR:-/data}"

# Nightly MsZ indexer via supercronic (background), server in foreground.
if [ "${DISABLE_CRON:-0}" != "1" ]; then
  supercronic -quiet /app/crontab &
fi

# --no-access-log: token-in-path variant (/mcp/<token>) sa nesmie zapisovať do access logu.
exec uvicorn zdroje.server:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*' --no-access-log
