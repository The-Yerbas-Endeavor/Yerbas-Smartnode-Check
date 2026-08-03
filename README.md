# Yerbas Smartnode Check

A Python 3 network-health utility that retrieves registered Yerbas smartnodes through `yerbas-cli`, tests each advertised endpoint, enriches the results, stores historical snapshots, and creates reports suitable for operators or the Yerbas Explorer.

## Features

- Reads addresses, RPC status, and protocol versions from `yerbas-cli smartnodelist`
- Checks the advertised TCP port, expecting Yerbas port `15420`
- Retries failed connections before marking a node unreachable
- Measures connection latency
- Supports IPv4 and IPv6
- Detects invalid, private/reserved, duplicate, and wrong-port addresses
- Flags old protocol versions with `--minimum-protocol`
- Optional reverse-DNS lookup
- Optional local MaxMind-compatible GeoIP lookup
- Optional cached no-account GeoIP enrichment for country, city, latitude, longitude, ASN, and provider data
- Exports CSV, JSON, and a standalone sortable-friendly HTML dashboard
- Stores timestamped JSON history snapshots
- Compares the current report with a previous report to detect new, removed, failed, and recovered nodes
- Sends Discord webhook alerts when health changes or falls below a threshold
- Uses concurrent checks and has useful cron-friendly exit codes

## Requirements

- Python 3.10 or newer
- A running and synchronized Yerbas wallet or daemon
- `yerbas-cli` configured to access the local daemon

The core checker and cached API enricher use only the Python standard library.

Local MaxMind-compatible GeoIP enrichment is optional and requires:

```bash
python3 -m pip install geoip2
```

It also requires locally downloaded `GeoLite2-City.mmdb` and/or `GeoLite2-ASN.mmdb` databases.

## Installation

```bash
git clone https://github.com/The-Yerbas-Endeavor/Yerbas-Smartnode-Check.git
cd Yerbas-Smartnode-Check
chmod +x yerbas-smartnode-check.py enrich-geoip.py enrich-geoip-api.py run-checker.sh
```

## Basic usage

```bash
./yerbas-smartnode-check.py
```

When `yerbas-cli` is not in `PATH`:

```bash
./yerbas-smartnode-check.py --cli "$HOME/yerbas/src/yerbas-cli"
```

Use retries, reverse DNS, and protocol validation:

```bash
./yerbas-smartnode-check.py \
  --retries 2 \
  --reverse-dns \
  --minimum-protocol 70223
```

Create reports directly in a web directory:

```bash
./yerbas-smartnode-check.py \
  --cli "$HOME/yerbas/src/yerbas-cli" \
  --retries 2 \
  --reverse-dns \
  --minimum-protocol 70223 \
  --csv /var/www/html/smartnodes/yerbas-smartnodes.csv \
  --json /var/www/html/smartnodes/yerbas-smartnodes.json \
  --html /var/www/html/smartnodes/index.html \
  --history-dir /var/www/html/smartnodes/history
```

## Local GeoIP enrichment

Use local MaxMind-compatible databases when they are available:

```bash
./yerbas-smartnode-check.py \
  --geoip-city-db /usr/share/GeoIP/GeoLite2-City.mmdb \
  --geoip-asn-db /usr/share/GeoIP/GeoLite2-ASN.mmdb
```

The checker stores country, country code, city, ASN, and provider information in its report when those databases are supplied.

Use `enrich-geoip.py` after the checker when latitude and longitude must also be written into an existing JSON report:

```bash
./enrich-geoip.py \
  --json /home/ex1/smartnode-health/yerbas-smartnodes.json \
  --geoip-city-db /usr/share/GeoIP/GeoLite2-City.mmdb \
  --geoip-asn-db /usr/share/GeoIP/GeoLite2-ASN.mmdb
```

## Cached no-account GeoIP enrichment

`enrich-geoip-api.py` provides a no-account alternative for the Yerbas Explorer Network Map. It performs server-side batch lookups, stores results in a persistent cache, and adds these fields to each Smartnode record:

```text
country
country_code
city
latitude
longitude
asn
organization
geoip_error
```

The first run looks up all uncached public IP addresses. Later runs normally reuse the cache and only query newly discovered addresses.

Run it after the checker creates its JSON report:

```bash
./enrich-geoip-api.py \
  --json /home/ex1/smartnode-health/yerbas-smartnodes.json \
  --cache /home/ex1/smartnode-health/geoip-cache.json
```

Force cached addresses to be refreshed:

```bash
./enrich-geoip-api.py \
  --json /home/ex1/smartnode-health/yerbas-smartnodes.json \
  --cache /home/ex1/smartnode-health/geoip-cache.json \
  --refresh
```

The report is replaced atomically so the explorer does not read a partially written JSON file.

### Verify GeoIP output

```bash
jq '{
  total: (.smartnodes | length),
  geolocated: (
    [.smartnodes[] |
      select(.latitude != null and .longitude != null)
    ] | length
  ),
  countries: .summary.countries
}' /home/ex1/smartnode-health/yerbas-smartnodes.json
```

Example result:

```json
{
  "total": 934,
  "geolocated": 932,
  "countries": 35
}
```

## Safe checker wrapper

`yerbas-smartnode-check.py` intentionally returns exit code `1` when one or more nodes are unreachable or invalid, even though it successfully generated a report. A shell command using `&&` would therefore skip GeoIP enrichment whenever the network is not fully reachable.

Use `run-checker.sh` instead. It treats exit codes `0` and `1` as successful report generation and then runs cached GeoIP enrichment. It skips enrichment only after a real configuration, CLI, or RPC failure with exit code `2`.

Default production paths in `run-checker.sh` are:

```text
CHECKER_DIR=/home/ex1/Yerbas-Smartnode-Check
CLI_PATH=/home/ex1/yerbas-build/yerbas-cli
REPORT_PATH=/home/ex1/smartnode-health/yerbas-smartnodes.json
HISTORY_DIR=/home/ex1/smartnode-health/history
CACHE_PATH=/home/ex1/smartnode-health/geoip-cache.json
MINIMUM_PROTOCOL=70223
```

Run it manually:

```bash
./run-checker.sh
```

Override paths without editing the script:

```bash
CHECKER_DIR=/opt/Yerbas-Smartnode-Check \
CLI_PATH=/opt/yerbas/bin/yerbas-cli \
REPORT_PATH=/var/lib/yerbas-health/yerbas-smartnodes.json \
HISTORY_DIR=/var/lib/yerbas-health/history \
CACHE_PATH=/var/lib/yerbas-health/geoip-cache.json \
MINIMUM_PROTOCOL=70223 \
./run-checker.sh
```

## Cron installation for the Explorer Network Map

Create the report directory and set ownership first:

```bash
sudo mkdir -p /home/ex1/smartnode-health/history
sudo chown -R ex1:ex1 /home/ex1/smartnode-health
```

Open the `ex1` user crontab:

```bash
crontab -e
```

Run the checker and GeoIP enrichment every 15 minutes:

```cron
*/15 * * * * /home/ex1/Yerbas-Smartnode-Check/run-checker.sh >> /home/ex1/smartnode-health/checker.log 2>&1
```

Confirm the installed cron entry:

```bash
crontab -l
```

Monitor future runs:

```bash
tail -f /home/ex1/smartnode-health/checker.log
```

Do not use this form:

```bash
yerbas-smartnode-check.py ... && enrich-geoip-api.py ...
```

The checker frequently returns exit code `1` for normal network-health results, which causes `&&` to skip enrichment and removes map coordinates from the newly generated report.

## Discord alerts

Store the webhook URL in an environment variable rather than placing it in shell history:

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
./yerbas-smartnode-check.py --alert-below 95
```

The checker can alert when:

- network reachability falls below the configured percentage
- a node is using an outdated protocol
- a node advertises the wrong port
- a smartnode registration appears or disappears
- a previously reachable node becomes unreachable
- an unreachable node recovers

The default comparison source is the existing JSON report before it is overwritten. A separate previous report can be selected with `--previous`.

Use `--always-alert` to send a summary after every run.

## Main checker options

```text
--cli PATH              Path to yerbas-cli
--port PORT             Expected Yerbas port; default 15420
--timeout SEC           TCP timeout; default 3 seconds
--threads COUNT         Concurrent checks; default 50
--retries COUNT         Retries after the first attempt; default 1
--minimum-protocol N    Flag protocols below N
--reverse-dns           Resolve hostnames
--geoip-city-db FILE    GeoLite2 City database
--geoip-asn-db FILE     GeoLite2 ASN database
--csv FILE              CSV output
--json FILE             JSON output
--html FILE             HTML dashboard output
--history-dir DIR       Store timestamped JSON snapshots
--previous FILE         Previous report for change detection
--discord-webhook URL   Discord webhook or DISCORD_WEBHOOK_URL
--alert-below PERCENT   Alert threshold; default 95
--always-alert          Send a report even without an alert condition
--open-only             Print only reachable nodes
--no-color              Disable terminal colors
```

## Explorer integration

`yerbas-smartnodes.json` is the integration boundary for the Yerbas Explorer. It contains:

- generation timestamp and expected network port
- aggregate reachability and status counts
- protocol, duplicate, wrong-port, and latency statistics
- one normalized object per Smartnode
- optional country, city, latitude, longitude, ASN, and provider fields

The explorer can read this file through `/ext/smartnodehealth` and `/ext/getnodemap?type=smartnodes`.

Verify the explorer map API:

```bash
curl -s \
  'http://127.0.0.1:3001/ext/getnodemap?type=smartnodes' |
jq '{
  total: (.nodes | length),
  with_coordinates: (
    [.nodes[] |
      select(.latitude != null and .longitude != null)
    ] | length
  ),
  countries: (
    [.nodes[] |
      select(.country_code != null and .country_code != "") |
      .country_code
    ] | unique | length
  )
}'
```

The Yerbas Explorer Network Map is normally available at:

```text
https://explorer.yerbas.org/node-map
```

IP geolocation is approximate. It normally identifies a hosting location or ISP region rather than the physical location of the Smartnode operator.

## Exit codes

- `0` — all advertised endpoints were reachable and valid
- `1` — a valid report was generated, but one or more endpoints were unreachable or invalid
- `2` — configuration, CLI, or RPC failure

## Important limitation

An open TCP port proves only that a service accepted the TCP connection. It does not prove the endpoint completed a valid Yerbas peer-to-peer `version`/`verack` handshake. Protocol numbers in this tool come from the local daemon's Smartnode RPC view, not from a direct remote P2P handshake.

## License

MIT
