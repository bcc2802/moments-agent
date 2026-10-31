"""Plan narrow updates to an existing owned catalog in the draft transaction.

The catalog links to Markdown; it never duplicates draft bodies or publication
state. Projects without a catalog retain their existing behavior.
"""
from pathlib import Path
import re

from moments_retention import safe_path


def catalog_cleanup_changes(retention, changes, deletes):
    """Remove retired variant bullets in every existing cleanup transaction."""
    from moments import fail, now
    path = retention.runner.archive / "00_朋友圈内容库.md"
    if not deletes:
        return {}
    safe_path(path)
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 262144:
        fail("UNSAFE_PATH", "朋友圈专属入口异常，未执行清理。")
    before = path.read_bytes()
    data = changes.get(path, before)
    try:
        source = data.decode("utf-8-sig")
    except UnicodeError:
        fail("NOT_OWNED", "朋友圈专属入口编码异常，未执行清理。")
    newline = "\r\n" if source.startswith("---\r\n") else "\n"
    text = source.replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n# 朋友圈内容库\n" not in text:
        fail("NOT_OWNED", "朋友圈专属入口不属于本Agent，未执行清理。")
    result = text
    for removed in deletes:
        result = re.sub(r"(?m)^- \[\[" + re.escape(removed.stem)
                        + r"(?:\|[^\]\n]*)?\]\][^\n]*(?:\n|$)", "", result)
    if result == text:
        return {}
    head, separator, tail = result[4:].partition("\n---\n")
    if not separator:
        fail("NOT_OWNED", "朋友圈专属入口元数据格式异常，未执行清理。")
    head = re.sub(r"(?m)^updated:[^\n]*", "updated: " + now()[:10], head, count=1)
    result = "---\n" + head + separator + tail
    retention.expected.setdefault(path, before)
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return {path: bom + result.replace("\n", newline).encode("utf-8")}


def catalog_changes(retention, notes, retired):
    from moments import fail, now
    runner = retention.runner
    path = runner.archive / "00_朋友圈内容库.md"
    safe_path(path)
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 262144:
        fail("UNSAFE_PATH", "朋友圈专属入口路径或大小异常，未写入稿件。")
    before = path.read_bytes()
    try:
        source = before.decode("utf-8-sig")
    except UnicodeError:
        fail("NOT_OWNED", "朋友圈专属入口编码异常，未覆盖。")
    newline = "\r\n" if source.startswith("---\r\n") else "\n"
    original = source.replace("\r\n", "\n")
    if not original.startswith("---\n") or "\n# 朋友圈内容库\n" not in original:
        fail("NOT_OWNED", "朋友圈专属入口不属于本Agent，未覆盖。")
    if not notes:
        return {}
    rid = notes[0]["meta"]["round_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", rid):
        fail("STATE_INVALID", "入口轮号格式异常，未写入。")
    text = original
    for row in retired:
        stem = Path(row["path"]).stem
        # Only the dedicated bullet for the removed original variant is removed.
        text = re.sub(r"(?m)^- \[\[" + re.escape(stem)
                      + r"(?:\|[^\]\n]*)?\]\][^\n]*(?:\n|$)", "", text)
    start, end = f"<!-- moments-round:{rid} -->", f"<!-- /moments-round:{rid} -->"
    stamp = notes[0]["meta"]["created_at"][:10]
    stems = [note["path"].stem for note in notes]
    if start in text or not all("[[" + stem + "]]" in text for stem in stems):
        lines = [start, f"## 本轮朋友圈（{stamp}）", "", f"轮号：`{rid}`。正文和发布状态以各稿Markdown为准。", ""]
        for note in notes:
            if note["meta"]["round_id"] != rid or note["path"].parent != runner.archive:
                fail("STATE_INVALID", "入口稿件归属或轮号不一致，未写入。")
            lines.append(f'- [[{note["path"].stem}]]：原第{note["meta"]["variant"]}稿，{note["meta"]["angle"]}。')
        requirement = notes[0]["meta"].get("media_requirement", "DEFAULT")
        labels = {"NO_SCREENSHOT": "本轮不截图。", "SKIP_ALL": "本轮不配图。",
                  "REUSE_ONLY": "本轮仅复用已有图片。"}
        lines.extend(["", labels.get(requirement, "配图按本轮明确要求执行。"), end])
        block = "\n".join(lines)
        if start in text:
            pattern = re.escape(start) + r".*?" + re.escape(end)
            if text.count(start) != 1 or text.count(end) != 1 or not re.search(pattern, text, re.S):
                fail("STATE_INVALID", "入口轮次记录损坏，未覆盖。")
            text = re.sub(pattern, lambda _: block, text, count=1, flags=re.S)
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    if text == original:
        return {}
    head, separator, tail = text[4:].partition("\n---\n")
    if not separator:
        fail("NOT_OWNED", "朋友圈专属入口元数据格式异常，未覆盖。")
    head = re.sub(r"(?m)^updated:[^\n]*", "updated: " + now()[:10], head, count=1)
    text = "---\n" + head + separator + tail
    bom = b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b""
    result = bom + text.replace("\n", newline).encode("utf-8")
    safe_path(path)
    retention.expected[path] = before
    return {path: result} if result != before else {}
