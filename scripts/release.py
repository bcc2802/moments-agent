"""Audit an exact release allowlist and build a local, reproducible ZIP.

No Git/network operation is performed. Unlisted files are never read or packed.
The resulting archive does not establish a license or publish the project.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from urllib.parse import unquote, urlsplit
import zipfile


CODE_ROOT = Path(__file__).resolve().parents[1]
SPEC_NAME = "release-files.json"
METADATA_NAME = "release.json"
MAX_FILES = 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_ENTRIES = 4096
_PRIVATE_COMPONENTS = {".git", ".obsidian", "backups", "runs", "iterations", "inbox",
                       "archive", "personal", "inputs", "_tasks", "_workflow", "_feedback", "jobs",
                       "node_modules", ".wrangler", "output"}
# These directories are never inventoried, even when a local install exists.
# Explicit allowlist entries below them still fail the private-path check.
_INVENTORY_SKIP_COMPONENTS = {".git", "node_modules", ".wrangler", "output"}
_PRIVATE_BASENAMES = {"config.json", "state.json", "trendradar.local.json", "moments.workspace.local.json"}
_PRIVATE_EXTENSIONS = {".sqlite", ".sqlite3", ".db", ".log", ".zip", ".pem", ".key", ".pfx", ".p12"}
_CREDENTIAL_NAME = re.compile(r"(?:^|[._ -])(?:env|credentials?|secrets?|tokens?|passwords?|api[_-]?keys?|id_rsa|id_ed25519)(?:[._ -]|$)", re.I)
_PRIVATE_PATH = re.compile(
    r"(?i)[a-z]:[\\/]+(?:users[\\/]+(?!public(?:[\\/]|$))[^\\/\s\"']+|"
    r"(?:AI-Projects|projects|repos|workspaces)[\\/]+[^\\/\s\"']+)"
)
_DOCUMENT_ABSOLUTE_PATH = re.compile(r"(?i)\b[a-z]:[\\/]+[^\s\"'<>]+")
_CREDENTIAL_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|xox[baprs]-[A-Za-z0-9-]{20,})\b|"
    r"(?im:^\s*[\"']?(?:api[_-]?key|app[_-]?secret|access[_-]?token|refresh[_-]?token)"
    r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{24,})"
)


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise ReleaseError(code, message)


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _plain(path, root=None, missing_ok=False):
    path = Path(path)
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path and not missing_ok:
                fail("MISSING_FILE", "缺少白名单文件或输出父目录。")
            continue
        except OSError:
            fail("READ_FAILED", "无法检查文件路径。")
        if _linked(info):
            fail("LINKED_PATH", "拒绝符号链接、目录联接或重解析路径。")
    resolved = path.resolve(strict=False)
    if root is not None and not _inside(resolved, root):
        fail("PATH_ESCAPE", "路径离开候选目录。")
    return resolved


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or value.startswith("/") or any(ord(ch) < 32 for ch in value)):
        fail("INVALID_ALLOWLIST_PATH", "白名单只能使用普通相对文件路径。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")) or str(path) != value:
        fail("INVALID_ALLOWLIST_PATH", "白名单不能包含绝对路径、父级跳转或非规范路径。")
    return value


def _private(relative):
    path = PurePosixPath(relative)
    return (any(part.casefold() in _PRIVATE_COMPONENTS for part in path.parts)
            or path.name.casefold() in _PRIVATE_BASENAMES
            or path.name.casefold() == ".dev.vars" or path.name.casefold().startswith(".dev.vars.")
            or path.suffix.casefold() in _PRIVATE_EXTENSIONS
            or any(_CREDENTIAL_NAME.search(part) for part in path.parts)
            or relative.casefold().startswith(".codex/"))


def _read(path, root, limit=MAX_FILE_BYTES):
    path = _plain(path, root)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            fail("NOT_REGULAR_FILE", "白名单条目必须是普通文件。")
        if before.st_nlink != 1:
            fail("HARDLINKED_FILE", "拒绝硬链接文件。")
        if before.st_size > limit:
            fail("FILE_TOO_LARGE", "文件超过发行检查上限。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
                fail("FILE_CHANGED", "检查时文件身份发生变化。")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > limit:
            fail("FILE_TOO_LARGE", "文件超过发行检查上限。")
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            fail("FILE_CHANGED", "检查期间文件发生变化。")
        _plain(path, root)
        return raw
    except OSError:
        fail("READ_FAILED", "无法读取白名单文件。")


def _spec(root):
    raw = _read(root / SPEC_NAME, root, limit=256 * 1024)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        fail("INVALID_ALLOWLIST", "release-files.json必须是UTF-8 JSON。")
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or set(value) - {"schema_version", "files", "ignored_paths"}
            or not isinstance(value.get("files"), list) or not 1 <= len(value["files"]) <= MAX_FILES):
        fail("INVALID_ALLOWLIST", "白名单须包含schema_version=1和非空files列表。")
    files = [_relative(item) for item in value["files"]]
    if len(set(name.casefold() for name in files)) != len(files):
        fail("DUPLICATE_ALLOWLIST_PATH", "白名单文件不能重复或仅大小写不同。")
    if "MANIFEST.json".casefold() in {name.casefold() for name in files}:
        fail("RESERVED_MANIFEST_PATH", "MANIFEST.json由打包工具生成，不能列入白名单。")
    ignored = value.get("ignored_paths", [])
    if not isinstance(ignored, list) or len(ignored) > 32:
        fail("INVALID_IGNORE_PATTERN", "ignored_paths必须是有界的cache/tmp模式列表。")
    for pattern in ignored:
        if (not isinstance(pattern, str) or not pattern or pattern.startswith("/")
                or "\\" in pattern or ":" in pattern or ".." in pattern.split("/")
                or any(ord(ch) < 32 for ch in pattern)
                or not any(part in {"__pycache__", ".pytest_cache", "tmp", "temp"} for part in pattern.split("/"))
                and pattern not in {".git/**", "**/node_modules/**", "**/.wrangler/**"}
                and not pattern.endswith("*.pyc")):
            fail("INVALID_IGNORE_PATTERN", "只允许明确的Git、依赖、cache、tmp或pyc忽略模式。")
    return sorted(files), ignored


def _matches(path, pattern):
    return fnmatch.fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))


def _inventory(root, files, patterns):
    warnings, ignored, count = [], [], 0
    pending = [root]
    allowed = set(files)
    prefixes = {str(PurePosixPath(name).parent) for name in files}
    for pattern in patterns:
        parts = []
        for part in pattern.split("/"):
            if any(ch in part for ch in "*?["):
                break
            parts.append(part)
        if parts:
            prefixes.add("/".join(parts))
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as stream:
                for entry in stream:
                    count += 1
                    if count > MAX_INVENTORY_ENTRIES:
                        warnings.append({"code": "INVENTORY_TRUNCATED"})
                        return warnings, ignored
                    path = Path(entry.path)
                    relative = path.relative_to(root).as_posix()
                    if entry.name.casefold() in _INVENTORY_SKIP_COMPONENTS:
                        ignored.append(relative + "/")
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                    candidate = relative + "/" if is_directory else relative
                    if any(_matches(relative, pattern) or _matches(candidate, pattern) for pattern in patterns):
                        ignored.append(candidate)
                        continue
                    if entry.is_symlink() or _linked(entry.stat(follow_symlinks=False)):
                        if relative not in allowed:
                            warnings.append({"code": "EXTRA_LINK_NOT_PACKED", "path": relative})
                        continue
                    if is_directory:
                        if any(prefix == relative or prefix.startswith(relative + "/") for prefix in prefixes):
                            pending.append(path)
                        else:
                            warnings.append({"code": "EXTRA_NOT_PACKED", "path": candidate})
                    elif relative not in allowed:
                        warnings.append({"code": "EXTRA_NOT_PACKED", "path": relative})
        except OSError:
            warnings.append({"code": "EXTRA_DIRECTORY_UNREADABLE", "path": directory.relative_to(root).as_posix()})
    return sorted(warnings, key=lambda item: (item.get("path", ""), item["code"])), sorted(ignored)


def _markdown_targets(text):
    # Do not interpret example code as live documentation links.
    lines, fenced, fence = [], False, None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            token = match[1][0]
            if not fenced:
                fenced, fence = True, token
            elif token == fence:
                fenced, fence = False, None
            continue
        if not fenced:
            lines.append(re.sub(r"`[^`]*`", "", line))
    clean = "\n".join(lines)
    for match in re.finditer(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+['\"][^\n]*?['\"])?\s*\)", clean):
        yield match[1].strip("<>")
    for match in re.finditer(r"(?m)^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", clean):
        yield match[1].strip("<>")


def _link_issue(source, target, root, allowed):
    if target.startswith("#"):
        return None
    target = re.sub(r"(\.[A-Za-z0-9]+):\d+(?:-\d+)?(?=[?#]|$)", r"\1", target)
    parsed = urlsplit(target)
    if parsed.scheme.lower() in {"http", "https", "mailto", "tel"}:
        return None
    if parsed.scheme or target.startswith(("/", "\\")):
        return "UNSAFE_MARKDOWN_LINK"
    path = unquote(parsed.path)
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    if not path:
        return None
    if "\\" in path or ":" in path or any(ord(ch) < 32 for ch in path):
        return "UNSAFE_MARKDOWN_LINK"
    target_path = root / PurePosixPath(source).parent / path
    if not _inside(target_path.resolve(strict=False), root):
        return "MARKDOWN_LINK_ESCAPE"
    try:
        resolved = _plain(target_path, root)
    except ReleaseError as exc:
        return "MARKDOWN_LINK_ESCAPE" if exc.code == "PATH_ESCAPE" else "BROKEN_MARKDOWN_LINK"
    relative = resolved.relative_to(root).as_posix()
    if resolved.is_dir():
        return None if any(name.startswith(relative.rstrip("/") + "/") for name in allowed) else "MARKDOWN_LINK_NOT_IN_RELEASE"
    return None if relative in allowed else "MARKDOWN_LINK_NOT_IN_RELEASE"


def _release_metadata(payloads):
    """Derive distribution labels from reviewed files, never from local config."""
    fallback = {"release_kind": "release-candidate", "version": None,
                "license": None, "license_status": "not-established-by-this-package"}
    if METADATA_NAME not in payloads:
        return fallback, []
    issue = {"code": "INVALID_RELEASE_METADATA", "path": METADATA_NAME}
    try:
        metadata = json.loads(payloads[METADATA_NAME].decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return fallback, [issue]
    if (not isinstance(metadata, dict) or set(metadata) != {"schema_version", "version", "license"}
            or metadata.get("schema_version") != 1 or metadata.get("license") != "MIT"
            or not isinstance(metadata.get("version"), str)
            or re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", metadata["version"]) is None):
        return fallback, [issue]
    if "LICENSE" not in payloads:
        return fallback, [{"code": "LICENSE_MISSING", "path": "LICENSE"}]
    try:
        license_text = " ".join(payloads["LICENSE"].decode("utf-8-sig").split())
    except UnicodeError:
        license_text = ""
    clauses = (
        "MIT License",
        "Permission is hereby granted, free of charge, to any person obtaining a copy",
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell",
        "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.",
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED',
        "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY",
    )
    if not all(clause in license_text for clause in clauses):
        return fallback, [{"code": "LICENSE_MISMATCH", "path": "LICENSE"}]
    return {"release_kind": "release", "version": metadata["version"],
            "license": "MIT", "license_status": "MIT"}, []


def _audit(root=None):
    root = _plain(Path(root or CODE_ROOT).resolve())
    files, patterns = _spec(root)
    issues, records, payloads, total = [], [], {}, 0
    for relative in files:
        if _private(relative):
            issues.append({"code": "PRIVATE_FILE_IN_ALLOWLIST", "path": relative})
            continue
        try:
            raw = _read(root / relative, root, limit=MAX_FILE_BYTES)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                fail("RELEASE_TOO_LARGE", "发行文件总大小超过上限。")
            records.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
            payloads[relative] = raw
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                if PurePosixPath(relative).suffix.lower() in {".py", ".md", ".json", ".txt", ".toml", ".yml", ".yaml"}:
                    fail("TEXT_ENCODING", "文本文件必须是UTF-8。")
                continue
            suffix = PurePosixPath(relative).suffix.lower()
            if _PRIVATE_PATH.search(text) or (suffix in {".md", ".json", ".toml", ".yaml", ".yml"}
                                             and _DOCUMENT_ABSOLUTE_PATH.search(text)):
                issues.append({"code": "PRIVATE_MACHINE_PATH", "path": relative})
            if _CREDENTIAL_CONTENT.search(text):
                issues.append({"code": "POSSIBLE_CREDENTIAL", "path": relative})
            if suffix == ".py":
                try:
                    ast.parse(text, filename=relative)
                except (SyntaxError, ValueError):
                    issues.append({"code": "PYTHON_SYNTAX", "path": relative})
            elif suffix == ".md":
                for target in _markdown_targets(text):
                    try:
                        issue = _link_issue(relative, target, root, set(files))
                    except (ValueError, OSError):
                        issue = "BROKEN_MARKDOWN_LINK"
                    if issue:
                        issues.append({"code": issue, "path": relative})
        except ReleaseError as exc:
            issues.append({"code": exc.code, "path": relative})
    warnings, ignored = _inventory(root, files, patterns)
    metadata, metadata_issues = _release_metadata(payloads)
    issues.extend(metadata_issues)
    unique_issues = [dict(code=code, path=path) for code, path in sorted({(item["code"], item["path"]) for item in issues})]
    report = {"status": "OK" if not unique_issues else "FAILED", **metadata, "files": records,
              "file_count": len(records), "total_bytes": total, "issues": unique_issues,
              "warnings": warnings, "ignored_paths": ignored,
              "notice": "仅审核白名单；警告项和忽略项不入包。检测不替代人工权利与隐私审查。"}
    return report, payloads


def check(root=None):
    return _audit(root)[0]


def _output_path(value, root):
    raw = os.fspath(value)
    path = Path(raw)
    if (not path.is_absolute() or ".." in path.parts or raw.startswith(("\\\\", "//"))
            or ":" in raw[len(path.drive):] or any(ord(ch) < 32 for ch in raw)):
        fail("INVALID_OUTPUT_PATH", "输出必须是明确的本机绝对ZIP路径。")
    result = _plain(path, missing_ok=True)
    if _inside(result, root):
        fail("OUTPUT_IN_CODE", "ZIP必须位于候选代码目录之外。")
    if result.suffix.lower() != ".zip":
        fail("INVALID_OUTPUT_PATH", "输出文件必须使用.zip后缀。")
    parent = _plain(result.parent)
    if not parent.is_dir():
        fail("INVALID_OUTPUT_PATH", "输出父目录必须存在。")
    return result


def _manifest(report):
    return {"schema_version": 1, **{name: report[name] for name in ("release_kind", "version", "license", "license_status")},
            "files": report["files"],
            "file_count": report["file_count"], "total_bytes": report["total_bytes"],
            "notice": "本工具仅在本地打包，不上传或发布；许可以随包LICENSE及docs/LICENSING.md为准。MANIFEST.json不包含自身哈希。"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def pack(value, root=None):
    root = _plain(Path(root or CODE_ROOT).resolve())
    output = _output_path(value, root)
    sidecar = output.with_name(output.name + ".manifest.json")
    if os.path.lexists(output) or os.path.lexists(sidecar):
        fail("OUTPUT_EXISTS", "ZIP或旁边的manifest已存在；打包不会覆盖。")
    report, payloads = _audit(root)
    if report["status"] != "OK":
        return {**report, "archive_created": False}
    manifest = _manifest(report)
    manifest_bytes = _json(manifest).encode("utf-8")
    created = []
    try:
        with output.open("xb") as archive_stream:
            created.append((output, os.fstat(archive_stream.fileno())))
            with sidecar.open("xb") as manifest_stream:
                created.append((sidecar, os.fstat(manifest_stream.fileno())))
                with zipfile.ZipFile(archive_stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                    for name, raw in sorted({**payloads, "MANIFEST.json": manifest_bytes}.items()):
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                manifest_stream.write(manifest_bytes)
        archive_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    except (OSError, zipfile.BadZipFile):
        # Remove only files opened exclusively by this attempt and still having
        # the same identity; never delete a pre-existing or swapped path.
        for path, identity in reversed(created):
            try:
                current = path.lstat()
                if (not _linked(current) and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
                        and path.resolve().parent == output.parent):
                    path.unlink()
            except OSError:
                pass
        fail("PACK_FAILED", "无法完整创建候选包；未覆盖已有输出。")
    return {"status": "OK", "release_kind": report["release_kind"], "version": report["version"],
            "license": report["license"], "archive": str(output),
            "manifest": str(sidecar), "archive_sha256": archive_sha,
            "file_count": report["file_count"], "warnings": report["warnings"],
            "license_status": manifest["license_status"], "archive_created": True}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    package = commands.add_parser("pack")
    package.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = check() if args.command == "check" else pack(args.output)
        print(_json(report), end="")
        return 0 if report["status"] == "OK" else 1
    except ReleaseError as exc:
        print(_json({"status": "ERROR", "code": exc.code, "message": str(exc), "archive_created": False}), end="")
        return 1
    except OSError:
        print(_json({"status": "ERROR", "code": "RELEASE_IO_ERROR",
                     "message": "无法完成发行检查或打包。", "archive_created": False}), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
