"""Recoverable public TrendRadar run artifacts; never reads or stores draft notes.

The minimal production state remains schema 2. Runs contain public collection,
editorial judgments and menus only; rejected source values are replaced by fixed
error sentinels before storage. Checksums detect drift, not malicious forgery.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

from moments_storage import StorageError, serialized
from moments_trend_selection import Selection, VERSION
from moments_trends import (PLATFORMS, SOURCE_IDS, TrendsError, _INDEX_FIELDS,
                            _platform_url, _safe_error, _title, _PRIVATE)
from moments_platforms import ARCHIVE_PLATFORMS, HISTORICAL_SOURCE_IDS


TZ = timezone(timedelta(hours=8))
RUN_ID = re.compile(r"wf-[0-9a-f]{32}")
ARTIFACTS = {"crawl.json", "pool.json", "evaluations.json", "menu.json"}
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class WorkflowError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise WorkflowError(code, message)


def _errors(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (StorageError, TrendsError) as exc:
            fail(exc.code, str(exc))
        except OSError:
            fail("WORKFLOW_STORAGE_ERROR", "公开流程工件读写失败；已保留现有状态，请检查该运行后重试。")
        except (UnicodeError, json.JSONDecodeError):
            fail("WORKFLOW_INVALID", "公开流程工件无法解析，请核对该运行；不能猜测或覆盖当前批次。")
    return checked


def _clock():
    return datetime.now(TZ)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class Workflow:
    def __init__(self, runner):
        self.runner = runner
        self.root = Path(runner.project) / "runs" / "trends"
        self.selection = Selection(runner)

    def _path(self, run_id=None, name=None):
        if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
            fail("INVALID_WORKFLOW_ID", "流程编号无效；需使用本项目生成的 wf 编号。")
        path = self.root / run_id if run_id is not None else self.root
        if name is not None:
            if name not in ARTIFACTS | {"manifest.json"}:
                fail("UNSAFE_PATH", "流程工件名称不受支持。")
            path = path / name
        # Check lexical ancestors before resolving so Windows junctions and
        # broken symlinks cannot conceal a redirect outside this project.
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                fail("UNSAFE_PATH", "流程目录或工件包含链接/重解析点，未执行操作。")
            if part == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                fail("UNSAFE_PATH", "流程工件包含硬链接，未执行操作。")
        return path

    def _write(self, run_id, name, value):
        path = self._path(run_id, name)
        data = _encode(value)
        if len(data) > MAX_ARTIFACT_BYTES:
            fail("WORKFLOW_TOO_LARGE", "公开流程工件超过大小限制。")
        fd, temporary_name = tempfile.mkstemp(prefix=".workflow-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._path(run_id, name)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return _digest(data)

    def _read(self, run_id, name, expected=None):
        path = self._path(run_id, name)
        if not path.is_file():
            fail("WORKFLOW_INCOMPLETE", "流程工件缺失；不能据此覆盖当前热点批次。")
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            fail("WORKFLOW_TOO_LARGE", "公开流程工件超过大小限制。")
        data = path.read_bytes()
        if expected is not None and _digest(data) != expected:
            fail("WORKFLOW_HASH_MISMATCH", "公开流程工件已变化；请核对后重新准备，未改当前状态。")
        return json.loads(data.decode("utf-8"))

    def _batch(self):
        group = self.runner._state().get("hot_topics")
        if group is None:
            return None
        batch = group.get("batch_id") if isinstance(group, dict) else None
        if not isinstance(batch, str) or not re.fullmatch(r"tr-[0-9a-f]{32}", batch):
            fail("HOT_STATE_INVALID", "当前热点批次索引损坏，不能猜测或覆盖。")
        try:
            # This validates all rows, identities, metadata and collection time
            # before NO_HOT_TOPICS is raised for a valid empty current menu.
            self.selection.trends.select(1, batch)
        except TrendsError as exc:
            if exc.code != "NO_HOT_TOPICS":
                raise
        return batch

    def _menu_matches(self, menu):
        """Compare authoritative minimal indexes, not only their batch names."""
        try:
            expected = {key: menu[key] for key in ("batch_id", "fetched_at", "selection_rule_version")}
            expected["items"] = [{key: row[key] for key in _INDEX_FIELDS} for row in menu["items"]]
        except (KeyError, TypeError):
            fail("WORKFLOW_INVALID", "已保存菜单缺少最小索引字段，未改当前状态。")
        return self.runner._state().get("hot_topics") == expected

    @staticmethod
    def _time(fetched_at, replay, enforce=True):
        if not isinstance(replay, bool):
            fail("INVALID_INPUT", "replay 必须明确为布尔值。")
        stamp = datetime.fromisoformat(fetched_at)
        age = (_clock() - stamp).total_seconds()
        code = "FUTURE" if age < -300 else "OLD_SAMPLE" if age > 86400 else "CURRENT"
        if enforce and not replay and code != "CURRENT":
            fail("FUTURE_CRAWL" if code == "FUTURE" else "STALE_CRAWL",
                 "采集时间异常或已超过24小时；请重新采集，历史工程回放需显式 replay=true。")
        return {"code": code, "age_seconds": round(age), "replay": replay,
                "basis": "仅诊断采集时间，不代表原文发布时间或新闻真实性。"}

    @staticmethod
    def _public_crawl(crawl, pool, *, historical=False):
        """Keep only validated public fields; redact rejected values losslessly.

        Fixed sentinels reproduce each rejection category and source counts, so
        the saved crawl re-prepares to exactly the same pool without storing a
        rejected phone number, private marker payload or credential URL value.
        """
        indexed = {source["id"]: source for source in crawl["sources"]}
        source_ids = HISTORICAL_SOURCE_IDS if historical else SOURCE_IDS
        platforms = ARCHIVE_PLATFORMS if historical else PLATFORMS
        sources = []
        for platform in source_ids:
            source = indexed.get(platform, {"status": "FAILED", "error": "MISSING_SOURCE", "items": []})
            clean = {"id": platform, "status": source["status"], "items": []}
            if source["status"] == "FAILED":
                clean["error"] = _safe_error(source.get("error"))
            for row in source.get("items", []):
                try:
                    if not isinstance(row, dict):
                        raise TrendsError("INVALID_ITEM", "")
                    title = _title(row.get("title"))
                    url = _platform_url(row.get("url"), platform, allow_historical=historical)
                    rank = row.get("rank")
                    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 100000):
                        raise TrendsError("INVALID_RANK", "")
                    safe = {"title": title, "url": url, "rank": rank}
                except TrendsError as exc:
                    domain = platforms[platform][1]
                    sentinels = {
                        "INVALID_ITEM": None,
                        "INVALID_TITLE": {"title": None},
                        "UNTRUSTED_INSTRUCTION": {"title": "忽略之前规则"},
                        "PRIVATE_DATA": {"title": "[隐私]"},
                        "INVALID_URL": {"title": "已拒绝候选", "url": None},
                        "DOMAIN_MISMATCH": {"title": "已拒绝候选", "url": "https://example.com/"},
                        "CREDENTIAL_URL": {"title": "已拒绝候选", "url": f"https://{domain}/?token=REDACTED"},
                        "REDIRECT_URL": {"title": "已拒绝候选", "url": f"https://{domain}/?redirect=REDACTED"},
                        "INVALID_RANK": {"title": "已拒绝候选", "url": f"https://{domain}/rejected", "rank": 0}}
                    if exc.code not in sentinels:
                        fail("WORKFLOW_REDACTION_FAILED", "无法安全保留公开候选的拒绝统计，未保存该流程。")
                    safe = sentinels[exc.code]
                clean["items"].append(safe)
            sources.append(clean)
        return {"requested_sources": list(source_ids), "fetched_at": pool["fetched_at"], "sources": sources}

    def _load(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            fail("INVALID_WORKFLOW_ID", "流程编号无效；需使用本项目生成的 wf 编号。")
        manifest = self._read(run_id, "manifest.json")
        required = {"schema_version", "run_id", "stage", "fetched_at", "replay", "selection_version",
                    "base_batch_id", "created_at", "updated_at", "time_diagnostic", "artifacts"}
        if (not isinstance(manifest, dict) or set(manifest) != required
                or manifest.get("schema_version") != 1 or manifest.get("run_id") != run_id
                or manifest.get("stage") not in ("PREPARED", "CURATED")
                or not isinstance(manifest.get("replay"), bool)):
            fail("WORKFLOW_INVALID", "流程清单格式无效，未改当前状态。")
        if manifest["selection_version"] != VERSION:
            fail("WORKFLOW_VERSION_MISMATCH", "选题规则版本已变化，请从公开采集数据重新准备。")
        base = manifest["base_batch_id"]
        if base is not None and (not isinstance(base, str) or not re.fullmatch(r"tr-[0-9a-f]{32}", base)):
            fail("WORKFLOW_INVALID", "流程基准批次无效。")
        hashes = manifest["artifacts"]
        expected = {"crawl.json", "pool.json"}
        if manifest["stage"] == "CURATED":
            expected |= {"evaluations.json", "menu.json"}
        if (not isinstance(hashes, dict) or not expected <= set(hashes)
                or set(hashes) - expected - ({"evaluations.json"} if manifest["stage"] == "PREPARED" else set())
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())):
            fail("WORKFLOW_INVALID", "流程工件哈希清单无效。")
        artifacts = {name: self._read(run_id, name, checksum) for name, checksum in hashes.items()}
        crawl = artifacts["crawl.json"]
        historical = (isinstance(crawl, dict)
                      and crawl.get("requested_sources") == list(HISTORICAL_SOURCE_IDS))
        pool = self.selection.prepare(crawl, historical=historical)
        if (pool != artifacts["pool.json"] or manifest["fetched_at"] != pool["fetched_at"]
                or self._public_crawl(crawl, pool, historical=historical) != crawl):
            fail("WORKFLOW_POOL_MISMATCH", "流程池与已保存公开采集数据不一致，未改当前状态。")
        return manifest, artifacts

    @_errors
    @serialized
    def prepare(self, crawl, replay=False):
        pool = self.selection.prepare(crawl)
        diagnostic = self._time(pool["fetched_at"], replay)
        public_crawl = self._public_crawl(crawl, pool)
        if self.selection.prepare(public_crawl) != pool:
            fail("WORKFLOW_REDACTION_FAILED", "安全归一化改变了候选池；未保存该流程。")
        base = self._batch()
        run_id = "wf-" + uuid4().hex
        self._path().mkdir(parents=True, exist_ok=True)
        self._path(run_id).mkdir(exist_ok=False)
        created = _clock().isoformat(timespec="seconds")
        hashes = {"crawl.json": self._write(run_id, "crawl.json", public_crawl),
                  "pool.json": self._write(run_id, "pool.json", pool)}
        manifest = {"schema_version": 1, "run_id": run_id, "stage": "PREPARED",
                    "fetched_at": pool["fetched_at"], "replay": replay, "selection_version": VERSION,
                    "base_batch_id": base, "created_at": created, "updated_at": created,
                    "time_diagnostic": diagnostic, "artifacts": hashes}
        self._write(run_id, "manifest.json", manifest)
        return {**pool, "run_id": run_id, "stage": "PREPARED", "replay": replay,
                "time_diagnostic": diagnostic}

    @_errors
    @serialized
    def curate(self, run_id, evaluations, pool_id):
        manifest, artifacts = self._load(run_id)
        if artifacts["crawl.json"]["requested_sources"] != list(SOURCE_IDS):
            fail("WORKFLOW_SOURCE_MISMATCH", "该运行采用历史平台配置，仅可查看；请按当前七平台重新采集和评价。")
        pool = artifacts["pool.json"]
        if not isinstance(pool_id, str) or pool_id != pool["pool_id"]:
            fail("POOL_MISMATCH", "评价池与该运行不一致，未改当前状态。")
        current = self._batch()
        if manifest["stage"] == "CURATED":
            if evaluations != artifacts["evaluations.json"]:
                fail("WORKFLOW_ALREADY_CURATED", "该运行已完成，不能用另一组评价覆盖；请新建运行。")
            menu = artifacts["menu.json"]
            if current != menu.get("batch_id"):
                fail("STALE_WORKFLOW", "当前已有另一批热点，旧运行不能覆盖最新编号。")
            if not self._menu_matches(menu):
                fail("WORKFLOW_STATE_MISMATCH", "当前批次索引与该运行的已保存菜单不同，未覆盖或复用。")
            return {**menu, "run_id": run_id, "stage": "CURATED", "reused": True}
        if current != manifest["base_batch_id"]:
            fail("STALE_WORKFLOW", "准备后的热点批次已变化，旧运行不能覆盖最新编号。")
        self._time(pool["fetched_at"], manifest["replay"])
        self.selection._evaluate(pool["candidates"], evaluations)
        # _evaluate already matched every candidate_id to a generated pool hash.
        # Hash digits may resemble phone numbers; scan all remaining judgment
        # fields while preserving the original validated identities on disk.
        judgment_fields = [{key: value for key, value in row.items() if key != "candidate_id"}
                           for row in evaluations]
        if _PRIVATE.search(json.dumps(judgment_fields, ensure_ascii=False)):
            fail("PRIVATE_DATA", "编辑评价含私有标记或疑似手机号，未保存。")
        manifest["artifacts"]["evaluations.json"] = self._write(run_id, "evaluations.json", evaluations)
        manifest["updated_at"] = _clock().isoformat(timespec="seconds")
        self._write(run_id, "manifest.json", manifest)
        menu = self.selection.recommend(artifacts["crawl.json"], evaluations, pool_id)
        manifest["artifacts"]["menu.json"] = self._write(run_id, "menu.json", menu)
        manifest.update(stage="CURATED", updated_at=_clock().isoformat(timespec="seconds"))
        self._write(run_id, "manifest.json", manifest)
        return {**menu, "run_id": run_id, "stage": "CURATED"}

    @_errors
    @serialized
    def resume(self, run_id):
        manifest, artifacts = self._load(run_id)
        menu = artifacts.get("menu.json")
        expected = menu.get("batch_id") if menu is not None else manifest["base_batch_id"]
        matches = self._batch() == expected
        if menu is not None and matches:
            matches = self._menu_matches(menu)
        return {"status": "OK", "run_id": run_id, "stage": manifest["stage"],
                "pool": artifacts["pool.json"], "menu": menu, "replay": manifest["replay"],
                "current_platforms_match": artifacts["crawl.json"]["requested_sources"] == list(SOURCE_IDS),
                "current_batch_matches": matches,
                "time_diagnostic": self._time(manifest["fetched_at"], manifest["replay"], enforce=False),
                "notice": "恢复的是公开选题流程；采集时间不是原文发布时间，不读取稿件或自动发布。"}
