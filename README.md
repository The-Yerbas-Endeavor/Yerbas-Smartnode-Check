# Yerbas Smartnode Check

A lightweight Python 3 utility that retrieves the registered Yerbas smartnode list through `yerbas-cli` and checks whether each advertised endpoint accepts TCP connections.

## Features

- Reads smartnodes from `yerbas-cli smartnodelist addr`
- Checks the advertised port, defaulting to Yerbas port `15420`
- Measures TCP connection latency
- Supports IPv4 and IPv6 addresses
- Reads `smartnodelist status` when available
- Detects duplicate IP registrations
- Exports CSV and JSON reports
- Uses concurrent checks for faster scans
- Returns useful exit codes for cron and monitoring

## Requirements

- Python 3.10 or newer
- A running and synchronized Yerbas wallet or daemon
- `yerbas-cli` configured to access the local daemon

No external Python packages are required.

## Installation

```bash
git clone https://github.com/The-Yerbas-Endeavor/Yerbas-Smartnode-Check.git
cd Yerbas-Smartnode-Check
chmod +x yerbas-smartnode-check.py
```

## Usage

```bash
./yerbas-smartnode-check.py
```

When `yerbas-cli` is not in your `PATH`:

```bash
./yerbas-smartnode-check.py --cli "$HOME/yerbas/src/yerbas-cli"
```

Faster scan with a two-second timeout:

```bash
./yerbas-smartnode-check.py --threads 100 --timeout 2
```

Display only smartnodes whose ports are open:

```bash
./yerbas-smartnode-check.py --open-only
```

Choose report locations:

```bash
./yerbas-smartnode-check.py \
  --csv /var/www/html/yerbas-smartnodes.csv \
  --json /var/www/html/yerbas-smartnodes.json
```

## Options

```text
--cli PATH       Path to yerbas-cli
--port PORT      Fallback port when an address has no port; default 15420
--timeout SEC    TCP connection timeout; default 3 seconds
--threads COUNT  Maximum concurrent checks; default 50
--csv FILE       CSV output file
--json FILE      JSON output file
--open-only      Only print endpoints with an open port
```

## Exit codes

- `0` — all advertised endpoints were reachable
- `1` — one or more endpoints were unreachable or invalid
- `2` — configuration, CLI, or RPC failure

## Hourly cron example

```cron
0 * * * * /usr/bin/python3 /home/anno/Yerbas-Smartnode-Check/yerbas-smartnode-check.py --cli /home/anno/yerbas/src/yerbas-cli >> /home/anno/yerbas-smartnode-check.log 2>&1
```

## Important limitation

An open TCP port only proves that a service accepted the connection. It does not prove that the endpoint completed a valid Yerbas peer-to-peer protocol handshake. A future version can add Yerbas `version` and `verack` message validation.

## License

MIT
