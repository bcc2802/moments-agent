"""On-demand client of the pinned, isolated upstream MCP; never reads the Vault.

The launcher uses Python's standard library. MCP/PyYAML stay in TrendRadar's venv.
No upstream source is copied here. The live tool response is normalized, not news
content verified. A failed collection returns an empty current batch, never cache.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from moments_platforms import SOURCE_DOMAINS


SOURCES = SOURCE_DOMAINS
PROJECT = Path(__file__).resolve().parents[1]


def timestamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def failed_crawl(code):
    return {"requested_sources": list(SOURCES), "fetched_at": timestamp(),
            "sources": [{"id": key, "status": "FAILED", "error": code, "items": []}
                        for key in SOURCES]}


def normalize_crawl(raw):
    """Fail closed on response shape/platform drift, preserving all current rows."""
    if not isinstance(raw, dict) or raw.get("success") is not True:
        return failed_crawl("UPSTREAM_ERROR")
    summary, rows = raw.get("summary"), raw.get("data")
    if not isinstance(summary, dict) or not isinstance(rows, list):
        return failed_crawl("PARSE_ERROR")
    success, failed = summary.get("platforms"), summary.get("failed_platforms")
    if (not isinstance(success, list) or not isinstance(failed, list)
            or any(not isinstance(x, str) or x not in SOURCES for x in success + failed)
            or len(set(success + failed)) != len(success + failed)):
        return failed_crawl("PARSE_ERROR")
    result = failed_crawl("MISSING_SOURCE")
    try:
        collected_at = datetime.strptime(summary["crawl_time"], "%Y-%m-%d %H:%M:%S")
        result["fetched_at"] = collected_at.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
    except (KeyError, TypeError, ValueError):
        return failed_crawl("PARSE_ERROR")
    collected = {key: [] for key in SOURCES}
    for row in rows:
        if not isinstance(row, dict) or row.get("platform_id") not in success:
            return failed_crawl("PARSE_ERROR")
        ranks = row.get("ranks", [])
        rank = ranks[0] if isinstance(ranks, list) and ranks else None
        collected[row["platform_id"]].append({"title": row.get("title"),
                                             "url": row.get("url"), "rank": rank})
    for source in result["sources"]:
        key = source["id"]
        if key in success:
            source.update(status="OK", items=collected[key])
            source.pop("error", None)
        elif key in failed:
            # Upstream returns IDs only, without per-source HTTP error details.
            source["error"] = "SOURCE_FAILED"
    # Completion time is explicitly collection time, never content publication.
    return result


def validate_config(config):
    platforms = config.get("platforms", {})
    entries = platforms.get("sources", [])
    if (platforms.get("enabled") is not True or not isinstance(entries, list)
            or len(entries) != len(SOURCES) or any(not isinstance(p, dict) for p in entries)
            or {p.get("id") for p in entries} != set(SOURCES)
            or any(p.get("expected_domain") != SOURCES.get(p.get("id"))
                   or p.get("enabled", True) is not True for p in entries)):
        raise ValueError("SOURCE_CONFIGURATION_MISMATCH")
    for section in ("rss", "schedule", "notification", "ai_analysis", "ai_translation"):
        if config.get(section, {}).get("enabled") is not False:
            raise ValueError("DISABLED_FEATURE_REQUIRED")
    if config.get("app", {}).get("timezone") != "Asia/Shanghai":
        raise ValueError("TIMEZONE_MISMATCH")
    storage = config.get("storage", {})
    if (storage.get("backend") != "local" or storage.get("pull", {}).get("enabled") is not False
            or storage.get("remote", {}).get("enabled", False)):
        raise ValueError("REMOTE_STORAGE_BLOCKED")
    return {"sources": list(SOURCES), "timezone": "Asia/Shanghai",
            "disabled": ["rss", "schedule", "notification", "ai_analysis", "ai_translation"]}


def validate_runtime_sources(runtime_ids):
    """The running MCP must agree with the same collection policy as YAML."""
    if (not isinstance(runtime_ids, list) or len(runtime_ids) != len(SOURCES)
            or any(not isinstance(value, str) for value in runtime_ids)
            or set(runtime_ids) != set(SOURCES)):
        raise ValueError("MCP_CONFIGURATION_MISMATCH")


def collect():
    """Use the same official stdio MCP when a running desktop cannot hot-load it."""
    try:
        process = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve())],
                                 capture_output=True, text=True, encoding="utf-8", timeout=480)
        value = json.loads(process.stdout)
        if process.returncode != 0 or not isinstance(value, dict) or "sources" not in value:
            return failed_crawl("MCP_ERROR")
        return value
    except (TimeoutError, subprocess.TimeoutExpired):
        return failed_crawl("TIMEOUT")
    except (OSError, ValueError):
        return failed_crawl("MCP_ERROR")


async def worker(installation, mode, evidence_dir=None):
    # Imports deliberately occur only under the isolated third-party interpreter.
    import yaml
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(installation["root"]).resolve()
    config = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8-sig"))
    guard = validate_config(config)
    parameters = StdioServerParameters(
        command=str(Path(installation["python"]).resolve()),
        args=["-B", "-m", "mcp_server.server", "--project-root", str(root), "--transport", "stdio"],
        cwd=str(root), env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TZ": "Asia/Shanghai"})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            tools = sorted(tool.name for tool in listing.tools)

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                if response.isError:
                    raise ValueError("MCP_TOOL_ERROR")
                for content in response.content:
                    if content.type == "text":
                        value = json.loads(content.text)
                        if isinstance(value, dict):
                            return value
                raise ValueError("MCP_RESPONSE_INVALID")

            crawler = await call("get_current_config", {"section": "crawler"})
            status = await call("get_system_status", {})
            if not crawler.get("success") or not status.get("success"):
                raise ValueError("MCP_PROBE_FAILED")
            runtime_ids = crawler.get("config", {}).get("platforms", [])
            validate_runtime_sources(runtime_ids)
            if mode == "probe":
                return {"status": "OK", "transport": "stdio", "tools": tools,
                        "called": ["get_current_config", "get_system_status"], "guard": guard}
            raw = await call("trigger_crawl", {"platforms": list(SOURCES),
                                               "save_to_local": False, "include_url": True})
            if evidence_dir:
                evidence = Path(evidence_dir).resolve()
                evidence.relative_to(PROJECT.parent / "iterations")
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / "mcp-trigger-crawl.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                (evidence / "mcp-probe.json").write_text(json.dumps({"tools": tools,
                    "called": ["get_current_config", "get_system_status", "trigger_crawl"],
                    "arguments": {"platforms": list(SOURCES), "save_to_local": False,
                                  "include_url": True}, "guard": guard}, ensure_ascii=False, indent=2), encoding="utf-8")
            return normalize_crawl(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", default=str(PROJECT / "trendradar.local.json"))
    parser.add_argument("--mode", choices=("collect", "probe"), default="collect")
    parser.add_argument("--evidence-dir")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        installation = json.loads(Path(args.installation).read_text(encoding="utf-8-sig"))
        interpreter = Path(installation["python"]).resolve()
        if not interpreter.is_file() or not Path(installation["root"]).is_dir():
            raise ValueError("INSTALLATION_MISSING")
        if not args.worker:
            command = [str(interpreter), "-B", str(Path(__file__).resolve()), "--worker",
                       "--installation", str(Path(args.installation).resolve()), "--mode", args.mode]
            if args.evidence_dir:
                command.extend(["--evidence-dir", args.evidence_dir])
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                     env={**os.environ, "PYTHONUTF8": "1"}, timeout=450)
            value = json.loads(process.stdout)
        else:
            value = asyncio.run(asyncio.wait_for(worker(installation, args.mode, args.evidence_dir), timeout=420))
    except (TimeoutError, subprocess.TimeoutExpired):
        value = failed_crawl("TIMEOUT") if args.mode == "collect" else {"status": "ERROR", "code": "TIMEOUT"}
    except Exception:
        # No raw exception strings: they may contain local paths/environment data.
        value = failed_crawl("MCP_ERROR") if args.mode == "collect" else {"status": "ERROR", "code": "MCP_ERROR"}
    print(json.dumps(value, ensure_ascii=False))
    return 0 if "sources" in value or value.get("status") == "OK" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
