#!/usr/bin/env python3
"""Enrich a Yerbas smartnode report with P2P handshake and uptime data."""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from yerbas_p2p import DEFAULT_PROTOCOL, probe_peer


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"report not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON report {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("smartnodes"), list):
        raise RuntimeError(f"{path} is not a Yerbas smartnode report")
    return value


def load_snapshots(history_dir: Path | None, current_path: Path, limit: int) -> list[dict[str, Any]]:
    files: list[Path] = []
    if history_dir and history_dir.exists():
        files.extend(sorted(history_dir.glob("yerbas-smartnodes-*.json"), reverse=True)[:limit])
    files.append(current_path)
    snapshots: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            snapshots.append(load_json(path))
        except RuntimeError:
            continue
    return snapshots


def build_uptime(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observations: dict[str, list[tuple[str, bool]]] = {}
    for report in reversed(snapshots):
        generated = str(report.get("generated_at", ""))
        for node in report.get("smartnodes", []):
            outpoint = str(node.get("outpoint", ""))
            if outpoint:
                observations.setdefault(outpoint, []).append((generated, bool(node.get("port_open"))))

    uptime: dict[str, dict[str, Any]] = {}
    for outpoint, samples in observations.items():
        total = len(samples)
        online = sum(state for _, state in samples)
        consecutive_failures = 0
        for _, state in reversed(samples):
            if state:
                break
            consecutive_failures += 1
        first_seen = next((stamp for stamp, _ in samples if stamp), "")
        last_seen = next((stamp for stamp, _ in reversed(samples) if stamp), "")
        uptime[outpoint] = {
            "samples": total,
            "online_samples": online,
            "uptime_percent": round(online / total * 100, 2) if total else None,
            "consecutive_failures": consecutive_failures,
            "first_seen": first_seen,
            "last_seen": last_seen,
        }
    return uptime


def classify(node: dict[str, Any], slow_ms: float, minimum_protocol: int) -> str:
    if not node.get("address_valid", True):
        return "INVALID"
    if not node.get("port_open"):
        return "OFFLINE"
    if node.get("p2p_success") is False:
        return "NOT_YERBAS"
    protocol = node.get("p2p_protocol") or node.get("protocol")
    if isinstance(protocol, int) and protocol < minimum_protocol:
        return "OUTDATED"
    latency = node.get("p2p_handshake_ms") or node.get("latency_ms")
    if isinstance(latency, (int, float)) and latency > slow_ms:
        return "SLOW"
    return "HEALTHY"


def enrich_report(
    report: dict[str, Any],
    *,
    timeout: float,
    threads: int,
    protocol: int,
    minimum_protocol: int,
    slow_ms: float,
    uptime: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    nodes = report["smartnodes"]
    eligible = [node for node in nodes if node.get("ip") and node.get("port_open")]

    with ThreadPoolExecutor(max_workers=threads) as executor:
        future_map = {
            executor.submit(
                probe_peer,
                str(node["ip"]),
                int(node.get("port", 15420)),
                timeout=timeout,
                protocol=protocol,
            ): node
            for node in eligible
        }
        for future in as_completed(future_map):
            node = future_map[future]
            try:
                probe = future.result().to_dict()
            except Exception as exc:
                probe = {
                    "success": False,
                    "version_received": False,
                    "verack_received": False,
                    "protocol": None,
                    "services": None,
                    "timestamp": None,
                    "user_agent": "",
                    "start_height": None,
                    "relay": None,
                    "handshake_ms": None,
                    "error": f"unexpected P2P probe error: {exc}",
                }
            for key, value in probe.items():
                node[f"p2p_{key}"] = value

    for node in nodes:
        if "p2p_success" not in node:
            node.update({
                "p2p_success": False,
                "p2p_version_received": False,
                "p2p_verack_received": False,
                "p2p_protocol": None,
                "p2p_services": None,
                "p2p_timestamp": None,
                "p2p_user_agent": "",
                "p2p_start_height": None,
                "p2p_relay": None,
                "p2p_handshake_ms": None,
                "p2p_error": node.get("error", "port unavailable"),
            })
        node.update(uptime.get(str(node.get("outpoint", "")), {
            "samples": 1,
            "online_samples": int(bool(node.get("port_open"))),
            "uptime_percent": 100.0 if node.get("port_open") else 0.0,
            "consecutive_failures": 0 if node.get("port_open") else 1,
            "first_seen": report.get("generated_at", ""),
            "last_seen": report.get("generated_at", ""),
        }))
        node["health_status"] = classify(node, slow_ms, minimum_protocol)

    statuses = Counter(str(node["health_status"]) for node in nodes)
    handshakes = [node.get("p2p_handshake_ms") for node in nodes if node.get("p2p_success") and node.get("p2p_handshake_ms") is not None]
    p2p_ok = statuses.get("HEALTHY", 0) + statuses.get("SLOW", 0) + statuses.get("OUTDATED", 0)
    report["enhanced_at"] = datetime.now(timezone.utc).isoformat()
    report["p2p"] = {
        "network_magic": "79657262",
        "network_magic_ascii": "yerb",
        "local_protocol": protocol,
        "minimum_protocol": minimum_protocol,
        "timeout_seconds": timeout,
    }
    report.setdefault("summary", {}).update({
        "p2p_verified": p2p_ok,
        "p2p_failed": len(nodes) - p2p_ok,
        "p2p_verified_percent": round(p2p_ok / len(nodes) * 100, 2) if nodes else 0,
        "healthy": statuses.get("HEALTHY", 0),
        "slow": statuses.get("SLOW", 0),
        "outdated": statuses.get("OUTDATED", 0),
        "not_yerbas": statuses.get("NOT_YERBAS", 0),
        "average_handshake_ms": round(mean(handshakes), 2) if handshakes else None,
        "health_statuses": dict(sorted(statuses.items())),
    })
    return report


def export_html(report: dict[str, Any], filename: Path) -> None:
    summary = report.get("summary", {})
    rows: list[str] = []
    for node in report.get("smartnodes", []):
        status = str(node.get("health_status", "UNKNOWN"))
        protocol = node.get("p2p_protocol") or node.get("protocol") or ""
        latency = node.get("p2p_handshake_ms")
        uptime = node.get("uptime_percent")
        flags = []
        if node.get("duplicate_ip"):
            flags.append(f"duplicate x{node.get('duplicate_count', 0)}")
        if not node.get("expected_port", True):
            flags.append("wrong port")
        if node.get("p2p_error"):
            flags.append(str(node["p2p_error"]))
        searchable = " ".join(str(node.get(key, "")) for key in (
            "advertised_address", "smartnode_status", "p2p_user_agent", "health_status", "reverse_dns"
        )).lower()
        rows.append(
            f'<tr data-status="{html.escape(status)}" data-search="{html.escape(searchable)}">'
            f'<td><span class="badge {status.lower()}">{html.escape(status)}</span></td>'
            f'<td>{html.escape(str(node.get("advertised_address", "")))}</td>'
            f'<td>{html.escape(str(node.get("smartnode_status", "")))}</td>'
            f'<td>{html.escape(str(protocol))}</td>'
            f'<td>{html.escape(str(node.get("p2p_user_agent", "")))}</td>'
            f'<td>{"" if latency is None else latency}</td>'
            f'<td>{"" if uptime is None else uptime}%</td>'
            f'<td>{node.get("consecutive_failures", 0)}</td>'
            f'<td>{html.escape("; ".join(flags))}</td></tr>'
        )

    cards = [
        ("Registered", summary.get("total", 0)),
        ("TCP reachable", summary.get("reachable", 0)),
        ("P2P verified", summary.get("p2p_verified", 0)),
        ("Healthy", summary.get("healthy", 0)),
        ("Slow", summary.get("slow", 0)),
        ("Outdated", summary.get("outdated", 0)),
        ("Not Yerbas", summary.get("not_yerbas", 0)),
        ("Offline", summary.get("unreachable", 0)),
    ]
    card_html = "".join(f'<div class="card"><small>{html.escape(label)}</small><strong>{value}</strong></div>' for label, value in cards)
    generated = html.escape(str(report.get("enhanced_at") or report.get("generated_at", "")))
    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yerbas Smartnode Health</title><style>
:root{{--bg:#f4f7f4;--panel:#fff;--text:#172217;--muted:#607060;--line:#dce5dc}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}}h1{{margin:0 0 4px}}.muted{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px;margin:20px 0}}
.card{{background:var(--panel);padding:14px;border:1px solid var(--line);border-radius:10px}}.card strong{{display:block;font-size:26px;margin-top:4px}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px}}input,select{{padding:10px;border:1px solid #bcc9bc;border-radius:7px;background:white}}
input{{min-width:280px;flex:1}}.scroll{{overflow:auto;max-height:72vh;background:white;border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;min-width:1050px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#eaf0ea;z-index:1}}
.badge{{display:inline-block;padding:3px 8px;border-radius:99px;font-weight:700;font-size:12px}}.healthy{{background:#d8f5df;color:#12652a}}.slow{{background:#fff0bf;color:#7a5600}}.outdated{{background:#dcecff;color:#174f8a}}.offline,.invalid,.not_yerbas{{background:#ffdede;color:#8c1e1e}}
</style></head><body><main><h1>Yerbas Smartnode Health</h1><div class="muted">Enhanced {generated} · direct version/verack validation</div>
<div class="cards">{card_html}</div><div class="controls"><input id="search" placeholder="Search address, status, user agent, hostname…"><select id="status"><option value="">All health states</option><option>HEALTHY</option><option>SLOW</option><option>OUTDATED</option><option>OFFLINE</option><option>NOT_YERBAS</option><option>INVALID</option></select></div>
<div class="scroll"><table><thead><tr><th>Health</th><th>Address</th><th>RPC status</th><th>Remote protocol</th><th>User agent</th><th>Handshake ms</th><th>Uptime</th><th>Failures</th><th>Flags / Error</th></tr></thead><tbody id="rows">{''.join(rows)}</tbody></table></div>
<script>const q=document.querySelector('#search'),s=document.querySelector('#status'),rs=[...document.querySelectorAll('#rows tr')];function f(){{const t=q.value.toLowerCase(),v=s.value;rs.forEach(r=>r.hidden=!!((t&&!r.dataset.search.includes(t))||(v&&r.dataset.status!==v)))}}q.addEventListener('input',f);s.addEventListener('change',f);</script>
</main></body></html>'''
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Add direct Yerbas P2P and uptime data to a smartnode report.")
    parser.add_argument("--json", type=Path, default=Path("yerbas-smartnodes.json"), help="Input and output JSON report")
    parser.add_argument("--html", type=Path, default=Path("yerbas-smartnodes.html"), help="Enhanced HTML dashboard")
    parser.add_argument("--history-dir", type=Path, help="Directory containing timestamped reports")
    parser.add_argument("--history-limit", type=int, default=360, help="Maximum snapshots used for uptime")
    parser.add_argument("--timeout", type=float, default=4.0, help="P2P handshake timeout")
    parser.add_argument("--threads", type=int, default=30, help="Concurrent P2P probes")
    parser.add_argument("--protocol", type=int, default=DEFAULT_PROTOCOL, help="Local advertised P2P protocol")
    parser.add_argument("--minimum-protocol", type=int, default=DEFAULT_PROTOCOL, help="Minimum acceptable remote protocol")
    parser.add_argument("--slow-ms", type=float, default=500.0, help="Mark handshakes slower than this as SLOW")
    args = parser.parse_args()
    if args.timeout <= 0 or args.threads < 1 or args.history_limit < 1 or args.slow_ms <= 0:
        parser.error("timeout, threads, history-limit, and slow-ms must be positive")
    try:
        report = load_json(args.json)
        snapshots = load_snapshots(args.history_dir, args.json, args.history_limit)
        uptime = build_uptime(snapshots)
        print(f"P2P probing {sum(bool(n.get('port_open')) for n in report['smartnodes'])} reachable smartnodes…")
        report = enrich_report(
            report,
            timeout=args.timeout,
            threads=args.threads,
            protocol=args.protocol,
            minimum_protocol=args.minimum_protocol,
            slow_ms=args.slow_ms,
            uptime=uptime,
        )
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        export_html(report, args.html)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"P2P verified: {summary['p2p_verified']}/{summary['total']} ({summary['p2p_verified_percent']}%)")
    print(f"Healthy: {summary['healthy']}  Slow: {summary['slow']}  Outdated: {summary['outdated']}  Not Yerbas: {summary['not_yerbas']}")
    print(f"JSON: {args.json}\nHTML: {args.html}")
    return 0 if summary["p2p_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
