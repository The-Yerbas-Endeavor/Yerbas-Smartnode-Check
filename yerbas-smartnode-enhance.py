#!/usr/bin/env python3
"""Enrich a Yerbas smartnode report with P2P, sync, and uptime data."""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from yerbas_p2p import DEFAULT_PROTOCOL, probe_peer

SYNCED_MAX_LAG = 12
SLIGHTLY_BEHIND_MAX_LAG = 720
BEHIND_MAX_LAG = 5_000
STALE_MAX_LAG = 20_000


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


def get_local_height(cli: str) -> int:
    try:
        process = subprocess.run([cli, "getblockcount"], capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cannot find {cli}. Specify its path with --cli.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{cli} getblockcount timed out after 30 seconds") from exc
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip() or "unknown error"
        raise RuntimeError(f"{cli} getblockcount failed: {message}")
    try:
        return int(process.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"{cli} getblockcount returned an invalid height") from exc


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
        uptime[outpoint] = {
            "samples": total,
            "online_samples": online,
            "uptime_percent": round(online / total * 100, 2) if total else None,
            "consecutive_failures": consecutive_failures,
            "first_seen": next((stamp for stamp, _ in samples if stamp), ""),
            "last_seen": next((stamp for stamp, _ in reversed(samples) if stamp), ""),
        }
    return uptime


def classify_sync(remote_height: Any, local_height: int) -> tuple[str, int | None]:
    if not isinstance(remote_height, int) or remote_height < 0:
        return "UNKNOWN", None
    lag = local_height - remote_height
    if lag < -SYNCED_MAX_LAG:
        return "POSSIBLE_WRONG_CHAIN", lag
    if lag <= SYNCED_MAX_LAG:
        return "SYNCED", max(lag, 0)
    if lag <= SLIGHTLY_BEHIND_MAX_LAG:
        return "SLIGHTLY_BEHIND", lag
    if lag <= BEHIND_MAX_LAG:
        return "BEHIND", lag
    if lag <= STALE_MAX_LAG:
        return "STALE", lag
    return "FORKED_OR_ABANDONED", lag


def classify(node: dict[str, Any], slow_ms: float, minimum_protocol: int) -> str:
    if not node.get("address_valid", True):
        return "INVALID"
    if not node.get("port_open"):
        return "OFFLINE"
    if node.get("p2p_success") is False:
        return "NOT_YERBAS"
    protocol = node.get("p2p_protocol") or node.get("protocol")
    outdated = isinstance(protocol, int) and protocol < minimum_protocol
    sync_status = str(node.get("sync_status", "UNKNOWN"))
    if sync_status == "FORKED_OR_ABANDONED":
        return "FORKED_OR_ABANDONED"
    if sync_status == "POSSIBLE_WRONG_CHAIN":
        return "POSSIBLE_WRONG_CHAIN"
    if sync_status == "STALE":
        return "OUTDATED_AND_STALE" if outdated else "STALE"
    if sync_status in {"BEHIND", "SLIGHTLY_BEHIND"}:
        return "OUTDATED_AND_BEHIND" if outdated else "BEHIND"
    if sync_status == "UNKNOWN":
        return "SYNC_UNKNOWN"
    if outdated:
        return "OUTDATED"
    latency = node.get("p2p_handshake_ms") or node.get("latency_ms")
    if isinstance(latency, (int, float)) and latency > slow_ms:
        return "SLOW"
    return "HEALTHY"


def enrich_report(report: dict[str, Any], *, timeout: float, threads: int, protocol: int,
                  minimum_protocol: int, slow_ms: float, local_height: int,
                  uptime: dict[str, dict[str, Any]]) -> dict[str, Any]:
    nodes = report["smartnodes"]
    eligible = [node for node in nodes if node.get("ip") and node.get("port_open")]
    with ThreadPoolExecutor(max_workers=threads) as executor:
        future_map = {
            executor.submit(probe_peer, str(node["ip"]), int(node.get("port", 15420)), timeout=timeout, protocol=protocol): node
            for node in eligible
        }
        for future in as_completed(future_map):
            node = future_map[future]
            try:
                probe = future.result().to_dict()
            except Exception as exc:
                probe = {"success": False, "version_received": False, "verack_received": False, "protocol": None,
                         "services": None, "timestamp": None, "user_agent": "", "start_height": None, "relay": None,
                         "handshake_ms": None, "error": f"unexpected P2P probe error: {exc}"}
            for key, value in probe.items():
                node[f"p2p_{key}"] = value
    for node in nodes:
        if "p2p_success" not in node:
            node.update({"p2p_success": False, "p2p_version_received": False, "p2p_verack_received": False,
                         "p2p_protocol": None, "p2p_services": None, "p2p_timestamp": None, "p2p_user_agent": "",
                         "p2p_start_height": None, "p2p_relay": None, "p2p_handshake_ms": None,
                         "p2p_error": node.get("error", "port unavailable")})
        node.update(uptime.get(str(node.get("outpoint", "")), {
            "samples": 1, "online_samples": int(bool(node.get("port_open"))),
            "uptime_percent": 100.0 if node.get("port_open") else 0.0,
            "consecutive_failures": 0 if node.get("port_open") else 1,
            "first_seen": report.get("generated_at", ""), "last_seen": report.get("generated_at", ""),
        }))
        sync_status, lag = classify_sync(node.get("p2p_start_height"), local_height)
        node["local_height"] = local_height
        node["blocks_behind"] = lag
        node["sync_status"] = sync_status
        node["health_status"] = classify(node, slow_ms, minimum_protocol)
    statuses = Counter(str(node["health_status"]) for node in nodes)
    sync_statuses = Counter(str(node["sync_status"]) for node in nodes if node.get("p2p_success"))
    handshakes = [node["p2p_handshake_ms"] for node in nodes if node.get("p2p_success") and node.get("p2p_handshake_ms") is not None]
    p2p_ok = sum(1 for node in nodes if node.get("p2p_success"))
    report["enhanced_at"] = datetime.now(timezone.utc).isoformat()
    report["p2p"] = {
        "network_magic": "79657262", "network_magic_ascii": "yerb", "local_protocol": protocol,
        "minimum_protocol": minimum_protocol, "local_height": local_height, "timeout_seconds": timeout,
        "sync_thresholds": {"synced": SYNCED_MAX_LAG, "slightly_behind": SLIGHTLY_BEHIND_MAX_LAG,
                            "behind": BEHIND_MAX_LAG, "stale": STALE_MAX_LAG},
    }
    report.setdefault("summary", {}).update({
        "p2p_verified": p2p_ok, "p2p_failed": len(nodes) - p2p_ok,
        "p2p_verified_percent": round(p2p_ok / len(nodes) * 100, 2) if nodes else 0,
        "healthy": statuses.get("HEALTHY", 0), "slow": statuses.get("SLOW", 0),
        "outdated": statuses.get("OUTDATED", 0), "behind": statuses.get("BEHIND", 0),
        "stale": statuses.get("STALE", 0), "outdated_and_behind": statuses.get("OUTDATED_AND_BEHIND", 0),
        "outdated_and_stale": statuses.get("OUTDATED_AND_STALE", 0),
        "forked_or_abandoned": statuses.get("FORKED_OR_ABANDONED", 0),
        "possible_wrong_chain": statuses.get("POSSIBLE_WRONG_CHAIN", 0),
        "sync_unknown": statuses.get("SYNC_UNKNOWN", 0), "not_yerbas": statuses.get("NOT_YERBAS", 0),
        "average_handshake_ms": round(mean(handshakes), 2) if handshakes else None,
        "health_statuses": dict(sorted(statuses.items())), "sync_statuses": dict(sorted(sync_statuses.items())),
    })
    return report


def export_html(report: dict[str, Any], filename: Path) -> None:
    summary = report.get("summary", {})
    rows: list[str] = []
    for node in report.get("smartnodes", []):
        status = str(node.get("health_status", "UNKNOWN"))
        protocol = node.get("p2p_protocol") or node.get("protocol") or ""
        latency, uptime, lag = node.get("p2p_handshake_ms"), node.get("uptime_percent"), node.get("blocks_behind")
        sync_status = str(node.get("sync_status", "UNKNOWN"))
        flags = []
        if node.get("duplicate_ip"):
            flags.append(f"duplicate x{node.get('duplicate_count', 0)}")
        if not node.get("expected_port", True):
            flags.append("wrong port")
        if node.get("p2p_error"):
            flags.append(str(node["p2p_error"]))
        searchable = " ".join(str(node.get(key, "")) for key in ("advertised_address", "smartnode_status", "p2p_user_agent", "health_status", "sync_status", "reverse_dns")).lower()
        rows.append(f'<tr data-status="{html.escape(status)}" data-search="{html.escape(searchable)}">'
                    f'<td><span class="badge {status.lower()}">{html.escape(status)}</span></td>'
                    f'<td>{html.escape(str(node.get("advertised_address", "")))}</td>'
                    f'<td>{html.escape(str(node.get("smartnode_status", "")))}</td>'
                    f'<td>{html.escape(str(protocol))}</td><td>{html.escape(str(node.get("p2p_user_agent", "")))}</td>'
                    f'<td>{html.escape(sync_status)}</td><td>{"" if lag is None else lag}</td>'
                    f'<td>{"" if latency is None else latency}</td><td>{"" if uptime is None else uptime}%</td>'
                    f'<td>{node.get("consecutive_failures", 0)}</td><td>{html.escape("; ".join(flags))}</td></tr>')
    cards = [("Registered", summary.get("total", 0)), ("TCP reachable", summary.get("reachable", 0)),
             ("P2P verified", summary.get("p2p_verified", 0)), ("Healthy", summary.get("healthy", 0)),
             ("Behind", summary.get("behind", 0) + summary.get("outdated_and_behind", 0)),
             ("Stale", summary.get("stale", 0) + summary.get("outdated_and_stale", 0)),
             ("Forked / abandoned", summary.get("forked_or_abandoned", 0)),
             ("Outdated", summary.get("outdated", 0)), ("Not Yerbas", summary.get("not_yerbas", 0)),
             ("Offline", summary.get("unreachable", 0))]
    card_html = "".join(f'<div class="card"><small>{html.escape(label)}</small><strong>{value}</strong></div>' for label, value in cards)
    generated = html.escape(str(report.get("enhanced_at") or report.get("generated_at", "")))
    options = "".join(f"<option>{s}</option>" for s in ("HEALTHY", "SLOW", "OUTDATED", "BEHIND", "OUTDATED_AND_BEHIND", "STALE", "OUTDATED_AND_STALE", "FORKED_OR_ABANDONED", "POSSIBLE_WRONG_CHAIN", "SYNC_UNKNOWN", "OFFLINE", "NOT_YERBAS", "INVALID"))
    document = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Yerbas Smartnode Health</title><style>:root{{--bg:#f4f7f4;--panel:#fff;--text:#172217;--muted:#607060;--line:#dce5dc}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}}main{{max-width:1600px;margin:auto;padding:24px}}h1{{margin:0 0 4px}}.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px;margin:20px 0}}.card{{background:var(--panel);padding:14px;border:1px solid var(--line);border-radius:10px}}.card strong{{display:block;font-size:26px;margin-top:4px}}.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px}}input,select{{padding:10px;border:1px solid #bcc9bc;border-radius:7px;background:white}}input{{min-width:280px;flex:1}}.scroll{{overflow:auto;max-height:72vh;background:white;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;min-width:1250px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#eaf0ea;z-index:1}}.badge{{display:inline-block;padding:3px 8px;border-radius:99px;font-weight:700;font-size:12px}}.healthy{{background:#d8f5df;color:#12652a}}.slow,.behind,.outdated_and_behind{{background:#fff0bf;color:#7a5600}}.outdated{{background:#dcecff;color:#174f8a}}.stale,.outdated_and_stale{{background:#ffe2ba;color:#814400}}.offline,.invalid,.not_yerbas,.forked_or_abandoned,.possible_wrong_chain{{background:#ffdede;color:#8c1e1e}}</style></head><body><main><h1>Yerbas Smartnode Health</h1><div class="muted">Enhanced {generated} · handshake + protocol + chain-sync validation</div><div class="cards">{card_html}</div><div class="controls"><input id="search" placeholder="Search address, status, version, sync state…"><select id="status"><option value="">All health states</option>{options}</select></div><div class="scroll"><table><thead><tr><th>Health</th><th>Address</th><th>RPC status</th><th>Protocol</th><th>User agent</th><th>Sync</th><th>Blocks behind</th><th>Handshake ms</th><th>Uptime</th><th>Failures</th><th>Flags / Error</th></tr></thead><tbody id="rows">{''.join(rows)}</tbody></table></div><script>const q=document.querySelector('#search'),s=document.querySelector('#status'),rs=[...document.querySelectorAll('#rows tr')];function f(){{const t=q.value.toLowerCase(),v=s.value;rs.forEach(r=>r.hidden=!!((t&&!r.dataset.search.includes(t))||(v&&r.dataset.status!==v)))}}q.addEventListener('input',f);s.addEventListener('change',f);</script></main></body></html>'''
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Add direct Yerbas P2P, sync, and uptime data to a smartnode report.")
    parser.add_argument("--json", type=Path, default=Path("yerbas-smartnodes.json"))
    parser.add_argument("--html", type=Path, default=Path("yerbas-smartnodes.html"))
    parser.add_argument("--history-dir", type=Path)
    parser.add_argument("--history-limit", type=int, default=360)
    parser.add_argument("--cli", default="yerbas-cli", help="Path to yerbas-cli used for getblockcount")
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--threads", type=int, default=30)
    parser.add_argument("--protocol", type=int, default=DEFAULT_PROTOCOL)
    parser.add_argument("--minimum-protocol", type=int, default=DEFAULT_PROTOCOL)
    parser.add_argument("--slow-ms", type=float, default=500.0)
    args = parser.parse_args()
    if args.timeout <= 0 or args.threads < 1 or args.history_limit < 1 or args.slow_ms <= 0:
        parser.error("timeout, threads, history-limit, and slow-ms must be positive")
    try:
        report = load_json(args.json)
        local_height = get_local_height(args.cli)
        snapshots = load_snapshots(args.history_dir, args.json, args.history_limit)
        uptime = build_uptime(snapshots)
        print(f"Local chain height: {local_height}")
        print(f"P2P probing {sum(bool(n.get('port_open')) for n in report['smartnodes'])} reachable smartnodes…")
        report = enrich_report(report, timeout=args.timeout, threads=args.threads, protocol=args.protocol,
                               minimum_protocol=args.minimum_protocol, slow_ms=args.slow_ms,
                               local_height=local_height, uptime=uptime)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        export_html(report, args.html)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"P2P verified: {summary['p2p_verified']}/{summary['total']} ({summary['p2p_verified_percent']}%)")
    print(f"Healthy: {summary['healthy']}  Behind: {summary['behind'] + summary['outdated_and_behind']}  Stale: {summary['stale'] + summary['outdated_and_stale']}  Forked/abandoned: {summary['forked_or_abandoned']}")
    print(f"JSON: {args.json}\nHTML: {args.html}")
    return 0 if summary["p2p_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
