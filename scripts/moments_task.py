"""Topic-scoped final text and exact-round actions without switching active state.

task.md is only an identity/selection pointer. final.md is the sole task final
body and publication-state source. Unknown selection never becomes variant 1.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from moments_storage import serialized
from moments_retention import Retention, atomic_bytes, metadata_bytes, safe_path, valid_round


def _fail(code, message):
    from moments import fail
    fail(code, message)


def _identity(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        _fail("INVALID_TASK", label + "格式无效。")
    return value


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _encode(meta, title, body):
    # Keep the user's body byte-for-byte, including leading/trailing newlines.
    head = "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in meta.items())
    return (head + "\n---\n\n# " + title + "\n\n" + body).encode("utf-8")


def _decode(raw, kind):
    try:
        text = raw.decode("utf-8")
        if not text.startswith("---\n"):
            raise ValueError()
        head, content = text[4:].split("\n---\n", 1)
        meta = {}
        for line in head.splitlines():
            key, value = line.split(":", 1)
            if key in meta:
                raise ValueError()
            meta[key] = json.loads(value.strip())
        if meta.get("moments_agent") != "moments-draft" or meta.get("record_type") != kind or meta.get("skill_version") != "0.2.0":
            raise ValueError()
        if not content.startswith("\n# "):
            raise ValueError()
        title, body = content[3:].split("\n\n", 1)
        return meta, title, body
    except (ValueError, UnicodeError, IndexError):
        _fail("TASK_FINAL_INVALID", "任务Markdown格式或归属异常，未猜测并覆盖。")


class Tasks:
    def __init__(self, runner):
        self.runner = runner
        self.root = runner.archive / "_tasks"

    def _path(self, task_id, name):
        task_id = _identity(task_id, "任务ID")
        path = self.root / task_id / name
        safe_path(path)
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 65536):
            _fail("UNSAFE_PATH", "任务文件类型、链接或大小异常。")
        return path

    def _read(self, task_id, name, kind):
        path = self._path(task_id, name)
        if not path.exists():
            return None
        raw = path.read_bytes()
        meta, title, body = _decode(raw, kind)
        if meta.get("task_id") != task_id:
            _fail("TASK_FINAL_INVALID", "任务ID与本地路径不一致。")
        _identity(meta.get("round_id"), "轮号")
        if name == "final.md":
            tags = meta.get("tags")
            if (not isinstance(tags, list) or not 1 <= len(tags) <= 5 or tags[0] not in ("待发布", "已发布")
                    or not all(isinstance(x, str) and x for x in tags) or not body.strip()
                    or meta.get("body_sha256") != _sha(body.encode("utf-8"))
                    or meta.get("selected_variant") not in (None, 1, 2, 3)
                    or type(meta.get("selected_variant")) is bool or "variant" in meta):
                _fail("TASK_FINAL_INVALID", "任务终稿正文、状态或来源校验不一致。")
        elif meta.get("selected_variant") not in (None, 1, 2, 3) or type(meta.get("selected_variant")) is bool:
            _fail("TASK_FINAL_INVALID", "任务选稿指针异常。")
        return {"path": path, "meta": meta, "title": title, "body": body, "raw": raw}

    def read_final(self, task_id):
        return self._read(task_id, "final.md", "moments_task_final")

    def iter_finals(self):
        safe_path(self.root)
        if not self.root.exists():
            return []
        directories = sorted(self.root.iterdir())
        if len(directories) > 10000:
            _fail("LIMIT_EXCEEDED", "任务目录超过有界读取上限。")
        result = []
        for directory in directories:
            safe_path(directory)
            if directory.is_dir() and (directory / "final.md").exists():
                note = self.read_final(directory.name)
                if note is not None:
                    result.append(note)
        return result

    def _public_final(self, note):
        meta, path = note["meta"], note["path"]
        return {"content_id": meta["task_id"] + ":final", "record_type": "moments_task_final",
                "task_id": meta["task_id"], "round_id": meta["round_id"], "variant": None,
                "selected_variant": meta.get("selected_variant"), "title": note["title"], "angle": None,
                "revised_from_selected": meta.get("revised_from_selected"),
                "selected_original_body_sha256": meta.get("selected_original_body_sha256"),
                "body": note["body"], "tags": meta["tags"], "source_ref": meta.get("source_ref"),
                "path": str(path), "relative_path": path.relative_to(self.runner.vault).as_posix(),
                "sha256": _sha(note["raw"]), "body_sha256": meta["body_sha256"],
                "confirmed_at": meta.get("confirmed_at"), "published_at": meta.get("published_at"),
                "created_at": meta.get("created_at"), "publication_text_basis": meta.get("publication_text_basis")}

    def _notes(self, round_id):
        round_id = _identity(round_id, "轮号")
        notes = []
        for number in (1, 2, 3):
            path = self.runner.archive / f"{round_id}-v{number}.md"
            safe_path(path)
            if path.exists():
                note = self.runner._read_note(path)
                if note["meta"]["round_id"] != round_id or note["meta"]["variant"] != number:
                    _fail("TASK_ROUND_MISMATCH", "任务稿件轮号或编号不一致。")
                notes.append(note)
        if notes and not valid_round(notes):
            _fail("STATE_INVALID", "任务轮次缺稿且无明确清理证据。")
        return notes

    def _binding(self, task_id, round_id=None):
        binding = self._read(task_id, "task.md", "moments_task_binding")
        if binding is not None and round_id is not None and binding["meta"]["round_id"] != round_id:
            _fail("TASK_ROUND_MISMATCH", "话题任务已绑定另一轮，未覆盖。")
        return binding

    @staticmethod
    def _working_metadata(binding):
        """Working mode stores provenance only, never draft text or publish state."""
        if binding is None or binding["meta"].get("working_only") is not True:
            return None
        meta = binding["meta"]
        tags, previous = meta.get("topic_tags"), meta.get("working_previous_hashes")
        retired = meta.get("working_retired_variants")
        if (not isinstance(meta.get("source_ref"), str) or not meta["source_ref"] or len(meta["source_ref"]) > 2048
                or not isinstance(tags, list) or not 1 <= len(tags) <= 4
                or any(not isinstance(t, str) or not t or t in {"待发布", "已发布"} for t in tags)
                or not isinstance(previous, dict) or not previous
                or any(k not in {"1", "2", "3"} or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in previous.items())
                or not isinstance(retired, list) or len(retired) != len(set(retired))
                or any(type(n) is not int or n not in (1, 2, 3) or str(n) not in previous for n in retired)):
            _fail("INVALID_TASK", "工作稿绑定的来源或清理证据异常，未猜测正文。")
        return meta

    def _working_context(self, binding, notes):
        meta = self._working_metadata(binding)
        return {"status": "OK", "task_id": meta["task_id"], "round_id": meta["round_id"],
                "working_only": True, "selected_variant": None, "notes": [], "final": None,
                "source_ref": meta["source_ref"], "topic_tags": list(meta["topic_tags"]),
                "preparation_id": meta.get("preparation_id"), "hot_selection": meta.get("hot_selection"),
                "retired_variants": list(meta["working_retired_variants"]),
                "preserved_variants": [n["meta"]["variant"] for n in notes],
                "notice": "候选只在本机私有工作区续聊；本人确认正文后才写唯一终稿，不表示已发布。"}

    @serialized
    def working_start(self, task_id, round_id, expected_hashes, source_message_id):
        """Move only this task's temporary archived candidates back to working mode."""
        from moments import now, json_text
        task_id, round_id = _identity(task_id, "任务ID"), _identity(round_id, "轮号")
        if (not isinstance(source_message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,200}", source_message_id)
                or not isinstance(expected_hashes, dict) or not expected_hashes
                or any(k not in {"1", "2", "3"} or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in expected_hashes.items())):
            _fail("INVALID_TASK", "退回工作稿须有本人来源消息及已回读的明确稿件哈希。")
        binding = self._binding(task_id, round_id)
        if binding is None:
            _fail("TASK_NOT_FOUND", "话题未绑定本轮稿件。")
        if self.read_final(task_id) is not None:
            _fail("TASK_FINAL_EXISTS", "本话题已有正式终稿，不能退回为临时工作稿。")
        notes = self._notes(round_id)
        self._round_owner(task_id, round_id, notes)
        if any(n["meta"]["tags"][0] == "已发布" for n in notes):
            _fail("ROUND_ALREADY_PUBLISHED", "已发布稿不能转为工作稿或被临时清理。")
        prior = self._working_metadata(binding)
        if prior is not None:
            if prior["working_previous_hashes"] != expected_hashes:
                _fail("STALE_CONTENT", "重试与已完成的工作稿转换不一致。")
            return {**self._working_context(binding, notes), "changed": False, "retired": [], "backups": []}
        if not notes:
            _fail("TASK_NOT_FOUND", "本轮没有可核对的候选，未补造工作稿。")
        if {str(n["meta"]["variant"]): _sha(n["raw"]) for n in notes} != expected_hashes:
            _fail("STALE_CONTENT", "本话题稿件已变化，请回读后重试。")
        retention = Retention(self.runner)
        changes, deletes, retired = retention.plan(notes)
        basis = notes[0]["meta"]
        meta = dict(binding["meta"])
        meta.update(working_only=True, selected_variant=None, source_ref=basis["source_ref"],
                    topic_tags=list(basis["tags"][1:]), working_previous_hashes=dict(expected_hashes),
                    working_retired_variants=[row["variant"] for row in retired],
                    working_source_message_id=source_message_id, working_started_at=now())
        for name in ("preparation_id", "hot_selection"):
            if basis.get(name) is not None:
                meta[name] = basis[name]
        changes[binding["path"]] = _encode(meta, "话题任务指针", "本话题候选仅保留在私有工作区，确认后正式正文只认final.md。")
        from moments_catalog import catalog_cleanup_changes
        changes.update(catalog_cleanup_changes(retention, changes, deletes))
        expected = {binding["path"]: binding["raw"], **retention.expected}
        state = self.runner._state()
        after = retention.state_after(state, deletes)
        if after != state:
            changes[self.runner.state_path] = json_text(after).encode("utf-8")
            expected[self.runner.state_path] = self.runner._observed_state_bytes
        backups = self._commit(changes, deletes, expected)
        saved = self._binding(task_id, round_id)
        return {**self._working_context(saved, self._notes(round_id)), "changed": True,
                "retired": retired, "backups": backups, "reread_verified": True}

    def _round_owner(self, task_id, round_id, notes):
        if any(n["meta"].get("task_id") not in (None, task_id) for n in notes):
            _fail("TASK_ROUND_MISMATCH", "稿件已属于其他话题任务。")
        if not self.root.exists():
            return
        safe_path(self.root)
        directories = list(self.root.iterdir())
        if len(directories) > 10000:
            _fail("LIMIT_EXCEEDED", "任务目录超过有界读取上限。")
        for directory in directories:
            safe_path(directory)
            if directory.is_dir() and directory.name != task_id and (directory / "task.md").exists():
                other = self._binding(directory.name)
                if other["meta"]["round_id"] == round_id:
                    _fail("TASK_ROUND_MISMATCH", "同一轮不能建立两个话题任务。")

    def _catalog_final(self, retention, changes, task_id):
        """Link the sole final in the existing owned index without copying status/body."""
        from moments import now
        path = self.runner.archive / "00_朋友圈内容库.md"
        safe_path(path)
        if not path.exists():
            return
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 262144:
            _fail("UNSAFE_PATH", "朋友圈专属入口异常。")
        before = path.read_bytes()
        data = changes.get(path, before)
        try:
            source = data.decode("utf-8-sig")
        except UnicodeError:
            _fail("NOT_OWNED", "朋友圈专属入口编码异常。")
        newline = "\r\n" if source.startswith("---\r\n") else "\n"
        text = source.replace("\r\n", "\n")
        if not text.startswith("---\n") or "\n# 朋友圈内容库\n" not in text:
            _fail("NOT_OWNED", "朋友圈专属入口不属于本Agent。")
        start, end = f"<!-- moments-task:{task_id} -->", f"<!-- /moments-task:{task_id} -->"
        block = (start + "\n## 话题终稿\n\n- [[_tasks/" + task_id
                 + "/final|任务终稿]]：正文和发布状态以该文件为准。\n" + end)
        if start in text or end in text:
            pattern = re.escape(start) + r".*?" + re.escape(end)
            if text.count(start) != 1 or text.count(end) != 1 or not re.search(pattern, text, re.S):
                _fail("STATE_INVALID", "专属入口话题记录损坏。")
            updated = re.sub(pattern, lambda _: block, text, count=1, flags=re.S)
        else:
            updated = text.rstrip() + "\n\n" + block + "\n"
        if updated != text:
            head, separator, tail = updated[4:].partition("\n---\n")
            if not separator:
                _fail("NOT_OWNED", "专属入口元数据格式异常。")
            head = re.sub(r"(?m)^updated:[^\n]*", "updated: " + now()[:10], head, count=1)
            bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
            changes[path] = bom + ("---\n" + head + separator + tail).replace("\n", newline).encode("utf-8")
            retention.expected.setdefault(path, before)

    def _commit(self, changes, deletes=(), expected=None):
        """Narrow all-target backup, conflict checks and rollback under the original lock."""
        expected = dict(expected or {})
        changes = {Path(p): d.encode("utf-8") if isinstance(d, str) else d for p, d in changes.items()}
        deletes = list(deletes)
        if set(changes) & set(deletes) or len(deletes) != len(set(deletes)):
            _fail("INVALID_TASK", "任务事务目标重复。")
        paths = [*changes, *deletes]
        before = {}
        for path in [*paths, *expected]:
            safe_path(path)
            root_note = path.parent == self.runner.archive and path.suffix == ".md"
            task_note = path.name in ("task.md", "final.md") and path.parent.parent == self.root
            if path != self.runner.state_path and not root_note and not task_note:
                _fail("UNSAFE_PATH", "任务事务越过专属目录。")
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                _fail("UNSAFE_PATH", "任务事务目标文件类型异常。")
            data = path.read_bytes() if path.exists() else None
            if path in expected and data != expected[path]:
                _fail("WRITE_CONFLICT", "任务文件在核对后变化，未覆盖。")
            if path in paths:
                before[path] = data
        backups = [str(self.runner._backup(p)) for p, data in before.items() if data is not None]
        for path, data in {**expected, **before}.items():
            if (path.read_bytes() if path.exists() else None) != data:
                _fail("WRITE_CONFLICT", "任务文件在备份期间变化，未覆盖。")
        touched = []
        try:
            for path in paths:
                safe_path(path)
                if (path.read_bytes() if path.exists() else None) != before[path]:
                    _fail("WRITE_CONFLICT", "任务文件在提交前变化，未覆盖。")
                if path in changes:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_bytes(path, changes[path])
                else:
                    path.unlink()
                touched.append(path)
        except BaseException:
            conflicts = []
            for path in reversed(touched):
                written = changes.get(path)
                if (path.read_bytes() if path.exists() else None) != written:
                    conflicts.append(path.name)
                    continue
                try:
                    if before[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_bytes(path, before[path])
                except OSError:
                    conflicts.append(path.name)
            if conflicts:
                _fail("ROLLBACK_INCOMPLETE", "任务部分文件无法回滚，原字节备份已保留。")
            raise
        return backups

    @serialized
    def bind(self, task_id, round_id):
        from moments import now
        task_id, round_id = _identity(task_id, "任务ID"), _identity(round_id, "轮号")
        old = self._binding(task_id, round_id)
        if old is not None:
            return {"status": "OK", "task_id": task_id, "round_id": round_id, "changed": False}
        notes = self._notes(round_id)
        if not notes:
            _fail("TASK_NOT_FOUND", "没有该明确轮次的任务稿件。")
        self._round_owner(task_id, round_id, notes)
        path = self._path(task_id, "task.md")
        meta = {"moments_agent": "moments-draft", "skill_version": "0.2.0", "record_type": "moments_task_binding",
                "task_id": task_id, "round_id": round_id, "selected_variant": None, "created_at": now()}
        changes = {path: _encode(meta, "话题任务指针", "仅保存任务身份和选稿指针；正文与发布状态以本目录final.md或本轮稿件为准。")}
        changes.update({n["path"]: metadata_bytes(n["raw"], "task_id", task_id) for n in notes if n["meta"].get("task_id") is None})
        self._commit(changes, expected={path: None, **{n["path"]: n["raw"] for n in notes}})
        return {"status": "OK", "task_id": task_id, "round_id": round_id, "changed": True}

    def get(self, task_id):
        binding = self._binding(task_id)
        final = self.read_final(task_id)
        if binding is None:
            _fail("TASK_NOT_FOUND", "该话题尚未绑定出稿任务。")
        rid = binding["meta"]["round_id"]
        if final is not None and final["meta"]["round_id"] != rid:
            _fail("TASK_ROUND_MISMATCH", "任务指针与终稿轮次不一致。")
        notes = self._notes(rid)
        if self._working_metadata(binding) is not None:
            if final is not None:
                _fail("INVALID_TASK", "工作稿绑定与正式终稿状态冲突。")
            return self._working_context(binding, notes)
        if final is None and not notes:
            _fail("TASK_NOT_FOUND", "任务稿件已缺失，未补造正文。")
        return {"status": "OK", "task_id": task_id, "round_id": rid,
                "selected_variant": binding["meta"].get("selected_variant"),
                "final": self._public_final(final) if final else None,
                "notes": [{**self.runner._public(n), "sha256": _sha(n["raw"])} for n in notes]}

    @serialized
    def final(self, task_id, round_id, body=None, selected_variant=None, published=None,
              source_message_id=None, expected_final_sha256=None, correct_published=False):
        from moments import now, privacy_check, json_text
        task_id, round_id = _identity(task_id, "任务ID"), _identity(round_id, "轮号")
        if selected_variant is not None and (type(selected_variant) is not int or selected_variant not in (1, 2, 3)):
            _fail("INVALID_TASK", "选稿编号仅接受本人明确的1、2、3，未知保持null。")
        if published is not None and type(published) is not bool:
            _fail("INVALID_TASK", "是否已发仅接受本人明确报告的布尔值或null。")
        if type(correct_published) is not bool:
            _fail("INVALID_TASK", "已发终稿更正标志需为布尔值。")
        if source_message_id is not None and (not isinstance(source_message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,200}", source_message_id)):
            _fail("INVALID_TASK", "来源消息ID无效。")
        old_binding = self._binding(task_id, round_id)
        old = self.read_final(task_id)
        working = self._working_metadata(old_binding)
        if old is not None and old["meta"]["round_id"] != round_id:
            _fail("TASK_ROUND_MISMATCH", "终稿已属于另一轮，未覆盖。")
        if expected_final_sha256 is not None and (not isinstance(expected_final_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_final_sha256)
                or old is None or _sha(old["raw"]) != expected_final_sha256):
            _fail("STALE_TASK_FINAL", "任务终稿已变化，请回读后重试。")
        if correct_published and (old is None or body is None or source_message_id is None or expected_final_sha256 is None):
            _fail("CORRECTION_AUTHORIZATION_REQUIRED", "更正已发终稿须有本人原消息、完整正文及已回读终稿哈希。")
        notes = self._notes(round_id)
        self._round_owner(task_id, round_id, notes)
        if old is None and not notes and working is None:
            _fail("TASK_NOT_FOUND", "该明确轮次无可核对稿件，未补造任务。")
        if any(n["meta"]["tags"][0] == "已发布" for n in notes):
            _fail("ROUND_ALREADY_PUBLISHED", "该轮已有历史已发布正文，未建立第二个已发布事实源。")
        selected = None if working is not None else next((n for n in notes if n["meta"]["variant"] == selected_variant), None)
        if selected_variant is not None and selected is None and working is None and (old is None or old["meta"].get("selected_variant") != selected_variant):
            _fail("VARIANT_RETIRED", "本人指定编号无可核对原稿，未猜测来源。")
        supplied_body = body is not None
        if working is not None and not supplied_body:
            _fail("TASK_FINAL_REQUIRED", "工作稿确认须由已确认候选或本人原消息提供完整正文，不猜测已删除稿件。")
        if body is None:
            if selected_variant is None and old is None and old_binding is not None:
                selected_variant = old_binding["meta"].get("selected_variant")
                selected = next((n for n in notes if n["meta"]["variant"] == selected_variant), None)
            body = old["body"] if old else (selected["body"] if selected else None)
        if body is None:
            _fail("TASK_FINAL_REQUIRED", "请直接提供终稿全文，或先明确选稿再报告是否已发。")
        if not isinstance(body, str) or not body.strip() or len(body) > 8000:
            _fail("INVALID_TASK", "请提供完整终稿，或明确选稿后报告是否已发。")
        privacy_check(body)
        state_label = "已发布" if published is True else (old["meta"]["tags"][0] if old and published is None else "待发布")
        if old and old["meta"]["tags"][0] == "已发布":
            if state_label != "已发布" or (body != old["body"] and not correct_published):
                _fail("ALREADY_PUBLISHED", "已发布任务终稿不可被普通回稿改写或降级。")
        # An unknown variant remains unknown even if the prose resembles a draft.
        variant = selected_variant if supplied_body else (selected_variant if selected_variant is not None else old["meta"].get("selected_variant") if old else None)
        if old and body == old["body"] and state_label == old["meta"]["tags"][0] and variant == old["meta"].get("selected_variant"):
            return {"status": "OK", "action": "task_final", "changed": False, "reread_verified": True,
                    "retired": [], "backups": [], **self._public_final(old)}
        basis_note = selected or (notes[0] if notes else None)
        stamp = now()
        original_body_hash = (_sha(selected["body"].encode("utf-8")) if selected else
                              old["meta"].get("selected_original_body_sha256") if old and variant is not None else None)
        meta = {"moments_agent": "moments-draft", "skill_version": "0.2.0", "record_type": "moments_task_final",
                "task_id": task_id, "round_id": round_id, "selected_variant": variant,
                "tags": [state_label] + (list(working["topic_tags"]) if working is not None else (old["meta"]["tags"][1:] if old else basis_note["meta"]["tags"][1:])),
                "source_ref": working["source_ref"] if working is not None else old["meta"].get("source_ref") if old else basis_note["meta"].get("source_ref"),
                "created_at": old["meta"]["created_at"] if old else stamp,
                "confirmed_at": old["meta"].get("confirmed_at") if old and old["meta"]["tags"][0] == "已发布" else stamp if state_label == "已发布" else None,
                "published_at": old["meta"].get("published_at") if old else None,
                "body_sha256": _sha(body.encode("utf-8")),
                "publication_text_basis": "USER_CONFIRMED_WORKING_TEXT" if working is not None else "USER_PROVIDED_FINAL" if supplied_body else old["meta"].get("publication_text_basis") if old else "CANDIDATE_UNVERIFIED",
                "source_message_id": source_message_id or (old["meta"].get("source_message_id") if old else None),
                "selected_source_sha256": _sha(selected["raw"]) if selected else (old["meta"].get("selected_source_sha256") if old and variant is not None else None)}
        meta["selected_original_body_sha256"] = original_body_hash
        meta["revised_from_selected"] = (_sha(body.encode("utf-8")) != original_body_hash
                                         if original_body_hash is not None else None)
        path = self._path(task_id, "final.md")
        binding_path = self._path(task_id, "task.md")
        binding_meta = dict(old_binding["meta"]) if old_binding else {"moments_agent": "moments-draft", "skill_version": "0.2.0", "record_type": "moments_task_binding", "task_id": task_id, "round_id": round_id, "created_at": stamp}
        binding_meta["selected_variant"] = variant
        if working is not None:
            binding_meta["working_only"] = False
        retention = Retention(self.runner)
        changes, deletes, retired = retention.plan(notes)
        expected = {n["path"]: n["raw"] for n in notes}
        expected.update({path: old["raw"] if old else None, binding_path: old_binding["raw"] if old_binding else None})
        changes.update({path: _encode(meta, "任务终稿", body), binding_path: _encode(binding_meta, "话题任务指针", "正文与发布状态只认本目录final.md。")})
        from moments_catalog import catalog_cleanup_changes
        changes.update(catalog_cleanup_changes(retention, changes, deletes))
        self._catalog_final(retention, changes, task_id)
        expected.update(retention.expected)
        state = self.runner._state()
        if deletes and (state.get("active_round") or {}).get("round_id") == round_id:
            after = retention.state_after(state, deletes)
            if after != state:
                changes[self.runner.state_path] = json_text(after).encode("utf-8")
                expected[self.runner.state_path] = self.runner._observed_state_bytes
        backups = self._commit(changes, deletes, expected)
        saved = self.read_final(task_id)
        if saved["body"] != body:
            _fail("TASK_FINAL_INVALID", "任务终稿回读不一致，未报告完成。")
        return {"status": "OK", "action": "task_final", "changed": True, "reread_verified": True,
                "retired": retired, "backups": backups, "style_case": None, **self._public_final(saved)}

    @serialized
    def handle(self, task_id, round_id, text, expected_content_sha256=None):
        from moments import encode_note, privacy_check
        self._binding(task_id, round_id) or _fail("TASK_NOT_FOUND", "话题未绑定出稿任务。")
        if not isinstance(text, str):
            _fail("INVALID_TASK", "任务操作需要明确文字。")
        text = text.strip()
        if text in ("已发", "已发布"):
            return self.final(task_id, round_id, published=True)
        match = re.fullmatch(r"(选|准备发|修改|保留|取消保留|已发)\s*([1-3])(?:\s*[:：]\s*([\s\S]+))?", text)
        if not match:
            _fail("UNSUPPORTED_COMMAND", "话题任务支持选N、修改N：正文、保留N、取消保留N、已发N。")
        action, number, body = match[1], int(match[2]), match[3]
        notes = self._notes(round_id)
        note = next((n for n in notes if n["meta"]["variant"] == number), None)
        if note is None:
            _fail("VARIANT_RETIRED", "该任务的原编号已清理，未转到其他话题。")
        if expected_content_sha256 is not None and _sha(note["raw"]) != expected_content_sha256:
            _fail("STALE_CONTENT", "该话题稿件已变化，请重新回读。")
        if action == "已发":
            return self.final(task_id, round_id, body=body, selected_variant=number, published=True)
        if action in ("选", "准备发"):
            if body is not None:
                _fail("INVALID_TASK", "选稿不接受正文替换。")
            binding = self._binding(task_id, round_id)
            meta = dict(binding["meta"])
            changed = meta.get("selected_variant") != number
            if changed:
                meta["selected_variant"] = number
                self._commit({binding["path"]: _encode(meta, "话题任务指针", "正文与发布状态只认本目录final.md或本轮稿件。")}, expected={binding["path"]: binding["raw"], note["path"]: note["raw"]})
            return {"status": "OK", "action": "task_select", "task_id": task_id, "changed": changed, **self.runner._public(note)}
        if note["meta"]["tags"][0] == "已发布":
            _fail("ALREADY_PUBLISHED", "历史已发布稿不能普通修改或改保留状态。")
        if action == "修改":
            if not isinstance(body, str) or not body.strip() or len(body) > 8000:
                _fail("INVALID_TASK", "修改需要完整正文。")
            privacy_check(body)
            updated = encode_note(note["meta"], note["title"], body)
        else:
            if body is not None:
                _fail("INVALID_TASK", "保留操作不接受正文替换。")
            updated = metadata_bytes(note["raw"], "draft_retention", "keep" if action == "保留" else "temporary")
        data = updated.encode("utf-8") if isinstance(updated, str) else updated
        changed = data != note["raw"]
        if changed:
            self._commit({note["path"]: data}, expected={note["path"]: note["raw"]})
        return {"status": "OK", "action": "task_modified" if action == "修改" else "task_keep",
                "task_id": task_id, "changed": changed, **self.runner._public(self.runner._read_note(note["path"]))}
