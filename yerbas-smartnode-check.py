#!/usr/bin/env python3

"""Yerbas Smartnode Health Checker.

Checks registered Yerbas smartnodes, enriches results, stores history, creates
CSV/JSON/HTML reports, and can send Discord alerts when network health changes.
The standard library is sufficient; optional GeoIP support uses the ``geoip2``
package and local MaxMind-compatible MMDB databases.
"""

from __future__ import annotations

import argparse
import csv
import html
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

DEFAULT_PORT = 15420
DEFAULT_TIMEOUT = 3.0
DEFAULT_THREADS = 50
DEFAULT_RETRIES = 1


@dataclass
class SmartnodeResult:
    outpoint: str
    advertised_address: str
    ip: str
    port: int
    address_valid: bool
    public_ip: bool
    expected_port: bool
    port_open: bool
    attempts: int
    latency_ms: float | None
    reverse_dns: str
    protocol: int | None
    protocol_current: bool | None
    smartnode_status: str
    duplicate_ip: bool
    duplicate_count: int
    country: str
    country_code: str
    city: str
    asn: int | None
    organization: str
    error: str


def run_cli(cli: str, *arguments: str) -> Any:
    command = [cli, *arguments]
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cannot find {cli}. Specify its path with --cli.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out after 30 seconds.") from exc

    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {message or 'unknown error'}")

    output = process.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{' '.join(command)} did not return valid JSON:\n{output}") from exc


def validate_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is outside the valid range")


def parse_address(advertised_address: str, default_port: int) -> tuple[str, int]:
    address = advertised_address.strip()
    if not address:
        raise ValueError("empty advertised address")

    if address.startswith("["):
        closing = address.find("]")
        if closing == -1:
            raise ValueError("invalid bracketed IPv6 address")
        host = address[1:closing]
        remainder = address[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            raise ValueError("invalid IPv6 port separator")
        port = int(remainder[1:]) if remainder else default_port
        parsed_ip = ipaddress.ip_address(host)
        validate_port(port)
        return str(parsed_ip), port

    try:
        parsed_ip = ipaddress.ip_address(address)
        return str(parsed_ip), default_port
    except ValueError:
        pass

    host, port_text = address.rsplit(":", 1) if ":" in address else (address, str(default_port))
    parsed_ip = ipaddress.ip_address(host)
    port = int(port_text)
    validate_port(port)
    return str(parsed_ip), port


def normalize_rpc_map(value: Any, fields: tuple[str, ...] = ()) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str):
            normalized[str(key)] = item
        elif isinstance(item, (int, float)):
            normalized[str(key)] = str(item)
        elif isinstance(item, dict):
            selected = next((item.get(field) for field in fields if item.get(field) is not None), None)
            if selected is None:
                selected = item.get("result", "")
            normalized[str(key)] = str(selected)
        else:
            normalized[str(key)] = str(item)
    return normalized


def get_optional_mode(cli: str, mode: str, fields: tuple[str, ...] = ()) -> dict[str, str]:
    try:
        return normalize_rpc_map(run_cli(cli, "smartnodelist", mode), fields)
    except RuntimeError:
        return {}


def parse_protocol(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def reverse_dns_lookup(ip: str, enabled: bool) -> str:
    if not enabled:
        return ""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""

def parse_full_maps(value: Any) -> tuple[dict[str, str], dict[str, str]]:
    status_map: dict[str, str] = {}
    protocol_map: dict[str, str] = {}

    if not isinstance(value, dict):
        return status_map, protocol_map

    for outpoint, raw_value in value.items():
        if not isinstance(raw_value, str):
            continue

        parts = raw_value.split()

        if len(parts) < 2:
            continue

        status_map[str(outpoint)] = parts[0]
        protocol_map[str(outpoint)] = parts[1]

    return status_map, protocol_map

def check_tcp_port(ip: str, port: int, timeout: float, retries: int) -> tuple[bool, float | None, int, str]:
    last_error = ""
    latencies: list[float] = []
    attempts = max(1, retries + 1)
    address = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET

    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                endpoint = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
                connection.connect(endpoint)
            latencies.append((time.perf_counter() - start) * 1000)
            return True, round(mean(latencies), 2), attempt, ""
        except socket.timeout:
            last_error = "connection timed out"
        except ConnectionRefusedError:
            last_error = "connection refused"
        except OSError as exc:
            last_error = str(exc)

        if attempt < attempts:
            time.sleep(min(0.25 * attempt, 1.0))

    return False, None, attempts, last_error


class GeoIPLookup:
    def __init__(self, city_db: Path | None, asn_db: Path | None):
        self.city_reader = None
        self.asn_reader = None
        self.error = ""
        if not city_db and not asn_db:
            return
        try:
            import geoip2.database  # type: ignore
        except ImportError:
            self.error = "GeoIP requested but geoip2 is not installed"
            return
        try:
            if city_db:
                self.city_reader = geoip2.database.Reader(str(city_db))
            if asn_db:
                self.asn_reader = geoip2.database.Reader(str(asn_db))
        except Exception as exc:
            self.error = f"GeoIP database error: {exc}"

    def lookup(self, ip: str) -> tuple[str, str, str, int | None, str]:
        country = country_code = city = organization = ""
        asn = None
        if self.city_reader:
            try:
                record = self.city_reader.city(ip)
                country = record.country.name or ""
                country_code = record.country.iso_code or ""
                city = record.city.name or ""
            except Exception:
                pass
        if self.asn_reader:
            try:
                record = self.asn_reader.asn(ip)
                asn = record.autonomous_system_number
                organization = record.autonomous_system_organization or ""
            except Exception:
                pass
        return country, country_code, city, asn, organization

    def close(self) -> None:
        for reader in (self.city_reader, self.asn_reader):
            if reader:
                reader.close()


def inspect_smartnode(
    outpoint: str,
    advertised_address: str,
    status: str,
    protocol: int | None,
    minimum_protocol: int | None,
    duplicate_counts: Counter[str],
    expected_port: int,
    timeout: float,
    retries: int,
    reverse_dns: bool,
    geoip: GeoIPLookup,
) -> SmartnodeResult:
    try:
        ip, port = parse_address(advertised_address, expected_port)
        parsed_ip = ipaddress.ip_address(ip)
    except (ValueError, TypeError) as exc:
        return SmartnodeResult(
            outpoint, advertised_address, "", expected_port, False, False, False,
            False, 0, None, "", protocol, None, status or "UNKNOWN", False, 0,
            "", "", "", None, "", f"invalid advertised address: {exc}"
        )

    public_ip = parsed_ip.is_global
    open_status, latency, attempts, error = check_tcp_port(ip, port, timeout, retries)
    hostname = reverse_dns_lookup(ip, reverse_dns)
    country, country_code, city, asn, organization = geoip.lookup(ip)
    duplicate_count = duplicate_counts[ip]
    protocol_current = None if minimum_protocol is None or protocol is None else protocol >= minimum_protocol

    return SmartnodeResult(
        outpoint=outpoint,
        advertised_address=advertised_address,
        ip=ip,
        port=port,
        address_valid=True,
        public_ip=public_ip,
        expected_port=port == expected_port,
        port_open=open_status,
        attempts=attempts,
        latency_ms=latency,
        reverse_dns=hostname,
        protocol=protocol,
        protocol_current=protocol_current,
        smartnode_status=status or "UNKNOWN",
        duplicate_ip=duplicate_count > 1,
        duplicate_count=duplicate_count,
        country=country,
        country_code=country_code,
        city=city,
        asn=asn,
        organization=organization,
        error=error,
    )


def build_report(results: list[SmartnodeResult], port: int) -> dict[str, Any]:
    # Raw port counts continue to include every registered Smartnode.
    reachable = sum(result.port_open for result in results)
    unreachable = len(results) - reachable

    # Reachability percentage only measures Smartnodes whose RPC status is ENABLED.
    # All other statuses are excluded from the percentage denominator.
    reachability_results = [
        result
        for result in results
        if result.smartnode_status.upper() == "ENABLED"
    ]
    reachability_reachable = sum(result.port_open for result in reachability_results)
    reachability_unreachable = len(reachability_results) - reachability_reachable
    reachability_eligible = len(reachability_results)
    reachability_excluded = len(results) - reachability_eligible
    reachability_percent = (
        round((reachability_reachable / reachability_eligible) * 100, 2)
        if reachability_eligible
        else 0
    )

    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
    statuses = Counter(result.smartnode_status.upper() for result in results)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network_port": port,
        "summary": {
            "total": len(results),
            "reachable": reachable,
            "unreachable": unreachable,
            "reachability_percent": reachability_percent,
            "reachability_eligible": reachability_eligible,
            "reachability_reachable": reachability_reachable,
            "reachability_unreachable": reachability_unreachable,
            "reachability_excluded": reachability_excluded,
            "reachability_excluded_unknown": statuses.get("UNKNOWN", 0),
            "reachability_excluded_pose_banned": statuses.get("POSE_BANNED", 0),
            "enabled": statuses.get("ENABLED", 0),
            "invalid_addresses": sum(not result.address_valid for result in results),
            "private_or_reserved_ips": sum(result.address_valid and not result.public_ip for result in results),
            "wrong_port": sum(result.address_valid and not result.expected_port for result in results),
            "duplicate_ips": len({result.ip for result in results if result.duplicate_ip and result.ip}),
            "outdated_protocol": sum(result.protocol_current is False for result in results),
            "average_latency_ms": round(mean(latencies), 2) if latencies else None,
            "statuses": dict(sorted(statuses.items())),
        },
        "smartnodes": [asdict(result) for result in results],
    }

def export_csv(results: list[SmartnodeResult], filename: Path) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    with filename.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(SmartnodeResult.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)


def export_json(report: dict[str, Any], filename: Path) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(json.dumps(report, indent=2), encoding="utf-8")


def export_history(report: dict[str, Any], history_dir: Path) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = history_dir / f"yerbas-smartnodes-{timestamp}.json"
    export_json(report, filename)
    return filename


def export_html(report: dict[str, Any], filename: Path) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    summary = report["summary"]
    rows = []
    for node in report["smartnodes"]:
        state = "OPEN" if node["port_open"] else "CLOSED"
        location = ", ".join(filter(None, [node["city"], node["country_code"]]))
        flags = []
        if node["duplicate_ip"]:
            flags.append(f"duplicate x{node['duplicate_count']}")
        if not node["expected_port"]:
            flags.append("wrong port")
        if node["protocol_current"] is False:
            flags.append("outdated protocol")
        if not node["public_ip"]:
            flags.append("non-public IP")
        rows.append(
            "<tr>"
            f"<td>{html.escape(node['advertised_address'])}</td>"
            f"<td>{state}</td>"
            f"<td>{html.escape(node['smartnode_status'])}</td>"
            f"<td>{node['protocol'] or ''}</td>"
            f"<td>{node['latency_ms'] if node['latency_ms'] is not None else ''}</td>"
            f"<td>{html.escape(node['reverse_dns'])}</td>"
            f"<td>{html.escape(location)}</td>"
            f"<td>{html.escape(node['organization'])}</td>"
            f"<td>{html.escape(', '.join(flags) or node['error'])}</td>"
            "</tr>"
        )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yerbas Smartnode Health</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f5f7f5;color:#172217}}h1{{margin-bottom:.25rem}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.5rem 0}}
.card{{background:white;padding:1rem;border-radius:.6rem;box-shadow:0 1px 4px #0002}}.value{{font-size:1.7rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;background:white;font-size:.9rem}}th,td{{padding:.55rem;border-bottom:1px solid #ddd;text-align:left}}th{{position:sticky;top:0;background:#e8eee8}}tr:hover{{background:#f3f8f3}}.scroll{{overflow:auto;max-height:70vh}}
</style></head><body>
<h1>Yerbas Smartnode Health</h1><div>Generated {html.escape(report['generated_at'])}</div>
<div class="cards">
<div class="card"><div>Registered</div><div class="value">{summary['total']}</div></div>
<div class="card"><div>Reachable</div><div class="value">{summary['reachable']}</div></div>
<div class="card"><div>Reachability</div><div class="value">{summary['reachability_percent']}%</div></div>
<div class="card"><div>Enabled</div><div class="value">{summary['enabled']}</div></div>
<div class="card"><div>Outdated</div><div class="value">{summary['outdated_protocol']}</div></div>
<div class="card"><div>Duplicate IPs</div><div class="value">{summary['duplicate_ips']}</div></div>
</div>
<div class="scroll"><table><thead><tr><th>Address</th><th>Port</th><th>RPC status</th><th>Protocol</th><th>Latency ms</th><th>Reverse DNS</th><th>Location</th><th>ASN / Provider</th><th>Flags / Error</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</body></html>"""
    filename.write_text(document, encoding="utf-8")


def load_previous_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def alert_lines(report: dict[str, Any], previous: dict[str, Any] | None, threshold: float) -> list[str]:
    summary = report["summary"]
    lines: list[str] = []
    if summary["reachability_percent"] < threshold:
        lines.append(f"Reachability is {summary['reachability_percent']}%, below {threshold}%.")
    if summary["outdated_protocol"]:
        lines.append(f"{summary['outdated_protocol']} smartnode(s) use an outdated protocol.")
    if summary["wrong_port"]:
        lines.append(f"{summary['wrong_port']} smartnode(s) advertise a non-15420 port.")
    if previous:
        old_nodes = {item["outpoint"]: item for item in previous.get("smartnodes", [])}
        new_nodes = {item["outpoint"]: item for item in report.get("smartnodes", [])}
        appeared = new_nodes.keys() - old_nodes.keys()
        disappeared = old_nodes.keys() - new_nodes.keys()
        went_down = [key for key in new_nodes.keys() & old_nodes.keys() if old_nodes[key].get("port_open") and not new_nodes[key].get("port_open")]
        recovered = [key for key in new_nodes.keys() & old_nodes.keys() if not old_nodes[key].get("port_open") and new_nodes[key].get("port_open")]
        if appeared:
            lines.append(f"{len(appeared)} new smartnode registration(s) appeared.")
        if disappeared:
            lines.append(f"{len(disappeared)} smartnode registration(s) disappeared.")
        if went_down:
            lines.append(f"{len(went_down)} previously reachable smartnode(s) are now unreachable.")
        if recovered:
            lines.append(f"{len(recovered)} smartnode(s) recovered.")
    return lines


def send_discord(webhook_url: str, report: dict[str, Any], lines: list[str]) -> None:
    summary = report["summary"]
    content = "**Yerbas Smartnode Health Alert**\n" + "\n".join(f"â€¢ {line}" for line in lines)
    content += f"\n\nReachable: {summary['reachable']}/{summary['total']} ({summary['reachability_percent']}%)"
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": content[:2000]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Yerbas-Smartnode-Check/2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord webhook returned HTTP {response.status}")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Discord webhook failed: {exc}") from exc


def print_result(result: SmartnodeResult, color: bool) -> None:
    state = "INVALID" if not result.address_valid else ("OPEN" if result.port_open else "CLOSED")
    if color:
        code = "\033[32m" if state == "OPEN" else "\033[31m"
        state = f"{code}{state}\033[0m"
    latency = f"{result.latency_ms:.2f}" if result.latency_ms is not None else "-"
    flags = []
    if result.duplicate_ip:
        flags.append(f"DUP({result.duplicate_count})")
    if result.address_valid and not result.expected_port:
        flags.append("WRONG_PORT")
    if result.protocol_current is False:
        flags.append("OUTDATED")
    print(f"{result.advertised_address[:39]:<40} {state:<17} {latency:<10} {result.smartnode_status[:14]:<14} {' '.join(flags)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Yerbas smartnode health and create reports.")
    parser.add_argument("--cli", default="yerbas-cli", help="Path to yerbas-cli")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Expected Yerbas P2P port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Connection timeout in seconds")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="Maximum concurrent checks")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after the initial connection attempt")
    parser.add_argument("--minimum-protocol", type=int, help="Flag protocol versions below this value")
    parser.add_argument("--reverse-dns", action="store_true", help="Perform reverse DNS lookups")
    parser.add_argument("--geoip-city-db", type=Path, help="Path to GeoLite2-City.mmdb")
    parser.add_argument("--geoip-asn-db", type=Path, help="Path to GeoLite2-ASN.mmdb")
    parser.add_argument("--csv", type=Path, default=Path("yerbas-smartnodes.csv"), help="CSV report filename")
    parser.add_argument("--json", type=Path, default=Path("yerbas-smartnodes.json"), help="JSON report filename")
    parser.add_argument("--html", type=Path, default=Path("yerbas-smartnodes.html"), help="HTML dashboard filename")
    parser.add_argument("--history-dir", type=Path, help="Store timestamped JSON snapshots in this directory")
    parser.add_argument("--previous", type=Path, help="Previous JSON report used to detect changes")
    parser.add_argument("--discord-webhook", default=os.getenv("DISCORD_WEBHOOK_URL"), help="Discord webhook URL or DISCORD_WEBHOOK_URL")
    parser.add_argument("--alert-below", type=float, default=95.0, help="Alert when reachability falls below this percentage")
    parser.add_argument("--always-alert", action="store_true", help="Send a Discord summary even when no alert condition exists")
    parser.add_argument("--open-only", action="store_true", help="Only print endpoints with an open port")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors")
    arguments = parser.parse_args()

    try:
        validate_port(arguments.port)
    except ValueError as exc:
        parser.error(str(exc))
    if arguments.timeout <= 0 or arguments.threads < 1 or arguments.retries < 0:
        parser.error("timeout and threads must be positive; retries cannot be negative")
    if not 0 <= arguments.alert_below <= 100:
        parser.error("--alert-below must be between 0 and 100")
    if shutil.which(arguments.cli) is None and not Path(arguments.cli).exists():
        print(f"Error: cannot find {arguments.cli}. Use --cli /full/path/to/yerbas-cli", file=sys.stderr)
        return 2

    try:
        address_map = normalize_rpc_map(
            run_cli(arguments.cli, "smartnodelist", "addr"),
            ("address", "addr"),
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not address_map:
        print(
            "No smartnodes were returned by smartnodelist addr.",
            file=sys.stderr,
        )
        return 2

    status_map = get_optional_mode(
        arguments.cli,
        "status",
        ("status",),
    )

    protocol_map = get_optional_mode(
        arguments.cli,
        "protocol",
        ("protocol", "version"),
    )

    try:
        full_map = run_cli(
            arguments.cli,
            "smartnodelist",
            "full",
        )

        full_status_map, full_protocol_map = parse_full_maps(
            full_map
        )

        if not status_map:
            status_map = full_status_map

        for outpoint, protocol in full_protocol_map.items():
            if not protocol_map.get(outpoint):
                protocol_map[outpoint] = protocol

    except RuntimeError:
        pass

    parsed_ips: list[str] = []
    for advertised_address in address_map.values():
        try:
            ip, _ = parse_address(advertised_address, arguments.port)
            parsed_ips.append(ip)
        except (ValueError, TypeError):
            pass
    duplicate_counts = Counter(parsed_ips)
    geoip = GeoIPLookup(arguments.geoip_city_db, arguments.geoip_asn_db)
    if geoip.error:
        print(f"Warning: {geoip.error}", file=sys.stderr)

    print(f"Checking {len(address_map)} registered Yerbas smartnodes...")
    print(f"{'ADDRESS':<40} {'PORT':<17} {'LATENCY':<10} {'STATUS':<14} FLAGS")
    print("-" * 100)
    results: list[SmartnodeResult] = []
    with ThreadPoolExecutor(max_workers=arguments.threads) as executor:
        futures = {
            executor.submit(
                inspect_smartnode,
                outpoint,
                address,
                status_map.get(outpoint, "UNKNOWN"),
                parse_protocol(protocol_map.get(outpoint, "")),
                arguments.minimum_protocol,
                duplicate_counts,
                arguments.port,
                arguments.timeout,
                arguments.retries,
                arguments.reverse_dns,
                geoip,
            ): outpoint
            for outpoint, address in address_map.items()
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                outpoint = futures[future]
                result = SmartnodeResult(
                    outpoint, address_map[outpoint], "", arguments.port, False, False, False,
                    False, 0, None, "", None, None, status_map.get(outpoint, "UNKNOWN"),
                    False, 0, "", "", "", None, "", f"unexpected checker error: {exc}"
                )
            results.append(result)
            if not arguments.open_only or result.port_open:
                print_result(result, sys.stdout.isatty() and not arguments.no_color)
    geoip.close()

    results.sort(key=lambda item: (not item.port_open, item.ip, item.outpoint))
    previous = load_previous_report(arguments.previous) if arguments.previous else load_previous_report(arguments.json)
    report = build_report(results, arguments.port)
    export_csv(results, arguments.csv)
    export_json(report, arguments.json)
    export_html(report, arguments.html)
    history_path = export_history(report, arguments.history_dir) if arguments.history_dir else None

    summary = report["summary"]
    print("\nYerbas smartnode summary")
    print("-" * 32)
    print(f"Registered        : {summary['total']}")
    print(f"Port open         : {summary['reachable']}")
    print(f"Port unreachable  : {summary['unreachable']}")
    print(f"Reachability      : {summary['reachability_percent']}%")
    print(
        f"Reachability pool : {summary['reachability_eligible']} "
        f"(excluded {summary['reachability_excluded']} non-ENABLED)"
    )
    print(f"RPC status ENABLED: {summary['enabled']}")
    print(f"Average latency   : {summary['average_latency_ms']} ms")
    print(f"Wrong port        : {summary['wrong_port']}")
    print(f"Outdated protocol : {summary['outdated_protocol']}")
    print(f"Duplicate IPs     : {summary['duplicate_ips']}")
    print(f"CSV report        : {arguments.csv}")
    print(f"JSON report       : {arguments.json}")
    print(f"HTML dashboard    : {arguments.html}")
    if history_path:
        print(f"History snapshot  : {history_path}")

    lines = alert_lines(report, previous, arguments.alert_below)
    if arguments.discord_webhook and (lines or arguments.always_alert):
        if not lines:
            lines = ["Scheduled health report completed with no alert conditions."]
        try:
            send_discord(arguments.discord_webhook, report, lines)
            print("Discord alert     : sent")
        except RuntimeError as exc:
            print(f"Warning: {exc}", file=sys.stderr)

    return 0 if summary["unreachable"] == 0 and summary["invalid_addresses"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
