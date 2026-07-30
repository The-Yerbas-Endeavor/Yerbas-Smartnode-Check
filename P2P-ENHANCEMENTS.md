# P2P, Dashboard, and Uptime Enhancements

This branch adds a second-stage enrichment pass to the existing smartnode checker. GeoIP remains optional and is not required.

## What is added

- Direct Yerbas `version` / `verack` P2P handshake
- Yerbas mainnet magic validation: `79 65 72 62` (`yerb`)
- Remote protocol version
- Remote user agent / wallet version
- Remote service flags
- Remote reported block height
- Relay preference
- Full handshake duration
- `ping` / `pong` handling during handshake
- Health classification:
  - `HEALTHY`
  - `SLOW`
  - `OUTDATED`
  - `OFFLINE`
  - `NOT_YERBAS`
  - `INVALID`
- Searchable and filterable HTML dashboard
- Snapshot-based uptime percentage
- First seen and last seen timestamps
- Consecutive failure count
- Explorer-ready enriched JSON fields

## Run the normal checker first

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

## Run the P2P and uptime enrichment pass

```bash
./yerbas-smartnode-enhance.py \
  --json /var/www/html/smartnodes/yerbas-smartnodes.json \
  --html /var/www/html/smartnodes/index.html \
  --history-dir /var/www/html/smartnodes/history \
  --protocol 70223 \
  --minimum-protocol 70223 \
  --threads 30 \
  --timeout 4 \
  --slow-ms 500
```

The second command updates the same JSON report and replaces the basic HTML report with the enhanced searchable dashboard.

## One-command shell sequence

```bash
cd /home/anno/Yerbas-Smartnode-Check-agent-p2p-dashboard-history

./yerbas-smartnode-check.py \
  --cli "$HOME/yerbas/src/yerbas-cli" \
  --retries 2 \
  --reverse-dns \
  --minimum-protocol 70223 \
  --csv /var/www/html/smartnodes/yerbas-smartnodes.csv \
  --json /var/www/html/smartnodes/yerbas-smartnodes.json \
  --html /var/www/html/smartnodes/index.html \
  --history-dir /var/www/html/smartnodes/history

./yerbas-smartnode-enhance.py \
  --json /var/www/html/smartnodes/yerbas-smartnodes.json \
  --html /var/www/html/smartnodes/index.html \
  --history-dir /var/www/html/smartnodes/history
```

The first checker may return exit code `1` when any node is offline. To ensure the enrichment step still runs in cron, separate the commands with `;` rather than `&&`, or explicitly capture the first exit code.

## Cron example

```cron
0 * * * * cd /home/anno/Yerbas-Smartnode-Check-agent-p2p-dashboard-history; ./yerbas-smartnode-check.py --cli /home/anno/yerbas/src/yerbas-cli --retries 2 --reverse-dns --minimum-protocol 70223 --csv /var/www/html/smartnodes/yerbas-smartnodes.csv --json /var/www/html/smartnodes/yerbas-smartnodes.json --html /var/www/html/smartnodes/index.html --history-dir /var/www/html/smartnodes/history; ./yerbas-smartnode-enhance.py --json /var/www/html/smartnodes/yerbas-smartnodes.json --html /var/www/html/smartnodes/index.html --history-dir /var/www/html/smartnodes/history >> /home/anno/yerbas-smartnode-check.log 2>&1
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Explorer integration

The enriched JSON remains suitable for an Explorer endpoint such as:

```text
/ext/smartnodehealth
```

Each smartnode now includes fields prefixed with `p2p_`, plus:

- `health_status`
- `uptime_percent`
- `samples`
- `online_samples`
- `consecutive_failures`
- `first_seen`
- `last_seen`

The report summary includes P2P verification and health-state counts that can be displayed directly as Explorer dashboard cards.
