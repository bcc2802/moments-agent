"""Bounded local screenshot previews and lossless, replayable image edits.

Uses the existing bundled Pillow runtime, never a browser/network. Coordinates
are integer source pixels, [left, top, right, bottom], with exclusive right and
bottom edges. ``run_job`` raises ImageOpsError; the CLI emits JSON on stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import warnings


MAX_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024
MAX_SIDE = 20000
MAX_PIXELS = 80000000
MAX_TOTAL_PIXELS = 160000000
MAX_REQUEST_BYTES = 128 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
JOB_FIELDS = {"action", "output_dir", "captures", "candidates"}
CANDIDATE_FIELDS = {"candidate_id", "path", "role", "crop", "title_box", "boxed"}


class ImageOpsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise ImageOpsError(code, message)


def _path(value, *, file=False):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith(("\\\\", "//")) or any(ord(ch) < 32 for ch in value)):
        fail("UNSAFE_PATH", "Use an explicit local absolute path, without device/network prefixes.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        fail("UNSAFE_PATH", "Relative paths and parent traversal are not allowed.")
    # Disallow Windows alternate data streams, including on non-Windows hosts.
    remainder = value[len(path.drive):]
    if ":" in remainder:
        fail("UNSAFE_PATH", "Alternate data streams are not allowed.")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            fail("UNSAFE_PATH", "Source/output paths and their parents must not be links or reparse points.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            fail("UNSAFE_PATH", "Hard-linked files are not allowed.")
        if part != path and not stat.S_ISDIR(info.st_mode):
            fail("UNSAFE_PATH", "A path parent is not a directory.")
        if part == path:
            if file and not stat.S_ISREG(info.st_mode):
                fail("INVALID_IMAGE", "Source must be a regular image file.")
            if not file and not stat.S_ISDIR(info.st_mode):
                fail("UNSAFE_PATH", "Output must be a directory.")
    if file and not path.is_file():
        fail("IMAGE_MISSING", "The local source file does not exist.")
    return path


def _pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        fail("PILLOW_UNAVAILABLE", "Run this worker with the existing bundled Python/Pillow runtime.")
    return Image, ImageDraw, ImageFont


def _decode(data):
    Image, _, _ = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ("PNG", "JPEG") or getattr(source, "n_frames", 1) != 1:
                    fail("INVALID_IMAGE", "Only single-frame PNG and JPEG images are accepted.")
                if source.format == "JPEG":
                    _jpeg_scan(data)
                width, height = source.size
                if (min(width, height) < 1 or max(width, height) > MAX_SIDE
                        or width * height > MAX_PIXELS):
                    fail("IMAGE_LIMIT", "Image dimensions exceed the bounded local worker limits.")
                suffix = ".png" if source.format == "PNG" else ".jpg"
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                source.load()  # Includes entropy/pixel decoding, not merely header checks.
                return source.copy(), suffix
    except ImageOpsError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail("INVALID_IMAGE", "Image data is corrupt or cannot be fully decoded as PNG/JPEG.")


def _jpeg_scan(data):
    # libjpeg can conceal a wholly missing entropy stream with synthesized
    # gray pixels. Reject that structural failure before its permissive decode.
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        fail("INVALID_IMAGE", "JPEG framing is incomplete.")
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xff:
            break
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in range(0xd0, 0xd9) or marker == 1:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        position += length
        if marker == 0xda:
            if position < len(data) - 2 and (data[position] != 0xff or data[position + 1] == 0):
                return
            break
    fail("INVALID_IMAGE", "JPEG has no complete initial entropy scan.")


def _read_image(path):
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        fail("INVALID_IMAGE", "Source filenames must have a PNG/JPEG suffix.")
    if path.stat().st_size > MAX_BYTES:
        fail("IMAGE_LIMIT", "A source image exceeds 16 MiB.")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        fail("IMAGE_LIMIT", "A source image is empty or exceeds 16 MiB.")
    return data


def _rect(value, size):
    if (not isinstance(value, list) or len(value) != 4
            or any(type(n) is not int for n in value)):
        fail("INVALID_RECT", "Rectangles require four integer source pixels; booleans are not coordinates.")
    left, top, right, bottom = value
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        fail("INVALID_RECT", "Rectangle is empty or outside the source image.")
    return tuple(value)


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        fail("INVALID_JOB", "Capture/candidate IDs must be 1-80 ASCII letters, digits, underscores or hyphens.")
    return value


def _validate(job):
    if not isinstance(job, dict) or set(job) != JOB_FIELDS or job["action"] not in ("preview", "render"):
        fail("INVALID_JOB", "Provide exactly action, output_dir, captures and candidates.")
    output = _path(job["output_dir"])
    captures, candidates = job["captures"], job["candidates"]
    if not isinstance(captures, list) or not isinstance(candidates, list):
        fail("INVALID_JOB", "captures and candidates must be lists.")
    if job["action"] == "preview":
        if not 1 <= len(captures) <= 8 or candidates:
            fail("INVALID_JOB", "Preview needs 1-8 captures and no candidates.")
        rows, id_key = captures, "capture_id"
    else:
        if not 1 <= len(candidates) <= 12 or captures:
            fail("INVALID_JOB", "Render needs 1-12 candidates and no captures.")
        rows, id_key = candidates, "candidate_id"
    seen, assets, total_bytes, total_pixels = set(), {}, 0, 0
    validated = []
    for row in rows:
        fields = {"capture_id", "path"} if id_key == "capture_id" else CANDIDATE_FIELDS
        if not isinstance(row, dict) or set(row) != fields:
            fail("INVALID_JOB", "Capture/candidate fields are missing or unsupported.")
        identity = _identifier(row[id_key])
        if identity in seen:
            fail("INVALID_JOB", "IDs must be unique within a job.")
        seen.add(identity)
        path = _path(row["path"], file=True)
        if path not in assets:
            data = _read_image(path)
            image, suffix = _decode(data)
            size, mode = image.size, image.mode
            image.close()
            total_bytes += len(data)
            total_pixels += size[0] * size[1]
            if total_bytes > MAX_TOTAL_BYTES or total_pixels > MAX_TOTAL_PIXELS:
                fail("IMAGE_LIMIT", "Job exceeds the total source byte/pixel budget.")
            assets[path] = (data, suffix, size, mode)
        data, suffix, size, mode = assets[path]
        record = dict(row, _asset=path)
        if id_key == "candidate_id":
            if row["role"] not in ("COVER", "SUPPLEMENT") or type(row["boxed"]) is not bool:
                fail("INVALID_JOB", "Candidates require a valid role and a boolean boxed flag.")
            if row["role"] == "COVER":
                if row["crop"] is not None:
                    fail("INVALID_COVER", "Covers must retain the complete source frame.")
                if row["boxed"]:
                    if row["title_box"] is not None:
                        fail("INVALID_COVER", "Already boxed covers must be copied without another title box.")
                else:
                    record["title_box"] = _rect(row["title_box"], size)
                    if mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK"):
                        fail("UNSUPPORTED_IMAGE_MODE", "Title annotation supports only standard 8-bit color/gray images.")
            else:
                if row["title_box"] is not None or row["boxed"]:
                    fail("INVALID_SUPPLEMENT", "Supplements cannot carry a title box or boxed flag.")
                if row["crop"] is not None:
                    record["crop"] = _rect(row["crop"], size)
        validated.append(record)
    return output, validated, assets


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        fail("IMAGE_LIMIT", "Rendered PNG exceeds 16 MiB; inputs remain unchanged.")
    return data


def _preview(image, identity):
    Image, ImageDraw, ImageFont = _pillow()
    width, height = image.size
    # At most 32 chunks cover every source pixel; no source material is omitted.
    chunk_height = max(640, math.ceil(width * 1.4), math.ceil(height / 32))
    chunks = math.ceil(height / chunk_height)
    columns = min(4, chunks)
    tile_width, tile_height, gutter, header = 280, 400, 12, 36
    sheet = Image.new("RGB", (gutter + columns * (tile_width + gutter),
                             header + math.ceil(chunks / columns) * (tile_height + gutter)), "#eeeeee")
    drawing = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    drawing.text((gutter, 8), f"{identity} | source {width}x{height} | pixel coordinates", fill="black", font=font)
    for index, top in enumerate(range(0, height, chunk_height)):
        bottom = min(height, top + chunk_height)
        tile = image.crop((0, top, width, bottom)).convert("RGB")
        tile.thumbnail((tile_width, tile_height - 24), Image.Resampling.LANCZOS)
        left = gutter + (index % columns) * (tile_width + gutter)
        y = header + (index // columns) * (tile_height + gutter)
        drawing.text((left, y), f"#{index + 1} x:0-{width} y:{top}-{bottom}", fill="black", font=font)
        sheet.paste(tile, (left, y + 24))
        tile.close()
    return sheet


def _write(output, data, suffix, created):
    digest = hashlib.sha256(data).hexdigest()
    target = output / f"{digest}{suffix}"
    _path(str(output))
    output.mkdir(parents=True, exist_ok=True)
    _path(str(output))
    if target.exists():
        _path(str(target), file=True)
        if target.read_bytes() != data:
            fail("OUTPUT_CONFLICT", "A content-addressed filename contains different bytes; not overwritten.")
        return target, digest
    # Publish complete bytes without overwrite. Windows rename also avoids a
    # crash leaving a second hard link; POSIX needs link for no-clobber behavior.
    descriptor, temporary_name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=output)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _path(str(output))
        try:
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError:
            _path(str(target), file=True)
            if target.read_bytes() != data:
                fail("OUTPUT_CONFLICT", "Concurrent output has different bytes; not overwritten.")
        else:
            created.append((target, digest))
    finally:
        temporary.unlink(missing_ok=True)
    return target, digest


def run_job(job):
    """Validate the entire job before output writes; preserve every input byte."""
    started = time.perf_counter()
    created = []
    try:
        output, rows, assets = _validate(job)
        result = []
        for row in rows:
            data, suffix, size, _ = assets[row["_asset"]]
            if job["action"] == "preview":
                image, _ = _decode(data)
                try:
                    sheet = _preview(image, row["capture_id"])
                    try:
                        size, rendered = sheet.size, _png(sheet)
                    finally:
                        sheet.close()
                finally:
                    image.close()
                path, _ = _write(output, rendered, ".png", created)
                result.append({"capture_id": row["capture_id"], "path": str(path), "width": size[0], "height": size[1]})
                continue
            if (row["role"] == "COVER" and not row["boxed"]) or row["crop"] is not None:
                Image, ImageDraw, _ = _pillow()
                image, _ = _decode(data)
                try:
                    if row["role"] == "COVER":
                        # Conversion retains decoded colors outside the box;
                        # preserve alpha where present. No EXIF transpose/rescale.
                        alpha = "A" in image.getbands() or "transparency" in image.info
                        edited = image.convert("RGBA" if alpha else "RGB")
                        left, top, right, bottom = row["title_box"]
                        color = (255, 0, 0, 255) if alpha else (255, 0, 0)
                        ImageDraw.Draw(edited).rectangle((left, top, right - 1, bottom - 1),
                                                       outline=color, width=3)
                    else:
                        edited = image.crop(row["crop"])
                    try:
                        size, data, suffix = edited.size, _png(edited), ".png"
                    finally:
                        edited.close()
                finally:
                    image.close()
            path, digest = _write(output, data, suffix, created)
            result.append({"candidate_id": row["candidate_id"], "path": str(path), "sha256": digest,
                           "width": size[0], "height": size[1]})
        return {"previews" if job["action"] == "preview" else "files": result,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        # Only clean up files exclusively created by this request. Existing
        # content-addressed files are never deleted or overwritten on failure.
        for path, digest in reversed(created):
            try:
                _path(str(path), file=True)
                if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    path.unlink()
            except (OSError, ImageOpsError):
                pass
        if isinstance(exc, ImageOpsError):
            raise
        if isinstance(exc, (OSError, ValueError, MemoryError)):
            fail("IMAGE_IO_ERROR", "Local image processing failed; inputs were not changed.")
        raise


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("INVALID_REQUEST", "Use --request-file with a local UTF-8 JSON request file.")


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--request-file", required=True)
        args = parser.parse_args(argv)
        path = _path(args.request_file, file=True)
        if path.stat().st_size > MAX_REQUEST_BYTES:
            fail("INVALID_REQUEST", "Request file exceeds 128 KiB.")
        with path.open("rb") as handle:
            raw = handle.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            fail("INVALID_REQUEST", "Request file exceeds 128 KiB.")
        try:
            job = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            fail("INVALID_REQUEST", "Request file must contain valid UTF-8 JSON.")
        result, exit_code = run_job(job), 0
    except ImageOpsError as exc:
        result, exit_code = {"error": {"code": exc.code, "message": str(exc)}}, 2
    except (OSError, ValueError):
        result, exit_code = {"error": {"code": "IMAGE_IO_ERROR", "message": "Local request file could not be read."}}, 2
    print(json.dumps(result, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
