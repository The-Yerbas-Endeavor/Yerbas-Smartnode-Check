#!/usr/bin/env python3

"""Yerbas Smartnode Health Checker.

Retrieves registered smartnodes through yerbas-cli, checks each advertised TCP
endpoint, measures connection latency, detects duplicate IP addresses, and
exports CSV and JSON reports.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PORT = 15420
DEFAULT_TIMEOUT = 3.0
DEFAULT_THREADS = 50


@dataclass
class SmartnodeResult:
    outpoint: str
    advertised_address: str
    ip: str
    port: int
    address_valid: bool
    port_open: bool
    latency_ms: float | None
    smartnode_status: str
    duplicate_ip: bool
    duplicate_count: int
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
        raise RuntimeError(
            f"{' '.join(command)} did not return valid JSON:\n{output}"
        ) from exc


def validate_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is outside the valid range")


def parse_address(advertised_address: str, default_port: int) -> tuple[str, int]:
    address = advertised_address.strip()
    if not address:
        raise ValueError("empty advertised address")

    if address.startswith("["):
        closing_bracket = address.find("]")
        if closing_bracket == -1:
            raise ValueError("invalid bracketed IPv6 address")

        host = address[1:closing_bracket]
        remainder = address[closing_bracket + 1 :]
        port = int(remainder[1:]) if remainder.startswith(":") else default_port
        if remainder and not remainder.startswith(":"):
            raise ValueError("invalid IPv6 port separator")

        parsed_ip = ipaddress.ip_address(host)
        validate_port(port)
        return str(parsed_ip), port

    try:
        parsed_ip = ipaddress.ip_address(address)
        return str(parsed_ip), default_port
    except ValueError:
        pass

    if ":" in address:
        host, port_text = address.rsplit(":", 1)
        port = int(port_text)
    else:
        host = address
        port = default_port

    parsed_ip = ipaddress.ip_address(host)
    validate_port(port)
    return str(parsed_ip), port


def normalize_rpc_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str):
            normalized[str(key)] = item
        elif isinstance(item, dict):
            normalized[str(key)] = str(
                item.get("address")
                or item.get("addr")
                or item.get("status")
                or item.get("result")
                or ""
            )
        else:
            normalized[str(key)] = str(item)

    return normalized


def get_optional_mode(cli: str, mode: str) -> dict[str, str]:
    try:
        return normalize_rpc_map(run_cli(cli, "smartnodelist", mode))
    except RuntimeError:
        return {}


def check_tcp_port(ip: str, port: int, timeout: float) -> tuple[bool, float | None, str]:
    try:
        address = ipaddress.ip_address(ip)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        start = time.perf_counter()

        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            if family == socket.AF_INET6:
                connection.connect((ip, port, 0, 0))
            else:
                connection.connect((ip, port))

        latency = round((time.perf_counter() - start) * 1000, 2)
        return True, latency, ""
    except socket.timeout:
        return False, None, "connection timed out"
    except ConnectionRefusedError:
        return False, None, "connection refused"
    except OSError as exc:
        return False, None, str(exc)


def inspect_smartnode(
    outpoint: str,
    advertised_address: str,
    status: str,
    duplicate_counts: Counter[str],
    default_port: int,
    timeout: float,
) -> SmartnodeResult:
    try:
        ip, port = parse_address(advertised_address, default_port)
    except (ValueError, TypeError) as exc:
        return SmartnodeResult(
            outpoint=outpoint,
            advertised_address=advertised_address,
            ip="",
            port=default_port,
            address_valid=False,
            port_open=False,
            latency_ms=None,
            smartnode_status=status or "UNKNOWN",
            duplicate_ip=False,
            duplicate_count=0,
            error=f"invalid advertised address: {exc}",
        )

    open_status, latency, error = check_tcp_port(ip, port, timeout)
    count = duplicate_counts[ip]

    return SmartnodeResult(
        outpoint=outpoint,
        advertised_address=advertised_address,
        ip=ip,
        port=port,
        address_valid=True,
        port_open=open_status,
        latency_ms=latency,
        smartnode_status=status or "UNKNOWN",
        duplicate_ip=count > 1,
        duplicate_count=count,
        error=error,
    )


def export_csv(results: list[SmartnodeResult], filename: Path) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    fields = list(SmartnodeResult.__dataclass_fields__.keys())

    with filename.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def export_json(results: list[SmartnodeResult], filename: Path, port: int) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    reachable = sum(result.port_open for result in results)
    enabled = sum(result.smartnode_status.upper() == "ENABLED" for result in results)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network_port": port,
        "summary": {
            "total": len(results),
            "reachable": reachable,
            "unreachable": len(results) - reachable,
            "enabled": enabled,
            "duplicate_registrations": sum(result.duplicate_ip for result in results),
            "invalid_addresses": sum(not result.address_valid for result in results),
        },
        "smartnodes": [asdict(result) for result in results],
    }

    with filename.open("w", encoding="utf-8") as output:
        json.dump(report, output, indent=2)


def print_result(result: SmartnodeResult) -> None:
    if not result.address_valid:
        reachability = "INVALID"
        latency = "-"
    elif result.port_open:
        reachability = "OPEN"
        latency = f"{result.latency_ms:.2f} ms"
    else:
        reachability = "CLOSED"
        latency = "-"

    duplicate = f" DUPLICATE({result.duplicate_count})" if result.duplicate_ip else ""
    address = result.advertised_address[:39]
    status = result.smartnode_status[:12]

    print(
        f"{address:<40} "
        f"{reachability:<8} "
        f"{latency:<12} "
        f"{status:<12}"
        f"{duplicate}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Yerbas smartnode addresses and TCP ports."
    )
    parser.add_argument("--cli", default="yerbas-cli", help="Path to yerbas-cli")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Fallback port when no port is advertised; default {DEFAULT_PORT}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Connection timeout in seconds; default {DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Maximum concurrent checks; default {DEFAULT_THREADS}",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("yerbas-smartnodes.csv"),
        help="CSV report filename",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("yerbas-smartnodes.json"),
        help="JSON report filename",
    )
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="Only print smartnodes with an open port",
    )
    arguments = parser.parse_args()

    try:
        validate_port(arguments.port)
    except ValueError as exc:
        parser.error(str(exc))

    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if arguments.threads < 1:
        parser.error("--threads must be at least one")

    if shutil.which(arguments.cli) is None and not Path(arguments.cli).exists():
        print(
            f"Error: cannot find {arguments.cli}. Use --cli /full/path/to/yerbas-cli",
            file=sys.stderr,
        )
        return 2

    try:
        address_map = normalize_rpc_map(
            run_cli(arguments.cli, "smartnodelist", "addr")
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not address_map:
        print("No smartnodes were returned by smartnodelist addr.", file=sys.stderr)
        return 2

    status_map = get_optional_mode(arguments.cli, "status")
    parsed_ips: list[str] = []

    for advertised_address in address_map.values():
        try:
            ip, _ = parse_address(advertised_address, arguments.port)
            parsed_ips.append(ip)
        except (ValueError, TypeError):
            continue

    duplicate_counts = Counter(parsed_ips)

    print(f"Checking {len(address_map)} registered Yerbas smartnodes...")
    print(f"{'ADDRESS':<40} {'PORT':<8} {'LATENCY':<12} {'STATUS':<12}")
    print("-" * 86)

    results: list[SmartnodeResult] = []
    with ThreadPoolExecutor(max_workers=arguments.threads) as executor:
        futures = {
            executor.submit(
                inspect_smartnode,
                outpoint,
                advertised_address,
                status_map.get(outpoint, "UNKNOWN"),
                duplicate_counts,
                arguments.port,
                arguments.timeout,
            ): outpoint
            for outpoint, advertised_address in address_map.items()
        }

        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                outpoint = futures[future]
                result = SmartnodeResult(
                    outpoint=outpoint,
                    advertised_address=address_map[outpoint],
                    ip="",
                    port=arguments.port,
                    address_valid=False,
                    port_open=False,
                    latency_ms=None,
                    smartnode_status=status_map.get(outpoint, "UNKNOWN"),
                    duplicate_ip=False,
                    duplicate_count=0,
                    error=f"unexpected checker error: {exc}",
                )

            results.append(result)
            if not arguments.open_only or result.port_open:
                print_result(result)

    results.sort(key=lambda item: (not item.port_open, item.ip, item.outpoint))
    export_csv(results, arguments.csv)
    export_json(results, arguments.json, arguments.port)

    total = len(results)
    reachable = sum(result.port_open for result in results)
    unreachable = total - reachable
    enabled = sum(result.smartnode_status.upper() == "ENABLED" for result in results)
    invalid = sum(not result.address_valid for result in results)
    duplicate_ips = {result.ip for result in results if result.duplicate_ip and result.ip}
    percentage = (reachable / total * 100) if total else 0

    print("\nYerbas smartnode summary")
    print("-" * 30)
    print(f"Registered        : {total}")
    print(f"Port open         : {reachable}")
    print(f"Port unreachable  : {unreachable}")
    print(f"Reachability      : {percentage:.2f}%")
    print(f"RPC status ENABLED: {enabled}")
    print(f"Invalid addresses : {invalid}")
    print(f"Duplicate IPs     : {len(duplicate_ips)}")
    print(f"CSV report        : {arguments.csv}")
    print(f"JSON report       : {arguments.json}")

    return 0 if unreachable == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
