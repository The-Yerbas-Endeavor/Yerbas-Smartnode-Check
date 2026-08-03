#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = (
    "http://ip-api.com/batch"
    "?fields=status,message,query,country,countryCode,"
    "city,lat,lon,isp,org,as,asname"
)
BATCH_SIZE = 100


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def valid_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def lookup_batch(addresses: list[str]) -> tuple[list[dict[str, Any]], str | None, str | None]:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(addresses).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Yerbas-Smartnode-Check/GeoIP",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload, response.headers.get("X-Rl"), response.headers.get("X-Ttl")


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "success":
        return {
            "geoip_error": result.get("message", "lookup failed"),
        }

    as_text = str(result.get("as") or "")
    asn = None

    if as_text.upper().startswith("AS"):
        token = as_text.split(" ", 1)[0][2:]
        try:
            asn = int(token)
        except ValueError:
            pass

    return {
        "country": result.get("country") or "",
        "country_code": result.get("countryCode") or "",
        "city": result.get("city") or "",
        "latitude": result.get("lat"),
        "longitude": result.get("lon"),
        "asn": asn,
        "organization": (
            result.get("org")
            or result.get("isp")
            or result.get("asname")
            or ""
        ),
        "geoip_error": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add cached GeoIP coordinates to a Yerbas Smartnode health report."
    )
    parser.add_argument("--json", type=Path, required=True, help="Smartnode JSON report to update")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("geoip-cache.json"),
        help="Persistent GeoIP cache file",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh addresses already present in the cache",
    )
    arguments = parser.parse_args()

    report = load_json(arguments.json, None)
    if not isinstance(report, dict):
        raise SystemExit(f"Unable to read report: {arguments.json}")

    smartnodes = report.get("smartnodes")
    if not isinstance(smartnodes, list):
        raise SystemExit("Report does not contain a smartnodes array")

    cache = load_json(arguments.cache, {})
    if not isinstance(cache, dict):
        cache = {}

    unique_ips = sorted({
        str(node.get("ip") or "")
        for node in smartnodes
        if isinstance(node, dict)
        and valid_public_ip(str(node.get("ip") or ""))
    })

    pending = [
        ip
        for ip in unique_ips
        if arguments.refresh or ip not in cache
    ]

    print(f"Unique public IPs : {len(unique_ips)}")
    print(f"Cached IPs        : {len(unique_ips) - len(pending)}")
    print(f"Pending lookups   : {len(pending)}")

    for batch_number, batch in enumerate(chunks(pending, BATCH_SIZE), start=1):
        print(f"Looking up batch {batch_number}: {len(batch)} IPs")

        try:
            results, remaining, reset_seconds = lookup_batch(batch)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            retry_after = int(exc.headers.get("X-Ttl", "60"))
            print(f"Rate limited; sleeping {retry_after + 1} seconds")
            time.sleep(retry_after + 1)
            results, remaining, reset_seconds = lookup_batch(batch)

        for result in results:
            ip = str(result.get("query") or "")
            if ip:
                cache[ip] = normalize_result(result)

        save_json_atomic(arguments.cache, cache)

        if remaining == "0" and reset_seconds:
            time.sleep(int(reset_seconds) + 1)
        else:
            time.sleep(1)

    enriched = 0
    failed = 0

    for node in smartnodes:
        if not isinstance(node, dict):
            continue

        ip = str(node.get("ip") or "")
        location = cache.get(ip)
        if not isinstance(location, dict):
            continue

        node.update(location)

        if node.get("latitude") is not None and node.get("longitude") is not None:
            enriched += 1
        elif node.get("geoip_error"):
            failed += 1

    countries = {
        str(node.get("country_code") or "")
        for node in smartnodes
        if isinstance(node, dict) and node.get("country_code")
    }

    report.setdefault("summary", {})
    report["summary"]["countries"] = len(countries)
    report["summary"]["geolocated"] = enriched

    report["geoip"] = {
        "provider": "ip-api.com",
        "cached": True,
        "geolocated": enriched,
        "failed": failed,
        "unique_ips": len(unique_ips),
    }

    save_json_atomic(arguments.json, report)

    print(f"Geolocated nodes  : {enriched}")
    print(f"Countries         : {len(countries)}")
    print(f"Updated report    : {arguments.json}")
    print(f"Cache             : {arguments.cache}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
