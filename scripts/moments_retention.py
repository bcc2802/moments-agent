"""Temporary draft lifecycle and reversible cleanup, using only local files."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from moments_storage import serialized


def safe_path(path):
    """Reject links and Windows junctions before resolving or touching a file."""
    path = Path(path)
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                from moments import fail
                fail("UNSAFE_PATH", "稿件路径包含链接或重解析点，未执行清理。")


def valid_round(notes):
    """Missing variants require consistent, explicit retirement evidence."""
    if not notes:
        return False
    numbers = [n["meta"]["variant"] for n in notes]
    if any(type(n) is not int or n not in (1, 2, 3) for n in numbers) or len(numbers) != len(set(numbers)):
        return False
    retired = notes[0]["meta"].get("retired_variants", [])
    if (not isinstance(retired, list) or any(type(n) is not int or n not in (1, 2, 3) for n in retired)
            or len(retired) != len(set(retired)) or set(numbers) & set(retired)):
        return False
    if any(n["meta"].get("retired_variants", []) != retired for n in notes):
        return False
    return set(numbers) | set(retired) == {1, 2, 3}


def metadata_bytes(data, key, value):
    """Change one JSON-compatible YAML field while keeping all other bytes exact."""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    source = data[len(bom):]
    newline = b"\r\n" if source.startswith(b"---\r\n") else b"\n"
    start = source.find(newline + b"---" + newline, 3 + len(newline))
    if not source.startswith(b"---" + newline) or start < 0:
        from moments import fail
        fail("NOT_OWNED", "稿件格式无效，未执行清理。")
    head, tail = source[:start], source[start:]
    line = (key + ": " + json.dumps(value, ensure_ascii=False)).encode("utf-8")
    pattern = re.compile(rb"(?m)^" + key.encode("ascii") + rb":[^\r\n]*")
    if pattern.search(head):
        head = pattern.sub(lambda _: line, head)
    else:
        head += newline + line
    return bom + head + tail


def atomic_bytes(path, data):
    fd, name = tempfile.mkstemp(prefix=".moments-retention-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Retention:
    def __init__(self, runner):
        self.runner = runner
        self.expected = {}
        self.expected_state = getattr(runner, "_observed_state_bytes", None)

    def plan(self, notes, *, include_kept=False, exclude=()):
        """Plan from already validated rounds, never infer deletion from filenames."""
        from moments import fail
        if type(include_kept) is not bool:
            fail("INVALID_INPUT", "include_kept 必须为布尔值。")
        groups = {}
        for note in notes:
            if note["path"].name != f'{note["meta"]["round_id"]}-v{note["meta"]["variant"]}.md':
                fail("STATE_INVALID", "稿件文件名与轮号或编号不一致，未执行清理。")
            groups.setdefault(note["meta"]["round_id"], []).append(note)
            self.expected[note["path"]] = note["raw"]
        changes, deletes, rows = {}, [], []
        for rid, group in groups.items():
            if not valid_round(group):
                fail("STATE_INVALID", "轮次缺稿或身份重复，不能将未知缺失当作已清理：" + rid)
            removed = [n for n in group if n["meta"]["tags"][0] == "待发布"
                       and (include_kept or n["meta"].get("draft_retention") != "keep")
                       and n["meta"]["variant"] not in exclude]
            if not removed:
                continue
            retired = sorted(set(group[0]["meta"].get("retired_variants", []))
                             | {n["meta"]["variant"] for n in removed})
            removed_paths = {n["path"] for n in removed}
            for note in group:
                path = note["path"]
                safe_path(path)
                if path in removed_paths:
                    deletes.append(path)
                    data = note["raw"]
                    digest = hashlib.sha256(data).hexdigest()
                    rows.append({"path": str(path), "round_id": rid,
                                 "variant": note["meta"]["variant"], "sha256": digest,
                                 "backup_path": str(self.runner.project / "backups/runtime" /
                                                    (path.name + "." + digest + ".bak"))})
                else:
                    changes[path] = metadata_bytes(note["raw"], "retired_variants", retired)
        return changes, deletes, rows

    def state_after(self, state, deletes):
        state = copy.deepcopy(state)
        active = state.get("active_round")
        if active is not None:
            removed = {p.relative_to(self.runner.vault).as_posix() for p in deletes}
            active["notes"] = [p for p in active["notes"] if p not in removed]
            if not active["notes"]:
                state["active_round"] = None
        return state

    def commit(self, changes, deletes, state=None, expected=None):
        """Validate, snapshot every existing target, then mutate with rollback."""
        from moments import fail, json_text
        changes = {Path(p): value.encode("utf-8") if isinstance(value, str) else value
                   for p, value in changes.items()}
        deletes = list(deletes)
        from moments_catalog import catalog_cleanup_changes
        changes.update(catalog_cleanup_changes(self, changes, deletes))
        self.expected.update(expected or {})
        if state is not None:
            data = json_text(state).encode("utf-8")
            if not self.runner.state_path.exists() or self.runner.state_path.read_bytes() != data:
                changes[self.runner.state_path] = data
                self.expected[self.runner.state_path] = self.expected_state
        if set(changes) & set(deletes) or len(deletes) != len(set(deletes)):
            fail("STATE_INVALID", "清理目标重复，未执行写入。")
        dependencies = {Path(path): data for path, data in self.expected.items()
                        if Path(path) not in changes and Path(path) not in deletes}

        def verify_dependencies():
            # A validated private preparation is a read dependency, not another
            # writable target. Catch edits after validation before any mutation.
            for path, expected_data in dependencies.items():
                safe_path(path)
                private_preparation = (path.parent == self.runner.archive / "_workflow"
                                       and re.fullmatch(r"prep-[0-9a-f]{32}\.json", path.name))
                if (not path.is_absolute() or path != self.runner.state_path
                        and path.parent != self.runner.archive and not private_preparation):
                    fail("UNSAFE_PATH", "事务只读依赖越界，未执行写入。")
                if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                    fail("UNSAFE_PATH", "事务只读依赖类型异常，未执行写入。")
                observed = path.read_bytes() if path.exists() else None
                if observed != expected_data:
                    fail("WRITE_CONFLICT", "来源准备或保留稿在核对后变化，未覆盖。")

        verify_dependencies()
        before = {}
        for path in [*changes, *deletes]:
            safe_path(path)
            if path != self.runner.state_path and path.parent != self.runner.archive:
                fail("UNSAFE_PATH", "清理目标越过朋友圈专属目录。")
            before[path] = path.read_bytes() if path.exists() else None
            if path in self.expected and before[path] != self.expected[path]:
                fail("WRITE_CONFLICT", "稿件或指针在计划之后发生变化，请重新核对后重试。")
            if path in deletes and before[path] is None:
                fail("STATE_INVALID", "清理前稿件已丢失，未执行写入。")
        backups = []
        for path, data in before.items():
            if data is not None:
                backups.append(str(self.runner._backup(path)))
        verify_dependencies()
        # An editor can write outside our process lock. Refuse stale plans.
        for path, data in before.items():
            if (path.read_bytes() if path.exists() else None) != data:
                fail("WRITE_CONFLICT", "稿件在备份期间发生变化，请重新核对后重试。")
        touched = []
        try:
            for path, data in changes.items():
                verify_dependencies()
                safe_path(path)
                if (path.read_bytes() if path.exists() else None) != before[path]:
                    fail("WRITE_CONFLICT", "稿件在写入前发生变化，已停止。")
                atomic_bytes(path, data)
                touched.append(path)
            for path in deletes:
                verify_dependencies()
                safe_path(path)
                if path.read_bytes() != before[path]:
                    fail("WRITE_CONFLICT", "稿件在删除前发生变化，已停止。")
                path.unlink()
                touched.append(path)
        except BaseException:
            conflicts = []
            for path in reversed(touched):
                expected = changes.get(path) if path in changes else None
                if (path.read_bytes() if path.exists() else None) != expected:
                    conflicts.append(str(path))
                    continue
                try:
                    if before[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_bytes(path, before[path])
                except OSError:
                    conflicts.append(str(path))
            if conflicts:
                fail("ROLLBACK_INCOMPLETE", "部分文件发生外部变化或无法回滚；原字节备份已保留。",
                     {"paths": conflicts, "backups": backups})
            raise
        return backups

    @serialized
    def cleanup(self, apply=False, include_kept=False):
        from moments import fail
        if type(apply) is not bool:
            fail("INVALID_INPUT", "apply 必须为布尔值。")
        runner = self.runner
        state = runner._state()
        self.expected_state = runner._observed_state_bytes
        notes = runner._retention_notes()
        if state.get("active_round") is not None:
            runner._active_notes(state["active_round"])
        changes, deletes, rows = self.plan(notes, include_kept=include_kept)
        result = {"status": "OK", "dry_run": not apply, "count": len(deletes),
                  "targets": rows, "metadata_updates": [str(p) for p in changes],
                  "preserved_media": True, "backups": []}
        if apply and deletes:
            result["backups"] = self.commit(changes, deletes, self.state_after(state, deletes))
        return result
