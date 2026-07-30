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
- Optional offline GeoIP country, city, ASN, and provider lookup
- Exports CSV, JSON, and a standalone sortable-friendly HTML dashboard
- Stores timestamped JSON history snapshots
- Compares the current report with a previous report to detect new, removed, failed, and recovered nodes
- Sends Discord webhook alerts when health changes or falls below a threshold
- Uses concurrent checks and has useful cron-friendly exit codes

## Requirements

- Python 3.10 or newer
- A running and synchronized Yerbas wallet or daemon
- `yerbas-cli` configured to access the local daemon

The core checker uses only the Python standard library.

GeoIP enrichment is optional and requires:

```bash
python3 -m pip install geoip2
```

It also requires locally downloaded MaxMind-compatible `GeoLite2-City.mmdb` and/or `GeoLite2-ASN.mmdb` databases. The script does not transmit smartnode IP addresses to an external GeoIP service.

## Installation

```bash
git clone https://github.com/The-Yerbas-Endeavor/Yerbas-Smartnode-Check.git
cd Yerbas-Smartnode-Check
chmod +x yerbas-smartnode-check.py
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
  --csv /var/www/html/smartnodes/yerbas-smartnodes.csv \
  --json /var/www/html/smartnodes/yerbas-smartnodes.json \
  --html /var/www/html/smartnodes/index.html \
  --history-dir /var/www/html/smartnodes/history
```

Add offline GeoIP and ASN enrichment:

```bash
./yerbas-smartnode-check.py \
  --geoip-city-db /usr/share/GeoIP/GeoLite2-City.mmdb \
  --geoip-asn-db /usr/share/GeoIP/GeoLite2-ASN.mmdb
```

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

## Main options

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

## Hourly cron example

```cron
0 * * * * cd /home/anno/Yerbas-Smartnode-Check && /usr/bin/python3 ./yerbas-smartnode-check.py --cli /home/anno/yerbas/src/yerbas-cli --retries 2 --minimum-protocol 70223 --json /var/www/html/smartnodes/yerbas-smartnodes.json --html /var/www/html/smartnodes/index.html --history-dir /var/www/html/smartnodes/history >> /home/anno/yerbas-smartnode-check.log 2>&1
```

Set `DISCORD_WEBHOOK_URL` in a protected environment file or systemd unit rather than putting the secret directly in crontab.

## Explorer integration readiness

`yerbas-smartnodes.json` is designed as the integration boundary for the Yerbas Explorer. It contains:

- generation timestamp and expected network port
- aggregate reachability and status counts
- protocol, duplicate, wrong-port, and latency statistics
- one normalized object per smartnode
- optional location and ASN/provider fields

The Explorer can initially read this JSON file on a schedule or expose it through an endpoint such as `/ext/smartnodehealth`. A later integration can persist snapshots in MongoDB for uptime charts and historical filtering.

## Exit codes

- `0` — all advertised endpoints were reachable and valid
- `1` — one or more endpoints were unreachable or invalid
- `2` — configuration, CLI, or RPC failure

## Important limitation

An open TCP port proves only that a service accepted the TCP connection. It does not prove the endpoint completed a valid Yerbas peer-to-peer `version`/`verack` handshake. Protocol numbers in this tool come from the local daemon's smartnode RPC view, not from a direct remote P2P handshake.

## License

MIT
