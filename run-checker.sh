#!/usr/bin/env bash

set -u

CHECKER_DIR="${CHECKER_DIR:-/home/ex1/Yerbas-Smartnode-Check}"
CLI_PATH="${CLI_PATH:-/home/ex1/yerbas-build/yerbas-cli}"
REPORT_PATH="${REPORT_PATH:-/home/ex1/smartnode-health/yerbas-smartnodes.json}"
HISTORY_DIR="${HISTORY_DIR:-/home/ex1/smartnode-health/history}"
CACHE_PATH="${CACHE_PATH:-/home/ex1/smartnode-health/geoip-cache.json}"
MINIMUM_PROTOCOL="${MINIMUM_PROTOCOL:-70223}"

cd "$CHECKER_DIR" || exit 2

/usr/bin/python3 ./yerbas-smartnode-check.py \
  --cli "$CLI_PATH" \
  --retries 2 \
  --reverse-dns \
  --minimum-protocol "$MINIMUM_PROTOCOL" \
  --json "$REPORT_PATH" \
  --history-dir "$HISTORY_DIR"

CHECKER_EXIT=$?

# Exit code 0 means all checks passed. Exit code 1 still means a valid report
# was generated, but one or more nodes were unreachable or invalid.
if [ "$CHECKER_EXIT" -le 1 ]; then
  /usr/bin/python3 ./enrich-geoip-api.py \
    --json "$REPORT_PATH" \
    --cache "$CACHE_PATH"
  exit $?
fi

echo "Checker failed with exit code $CHECKER_EXIT; skipping GeoIP enrichment." >&2
exit "$CHECKER_EXIT"
