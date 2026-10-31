"""Recoverable local media preparation; browser calls and editorial review stay with the agent.

This module never drives a browser, reads cookies, installs dependencies, generates
webpage evidence, changes draft state, or infers publication/media usage.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
from uuid import uuid4

from moments_media import (Media, MediaError, MAX_BYTES, _errors, _safe_path,
                           _timestamp, fail, image_info)
from moments_storage import serialized
from moments_trends import TrendsError

RUN = re.compile(r"mw-[0-9a-f]{32}")
KINDS = {"COVER", "COVER_BOXED", "PAGE", "ASSET"}
MAX_JSON = 262144
NOTICE = "仅准备图片；浏览器采集、选图理由和逐张目视由调用方完成，不改稿件、state或发布记录。"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def bounded(value, label, maximum=300):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(unicodedata.category(c) in {"Cc", "Cf"} for c in value)):
        fail("INVALID_MEDIA_WORKFLOW", f"{label}需为有界非空单行文字。")
    return value.strip()


def number(value, maximum=3600000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
        fail("INVALID_MEDIA_WORKFLOW", "耗时需为有界非负毫秒数。")
    return value


def rectangle(value, capture):
    if (not isinstance(value, list) or len(value) != 4
            or any(isinstance(x, bool) or not isinstance(x, int) for x in value)):
        fail("INVALID_MEDIA_WORKFLOW", "矩形需为原图像素坐标[x1,y1,x2,y2]。")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= capture["width"] and 0 <= y1 < y2 <= capture["height"]):
        fail("INVALID_MEDIA_WORKFLOW", "矩形超出原图或没有有效面积。")
    return value


class MediaWorkflow:
    def __init__(self, runner, *, atomic, now, public_url, privacy_check):
        self.runner, self.atomic, self.now = runner, atomic, now
        self.privacy_check = privacy_check
        self.media = Media(runner, atomic=atomic, now=now, public_url=public_url,
                           privacy_check=privacy_check)
        # Captures can contain signed-in UI. Never put them in public trend runs.
        self.root = runner.project / "backups" / "media-workflow"

    def _requirement(self, round_id):
        from moments_preparation import MEDIA_REQUIREMENTS
        context, notes = self.media._round(round_id, with_notes=True)
        requirements = {note["meta"].get("media_requirement", "DEFAULT") for note in notes}
        if len(requirements) != 1 or not requirements <= MEDIA_REQUIREMENTS:
            fail("INVALID_MEDIA_REQUIREMENT", "本轮稿件的媒体要求无效或不一致。")
        return next(iter(requirements))

    def _folder(self, run_id):
        if not isinstance(run_id, str) or not RUN.fullmatch(run_id):
            fail("INVALID_MEDIA_WORKFLOW", "需明确有效的配图运行ID，不猜最新轮次。")
        _safe_path(self.root)
        return _safe_path(self.root / run_id)

    def _write(self, value):
        folder = self._folder(value["run_id"])
        target = _safe_path(folder / "workflow.json")
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_JSON:
            fail("MEDIA_WORKFLOW_LIMIT", "配图工件达到上限，请保留该轮并新开运行。")
        if target.exists():
            if target.read_text(encoding="utf-8") == payload:
                return
            self.runner._backup(target)
        self.atomic(target, payload)

    def _bundle_hash(self, round_id):
        path = self.media._folder(round_id) / "manifest.json"
        return digest(_safe_path(path, must_file=True).read_bytes()) if path.exists() else None

    def _capture_path(self, folder, row):
        name = row.get("file")
        if (not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg)", name)
                or row.get("capture_id") != "cap-" + str(row.get("sha256", ""))[:24]):
            fail("MEDIA_WORKFLOW_INVALID", "原图文件身份异常。")
        path = _safe_path(folder / "originals" / name, must_file=True)
        if path.stat().st_size > MAX_BYTES:
            fail("INVALID_MEDIA_IMAGE", "原图超过16MiB上限。")
        data = path.read_bytes()
        info, suffix = image_info(data)
        if (digest(data) != row.get("sha256") or name != row["sha256"] + suffix
                or any(row.get(k) != v for k, v in info.items())):
            fail("MEDIA_HASH_MISMATCH", "已保存原图被改动，未重新生成或提交。")
        if row.get("kind") not in KINDS or row.get("source_type") not in {
                "USER_PROVIDED", "PUBLIC_CAPTURE", "PUBLIC_ORIGINAL"}:
            fail("MEDIA_WORKFLOW_INVALID", "原图来源类型异常。")
        _timestamp(row.get("captured_at"), False)
        _timestamp(row.get("content_published_at"), True)
        if row["source_type"] != "USER_PROVIDED" and row.get("captured_at") is None:
            fail("MEDIA_WORKFLOW_INVALID", "公开原图缺少采集时间。")
        return path

    def _load(self, run_id, current=False):
        folder = self._folder(run_id)
        path = _safe_path(folder / "workflow.json", must_file=True)
        if path.stat().st_size > MAX_JSON:
            fail("MEDIA_WORKFLOW_INVALID", "配图工件超出读取上限。")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("marker") != "moments_media_workflow"
                or value.get("schema_version") != 1 or value.get("run_id") != run_id
                or value.get("stage") not in {"CAPTURING", "REVIEWED", "RENDERED", "COMMITTED"}
                or not isinstance(value.get("captures"), list) or len(value["captures"]) > 8
                or not isinstance(value.get("candidates"), list) or len(value["candidates"]) > 12
                or not isinstance(value.get("renders"), list) or len(value["renders"]) > 4
                or not isinstance(value.get("events"), list) or len(value["events"]) > 40):
            fail("MEDIA_WORKFLOW_INVALID", "配图工件结构或归属异常。")
        context = self.media._round(value.get("round_id"), current=current)
        if current and self._requirement(value["round_id"]) in {"SKIP_ALL", "REUSE_ONLY"}:
            fail("MEDIA_NOT_ALLOWED", "本轮明确跳过新配图，仅可只读查看既有材料。")
        if any(context[k] != value.get(k) for k in ("source_ref", "hot_selection")):
            fail("MEDIA_WORKFLOW_INVALID", "运行来源与实际三稿不一致。")
        seen = set()
        for row in value["captures"]:
            if not isinstance(row, dict) or row.get("capture_id") in seen:
                fail("MEDIA_WORKFLOW_INVALID", "原图条目异常或重复。")
            self._capture_path(folder, row)
            self.media._url(row.get("source_url"), context, row["kind"] in {"COVER", "COVER_BOXED"})
            seen.add(row["capture_id"])
        if value["candidates"]:
            if any(not isinstance(c, dict) for c in value["candidates"]):
                fail("MEDIA_WORKFLOW_INVALID", "候选条目异常。")
            checked = self._review(value, [{k: v for k, v in c.items() if k != "candidate_id"}
                                          for c in value["candidates"]])
            if checked != value["candidates"]:
                fail("MEDIA_WORKFLOW_INVALID", "候选身份与已审坐标不一致。")
        selected = [c["candidate_id"] for c in value["candidates"] if c["decision"] == "SELECT"]
        if (any(not isinstance(r, dict) for r in value["renders"])
                or value["renders"] and [r.get("candidate_id") for r in value["renders"]] != selected):
            fail("MEDIA_WORKFLOW_INVALID", "成品与已审候选不一致。")
        for row in value.get("renders", []):
            path = self._render_path(folder, row)
            if digest(path.read_bytes()) != row.get("sha256"):
                fail("MEDIA_HASH_MISMATCH", "成品图片已变化，不能提交旧目视记录。")
        return value

    def _render_path(self, folder, row):
        # Only worker output under this run can become a publication candidate.
        source = row.get("path")
        if (not isinstance(source, str) or not Path(source).is_absolute()
                or source.startswith(("//", "\\\\")) or ".." in Path(source).parts):
            fail("UNSAFE_PATH", "成品需为本次运行内的本地绝对路径。")
        path = Path(source)
        base = folder / "rendered"
        try:
            path.relative_to(base)
        except ValueError:
            fail("UNSAFE_PATH", "成品图片越出本次运行。")
        if (path.parent != base or not re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg)", path.name)
                or path.stem != row.get("sha256")):
            fail("UNSAFE_PATH", "成品路径与内容身份不符。")
        return _safe_path(path, must_file=True)

    def _public(self, value):
        folder = self._folder(value["run_id"])
        matches = True
        try:
            self.media._round(value["round_id"], current=True)
        except (MediaError, TrendsError):
            matches = False
        requirement = self._requirement(value["round_id"])
        return {"status": "OK", **value, "current_batch_matches": matches,
                "media_requirement": requirement,
                "new_screenshot_allowed": requirement == "DEFAULT",
                "captures_dir": str(folder / "incoming"),
                "captures": [{**c, "path": str(folder / "originals" / c["file"])}
                             for c in value["captures"]], "files": value.get("renders", []),
                "metrics": {"capture_count": len(value["captures"]),
                            "selected_count": sum(c["decision"] == "SELECT" for c in value["candidates"]),
                            "capture_duration_ms": sum(c.get("duration_ms") or 0 for c in value["captures"]),
                            "event_count": len(value["events"])}, "notice": NOTICE}

    @_errors
    @serialized
    def start(self, round_id, refresh=False):
        if not isinstance(refresh, bool):
            fail("INVALID_MEDIA_WORKFLOW", "refresh需明确布尔值。")
        context = self.media._round(round_id, current=True)
        requirement = self._requirement(round_id)
        if requirement == "SKIP_ALL":
            return {"status": "SKIPPED", "round_id": round_id, "media_requirement": requirement,
                    "next_action": "SKIP_MEDIA", "browser_calls_required": 0,
                    "notice": "本人本轮明确不配图；未创建配图运行，既有媒体仍可只读查看。"}
        existing = self.media.get(round_id)
        if existing["found"] and (not refresh or requirement != "DEFAULT"):
            return {**existing, "status": "REUSE_READY", "next_action": "REUSE_BYTES",
                    "media_requirement": requirement, "browser_calls_required": 0, "notice": NOTICE}
        if requirement in {"NO_SCREENSHOT", "REUSE_ONLY"}:
            return {**existing, "status": "NEEDS_EXISTING_MEDIA", "round_id": round_id,
                    "media_requirement": requirement, "next_action": "USE_EXISTING_OR_USER_IMAGES",
                    "browser_calls_required": 0, "notice": "本轮不启动新截图；可提供已有图片，或明确修改本轮媒体要求。"}
        run_id = "mw-" + uuid4().hex
        folder = self._folder(run_id)
        (folder / "originals").mkdir(parents=True, exist_ok=False)
        (folder / "incoming").mkdir()
        value = {"marker": "moments_media_workflow", "schema_version": 1,
                 "run_id": run_id, **context, "created_at": self.now(), "stage": "CAPTURING",
                 "replace": refresh, "base_media_sha256": self._bundle_hash(round_id),
                 "captures": [], "candidates": [], "renders": [], "events": []}
        self._write(value)
        return {**self._public(value), "status": "CAPTURE_REQUIRED", "next_action": "CAPTURE_COVER"}

    @_errors
    def resume(self, run_id):
        return self._public(self._load(run_id))

    @_errors
    @serialized
    def capture(self, run_id, path, kind, source_url, source_type, captured_at,
                content_published_at=None, duration_ms=None):
        value = self._load(run_id, current=True)
        if source_type == "PUBLIC_CAPTURE" and self._requirement(value["round_id"]) == "NO_SCREENSHOT":
            fail("SCREENSHOT_NOT_ALLOWED", "本人本轮明确不用截图，不能通过旧配图运行登记新截图。")
        if value["stage"] == "COMMITTED":
            fail("MEDIA_WORKFLOW_COMPLETE", "已完成运行只读保留；修改配图需显式新建刷新运行。")
        if kind not in KINDS:
            fail("INVALID_MEDIA_WORKFLOW", "原图角色需COVER/COVER_BOXED/PAGE/ASSET。")
        if (not isinstance(path, str) or not Path(path).is_absolute() or path.startswith(("//", "\\\\"))
                or Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg"}):
            fail("UNSAFE_PATH", "原图需为本地绝对PNG/JPEG路径。")
        if source_type not in {"USER_PROVIDED", "PUBLIC_CAPTURE", "PUBLIC_ORIGINAL"}:
            fail("INVALID_MEDIA_WORKFLOW", "原图来源类型无效。")
        if kind in {"COVER", "COVER_BOXED"} and source_type == "PUBLIC_ORIGINAL":
            fail("INVALID_MEDIA_WORKFLOW", "首图必须来自完整网页截图。")
        source_url = self.media._url(source_url, value, kind in {"COVER", "COVER_BOXED"})
        _timestamp(captured_at, False)
        _timestamp(content_published_at, True)
        if source_type != "USER_PROVIDED" and captured_at is None:
            fail("INVALID_MEDIA_WORKFLOW", "公开采集图必须有实际采集时间。")
        if duration_ms is not None:
            number(duration_ms)
        src = _safe_path(path, must_file=True)
        if src.stat().st_size > MAX_BYTES:
            fail("INVALID_MEDIA_IMAGE", "原图超过16MiB。")
        data = src.read_bytes()
        info, suffix = image_info(data)
        sha = digest(data)
        row = {"capture_id": "cap-" + sha[:24], "sha256": sha, "file": sha + suffix,
               **info, "kind": kind, "source_url": source_url, "source_type": source_type,
               "captured_at": captured_at, "content_published_at": content_published_at,
               "duration_ms": duration_ms}
        old = next((c for c in value["captures"] if c["capture_id"] == row["capture_id"]), None)
        if old is not None:
            if {k:v for k,v in old.items() if k != "duration_ms"} != {k:v for k,v in row.items() if k != "duration_ms"}:
                fail("MEDIA_CAPTURE_CONFLICT", "同一原图已有不同来源或角色，不静默改写证据。")
            return {**self._public(value), "capture": old, "reused": True}
        if len(value["captures"]) >= 8:
            fail("MEDIA_WORKFLOW_LIMIT", "每次运行最多8份原图，先筛选已有候选。")
        target = _safe_path(self._folder(run_id) / "originals" / row["file"])
        if target.exists():
            if target.read_bytes() != data:
                fail("MEDIA_HASH_MISMATCH", "内容寻址原图已损坏。")
        else:
            # Publish complete bytes only. A terminated write may leave a .tmp,
            # but never a partial content-addressed original that blocks retry.
            fd, name = tempfile.mkstemp(prefix=".capture-", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    if os.name == "nt":
                        os.rename(temporary, target)  # Windows refuses an existing target.
                    else:
                        os.link(temporary, target)
                except FileExistsError:
                    if _safe_path(target, must_file=True).read_bytes() != data:
                        fail("MEDIA_HASH_MISMATCH", "内容寻址原图已变化。")
            finally:
                temporary.unlink(missing_ok=True)
        value["captures"].append(row)
        self._write(value)
        return {**self._public(value), "capture": row, "reused": False}

    def _review(self, value, candidates):
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
            fail("INVALID_MEDIA_WORKFLOW", "每轮需1至12项有理由的候选评价。")
        known = {c["capture_id"]: c for c in value["captures"]}
        result, selected, seen = [], [], set()
        fields = {"capture_id", "role", "crop", "title_box", "decision", "reason", "author"}
        for c in candidates:
            if not isinstance(c, dict) or set(c) != fields or c.get("capture_id") not in known:
                fail("INVALID_MEDIA_WORKFLOW", "候选字段不完整或引用未知原图。")
            cap = known[c["capture_id"]]
            if c["role"] not in {"COVER", "SUPPLEMENT"} or c["decision"] not in {"SELECT", "REJECT"}:
                fail("INVALID_MEDIA_WORKFLOW", "候选角色或编辑决定无效。")
            reason = bounded(c["reason"], "读者看点/排除理由", 230)
            self.privacy_check(reason)
            author = None if c["author"] is None else bounded(c["author"], "作者", 80)
            if author is not None:
                self.privacy_check(author)
            if len(reason + ("；作者：" + author if author else "")) > 300:
                fail("INVALID_MEDIA_WORKFLOW", "看点与作者合计需在300字以内。")
            if c["role"] == "COVER":
                if c["crop"] is not None or cap["kind"] not in {"COVER", "COVER_BOXED"}:
                    fail("INVALID_MEDIA_WORKFLOW", "首图必须完整，不允许裁剪页面。")
                if cap["kind"] == "COVER_BOXED":
                    if c["title_box"] is not None:
                        fail("INVALID_MEDIA_WORKFLOW", "合格红框首图应按原字节复用，不再标注。")
                elif c["decision"] == "SELECT" or c["title_box"] is not None:
                    rectangle(c["title_box"], cap)
            else:
                if c["title_box"] is not None:
                    fail("INVALID_MEDIA_WORKFLOW", "补图不能冒充原标题首图。")
                if c["crop"] is not None:
                    rectangle(c["crop"], cap)
                    if min(c["crop"][2]-c["crop"][0], c["crop"][3]-c["crop"][1]) < 160:
                        fail("INVALID_MEDIA_IMAGE", "补图裁取后过小，需保留可阅读上下文。")
            ident = identity({k:c[k] for k in ("capture_id", "role", "crop", "title_box")})[:24]
            if ident in seen:
                fail("INVALID_MEDIA_WORKFLOW", "同一候选区域不能重复占位。")
            seen.add(ident)
            row = {**c, "reason": reason, "author": author, "candidate_id": "cand-" + ident}
            result.append(row)
            if c["decision"] == "SELECT":
                selected.append(row)
        if (not 1 <= len(selected) <= 4 or selected[0]["role"] != "COVER"
                or any(c["role"] != "SUPPLEMENT" for c in selected[1:])):
            fail("INVALID_MEDIA_WORKFLOW", "选中项需依顺序为1张首图及0至3张合格补图。")
        return result

    @_errors
    @serialized
    def review(self, run_id, candidates):
        value = self._load(run_id, current=True)
        if value["stage"] == "COMMITTED":
            fail("MEDIA_WORKFLOW_COMPLETE", "已完成运行不能改写候选。")
        reviewed = self._review(value, candidates)
        if reviewed == value["candidates"]:
            return {**self._public(value), "reused": True}
        value.update(candidates=reviewed, renders=[], stage="REVIEWED")
        self._write(value)
        return self._public(value)

    def _worker(self, value, image_python, action):
        if (not isinstance(image_python, str) or not Path(image_python).is_absolute()
                or image_python.startswith(("//", "\\\\"))
                or not _safe_path(image_python, must_file=True).is_file()):
            fail("IMAGE_RUNTIME_UNAVAILABLE", "需使用load_workspace_dependencies提供的本地Pillow解释器；不自动安装。")
        folder = self._folder(value["run_id"])
        output = _safe_path(folder / ("previews" if action == "preview" else "rendered"))
        output.mkdir(exist_ok=True)
        by_id = {c["capture_id"]: c for c in value["captures"]}
        captures = [{"capture_id": c["capture_id"], "path": str(self._capture_path(folder, c))}
                    for c in value["captures"]] if action == "preview" else []
        selected = [{"candidate_id": c["candidate_id"], "role": c["role"],
                     "crop": c["crop"], "title_box": c["title_box"],
                     "path": str(self._capture_path(folder, by_id[c["capture_id"]])),
                     "boxed": c["role"] == "COVER" and by_id[c["capture_id"]]["kind"] == "COVER_BOXED"}
                    for c in value["candidates"] if c["decision"] == "SELECT"]
        if action == "preview":
            selected = []
        job = {"action": action, "output_dir": str(output), "captures": captures, "candidates": selected}
        request = folder / (action + "-request.json")
        if request.exists():
            self.runner._backup(request)
        self.atomic(request, json.dumps(job, ensure_ascii=False))
        try:
            process = subprocess.run([image_python, "-B", str(Path(__file__).with_name("media_image_ops.py")),
                                      "--request-file", str(request)], capture_output=True, text=True,
                                     encoding="utf-8", timeout=60)
        except subprocess.TimeoutExpired:
            fail("IMAGE_RUNTIME_TIMEOUT", "本地处理超时，原图和选图工件已保留，可恢复。")
        if len(process.stdout) > MAX_JSON:
            fail("IMAGE_RUNTIME_FAILED", "本地图像处理返回过大，未提交配图。")
        try:
            result = json.loads(process.stdout)
        except ValueError:
            fail("IMAGE_RUNTIME_UNAVAILABLE", "本地图像运行时不可用或缺少Pillow；原图已保留，不自动安装。")
        if not isinstance(result, dict):
            fail("IMAGE_RUNTIME_FAILED", "本地处理结果格式异常，未提交配图。")
        if process.returncode != 0 or "error" in result:
            error = result.get("error", {})
            code = error.get("code", "IMAGE_RUNTIME_FAILED") if isinstance(error, dict) else "IMAGE_RUNTIME_FAILED"
            fail(code, "本地图像处理未成功；原图已保留，未提交配图。")
        return result

    @_errors
    @serialized
    def preview(self, run_id, image_python):
        value = self._load(run_id)
        if self._requirement(value["round_id"]) in {"SKIP_ALL", "REUSE_ONLY"}:
            fail("MEDIA_NOT_ALLOWED", "本轮明确跳过新配图，仅可只读查看既有材料。")
        if not value["captures"]:
            fail("MEDIA_CAPTURE_REQUIRED", "先保存实际原图，再生成候选预览。")
        return {"status": "OK", "run_id": run_id, **self._worker(value, image_python, "preview")}

    @_errors
    @serialized
    def render(self, run_id, image_python):
        value = self._load(run_id, current=True)
        if not value["candidates"]:
            fail("MEDIA_REVIEW_REQUIRED", "先说明读者看点并选择候选，不能自动凑图。")
        if value["renders"]:
            return {**self._public(value), "status": "NEEDS_VISUAL_REVIEW", "reused": True}
        result = self._worker(value, image_python, "render")
        rows = result.get("files")
        expected = [c["candidate_id"] for c in value["candidates"] if c["decision"] == "SELECT"]
        if not isinstance(rows, list) or [c.get("candidate_id") for c in rows] != expected:
            fail("IMAGE_RUNTIME_FAILED", "成品与所选候选不一致。")
        for row in rows:
            path = self._render_path(self._folder(run_id), row)
            data = path.read_bytes()
            image_info(data)
            if digest(data) != row.get("sha256"):
                fail("MEDIA_HASH_MISMATCH", "成品校验失败，未保存目视待审记录。")
        value.update(renders=rows, stage="RENDERED", render_duration_ms=result.get("elapsed_ms"))
        self._write(value)
        return {**self._public(value), "status": "NEEDS_VISUAL_REVIEW"}

    @_errors
    @serialized
    def commit(self, run_id, approved_sha256):
        value = self._load(run_id, current=True)
        if not value["renders"] or value["stage"] not in {"RENDERED", "COMMITTED"}:
            fail("MEDIA_VISUAL_REVIEW_REQUIRED", "先生成并逐张目视成品，再提交其校验值。")
        actual = [r["sha256"] for r in value["renders"]]
        if not isinstance(approved_sha256, list) or approved_sha256 != actual:
            fail("MEDIA_VISUAL_REVIEW_REQUIRED", "目视确认的成品校验值与当前图片顺序不一致。")
        current_hash = self._bundle_hash(value["round_id"])
        if value["stage"] == "COMMITTED":
            if current_hash != value.get("committed_media_sha256"):
                fail("MEDIA_WORKFLOW_CONFLICT", "配图包后来已变化，旧运行不能覆盖。")
            return {**self.media.get(value["round_id"]), "run_id": run_id, "reused": True}
        caps = {c["capture_id"]: c for c in value["captures"]}
        chosen = [c for c in value["candidates"] if c["decision"] == "SELECT"]
        items = []
        for candidate, output in zip(chosen, value["renders"]):
            cap = caps[candidate["capture_id"]]
            caption = candidate["reason"] + ("；作者：" + candidate["author"] if candidate["author"] else "")
            items.append({"path": output["path"], "role": candidate["role"],
                          "source_url": cap["source_url"], "source_type": cap["source_type"],
                          "caption": caption, "captured_at": cap["captured_at"],
                          "content_published_at": cap["content_published_at"],
                          "title_boxed": candidate["role"] == "COVER"})
        expected = [row for row, _ in self.media._items(items, value)]
        intent = identity(expected)
        if current_hash != value["base_media_sha256"]:
            # Recover the narrow crash window after media.save succeeded but
            # before the workflow completion record became durable. Never
            # replace later, different work under the guise of resuming.
            result = self.media.get(value["round_id"])
            if (value.get("commit_intent_items_sha256") != intent or not result["found"]
                    or result["media"]["items"] != expected):
                fail("MEDIA_WORKFLOW_CONFLICT", "准备过程中配图包已变化，未覆盖后来的成果。")
        else:
            value["commit_intent_items_sha256"] = intent
            self._write(value)
            result = self.media.save(value["round_id"], items, replace=value["replace"])
        value.update(stage="COMMITTED", committed_media_sha256=self._bundle_hash(value["round_id"]),
                     approved_sha256=actual, committed_at=self.now())
        self._write(value)
        return {**result, "run_id": run_id}

    @_errors
    @serialized
    def event(self, run_id, phase, channel, outcome, duration_ms=0,
              started_at=None, finished_at=None):
        value = self._load(run_id)
        if (phase not in {"OPEN", "DOM", "CAPTURE"} or channel not in {"IAB", "CHROME"}
                or outcome not in {"OK", "TIMEOUT", "CONNECTION_FAILED", "LOGIN_REQUIRED",
                                   "SAFETY_STOP", "PERMISSION_REQUIRED"}):
            fail("INVALID_MEDIA_WORKFLOW", "只记录已观察到的阶段、通道及结果码。")
        number(duration_ms)
        timing = None
        if started_at is not None or finished_at is not None:
            try:
                start, finish = (datetime.fromisoformat(x.replace("Z", "+00:00"))
                                 for x in (started_at, finished_at))
                if start.tzinfo is None or finish.tzinfo is None or finish < start:
                    raise ValueError("invalid event time")
            except (AttributeError, TypeError, ValueError):
                fail("INVALID_MEDIA_WORKFLOW", "动作起止时间需成对提供带时区ISO时间，结束不能早于开始。")
            timing = {"started_at": started_at, "finished_at": finished_at,
                      "recorded_at": self.now(), "wall_duration_ms": round((finish-start).total_seconds()*1000)}
        if len(value["events"]) >= 40:
            fail("MEDIA_WORKFLOW_LIMIT", "错误记录已达上限，不能继续无界重试。")
        event = {"phase": phase, "channel": channel, "outcome": outcome,
                 "duration_ms": duration_ms, "observed_at": self.now()}
        if timing:
            event.update(timing)
            event["observed_at"] = finished_at
        value["events"].append(event)
        if outcome in {"SAFETY_STOP", "LOGIN_REQUIRED", "PERMISSION_REQUIRED"}:
            action = "STOP_FOR_USER"
        elif outcome == "OK":
            action = "CONTINUE"
        elif phase == "DOM":
            action = "DIRECT_CAPTURE"
        else:
            failures = 0
            for previous in reversed(value["events"]):
                if previous["channel"] != channel or previous["phase"] != phase:
                    continue
                if previous["outcome"] == "OK":
                    break
                failures += 1
            action = "RECONNECT_ONCE" if failures == 1 else "SWITCH_CHANNEL"
        self._write(value)
        return {"status": "OK", "run_id": run_id, "event": event, "next_action": action,
                "timing_basis": "ACTION_INTERVAL" if timing else "REPORTED_DURATION_ONLY",
                "delivery_instruction": "正文已经可读时先展示正文；本动作与补图不得阻塞三稿交付。",
                "timeout_hint_ms": 60000,
                "notice": "仅给出恢复建议，不自动操作浏览器；切换须使用已授权通道，安全停止不能绕过。"}
