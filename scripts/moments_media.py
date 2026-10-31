"""Local, provenance-bearing images for a hot-topic draft round.

This module copies already captured PNG/JPEG files. It never captures a browser,
generates evidence, decides reuse rights, changes draft text, or publishes.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import unicodedata
from urllib.parse import urlsplit
import zlib

from moments_storage import StorageError, serialized
from moments_trends import PLATFORMS, Trends, TrendsError, validate_hot_url
from moments_platforms import ARCHIVE_PLATFORMS


MARKER = "moments_media_bundle"
MAX_BYTES = 16 * 1024 * 1024
MAX_MANIFEST = 65536
ROUND = re.compile(r"[A-Za-z0-9_-]{1,80}")
ITEM_FIELDS = {"path", "role", "source_url", "source_type", "caption", "captured_at",
               "content_published_at", "title_boxed"}
SAVED_FIELDS = {"number", "file", "sha256", "width", "height", "mime_type", "role",
                "source_url", "source_type", "caption", "captured_at", "content_published_at",
                "title_boxed", "reuse_review"}
NOTICE = ("截图内容、红框位置及来源匹配须由调用方目视核对；来源记录不证明内容真实或图片使用权。"
          "图片张数不代表编辑质量；准备清单不是实际发布清单。采用记录仅为本人明确报告的子集，"
          "未报告图片是否发布未知。本工具不改正文或发布状态，不操作微信。")


class MediaError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise MediaError(code, message)


def _errors(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (StorageError, TrendsError) as exc:
            fail(exc.code, str(exc))
        except OSError:
            fail("MEDIA_STORAGE_ERROR", "图片文件或本地配图记录读写失败，未改稿件和发布状态。")
        except (UnicodeError, json.JSONDecodeError):
            fail("MEDIA_INVALID", "配图记录无法解析，未猜测或覆盖已有记录。")
        except MediaError:
            raise
        except Exception as exc:
            # The runner's shared guard translates storage/privacy exceptions
            # into its own typed error before this adapter sees them.
            if isinstance(getattr(exc, "code", None), str):
                fail(exc.code, str(exc))
            raise
    return checked


def _safe_path(path, must_file=False):
    path = Path(path)
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            fail("UNSAFE_PATH", "图片源或目标包含链接或重解析点，未执行操作。")
        if part == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            fail("UNSAFE_PATH", "图片源或目标包含硬链接，未执行操作。")
    if must_file and not path.is_file():
        fail("MEDIA_SOURCE_MISSING", "待使用的本地图片不存在或不是普通文件。")
    return path


def _timestamp(value, allow_date):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        fail("INVALID_MEDIA", "图片时间必须为明确的日期、带时区时间或null。")
    try:
        if allow_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            datetime.strptime(value, "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
    except ValueError:
        fail("INVALID_MEDIA", "图片时间无效；采集时间不能代替来源发布日期。")
    return value


def image_info(data):
    """Validate PNG container CRCs / JPEG framing and read original dimensions.

    Pixel meaning and complete JPEG entropy decoding remain visual-tool checks.
    No image editing library or additional runtime dependency is required.
    """
    if not data or len(data) > MAX_BYTES:
        fail("INVALID_MEDIA_IMAGE", "图片为空或超过16MiB上限。")
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, chunks, has_data = 8, 0, False
        while pos + 12 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            kind = data[pos + 4:pos + 8]
            end = pos + 12 + length
            if end > len(data) or length > MAX_BYTES:
                fail("INVALID_MEDIA_IMAGE", "PNG结构不完整。")
            payload = data[pos + 8:pos + 8 + length]
            checksum = struct.unpack(">I", data[pos + 8 + length:end])[0]
            if zlib.crc32(kind + payload) & 0xffffffff != checksum:
                fail("INVALID_MEDIA_IMAGE", "PNG校验失败。")
            if chunks == 0:
                if kind != b"IHDR" or length != 13:
                    fail("INVALID_MEDIA_IMAGE", "PNG缺少有效尺寸头。")
                width, height = struct.unpack(">II", payload[:8])
            if kind == b"IDAT" and length:
                has_data = True
            chunks += 1
            pos = end
            if kind == b"IEND":
                if length or not has_data or pos != len(data):
                    fail("INVALID_MEDIA_IMAGE", "PNG结束标记或像素数据异常。")
                break
        else:
            fail("INVALID_MEDIA_IMAGE", "PNG缺少结束标记。")
        suffix, mime = ".png", "image/png"
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        pos, has_scan = 2, False
        while pos + 4 <= len(data):
            if data[pos] != 0xff:
                fail("INVALID_MEDIA_IMAGE", "JPEG标记异常。")
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker == 0xda:
                if pos + 2 <= len(data):
                    scan_size = struct.unpack(">H", data[pos:pos + 2])[0]
                    has_scan = scan_size >= 2 and pos + scan_size < len(data) - 2
                break
            if marker == 0xd9:
                break
            if marker in range(0xd0, 0xd8) or marker == 1:
                continue
            if pos + 2 > len(data):
                break
            length = struct.unpack(">H", data[pos:pos + 2])[0]
            if length < 2 or pos + length > len(data):
                fail("INVALID_MEDIA_IMAGE", "JPEG结构不完整。")
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if length < 8:
                    fail("INVALID_MEDIA_IMAGE", "JPEG尺寸头异常。")
                height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            pos += length
        if not has_scan:
            fail("INVALID_MEDIA_IMAGE", "JPEG缺少完整扫描数据。")
        suffix, mime = ".jpg", "image/jpeg"
    else:
        fail("INVALID_MEDIA_IMAGE", "仅接受实际PNG或JPEG图片，不按扩展名猜测类型。")
    if (not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 160
            or max(width, height) > 20000 or width * height > 80000000):
        fail("INVALID_MEDIA_IMAGE", "图片尺寸无效或过小，需可清楚阅读的截图。")
    return {"width": width, "height": height, "mime_type": mime}, suffix


class Media:
    def __init__(self, runner, *, atomic, now, public_url, privacy_check):
        self.runner, self.atomic, self.now = runner, atomic, now
        self.public_url, self.privacy_check = public_url, privacy_check
        self.root = runner.archive / "_media"

    def _round(self, round_id, current=False, *, with_notes=False):
        if not isinstance(round_id, str) or not ROUND.fullmatch(round_id):
            fail("INVALID_MEDIA_ROUND", "配图需明确有效的稿件轮号。")
        notes = []
        try:
            for number in (1, 2, 3):
                path = self.runner.archive / f"{round_id}-v{number}.md"
                # Sparse rounds are valid only with consistent retirement evidence below.
                if not path.exists() and not path.is_symlink():
                    continue
                _safe_path(path, must_file=True)
                note = self.runner._read_note(path)
                if note["meta"]["round_id"] != round_id or note["meta"]["variant"] != number:
                    fail("INVALID_MEDIA_ROUND", "稿件轮号或编号与文件不匹配。")
                notes.append(note)
            from moments_retention import valid_round
            if not valid_round(notes):
                fail("INVALID_MEDIA_ROUND", "缺少稿件且无一致清理记录，无法核实配图轮次。")
        except MediaError:
            raise
        except Exception as exc:
            if hasattr(exc, "code"):
                fail("INVALID_MEDIA_ROUND", "无法核实本轮三份Agent稿件，未写入配图。")
            raise
        meta = notes[0]["meta"]
        hot = meta.get("hot_selection")
        required = {"batch_id", "candidate_id", "number", "fetched_at"}
        if (not isinstance(hot, dict) or set(hot) != required
                or isinstance(hot.get("number"), bool) or not isinstance(hot.get("number"), int)
                or not 1 <= hot["number"] <= 10
                or not re.fullmatch(r"tr-[0-9a-f]{32}", str(hot.get("batch_id", "")))
                or not re.fullmatch(r"[0-9a-f]{24}", str(hot.get("candidate_id", "")))):
            fail("INVALID_MEDIA_ROUND", "配图仅绑定已有热点稿件及其明确批次，不猜编号。")
        source = meta.get("source_ref")
        external = meta.get("external_source")
        if (not isinstance(external, dict) or external.get("url") != source
                or any(n["meta"].get("hot_selection") != hot or n["meta"].get("source_ref") != source
                       for n in notes)):
            fail("INVALID_MEDIA_ROUND", "三稿的热点来源不一致，未保存配图。")
        _timestamp(hot["fetched_at"], False)
        if hot["fetched_at"] is None:
            fail("INVALID_MEDIA_ROUND", "热点采集时间缺失，未保存配图。")
        host = urlsplit(source).hostname if isinstance(source, str) else None
        platforms = PLATFORMS if current else ARCHIVE_PLATFORMS
        platform = next((key for key, (_, domain) in platforms.items()
                         if host == domain or (host and host.endswith("." + domain))), None)
        if platform is None or validate_hot_url(platform, source, allow_historical=not current) != source:
            fail("INVALID_MEDIA_SOURCE", "本轮热点来源URL不符合安全规则。")
        # Archived reads remain possible after a new menu; new writes must use
        # the currently selected batch and exact saved candidate identity.
        if current:
            selected = Trends(self.runner).select(hot["number"], hot["batch_id"])
            if selected["candidate_id"] != hot["candidate_id"] or selected["url"] != source:
                fail("INVALID_MEDIA_ROUND", "当前热点候选与本轮来源不一致，未保存配图。")
        context = {"round_id": round_id, "hot_selection": dict(hot), "source_ref": source}
        return (context, notes) if with_notes else context

    def _folder(self, round_id):
        _safe_path(self.root)
        return _safe_path(self.root / round_id)

    def _url(self, value, context, cover=False):
        if cover:
            if value != context["source_ref"]:
                fail("INVALID_MEDIA_SOURCE", "首图来源必须与本轮原始热点URL完全一致。")
            # Re-use the validated canonical hot URL; no query stripping.
            return value
        try:
            return self.public_url(value)
        except Exception as exc:
            if hasattr(exc, "code"):
                fail("INVALID_MEDIA_SOURCE", "补充图需无凭证、无跳转参数的公开来源页面URL。")
            raise

    def _items(self, items, context):
        if not isinstance(items, list) or not 1 <= len(items) <= 4:
            fail("INVALID_MEDIA", "每组需1张首图及0至3张补充图，共1至4张；不为凑数加入低质量图片。")
        prepared = []
        for number, item in enumerate(items, 1):
            if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
                fail("INVALID_MEDIA", "配图字段不完整或含不支持的字段。")
            role = "COVER" if number == 1 else "SUPPLEMENT"
            if item["role"] != role or item["title_boxed"] is not (number == 1):
                fail("INVALID_MEDIA", "首图需网页标题红框，后续图片需标为补充图。")
            if item["source_type"] not in ("USER_PROVIDED", "PUBLIC_CAPTURE", "PUBLIC_ORIGINAL"):
                fail("INVALID_MEDIA", "配图仅接受用户提供图、实际公开页面截图或公开原图。")
            if role == "COVER" and item["source_type"] == "PUBLIC_ORIGINAL":
                fail("INVALID_MEDIA", "首图应为网页界面截图，不能用独立原图代替。")
            caption = item["caption"]
            if (not isinstance(caption, str) or not caption.strip() or len(caption) > 300
                    or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in caption)):
                fail("INVALID_MEDIA", "图片用途说明需为300字以内的非空单行文本。")
            self.privacy_check(caption)
            captured = _timestamp(item["captured_at"], False)
            if item["source_type"] != "USER_PROVIDED" and captured is None:
                fail("INVALID_MEDIA", "实际采集的补充图片需记录明确采集时间。")
            published = _timestamp(item["content_published_at"], True)
            source = item["path"]
            if (not isinstance(source, str) or source.startswith(("\\\\", "//"))
                    or not Path(source).is_absolute()):
                fail("UNSAFE_PATH", "待复制图片需使用明确本地绝对路径。")
            if Path(source).suffix.lower() not in (".png", ".jpg", ".jpeg"):
                fail("INVALID_MEDIA_IMAGE", "待使用文件需为PNG或JPEG图片后缀，未读取非图片文件。")
            path = _safe_path(Path(source), must_file=True)
            if path.stat().st_size > MAX_BYTES:
                fail("INVALID_MEDIA_IMAGE", "图片超过16MiB上限。")
            data = path.read_bytes()
            info, suffix = image_info(data)
            sha = hashlib.sha256(data).hexdigest()
            saved = {"number": number, "file": f"{number:02d}-{sha[:24]}{suffix}",
                     "sha256": sha, **info, "role": role,
                     "source_url": self._url(item["source_url"], context, number == 1),
                     "source_type": item["source_type"], "caption": caption.strip(),
                     "captured_at": captured, "content_published_at": published,
                     "title_boxed": number == 1, "reuse_review": "USER_REQUESTED_REVIEW"}
            prepared.append((saved, data))
        if len({saved["sha256"] for saved, _ in prepared}) != len(prepared):
            fail("INVALID_MEDIA", "配图包含相同字节的重复图片，不能当作多张补充。")
        return prepared

    def _load(self, context):
        folder = self._folder(context["round_id"])
        path = _safe_path(folder / "manifest.json")
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_MANIFEST:
            fail("MEDIA_INVALID", "配图记录文件异常，未覆盖。")
        value = json.loads(path.read_text(encoding="utf-8"))
        required = {"schema_version", "moments_media", "round_id", "hot_selection", "source_ref", "recorded_at", "items"}
        if (not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1
                or isinstance(value["schema_version"], bool) or value["moments_media"] != MARKER
                or any(value[key] != context[key] for key in ("round_id", "hot_selection", "source_ref"))
                or not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= 4):
            fail("MEDIA_INVALID", "配图记录归属或结构与本轮不符，未覆盖。")
        _timestamp(value["recorded_at"], False)
        if value["recorded_at"] is None:
            fail("MEDIA_INVALID", "配图记录缺少创建时间。")
        seen = set()
        for number, row in enumerate(value["items"], 1):
            self._saved_row(context, row, number, seen)
        return value

    def _saved_row(self, context, row, number, seen):
        """Validate immutable file identity, including historical usage rows."""
        if (not isinstance(row, dict) or set(row) != SAVED_FIELDS
                or isinstance(row["number"], bool) or row["number"] != number
                or row["role"] != ("COVER" if number == 1 else "SUPPLEMENT")
                or row["title_boxed"] is not (number == 1)
                or row["source_type"] not in ("USER_PROVIDED", "PUBLIC_CAPTURE", "PUBLIC_ORIGINAL")
                or (number == 1 and row["source_type"] == "PUBLIC_ORIGINAL")
                or row["reuse_review"] != "USER_REQUESTED_REVIEW"
                or not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or row["sha256"] in seen):
            fail("MEDIA_INVALID", "配图条目顺序、来源或校验字段异常。")
        seen.add(row["sha256"])
        self._url(row["source_url"], context, number == 1)
        _timestamp(row["captured_at"], False)
        _timestamp(row["content_published_at"], True)
        if row["source_type"] != "USER_PROVIDED" and row["captured_at"] is None:
            fail("MEDIA_INVALID", "公开采集图片缺少采集时间。")
        if (not isinstance(row["caption"], str) or not row["caption"].strip() or len(row["caption"]) > 300
                or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in row["caption"])):
            fail("MEDIA_INVALID", "配图用途说明异常。")
        self.privacy_check(row["caption"])
        if not isinstance(row["file"], str) or not re.fullmatch(rf"{number:02d}-[0-9a-f]{{24}}\.(?:png|jpg)", row["file"]):
            fail("MEDIA_INVALID", "配图文件名异常，拒绝越界路径。")
        target = _safe_path(self._folder(context["round_id"]) / row["file"], must_file=True)
        if target.stat().st_size > MAX_BYTES:
            fail("MEDIA_INVALID", "归档图片超过大小限制。")
        data = target.read_bytes()
        info, suffix = image_info(data)
        if (hashlib.sha256(data).hexdigest() != row["sha256"] or row["file"] != f"{number:02d}-{row['sha256'][:24]}{suffix}"
                or any(row[key] != info[key] or isinstance(row[key], bool) for key in info)):
            fail("MEDIA_HASH_MISMATCH", "归档图片已变化或尺寸记录不符，需核对原图后重试。")

    def _usage(self, context, note):
        value = note["meta"].get("publication_media")
        if value is None:
            return None
        if (note["meta"]["tags"][0] != "已发布" or not isinstance(value, dict)
                or set(value) != {"basis", "scope", "confirmed_at", "items"}
                or value["basis"] != "USER_REPORTED" or value["scope"] != "USER_REPORTED_SUBSET"
                or value["confirmed_at"] is None or not isinstance(value["items"], list)
                or not 1 <= len(value["items"]) <= 4):
            fail("MEDIA_USAGE_INVALID", "本人采用图片记录异常，未猜测或覆盖。")
        _timestamp(value["confirmed_at"], False)
        seen, numbers = set(), set()
        for row in value["items"]:
            number = row.get("number") if isinstance(row, dict) else None
            if type(number) is not int or not 1 <= number <= 4 or number in numbers:
                fail("MEDIA_USAGE_INVALID", "本人采用图片编号异常或重复。")
            numbers.add(number)
            self._saved_row(context, row, number, seen)
        return value

    def _usages(self, context):
        # Recheck the whole round: absent candidates are acceptable only when
        # the surviving notes carry consistent retirement evidence. Preserve
        # the existing variant keys rather than renumbering a sparse round.
        verified, notes = self._round(context["round_id"], with_notes=True)
        if verified != context:
            fail("INVALID_MEDIA_ROUND", "稿件热点来源已变化，未读取或写入采用记录。")
        result = {str(variant): None for variant in (1, 2, 3)}
        for note in notes:
            result[str(note["meta"]["variant"])] = self._usage(context, note)
        return result

    def _public(self, context, value, reused=False):
        folder = self._folder(context["round_id"])
        return {"status": "OK", "found": value is not None, "round_id": context["round_id"],
                "reused": reused, "manifest_path": str(folder / "manifest.json") if value else None,
                "image_count": len(value["items"]) if value else 0,
                "media": value, "images": [{**row, "path": str(folder / row["file"])} for row in value["items"]] if value else [],
                "publication_media": self._usages(context),
                "notice": NOTICE}

    @_errors
    @serialized
    def used(self, round_id, variant, numbers, replace=False):
        """Record only the explicitly reported subset, after publication."""
        if type(variant) is not int or variant not in (1, 2, 3) or not isinstance(replace, bool):
            fail("INVALID_MEDIA_USAGE", "稿件编号需为1至3，replace需为明确布尔值。")
        if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 4
                or any(type(number) is not int or not 1 <= number <= 4 for number in numbers)
                or len(set(numbers)) != len(numbers)):
            fail("INVALID_MEDIA_USAGE", "采用图片需为本人明确报告的非空、不重复整数编号列表。")
        context = self._round(round_id)
        value = self._load(context)
        if value is None or any(number > len(value["items"]) for number in numbers):
            fail("INVALID_MEDIA_USAGE", "所报图片编号不在本轮已核验准备清单中。")
        # Validate all pre-existing receipts before any write. Their immutable
        # files may differ from a later prepared manifest at the same number.
        self._usages(context)
        path = _safe_path(self.runner.archive / f"{round_id}-v{variant}.md", must_file=True)
        note = self.runner._read_note(path)
        if note["meta"]["tags"][0] != "已发布":
            fail("MEDIA_NOT_PUBLISHED", "仅本人已明确确认发布的指定稿件可记录采用图片。")
        rows = [dict(value["items"][number - 1]) for number in numbers]
        previous = self._usage(context, note)
        if previous is not None and previous["items"] == rows:
            return {**self._public(context, value, reused=True), "action": "media-used",
                    "variant": variant, "changed": False}
        if previous is not None and not replace:
            fail("MEDIA_USAGE_REPLACE_REQUIRED", "该稿已有不同采用记录；仅本人明确更正后传replace=true。")
        receipt = {"basis": "USER_REPORTED", "scope": "USER_REPORTED_SUBSET",
                   "confirmed_at": self.now(), "items": rows}
        _timestamp(receipt["confirmed_at"], False)
        if receipt["confirmed_at"] is None:
            fail("INVALID_MEDIA_USAGE", "采用确认时间不可为空。")
        # Replace only this one JSON-compatible frontmatter line. Keep all
        # other metadata, headings, whitespace and body bytes exactly as read.
        original = path.read_bytes().decode("utf-8")
        front, separator, content = original.partition("\n---\n")
        if not separator or not front.startswith("---\n"):
            fail("MEDIA_USAGE_INVALID", "稿件frontmatter边界异常，未修改。")
        line = "publication_media: " + json.dumps(receipt, ensure_ascii=False)
        lines = front.split("\n")
        positions = [i for i, old in enumerate(lines) if old.startswith("publication_media:")]
        if len(positions) > 1:
            fail("MEDIA_USAGE_INVALID", "稿件采用图片字段重复，未修改。")
        if positions:
            lines[positions[0]] = line
        else:
            lines.append(line)
        updated = "\n".join(lines) + separator + content
        if len(updated.encode("utf-8")) > 65536:
            fail("MEDIA_USAGE_INVALID", "采用图片记录会使稿件超过大小限制，未修改。")
        self.runner._backup(path)
        self.atomic(path, updated)
        return {**self._public(context, value), "action": "media-used", "variant": variant, "changed": True}

    @_errors
    @serialized
    def get(self, round_id):
        context = self._round(round_id)
        return self._public(context, self._load(context))

    @_errors
    @serialized
    def save(self, round_id, items, replace=False):
        if not isinstance(replace, bool):
            fail("INVALID_MEDIA", "replace需为明确布尔值。")
        context = self._round(round_id, current=True)
        self._usages(context)
        prepared = self._items(items, context)
        previous = self._load(context)
        rows = [row for row, _ in prepared]
        if previous is not None and previous["items"] == rows:
            return self._public(context, previous, reused=True)
        if previous is not None and not replace:
            fail("MEDIA_REPLACE_REQUIRED", "本轮已有不同配图；仅在本人明确修改后传replace=true。")
        folder = self._folder(round_id)
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "manifest.json"
        value = {"schema_version": 1, "moments_media": MARKER, **context,
                 "recorded_at": self.now(), "items": rows}
        created = []
        try:
            if previous is not None:
                self.runner._backup(manifest)
            for row, data in prepared:
                target = _safe_path(folder / row["file"])
                if target.exists():
                    if not target.is_file() or target.read_bytes() != data:
                        fail("MEDIA_HASH_MISMATCH", "已有同名图片内容异常，未覆盖。")
                    continue
                handle = target.open("xb")
                # Ownership begins as soon as exclusive open succeeds. A failed
                # write/flush/fsync must not leave a partial content-addressed
                # file that makes the same request impossible to retry.
                created.append(target)
                with handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            self.atomic(manifest, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", exclusive=previous is None)
        except BaseException:
            for target in created:
                target.unlink(missing_ok=True)
            raise
        return self._public(context, self._load(context))
