"""Anonymous current feedback snapshots; source Markdown remains authoritative.

Only the standard library and the read-only History adapter are imported here.
The host injects its existing atomic writer, privacy guard, and clock.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import stat
from moments_storage import serialized

from moments_history import History, HistoryError


RECORD_TYPE = "moments_feedback_snapshot"
SCHEMA_VERSION = 1
MAX_BYTES = 16384
FIELDS = {"record_type", "schema_version", "content_id", "source_basis", "inquiries_count",
          "observed_at", "note", "subject_body_sha256", "recorded_at"}
ID_RE = re.compile(r"([A-Za-z0-9_-]{1,80}):v([1-3])\Z")


class FeedbackError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.details = {}


def _fail(code, message):
    raise FeedbackError(code, message)


class Feedback:
    def __init__(self, runner, *, atomic, privacy_check, now, history_factory=History):
        if not all(callable(item) for item in (atomic, privacy_check, now, history_factory)):
            _fail("FEEDBACK_CONFIG_INVALID", "反馈依赖配置无效。")
        self.runner, self.atomic, self.privacy_check, self.now = runner, atomic, privacy_check, now
        self.history_factory = history_factory
        try:
            self.vault = Path(runner.vault).absolute()
            self.archive = Path(runner.archive).absolute()
            self.archive.relative_to(self.vault)
            if self.archive == self.vault or ".." in self.archive.parts:
                raise ValueError()
        except (ValueError, TypeError, OSError):
            _fail("UNSAFE_PATH", "反馈只允许使用受控存档子目录。")
        self.root = self.archive / "_feedback"
        self.backups = self.root / "_backups"
        self._safe_path(self.archive, directory=True)

    def _safe_path(self, path, *, directory=False):
        """Check lexical containment and every existing component without links."""
        try:
            relative = path.relative_to(self.vault)
            if ".." in relative.parts:
                raise ValueError()
            chain = [self.vault]
            for part in relative.parts:
                chain.append(chain[-1] / part)
            for index, current in enumerate(chain):
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                    _fail("UNSAFE_PATH", "反馈路径包含不允许的链接或重解析点。")
                must_be_dir = index < len(chain) - 1 or directory
                if (must_be_dir and not stat.S_ISDIR(info.st_mode)) or (not must_be_dir and not stat.S_ISREG(info.st_mode)):
                    _fail("UNSAFE_PATH", "反馈路径类型不符合受控存档要求。")
        except FeedbackError:
            raise
        except (ValueError, TypeError):
            _fail("UNSAFE_PATH", "反馈路径越界。")
        except OSError:
            _fail("FEEDBACK_READ_FAILED", "无法检查反馈存档路径；未读取或写入反馈。")

    @staticmethod
    def _timestamp(value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 64:
            _fail("INVALID_FEEDBACK", "观察时间需为日期、带时区时间或 null。")
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dt.date.fromisoformat(value)
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,12})?)?(?:Z|[+-]\d{2}:\d{2})", value):
                stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    raise ValueError()
            else:
                raise ValueError()
        except ValueError:
            _fail("INVALID_FEEDBACK", "观察时间需为日期、带时区时间或 null。")
        return value  # Preserve supplied precision and timezone; do not invent an observation time.

    def _values(self, count, observed_at, note):
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
            _fail("INVALID_FEEDBACK", "咨询数需为本人报告的非负整数或 null。")
        if not isinstance(note, str) or len(note) > 300:
            _fail("INVALID_FEEDBACK", "反馈备注需为不超过300字的脱敏文字。")
        note = note.strip()
        try:
            self.privacy_check(note)
        except Exception:
            _fail("PRIVACY_BLOCKED", "反馈含隐私标记或联系方式，请先脱敏。")
        if re.search(r"https?://|[^\s@]+@[^\s@]+\.[^\s@]+|(?:微信|手机号|邮箱|联系人)\s*[:：]", note, re.I):
            _fail("PRIVACY_BLOCKED", "反馈只记录匿名汇总，不保存联系方式或外部链接。")
        if count is None and not note:
            _fail("EMPTY_FEEDBACK", "至少需要本人报告的咨询数或一条实际观察备注。")
        return {"inquiries_count": count, "observed_at": self._timestamp(observed_at), "note": note}

    def _subject(self, content_id, *, published=False):
        if not isinstance(content_id, str) or not ID_RE.fullmatch(content_id):
            _fail("INVALID_INPUT", "反馈需使用明确的唯一内容ID。")
        try:
            resolved = self.history_factory(self.runner).resolve(content_id)
        except HistoryError as exc:
            raise FeedbackError(exc.code, "内容ID无法唯一、安全地定位；未写入反馈。") from None
        row = resolved["item"]
        try:
            relative = Path(row["relative_path"])
            if relative.is_absolute() or ".." in relative.parts:
                _fail("UNSAFE_PATH", "反馈对应稿件路径越界。")
            path = self.vault / relative
            if path.parent != self.archive:
                _fail("UNSAFE_PATH", "反馈仅绑定受控存档中的自有稿件。")
            self._safe_path(path)
            note = self.runner._read_note(path)
            actual_id = f'{note["meta"]["round_id"]}:v{note["meta"]["variant"]}'
            if actual_id != content_id:
                _fail("AMBIGUOUS_CONTENT", "内容ID在读取期间发生变化；未写入反馈。")
            status = note["meta"]["tags"][0]
            if published and status != "已发布":
                _fail("FEEDBACK_NOT_PUBLISHED", "只能为当前已确认发布的内容记录反馈。")
            return status, hashlib.sha256(note["body"].encode("utf-8")).hexdigest()
        except FeedbackError:
            raise
        except Exception:
            _fail("FEEDBACK_SUBJECT_READ_FAILED", "无法重新读取反馈对应的稿件；未写入反馈。")

    def _path(self, content_id):
        match = ID_RE.fullmatch(content_id)
        path = self.root / f"{match[1]}-v{match[2]}.md"
        self._safe_path(path)
        return path

    @staticmethod
    def _encode(record):
        header = "\n".join(key + ": " + json.dumps(record[key], ensure_ascii=False) for key in sorted(FIELDS))
        return "---\n" + header + "\n---\n\n# 匿名手工反馈\n\n本人报告的当前快照；不代表已核对微信，也不推算收入。\n"

    def _read(self, path, content_id):
        self._safe_path(path)
        try:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None, None
            if info.st_size > MAX_BYTES:
                _fail("FEEDBACK_CORRUPT", "反馈快照大小或格式无效。")
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            original_text = raw.decode("utf-8")
            text = raw.decode("utf-8-sig").replace("\r\n", "\n")
            if len(raw) > MAX_BYTES or not text.startswith("---\n") or "\n---\n" not in text[4:]:
                raise ValueError()
            front = text[4:].split("\n---\n", 1)[0]
            record = {}
            for line in front.splitlines():
                key, value = line.split(":", 1)
                if key in record:
                    raise ValueError()
                record[key] = json.loads(value)
            if (set(record) != FIELDS or record["record_type"] != RECORD_TYPE or
                    type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION or
                    record["source_basis"] != "USER_REPORTED" or record["content_id"] != content_id or
                    not isinstance(record["subject_body_sha256"], str) or
                    not re.fullmatch(r"[0-9a-f]{64}", record["subject_body_sha256"])):
                raise ValueError()
            self._values(record["inquiries_count"], record["observed_at"], record["note"])
            if self._timestamp(record["recorded_at"]) is None or "T" not in record["recorded_at"]:
                raise ValueError()
            return record, original_text  # Back up original BOM/newlines, not the parser's normalized view.
        except OSError:
            _fail("FEEDBACK_READ_FAILED", "反馈快照读取失败；不能当成没有反馈。")
        except (UnicodeError, ValueError, TypeError, KeyError, FeedbackError):
            _fail("FEEDBACK_CORRUPT", "反馈快照损坏或不属于此内容；未覆盖原文件。")

    @staticmethod
    def _response(content_id, status, body_hash, record, *, reused=None):
        changed = bool(record and record["subject_body_sha256"] != body_hash)
        response = {"status": "OK", "content_id": content_id, "found": record is not None,
                    "subject_status": status, "subject_body_sha256": body_hash,
                    "subject_body_changed": changed, "feedback": record,
                    "notice": "反馈对应旧存档正文；当前正文已变化，不代表新文案效果。" if changed else
                              "本人匿名手工报告；未核对微信实际文字，不代表系统归因或收入。"}
        if reused is not None:
            response["reused"] = reused
        return response

    def get(self, content_id):
        status, body_hash = self._subject(content_id)
        record, _ = self._read(self._path(content_id), content_id)
        return self._response(content_id, status, body_hash, record)

    @serialized
    def record(self, content_id, inquiries_count=None, observed_at=None, note="", replace=False):
        if type(replace) is not bool:
            _fail("INVALID_FEEDBACK", "替换反馈需明确的布尔确认。")
        values = self._values(inquiries_count, observed_at, note)
        status, body_hash = self._subject(content_id, published=True)
        path = self._path(content_id)
        prior, prior_text = self._read(path, content_id)
        payload = {"record_type": RECORD_TYPE, "schema_version": SCHEMA_VERSION,
                   "content_id": content_id, "source_basis": "USER_REPORTED", **values,
                   "subject_body_sha256": body_hash}
        if prior and all(prior[key] == value for key, value in payload.items()):
            return self._response(content_id, status, body_hash, prior, reused=True)
        if prior and not replace:
            _fail("FEEDBACK_REPLACE_REQUIRED", "反馈或对应正文版本已变化；需本人明确修改反馈后再替换。")
        recorded_at = self.now()
        if self._timestamp(recorded_at) is None or "T" not in recorded_at:
            _fail("FEEDBACK_CONFIG_INVALID", "反馈记录时钟无效。")
        payload["recorded_at"] = recorded_at
        try:
            self._safe_path(self.root, directory=True)
            self.root.mkdir(exist_ok=True)
            self._safe_path(path)
            if prior:
                self._safe_path(self.backups, directory=True)
                self.backups.mkdir(exist_ok=True)
                backup = self.backups / (path.stem + ".previous.md")
                self._read(backup, content_id)  # Reject links or foreign/corrupt existing backups.
                self.atomic(backup, prior_text)
            self._safe_path(path)
            self.atomic(path, self._encode(payload), exclusive=prior is None)
        except FeedbackError:
            raise
        except OSError:
            _fail("FEEDBACK_WRITE_FAILED", "反馈写入失败；请检查本地存档后重试。")
        return self._response(content_id, status, body_hash, payload, reused=False)
