"""One local receipt for user-supplied revisions, evidence and entry links.

This adapter never generates prose or infers publication. Markdown remains the
only draft body/status store. Optional observations are supplied by the agent,
kept as single-case observations, and are never promoted into global rules.
"""
from __future__ import annotations

import difflib
from functools import wraps
import hashlib
import json
from pathlib import Path
import re

from moments_storage import serialized
from moments_retention import safe_path


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RevisionError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(code, message, details=None):
    raise RevisionError(code, message, details)


def _errors(method):
    @wraps(method)
    def checked(*args, **kwargs):
        from moments import MomentsError
        try:
            return method(*args, **kwargs)
        except MomentsError as exc:
            raise RevisionError(exc.code, str(exc), getattr(exc, "details", None)) from exc
        except (OSError, UnicodeError):
            _fail("REVISION_STORAGE_ERROR", "回稿存储检查失败；请回读目标并重试同一请求，未宣布记录完成。")
    return checked


def _field(text, name):
    header = text.split("\n---\n", 1)[0] if text.startswith("---\n") else ""
    match = re.search(r"(?m)^" + re.escape(name) + r":\s*(.+)$", header)
    if not match:
        return None
    try:
        return json.loads(match[1])
    except ValueError:
        return match[1].strip("\"'")


def _set_field(text, name, value):
    header, sep, rest = text.partition("\n---\n")
    line = name + ": " + json.dumps(value, ensure_ascii=False)
    pattern = r"(?m)^" + re.escape(name) + r":.*$"
    header = re.sub(pattern, lambda _: line, header, count=1) if re.search(pattern, header) else header + "\n" + line
    return header + sep + rest


def _block(text, key, content):
    start, end = "<!-- moments_revision:" + key + " -->", "<!-- /moments_revision:" + key + " -->"
    replacement = start + "\n" + content.rstrip() + "\n" + end
    pattern = re.escape(start) + r"[\s\S]*?" + re.escape(end)
    if start in text:
        if len(re.findall(pattern, text)) != 1:
            _fail("REVISION_EVIDENCE_INVALID", "回稿留痕区块不完整或重复，未覆盖现有记录。")
        return re.sub(pattern, lambda _: replacement, text, count=1)
    return text.rstrip() + "\n\n" + replacement + "\n"


class Revision:
    def __init__(self, runner):
        self.runner = runner

    def _path(self, path):
        path = Path(path)
        safe_path(path)
        try:
            path.resolve().relative_to(self.runner.archive)
        except ValueError:
            _fail("UNSAFE_PATH", "回稿记录只允许写入本Agent专属存档。")
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            _fail("UNSAFE_PATH", "回稿记录目标包含异常文件类型或硬链接。")
        return path

    def _read_owned(self, path, kind):
        self._path(path)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 262144:
                _fail("REVISION_EVIDENCE_INVALID", "回稿辅助记录过大，未覆盖。")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            _fail("REVISION_READ_FAILED", "无法读取回稿辅助记录，未修改正文。")
        if kind == "entry":
            owned = text.startswith("---\n") and "\n# 朋友圈内容库\n" in text
        else:
            owned = _field(text, "moments_agent") == "moments-draft" and _field(text, "record_type") == kind
        if not owned:
            _fail("NOT_OWNED", "回稿辅助记录不属于本Agent，未覆盖。")
        return text

    @staticmethod
    def _original(case_text):
        match = re.search(r"(?m)^## 修订前的AI稿快照\n\n", case_text)
        if not match:
            return None
        tail = case_text[match.end():]
        # The snapshot may itself contain Markdown headings. Its recorded hash
        # selects the exact boundary instead of guessing from the first heading.
        ends = [m.start() for m in re.finditer(r"(?m)^## |^<!-- moments_revision:", tail)] + [len(tail)]
        for end in ends:
            body = tail[:end].rstrip()
            if _sha(body) == _field(case_text, "original_body_sha256"):
                return body
        return None

    def _recover_original(self, note, body):
        """Retry after a partial write: use only unambiguous byte-exact backups."""
        from moments import decode_note, MomentsError
        root = self.runner.project / "backups" / "runtime"
        safe_path(root)
        bodies = {}
        for path in root.glob(note["path"].name + ".*.bak") if root.is_dir() else ():
            safe_path(path)
            if not path.is_file() or path.stat().st_size > 262144:
                continue
            raw = path.read_bytes()
            if not path.name.endswith("." + hashlib.sha256(raw).hexdigest() + ".bak"):
                continue
            try:
                meta, _, candidate = decode_note(raw.decode("utf-8"))
            except (MomentsError, UnicodeError):
                continue
            if (meta.get("round_id") == note["meta"]["round_id"] and meta.get("variant") == note["meta"]["variant"]
                    and candidate != body):
                bodies[_sha(candidate)] = candidate
        return (next(iter(bodies.values())) if len(bodies) == 1 else None, bool(bodies))

    @_errors
    @serialized
    def record(self, n, body, round_id, published=False, observations=None):
        from moments import atomic, now, privacy_check
        if (type(n) is not int or n not in (1, 2, 3) or type(published) is not bool
                or not isinstance(round_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", round_id)
                or not isinstance(body, str) or not body.strip() or len(body) > 8000):
            _fail("INVALID_REVISION", "回稿需明确轮号、原稿编号、完整正文及是否本人已发。")
        body = body.strip()
        privacy_check(body)
        if observations is None:
            observations = []
        if (not isinstance(observations, list) or len(observations) > 5
                or any(not isinstance(x, str) or not x.strip() or len(x) > 300 or "\n" in x for x in observations)):
            _fail("INVALID_REVISION", "单案例观察最多5条简短单行文字。")
        for item in observations:
            privacy_check(item)
        current = self.runner.resume()
        if current.get("round_id") != round_id:
            _fail("STALE_REVISION", "回稿轮号与当前活动轮不符，未按旧编号写入。")
        notes = self.runner._active_notes(self.runner._state()["active_round"])
        note = next((row for row in notes if row["meta"]["variant"] == n), None)
        if note is None:
            _fail("VARIANT_RETIRED", "该原稿编号已清理，未恢复或补造正文。")
        if note["meta"]["tags"][0] == "已发布" and not published:
            _fail("ALREADY_PUBLISHED", "此稿已发布，普通回稿不能隐式改写已发布正文。")
        self._path(note["path"])
        style = self.runner.archive / "_style"
        case = self._path(style / "cases" / f"{round_id}-v{n}.md")
        profile = self._path(style / "00_写作偏好.md")
        entry = self._path(self.runner.archive / "00_朋友圈内容库.md")
        old_case = self._read_owned(case, "moments_style_case")
        old_profile = self._read_owned(profile, "moments_style_profile")
        old_entry = self._read_owned(entry, "entry")
        cid, case_id, stamp = f"{round_id}:v{n}", f"{round_id}-v{n}", now()
        if old_case is not None and (_field(old_case, "case_id") != case_id or _field(old_case, "content_id") != cid):
            _fail("REVISION_EVIDENCE_INVALID", "既有改稿案例身份不匹配，未覆盖。")
        same_evidence = old_case is not None and _field(old_case, "user_body_sha256") == _sha(body)
        recovered_difference = False
        if old_case is not None:
            original = self._original(old_case)
        elif note["body"] != body:
            original = note["body"]
        else:
            original, recovered_difference = self._recover_original(note, body)
        if old_case is not None and _field(old_case, "original_body_sha256") is not None and original is None:
            _fail("REVISION_EVIDENCE_INVALID", "既有修订前快照与哈希不一致，未覆盖。")
        create_case = old_case is not None or note["body"] != body or recovered_difference
        case_text = old_case
        if create_case and not same_evidence:
            if old_case is None:
                fields = {"moments_agent": "moments-draft", "record_type": "moments_style_case", "case_id": case_id,
                          "content_id": cid, "source_note": note["path"].name, "original_body_sha256": _sha(original) if original is not None else None,
                          "user_body_sha256": _sha(body), "recorded_at": stamp}
                case_text = "---\n" + "\n".join(key + ": " + json.dumps(value, ensure_ascii=False) for key, value in fields.items()) + "\n---\n\n# 本人回稿差异\n\n"
                case_text += f"完整当前本人文字只认[原稿件](../../{note['path'].name})；本案例不复制完整新版正文或另设发布状态。\n\n"
                case_text += "## 修订前的AI稿快照\n\n" + (original if original is not None else "修订前正文无法从证据唯一恢复，不编造原稿。") + "\n"
            else:
                case_text = _set_field(old_case, "user_body_sha256", _sha(body))
                case_text = _set_field(case_text, "updated_at", stamp)
            if original is None:
                diff = "缺少可验证的原稿，不推断差异。"
            else:
                delta = list(difflib.unified_diff(original.splitlines(), body.splitlines(), fromfile="修订前", tofile="本人当前稿", lineterm=""))
                # Keep only changed hunks; current full text remains in the note.
                lines = delta[:2] + [line for line in delta[2:] if line.startswith(("+", "-", "@@"))]
                fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", "\n".join(lines))), default=3))
                diff = fence + "diff\n" + "\n".join(lines) + "\n" + fence
            content = "## 机械差异与场景观察\n\n" + diff + "\n\n"
            content += "\n".join("- " + x for x in observations) + ("\n" if observations else "")
            content += "\n仅为本内容ID的单样本证据；不自动升级为长期偏好，不从主题推导本人经历、立场或成效。"
            case_text = _block(case_text, "diff", content)
        profile_text = old_profile or '---\nmoments_agent: "moments-draft"\nrecord_type: "moments_style_profile"\nversion: 1\n---\n\n# 朋友圈写作偏好\n'
        if create_case and f"cases/{case.name}" not in profile_text:
            profile_text = profile_text.rstrip() + f"\n\n- [本人回稿证据 {cid}](cases/{case.name})：单案例机械差异与场景观察，写作时按主题回读，不作为通用规则。\n"
        entry_text = old_entry or '---\nversion: "0.2"\n---\n\n# 朋友圈内容库\n'
        entry_line = f"### 本人回稿 {cid}\n\n已按本人提供全文原样回读保存到[[{note['path'].stem}]]；正文及发布状态以原Markdown为准。"
        if create_case:
            entry_line += f"\n\n[本轮改稿证据](_style/cases/{case.name})；[写作偏好](_style/00_写作偏好.md)。"
        entry_text = _block(entry_text, cid, entry_line)
        writes = [(case, case_text)] if create_case else []
        if create_case:
            writes.append((profile, profile_text))
        writes.append((entry, entry_text))
        # Fail before body mutation on known ownership/path/backup errors.
        for path, text in writes:
            if path.exists() and path.read_text(encoding="utf-8") != text:
                self.runner._backup(path)
        result = self.runner.handle((f"已发{n}，终稿：" if published else f"修改{n}：") + body)
        retired_numbers = set(result.get("retired_variants", [])) | {x["variant"] for x in result.get("retired", [])}
        for number in retired_numbers:
            stem = f"{round_id}-v{number}"
            entry_text = re.sub(r"\[\[" + re.escape(stem) + r"(?:\|[^\]]+)?\]\]", f"第{number}稿（已按约定清理）", entry_text)
            entry_text = re.sub(r"\[([^\]]+)\]\((?:\./)?" + re.escape(stem) + r"\.md\)", lambda m: m[1] + "（已按约定清理）", entry_text)
        writes = [(path, entry_text if path == entry else text) for path, text in writes]
        changed_paths = []
        try:
            for path, text in writes:
                self._path(path)
                if path.exists() and path.read_text(encoding="utf-8") == text:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    self.runner._backup(path)
                atomic(path, text, exclusive=not path.exists())
                changed_paths.append(path.relative_to(self.runner.archive).as_posix())
        except OSError:
            _fail("REVISION_RECORD_INCOMPLETE", "正文已保存，但辅助记录尚未完成；重试同一回稿即可继续，未宣布完整完成。", {"body_saved": True, "content_id": cid})
        saved = self.runner._read_note(note["path"])
        if saved["body"] != body:
            _fail("REVISION_VERIFY_FAILED", "回读正文与本人提供文字不一致，未确认记录完成。")
        if create_case and (_field(case.read_text(encoding="utf-8"), "user_body_sha256") != _sha(body)
                            or f"cases/{case.name}" not in profile.read_text(encoding="utf-8")):
            _fail("REVISION_VERIFY_FAILED", "回稿证据或入口回读不一致，未确认记录完成。")
        if note["path"].stem not in entry.read_text(encoding="utf-8"):
            _fail("REVISION_VERIFY_FAILED", "回稿入口回读不一致，未确认记录完成。")
        return {"status": "OK", "action": "recorded_user_revision", "content_id": cid, "round_id": round_id,
                "variant": n, "saved_text": saved["body"], "body_sha256": _sha(body),
                "state": saved["meta"]["tags"][0], "confirmed_at": saved["meta"].get("confirmed_at"),
                "published_at": saved["meta"].get("published_at"), "path": str(note["path"]),
                "changed": result["changed"] or bool(changed_paths), "case_reused": same_evidence,
                "style_case": case.relative_to(self.runner.archive).as_posix() if create_case else None,
                "auxiliary_paths_changed": changed_paths, "reread_verified": True,
                "display_instruction": "DISPLAY_SAVED_TEXT_NOW", "retired": result.get("retired", [])}
