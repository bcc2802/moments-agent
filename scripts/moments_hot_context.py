"""Small, read-only public context for history exclusion and editorial shards.

No cache is authoritative. A completed menu is conservatively treated as prior
recommendation evidence; it does not establish that the chat UI displayed it.
"""
from __future__ import annotations

from collections import Counter
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import unquote, urlsplit

from moments import MomentsError, public_url
from moments_platforms import ARCHIVE_PLATFORMS, SOURCE_IDS
from moments_trends import TrendsError, _INDEX_FIELDS, _INJECTION, _PRIVATE, _platform_url, _title
from moments_workflow import Workflow, WorkflowError, RUN_ID

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RUNS = 1024
MAX_RESEARCH = 128
MAX_ROWS = 20000
HASH = re.compile(r"[0-9a-f]{64}")
BATCH = re.compile(r"tr-[0-9a-f]{32}")
IDENTITY = re.compile(r"[0-9a-f]{24}")
LEGACY_RELATIVE = Path("iterations/2026-10-20/trendradar/selection-v1/final-menu.json")
LEGACY_BATCH = "tr-12e60203a9c84532a8c801bdb043b3da"


class ContextError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message="公共上下文无法安全读取；未改稿件或状态。"):
    raise ContextError(code, message)


def _errors(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (TrendsError, MomentsError, WorkflowError) as exc:
            fail(exc.code, str(exc))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            fail("HOT_CONTEXT_INVALID")
    return checked


def _text(value, limit=500, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value)
            or _PRIVATE.search(value) or _INJECTION.search(value)):
        fail("UNSAFE_PUBLIC_TEXT")
    return value.strip()


def _research_url(value):
    url = public_url(value)
    path = unquote(urlsplit(url).path)
    if (_PRIVATE.search(path) or re.search(
            r"(?:^|/)(?:token|password|passwd|secret|cookie|session|credential|api[_-]?key|access[_-]?token)(?:/|=|:|$)",
            path, re.I)):
        fail("CREDENTIAL_URL")
    return url


class Context:
    def __init__(self, runner):
        self.runner = runner
        self.project = Path(runner.project).absolute()
        self.trends = self.project / "runs" / "trends"
        self.research = self.project / "runs" / "research"
        # This single production source is documented by CURRENT_STATUS; never
        # discover arbitrary test/replay files under the iterations directory.
        self.legacy = self.project.parent / LEGACY_RELATIVE

    def _safe(self, path):
        path = Path(path)
        if path.absolute() != self.legacy and not path.absolute().is_relative_to(self.project):
            fail("UNSAFE_PATH")
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                fail("UNSAFE_PATH")
            if part == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                fail("UNSAFE_PATH")
        return path

    def _bytes(self, path):
        path = self._safe(path)
        if not path.is_file():
            fail("PUBLIC_ARTIFACT_MISSING")
        if path.stat().st_size > MAX_FILE_BYTES:
            fail("PUBLIC_ARTIFACT_TOO_LARGE")
        value = path.read_bytes()
        self._safe(path)
        return value

    def _json(self, path):
        return json.loads(self._bytes(path).decode("utf-8-sig"))

    def _label(self, path):
        if Path(path).absolute() == self.legacy:
            return "../" + LEGACY_RELATIVE.as_posix()
        relative = Path(path).relative_to(self.project).as_posix()
        # Generated workflow IDs can happen to contain eleven phone-like digits.
        # Their strict path grammar is sufficient; editorial text and arbitrary
        # research directory names still receive the full public-text checks.
        if re.fullmatch(r"runs/trends/wf-[0-9a-f]{32}/menu\.json", relative):
            return relative
        return _text(relative, 600)

    def _legacy_menu(self):
        raw = self._bytes(self.legacy)
        value = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(value, dict) or value.get("status") != "OK"
                or value.get("batch_id") != LEGACY_BATCH
                or value.get("selection_rule_version") != "1.0"):
            fail("INVALID_LEGACY_MENU")
        menu = self._menu(value, {"fetched_at": value.get("fetched_at"), "selection_version": "1.0"})
        return menu, {"path": self._label(self.legacy), "sha256": hashlib.sha256(raw).hexdigest(),
                      "basis": "LEGACY_DOCUMENTED_NO_MANIFEST_HASH_CHAIN"}

    @staticmethod
    def _menu(value, manifest):
        if (not isinstance(value, dict) or not isinstance(value.get("batch_id"), str)
                or not BATCH.fullmatch(value["batch_id"])
                or value.get("fetched_at") != manifest.get("fetched_at")
                or value.get("selection_rule_version") != manifest.get("selection_version")
                or not isinstance(value.get("items"), list) or len(value["items"]) > 10):
            fail("INVALID_HISTORY_MENU")
        clean = []
        for number, item in enumerate(value["items"], 1):
            if (not isinstance(item, dict) or type(item.get("number")) is not int
                    or item["number"] != number or not isinstance(item.get("candidate_id"), str)
                    or not IDENTITY.fullmatch(item["candidate_id"])):
                fail("INVALID_HISTORY_MENU")
            platform = item.get("platform")
            url = _platform_url(item.get("url"), platform, allow_historical=True)
            primary = {"title": _title(item.get("title")), "url": url,
                       "number": number, "candidate_id": item["candidate_id"],
                       "event_key": _text(item.get("event_key"), 150, True),
                       "angle": _text(item.get("angle"), 500, True)}
            clean.append(primary)
            # A selected event may have been observed on several sources. Every
            # displayed event's source pointers count as history, not just the
            # representative URL chosen by the composer.
            occurrences = item.get("occurrences", [])
            if not isinstance(occurrences, list) or len(occurrences) > 100:
                fail("INVALID_HISTORY_MENU")
            seen_urls = {url}
            for occurrence in occurrences:
                if not isinstance(occurrence, dict):
                    fail("INVALID_HISTORY_MENU")
                occurrence_url = _platform_url(occurrence.get("url"), occurrence.get("platform"),
                                               allow_historical=True)
                occurrence_title = _title(occurrence.get("title"))
                if occurrence_url not in seen_urls:
                    clean.append({**primary, "title": occurrence_title, "url": occurrence_url})
                    seen_urls.add(occurrence_url)
        return {"batch_id": value["batch_id"], "items": clean}

    def _run(self, path):
        manifest = self._json(path / "manifest.json")
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("run_id") != path.name
                or manifest.get("stage") not in ("PREPARED", "CURATED")
                or type(manifest.get("replay")) is not bool):
            fail("INVALID_HISTORY_MANIFEST")
        if manifest["replay"]:
            return None, "REPLAY_EXCLUDED"
        if manifest["stage"] != "CURATED":
            return None, "NOT_CURATED"
        artifacts = manifest.get("artifacts")
        required = {"crawl.json", "pool.json", "evaluations.json", "menu.json"}
        if (not isinstance(artifacts, dict) or set(artifacts) != required
                or any(not isinstance(v, str) or not HASH.fullmatch(v) for v in artifacts.values())):
            fail("INVALID_HISTORY_MANIFEST")
        menu_bytes = None
        for name, expected in artifacts.items():
            raw = self._bytes(path / name)
            if hashlib.sha256(raw).hexdigest() != expected:
                fail("PUBLIC_ARTIFACT_HASH_MISMATCH")
            if name == "menu.json":
                menu_bytes = raw
        value = json.loads(menu_bytes.decode("utf-8"))
        menu = self._menu(value, manifest)
        menu["path"] = self._label(path / "menu.json")
        menu["pool_sha256"] = artifacts["pool.json"]
        menu["evaluations_sha256"] = artifacts["evaluations.json"]
        menu["index"] = {key: value[key] for key in ("batch_id", "fetched_at", "selection_rule_version")}
        menu["index"]["items"] = [{key: row[key] for key in _INDEX_FIELDS} for row in value["items"]]
        return menu, None

    def _research_paths(self, skipped):
        self._safe(self.research)
        if not self.research.exists():
            return []
        paths = []
        def onerror(error):
            skipped.append({"source_id": "research-scan", "code": "SCAN_FAILED"})
        for root, dirs, files in os.walk(self.research, followlinks=False, onerror=onerror):
            dirs.sort()
            for name in list(dirs):
                try:
                    self._safe(Path(root) / name)
                except ContextError:
                    dirs.remove(name)
                    skipped.append({"source_id": "research-path", "code": "UNSAFE_PATH"})
            if "RECOMMENDATIONS.json" in files:
                paths.append(Path(root) / "RECOMMENDATIONS.json")
                if len(paths) > MAX_RESEARCH:
                    skipped.append({"source_id": "research-scan", "code": "RESEARCH_LIMIT"})
                    return paths[:MAX_RESEARCH]
        return paths

    def _attachment(self, path, menus):
        raw = self._bytes(path)
        sidecar = path.with_suffix(".sha256")
        if sidecar.exists() or sidecar.is_symlink():
            checksum = self._bytes(sidecar).decode("ascii").strip()
            if not HASH.fullmatch(checksum) or hashlib.sha256(raw).hexdigest() != checksum:
                fail("PUBLIC_ARTIFACT_HASH_MISMATCH")
        value = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("hot_run_id") not in menus or not isinstance(value.get("items"), list)
                or len(value["items"]) > 10):
            fail("UNBOUND_RESEARCH_ATTACHMENT")
        menu = menus[value["hot_run_id"]]
        if value.get("hot_batch_id") != menu["batch_id"]:
            fail("UNBOUND_RESEARCH_ATTACHMENT")
        clean = []
        for number, item in enumerate(value["items"], 1):
            if not isinstance(item, dict) or type(item.get("number")) is not int or item["number"] != number:
                fail("INVALID_RESEARCH_ATTACHMENT")
            kind = item.get("kind")
            if kind == "hot":
                match = next((r for r in menu["items"] if r["number"] == item.get("hot_number")), None)
                if (match is None or any(item.get(k) != match[k] for k in ("candidate_id", "url"))
                        or item.get("source_title") != match["title"]):
                    fail("UNBOUND_RESEARCH_ATTACHMENT")
                url = match["url"]
            elif kind == "research":
                url = _research_url(item.get("url"))
            else:
                fail("INVALID_RESEARCH_ATTACHMENT")
            clean.append({"url": url, "title": _title(item.get("source_title")),
                          "angle": _text(item.get("angle"), 500, True), "event_key": None})
        return clean, menu

    @_errors
    def history(self, *, optional_legacy=False, exclude_index=None,
                exclude_pool_sha256=None, exclude_evaluations_sha256=None):
        """Read canonical public artifacts only; never touches the Vault or state."""
        retry_fields = (exclude_index, exclude_pool_sha256, exclude_evaluations_sha256)
        if (type(optional_legacy) is not bool
                or (any(value is not None for value in retry_fields)
                    and (not isinstance(exclude_index, dict)
                         or any(not isinstance(value, str) or not HASH.fullmatch(value)
                                for value in retry_fields[1:])))):
            fail("INVALID_HISTORY_FILTER")

        def same_retry(menu):
            # The caller supplies its complete current minimal index; Context
            # itself never reads state. Compare JSON types exactly (True != 1),
            # and require both complete inputs of this hash-verified formal run.
            # In particular, changed judgments on the same pool are a new menu.
            return (exclude_index is not None
                    and json.dumps(menu["index"], sort_keys=True, allow_nan=False)
                    == json.dumps(exclude_index, sort_keys=True, allow_nan=False)
                    and menu["pool_sha256"] == exclude_pool_sha256
                    and menu["evaluations_sha256"] == exclude_evaluations_sha256)

        self._safe(self.trends)
        skipped, excluded, menus, collected = [], [], {}, []
        paths = sorted(self.trends.iterdir(), key=lambda p: p.name) if self.trends.exists() else []
        runs = [p for p in paths if RUN_ID.fullmatch(p.name)]
        if len(runs) > MAX_RUNS:
            skipped.append({"source_id": "trends-scan", "code": "RUN_LIMIT"})
        for path in runs[:MAX_RUNS]:
            try:
                menu, reason = self._run(path)
                if reason:
                    excluded.append({"source_id": path.name, "code": reason})
                    continue
                menus[path.name] = menu
                if same_retry(menu):
                    excluded.append({"source_id": path.name, "code": "CURRENT_RETRY_EXCLUDED"})
                else:
                    collected.extend((row, menu["path"], menu["batch_id"], "hot") for row in menu["items"])
            except (ContextError, TrendsError, OSError, ValueError, TypeError, KeyError) as exc:
                skipped.append({"source_id": path.name, "code": getattr(exc, "code", "INVALID_PUBLIC_ARTIFACT")})
        legacy_sources = []
        self._safe(self.legacy)
        if optional_legacy and not self.legacy.parent.exists():
            # Fresh installations have no documented 2026-10-20 archive. An
            # existing directory with a missing/bad file is still incomplete.
            excluded.append({"source_id": "documented-legacy-menu", "code": "LEGACY_NOT_CONFIGURED"})
        else:
            try:
                legacy_menu, legacy_evidence = self._legacy_menu()
                legacy_sources.append(legacy_evidence)
                collected.extend((row, legacy_evidence["path"], legacy_menu["batch_id"], "legacy_documented_menu")
                                 for row in legacy_menu["items"])
            except (ContextError, TrendsError, OSError, ValueError, TypeError, KeyError) as exc:
                skipped.append({"source_id": "documented-legacy-menu",
                                "code": getattr(exc, "code", "INVALID_PUBLIC_ARTIFACT")})
        research_count = 0
        for index, path in enumerate(self._research_paths(skipped)):
            try:
                rows, menu = self._attachment(path, menus)
                label = self._label(path)
                if not same_retry(menu):
                    collected.extend((row, label, None, "research_attachment") for row in rows)
                research_count += 1
            except (ContextError, MomentsError, TrendsError, OSError, ValueError, TypeError, KeyError) as exc:
                skipped.append({"source_id": "research-" + str(index + 1),
                                "code": getattr(exc, "code", "INVALID_PUBLIC_ARTIFACT")})
        if len(collected) > MAX_ROWS:
            skipped.append({"source_id": "history-rows", "code": "ROW_LIMIT"})
        grouped = {}
        for row, path, batch_id, kind in collected[:MAX_ROWS]:
            entry = grouped.setdefault(row["url"], {"url": row["url"], "title": row["title"],
                "other_titles": [], "event_keys": [], "angles": [], "evidence": []})
            if row["title"] != entry["title"] and row["title"] not in entry["other_titles"]:
                entry["other_titles"].append(row["title"])
            for source, target in (("event_key", "event_keys"), ("angle", "angles")):
                if row[source] and row[source] not in entry[target]:
                    entry[target].append(row[source])
            evidence = {"path": path, "kind": kind, "batch_id": batch_id}
            if evidence not in entry["evidence"]:
                entry["evidence"].append(evidence)
        return {"status": "PARTIAL" if skipped else "OK", "coverage_complete": not skipped,
                "scope": "runs/trends/wf-*/manifest.json + hash-verified artifacts; linked research attachments",
                "menu_count": len(menus), "research_attachment_count": research_count,
                "legacy_menu_count": len(legacy_sources), "legacy_sources": legacy_sources,
                "record_count": len(collected), "unique_url_count": len(grouped),
                "entries": [grouped[url] for url in sorted(grouped)], "skipped": skipped, "excluded": excluded,
                "limitations": ["Only the single documented 2026-10-20 legacy menu is included outside canonical roots; its observed hash is not a manifest hash chain.",
                                "Completed menus are conservative exclusion evidence, not proof of actual chat display."],
                "notice": "只做公开历史精确URL索引；事件和读者问题仍由模型判断，未联网、不读私人笔记、不改state。"}

    @_errors
    def review(self, run_id, platforms=None):
        """Return all requested shard rows, retaining complete-pool identities."""
        if platforms is not None and (not isinstance(platforms, list) or not platforms
                or any(not isinstance(p, str) or p not in SOURCE_IDS for p in platforms)
                or len(set(platforms)) != len(platforms)):
            fail("INVALID_REVIEW_PLATFORMS")
        restored = Workflow(self.runner).resume(run_id)
        pool = restored["pool"]
        history = self.history()
        seen = {row["url"] for row in history["entries"]}
        all_rows = pool["candidates"]
        selected = [r for r in all_rows if platforms is None or r["platform"] in platforms]
        columns = ["candidate_id", "platform", "rank", "title", "url", "additional_occurrences", "history_exact_urls"]
        rows = []
        for row in selected:
            occurrences = [{k: o[k] for k in ("platform", "rank", "title", "url")}
                           for o in row["occurrences"]]
            primary = {k: row[k] for k in ("platform", "rank", "title", "url")}
            additional = [o for o in occurrences if o != primary]
            rows.append([row["candidate_id"], row["platform"], row["rank"], row["title"], row["url"],
                         additional, sorted({o["url"] for o in occurrences if o["url"] in seen})])
        identities = sorted(r["candidate_id"] for r in all_rows)
        selected_ids = sorted(r["candidate_id"] for r in selected)
        return {"status": history["status"], "run_id": run_id, "stage": restored["stage"],
                "pool_id": pool["pool_id"], "fetched_at": pool["fetched_at"],
                "current_batch_matches": restored["current_batch_matches"],
                "current_platforms_match": restored["current_platforms_match"],
                "replay": restored["replay"], "time_diagnostic": restored["time_diagnostic"],
                "columns": columns, "rows": rows,
                "occurrence_encoding": "Primary occurrence is reconstructed from platform/rank/title/url columns; additional_occurrences preserves all other source pointers.",
                "coverage": {"pool_total": len(all_rows), "returned_count": len(rows),
                    "covers_full_pool": len(rows) == len(all_rows), "platforms": platforms,
                    "pool_by_platform": dict(Counter(r["platform"] for r in all_rows)),
                    "returned_by_platform": dict(Counter(r["platform"] for r in selected)),
                    "pool_identity_sha256": hashlib.sha256("\n".join(identities).encode()).hexdigest(),
                    "shard_identity_sha256": hashlib.sha256("\n".join(selected_ids).encode()).hexdigest()},
                "history": {key: history[key] for key in ("status", "coverage_complete", "menu_count",
                    "research_attachment_count", "legacy_menu_count", "unique_url_count", "skipped", "excluded", "limitations")},
                "notice": "分片仅减少传输；所有候选仍须完整评价。精确命中仅作标记，不自动排除或降低门槛；跨事件与切口排重另读hot-history。"}
