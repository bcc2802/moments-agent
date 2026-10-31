"""Private, resumable preparation between a public menu and authoritative drafts.

The sole state file stores a pointer and a linked round ID, never source bodies,
personal views, draft text, or publication status. All inputs are caller-reviewed;
this module neither browses nor supplies a person's opinion.
"""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import stat
from uuid import uuid4

from moments_storage import serialized

IDENTITY = re.compile(r"prep-[0-9a-f]{32}")
HASH = re.compile(r"[0-9a-f]{64}")
MEDIA_REQUIREMENTS = {"DEFAULT", "NO_SCREENSHOT", "SKIP_ALL", "REUSE_ONLY"}
MAX_BYTES = 32768
SOURCE_FIELDS = {"kind", "url", "platform", "title", "content_published_at", "event_at", "claims", "risk_level"}


class PreparationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message="本轮准备记录无法安全恢复；未改稿件或发布状态。"):
    raise PreparationError(code, message)


def _errors(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except PreparationError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            fail("PREPARATION_INVALID")
        except Exception as exc:
            if isinstance(getattr(exc, "code", None), str):
                fail(exc.code, str(exc))
            raise
    return checked


def validate_pointer(pointer):
    """Cheap shape check for the shared state reader; raises ValueError only."""
    if (not isinstance(pointer, dict) or set(pointer) != {"preparation_id", "path", "sha256", "round_id"}
            or not isinstance(pointer.get("preparation_id"), str) or not IDENTITY.fullmatch(pointer["preparation_id"])
            or not isinstance(pointer.get("path"), str)
            or not isinstance(pointer.get("sha256"), str) or not HASH.fullmatch(pointer["sha256"])
            or pointer.get("round_id") is not None and
               (not isinstance(pointer["round_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pointer["round_id"]))):
        raise ValueError("preparation pointer")


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class Preparation:
    def __init__(self, runner):
        self.runner = runner
        self.root = runner.archive / "_workflow"

    def _path(self, preparation_id):
        if not isinstance(preparation_id, str) or not IDENTITY.fullmatch(preparation_id):
            fail("INVALID_PREPARATION_ID", "需明确本轮准备ID，不猜测最新记录。")
        path = self.root / (preparation_id + ".json")
        self._safe(path)
        return path

    @staticmethod
    def _safe(path, directory=False):
        from moments_retention import safe_path
        safe_path(path)
        if path.exists():
            info = path.lstat()
            if directory and stat.S_ISDIR(info.st_mode):
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                fail("UNSAFE_PATH", "准备记录包含非普通文件或硬链接，未执行操作。")

    def _pointer(self, value, data, round_id=None):
        return {"preparation_id": value["preparation_id"],
                "path": self._path(value["preparation_id"]).relative_to(self.runner.vault).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(), "round_id": round_id}

    def _source_review(self, value, selection):
        from moments import external_context
        from moments_trends import validate_hot_url
        if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
            fail("INVALID_SOURCE_REVIEW", "核验记录需完整来源字段和claims，不包含本人观点或额外字段。")

        def url_validator(url):
            checked = validate_hot_url(selection["platform"], url, allow_historical=True)
            if checked != selection["url"]:
                fail("PREPARATION_SOURCE_MISMATCH", "核验URL与选中热点不同，未改准备记录。")
            return checked

        # The temporary mode validates source structure only. It is removed and
        # never taken as the user's permission for a no-stance draft.
        checked = external_context({**value, "personal_view": {"mode": "NO_STANCE_CONFIRMED", "text": None}}, url_validator)
        return {key: checked[key] for key in SOURCE_FIELDS}

    @staticmethod
    def _view(value):
        from moments import privacy_check
        if (not isinstance(value, dict) or set(value) != {"mode", "text"}
                or value["mode"] not in {"CONFIRMED", "NO_STANCE_CONFIRMED"}):
            fail("INVALID_PERSONAL_VIEW", "需本人真实表达或明确客观讨论模式。")
        if value["mode"] == "CONFIRMED":
            if not isinstance(value["text"], str) or not value["text"].strip() or len(value["text"]) > 1000:
                fail("INVALID_PERSONAL_VIEW", "本人观点需为有界非空文字。")
            privacy_check(value["text"])
        elif value["text"] is not None:
            fail("INVALID_PERSONAL_VIEW", "客观讨论模式的text必须为空。")
        return deepcopy(value)

    def _load(self, preparation_id=None):
        state = self.runner._state()
        pointer = state.get("preparation")
        if pointer is not None:
            validate_pointer(pointer)
            expected_path = self._path(pointer["preparation_id"]).relative_to(self.runner.vault).as_posix()
            if pointer["path"] != expected_path:
                fail("INVALID_PREPARATION_POINTER", "准备指针越界或身份不符，未猜测路径。")
        if preparation_id is None:
            if pointer is None:
                fail("NO_PREPARATION", "尚无本轮准备记录；先明确选择热点。")
            preparation_id = pointer["preparation_id"]
        path = self._path(preparation_id)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            fail("PREPARATION_MISSING", "准备记录缺失或超过读取上限。")
        data = path.read_bytes()
        current = pointer is not None and pointer["preparation_id"] == preparation_id
        if current and hashlib.sha256(data).hexdigest() != pointer["sha256"]:
            fail("PREPARATION_HASH_MISMATCH", "准备记录字节与指针不一致，未采用变化内容。")
        value = json.loads(data.decode("utf-8-sig"))
        required = {"marker", "schema_version", "preparation_id", "selection", "base_round_id", "source_review",
                    "personal_view", "media_requirement", "created_at", "updated_at"}
        if (not isinstance(value, dict) or set(value) != required or value["marker"] != "moments_preparation"
                or value["schema_version"] != 1 or value["preparation_id"] != preparation_id
                or value["media_requirement"] not in MEDIA_REQUIREMENTS):
            fail("PREPARATION_INVALID")
        selected = value["selection"]
        if (not isinstance(selected, dict) or set(selected) != {"n", "batch_id", "candidate_id", "url", "platform", "title", "fetched_at"}
                or type(selected.get("n")) is not int or not 1 <= selected["n"] <= 10
                or not isinstance(selected.get("batch_id"), str) or not re.fullmatch(r"tr-[0-9a-f]{32}", selected["batch_id"])
                or not isinstance(selected.get("candidate_id"), str) or not re.fullmatch(r"[0-9a-f]{24}", selected["candidate_id"])):
            fail("PREPARATION_INVALID")
        from moments_trends import validate_hot_url
        if validate_hot_url(selected["platform"], selected["url"], allow_historical=True) != selected["url"]:
            fail("PREPARATION_INVALID")
        if value["source_review"] is not None and self._source_review(value["source_review"], selected) != value["source_review"]:
            fail("PREPARATION_INVALID")
        if value["personal_view"] is not None:
            self._view(value["personal_view"])
        from moments import now
        from datetime import datetime
        for field in ("created_at", "updated_at"):
            if not isinstance(value[field], str) or datetime.fromisoformat(value[field]).tzinfo is None:
                fail("PREPARATION_INVALID")
        if value["base_round_id"] is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(value["base_round_id"])):
            fail("PREPARATION_INVALID")
        return state, pointer, value, data, current

    def _context(self, state, pointer, value, current):
        selected = value["selection"]
        batch_matches = (state.get("hot_topics") or {}).get("batch_id") == selected["batch_id"]
        if batch_matches:
            from moments_trends import Trends
            actual = Trends(self.runner).select(selected["n"], selected["batch_id"])
            if any(actual[key] != selected[key] for key in ("batch_id", "candidate_id", "url", "platform", "title", "fetched_at")):
                fail("PREPARATION_SOURCE_MISMATCH", "当前菜单与准备中的候选身份不一致。")
        active = state.get("active_round")
        active_id = active["round_id"] if isinstance(active, dict) else None
        linked = pointer.get("round_id") if current else None
        round_matches = active_id == (linked if linked is not None else value["base_round_id"])
        if linked is not None:
            if not active or active_id != linked:
                round_matches = False
            else:
                notes = self.runner._active_notes(active)
                if any(n["meta"].get("preparation_id") != value["preparation_id"]
                       or n["meta"].get("media_requirement") != value["media_requirement"] for n in notes):
                    fail("PREPARATION_ROUND_MISMATCH", "准备记录关联轮次与真实Markdown不符。")
        can_continue = current and batch_matches and round_matches
        stage = ("DRAFTED" if linked is not None else "READY" if value["source_review"] is not None and value["personal_view"] is not None
                 else "REVIEWED" if value["source_review"] is not None else "SELECTED")
        next_action = ("VIEW_ONLY" if not can_continue else "DISPLAY_DRAFTS" if linked is not None
                       else "REVIEW_SOURCE" if value["source_review"] is None else "ASK_PERSONAL_VIEW" if value["personal_view"] is None
                       else "CREATE_DRAFTS")
        return {"status": "OK", **deepcopy(value), "stage": stage, "round_id": linked,
                "current_preparation_matches": current, "current_batch_matches": batch_matches,
                "current_round_matches": round_matches, "can_continue": can_continue,
                "next_action": next_action, "notice": "私有写作准备，不是发布状态；事实与本人表达由调用方实际核验。"}

    def _commit(self, value, state, *, round_id=None, note_changes=None, expected=None, expected_state=None):
        from moments import atomic, json_text
        from moments_retention import atomic_bytes
        target = self._path(value["preparation_id"])
        if target.exists() and (state.get("preparation") or {}).get("preparation_id") != value["preparation_id"]:
            fail("PREPARATION_EXISTS", "该准备ID已有记录，未覆盖历史文件。")
        data = _encode(value)
        if len(data) > MAX_BYTES:
            fail("PREPARATION_LIMIT", "本轮准备材料超过上限，未保存。")
        state = deepcopy(state)
        state["preparation"] = self._pointer(value, data, round_id)
        changes = {target: data, self.runner.state_path: json_text(state).encode("utf-8"), **(note_changes or {})}
        before = {}
        for path in changes:
            self._safe(path)
            if path != self.runner.state_path and path != target and path.parent != self.runner.archive:
                fail("UNSAFE_PATH")
            before[path] = path.read_bytes() if path.exists() else None
            if expected is not None and path in expected and before[path] != expected[path]:
                fail("WRITE_CONFLICT", "稿件或入口在计划后变化，未覆盖。")
        observed_state = expected_state if expected_state is not None else getattr(self.runner, "_observed_state_bytes", None)
        if before[self.runner.state_path] != observed_state:
            fail("WRITE_CONFLICT", "状态在准备计划后变化，未覆盖。")
        changes = {path: content for path, content in changes.items() if before[path] != content}
        for path in changes:
            if before[path] is not None:
                self.runner._backup(path)
        for path in changes:
            if (path.read_bytes() if path.exists() else None) != before[path]:
                fail("WRITE_CONFLICT", "准备记录在备份期间变化，未覆盖。")
        self._safe(self.root, directory=True)
        self.root.mkdir(parents=True, exist_ok=True)
        touched = []
        try:
            for path, content in changes.items():
                self._safe(path)
                if (path.read_bytes() if path.exists() else None) != before[path]:
                    fail("WRITE_CONFLICT", "准备记录在写入前变化，已停止。")
                atomic(path, content.decode("utf-8"))
                touched.append(path)
        except BaseException:
            conflicts = []
            for path in reversed(touched):
                try:
                    self._safe(path)
                    if not path.exists() or path.read_bytes() != changes[path]:
                        conflicts.append(str(path))
                        continue
                    if before[path] is None:
                        path.unlink()
                    else:
                        atomic_bytes(path, before[path])
                except Exception:
                    conflicts.append(str(path))
            if conflicts:
                fail("ROLLBACK_INCOMPLETE", "部分准备文件发生外部变化或无法回滚；其他文件已回滚，原字节备份已保留。")
            raise

    @_errors
    @serialized
    def start(self, n, batch_id, candidate_id, media_requirement=None):
        from moments import now
        from moments_trends import Trends
        if media_requirement is not None and media_requirement not in MEDIA_REQUIREMENTS:
            fail("INVALID_MEDIA_REQUIREMENT", "媒体要求需DEFAULT/NO_SCREENSHOT/SKIP_ALL/REUSE_ONLY。")
        selected = Trends(self.runner).select(n, batch_id)
        if selected["candidate_id"] != candidate_id:
            fail("PREPARATION_SOURCE_MISMATCH", "热点候选ID与本轮菜单不符。")
        state = self.runner._state()
        if state.get("preparation") is not None:
            old_state, pointer, old, data, current = self._load()
            if old["selection"]["batch_id"] == batch_id and old["selection"]["candidate_id"] == candidate_id:
                restored = self._context(old_state, pointer, old, current)
                if not restored["can_continue"]:
                    fail("STALE_PREPARATION", "该准备记录已经过期，仅可查看；不能覆盖后来轮次。")
                if media_requirement is not None and media_requirement != old["media_requirement"]:
                    return self.update(old["preparation_id"], media_requirement=media_requirement)
                return {**restored, "reused": True}
        value = {"marker": "moments_preparation", "schema_version": 1, "preparation_id": "prep-" + uuid4().hex,
                 "selection": {"n": n, **{key: selected[key] for key in ("batch_id", "candidate_id", "url", "platform", "title", "fetched_at")}},
                 "base_round_id": state["active_round"]["round_id"] if state.get("active_round") else None,
                 "source_review": None, "personal_view": None, "media_requirement": media_requirement or "DEFAULT",
                 "created_at": now(), "updated_at": now()}
        self._commit(value, state)
        return {**self.resume(value["preparation_id"]), "reused": False}

    @_errors
    @serialized
    def update(self, preparation_id, source_review=None, personal_view=None, media_requirement=None):
        from moments import now
        from moments_retention import metadata_bytes
        state, pointer, value, before, current = self._load(preparation_id)
        expected_state = self.runner._observed_state_bytes
        restored = self._context(state, pointer, value, current)
        if not restored["can_continue"]:
            fail("STALE_PREPARATION", "该准备记录不是当前有效流程，仅可查看。")
        linked = pointer["round_id"]
        if linked is not None and (source_review is not None or personal_view is not None):
            fail("PREPARATION_ALREADY_DRAFTED", "稿件已创建，事实或本人观点变更应走明确改稿流程。")
        updated = deepcopy(value)
        if source_review is not None:
            updated["source_review"] = self._source_review(source_review, value["selection"])
        if personal_view is not None:
            updated["personal_view"] = self._view(personal_view)
        if media_requirement is not None:
            if media_requirement not in MEDIA_REQUIREMENTS:
                fail("INVALID_MEDIA_REQUIREMENT", "媒体要求需DEFAULT/NO_SCREENSHOT/SKIP_ALL/REUSE_ONLY。")
            updated["media_requirement"] = media_requirement
        changed = updated != value
        if changed:
            updated["updated_at"] = now()
            note_changes = {}
            expected = {self._path(preparation_id): before}
            if linked is not None and updated["media_requirement"] != value["media_requirement"]:
                from moments_retention import Retention
                from moments_catalog import catalog_changes
                notes = self.runner._active_notes(state["active_round"])
                for note in notes:
                    note_changes[note["path"]] = metadata_bytes(note["raw"], "media_requirement", updated["media_requirement"])
                    expected[note["path"]] = note["raw"]
                    note["meta"]["media_requirement"] = updated["media_requirement"]
                retention = Retention(self.runner)
                note_changes.update(catalog_changes(retention, notes, []))
                expected.update(retention.expected)
            self._commit(updated, state, round_id=linked, note_changes=note_changes,
                         expected=expected, expected_state=expected_state)
        return {**self.resume(preparation_id), "changed": changed}

    @_errors
    def resume(self, preparation_id=None):
        state, pointer, value, data, current = self._load(preparation_id)
        return self._context(state, pointer, value, current)

    @_errors
    def for_create(self, preparation_id):
        state, pointer, value, data, current = self._load(preparation_id)
        state_data = self.runner._observed_state_bytes
        restored = self._context(state, pointer, value, current)
        if not restored["can_continue"]:
            fail("STALE_PREPARATION", "旧准备不能用于生成或覆盖当前轮次。")
        if value["source_review"] is None:
            fail("NEEDS_VERIFICATION", "先实际核验选中来源，不以标题成稿。")
        if value["personal_view"] is None:
            fail("NEEDS_USER_CONFIRMATION", "先记录本人真实表达或明确客观讨论模式。")
        return {"source_ref": value["selection"]["url"],
                "hot_selection": {key: value["selection"][key] for key in ("n", "batch_id", "candidate_id")},
                "external_source": {**deepcopy(value["source_review"]), "personal_view": deepcopy(value["personal_view"])},
                "media_requirement": value["media_requirement"],
                "round_id": restored["round_id"], "pointer": deepcopy(pointer),
                "path": self._path(value["preparation_id"]), "data": data, "state_data": state_data}
