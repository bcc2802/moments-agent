"""Read-only history queries over owned Markdown; no state, index or database writes."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import unicodedata


MAX_FILES = 10000
MAX_BYTES = 65536
MAX_RESULTS = 10
OWNER_RE = re.compile(r'^moments_agent:\s*[\"\']?moments-draft[\"\']?\s*$', re.M)
ID_RE = re.compile(r'([A-Za-z0-9_-]{1,80}):v([1-3])\Z')
TASK_CONTENT_ID_RE = re.compile(r'([A-Za-z0-9_-]{1,100}):final\Z')


class HistoryError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def normalized_text(text):
    """Only Unicode NFKC and whitespace folding: no semantic or case equivalence."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()


def _content_id(value):
    if not isinstance(value, str) or not (ID_RE.fullmatch(value) or TASK_CONTENT_ID_RE.fullmatch(value)):
        raise HistoryError('INVALID_INPUT', '内容ID需为明确轮号:v1、v2或v3，或明确任务ID:final。')
    return value


def _limit(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RESULTS:
        raise HistoryError('INVALID_INPUT', '每次结果上限需为1至10。')


class History:
    """Adapter: runner provides vault/archive and its existing _read_note(path)."""

    def __init__(self, runner):
        self.runner = runner
        try:
            self.vault = Path(runner.vault).resolve()
            self.archive = Path(runner.archive).resolve()
            self.archive.relative_to(self.vault)
            if self.archive == self.vault:
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise HistoryError('UNSAFE_PATH', '历史读取目录必须是已配置的专属子目录。') from None

    @staticmethod
    def _scan_state():
        return {'scanned_files': 0, 'owned_files': 0, 'failed_file_count': 0,
                'failed_directory_count': 0, 'failed_sources': [], 'truncated': False}

    @staticmethod
    def _problem(scan, code, directory=False):
        field = 'failed_directory_count' if directory else 'failed_file_count'
        scan[field] += 1
        # Identifiers are local to this response, not hashes of private paths.
        if len(scan['failed_sources']) < 20:
            scan['failed_sources'].append({'source_id': f'history-{len(scan["failed_sources"]) + 1:03d}',
                                           'code': code})

    @staticmethod
    def _coverage(scan):
        return {**scan, 'coverage_complete': not (scan['failed_file_count'] or
                scan['failed_directory_count'] or scan['truncated'])}

    def _iter_notes(self, scan):
        paths = []
        try:
            with os.scandir(self.archive) as entries:
                for entry in entries:
                    if Path(entry.name).suffix.lower() != '.md':
                        continue
                    if len(paths) >= MAX_FILES:
                        scan['truncated'] = True
                        break
                    paths.append(Path(entry.path))
        except FileNotFoundError:
            # An archive not yet created is empty; an existing file is not a directory.
            return
        except OSError as exc:
            self._problem(scan, 'PERMISSION_DENIED' if isinstance(exc, PermissionError)
                          else 'SOURCE_IO_ERROR', directory=True)
            return
        for path in sorted(paths, key=lambda p: p.name):
            scan['scanned_files'] += 1
            try:
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                    self._problem(scan, 'UNSAFE_PATH')
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if path.resolve().parent != self.archive:
                    self._problem(scan, 'UNSAFE_PATH')
                    continue
                if info.st_size > MAX_BYTES:
                    self._problem(scan, 'SOURCE_TOO_LARGE')
                    continue
                with path.open('rb') as handle:
                    raw = handle.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    self._problem(scan, 'SOURCE_TOO_LARGE')
                    continue
                text = raw.decode('utf-8-sig').replace('\r\n', '\n')
                front = text.split('\n---\n', 1)[0] if text.startswith('---\n') else ''
                if not OWNER_RE.search(front):
                    continue  # Ordinary index/foreign Markdown is outside this collection.
                note = self.runner._read_note(path)
                meta = note['meta']
                rid, variant = meta.get('round_id'), meta.get('variant')
                if (not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', rid)
                        or isinstance(variant, bool) or variant not in (1, 2, 3)
                        or not isinstance(note.get('title'), str) or not isinstance(note.get('body'), str)
                        or not isinstance(meta.get('source_ref'), str)):
                    self._problem(scan, 'INVALID_OWNED_NOTE')
                    continue
                scan['owned_files'] += 1
                yield note
            except OSError as exc:
                self._problem(scan, 'PERMISSION_DENIED' if isinstance(exc, PermissionError) else 'SOURCE_IO_ERROR')
            except (UnicodeError, ValueError, TypeError, KeyError):
                self._problem(scan, 'INVALID_OWNED_NOTE')
            except Exception as exc:
                # Existing runtime ownership/parser errors have a code; never echo their message.
                if not hasattr(exc, 'code'):
                    raise
                self._problem(scan, 'INVALID_OWNED_NOTE')
        # Only the dedicated one-level task final collection is added. Auxiliary
        # task.md pointers, style cases, feedback and arbitrary subfolders stay out.
        if not os.path.lexists(self.archive / '_tasks'):
            return
        try:
            from moments_task import Tasks
            for note in Tasks(self.runner).iter_finals():
                if scan['scanned_files'] >= MAX_FILES:
                    scan['truncated'] = True
                    break
                scan['scanned_files'] += 1
                scan['owned_files'] += 1
                yield note
        except OSError as exc:
            self._problem(scan, 'PERMISSION_DENIED' if isinstance(exc, PermissionError) else 'SOURCE_IO_ERROR')
        except Exception as exc:
            if not hasattr(exc, 'code'):
                raise
            code = str(exc.code)
            self._problem(scan, code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,80}', code) else 'INVALID_OWNED_NOTE')

    @staticmethod
    def _id(note):
        if note['meta'].get('record_type') == 'moments_task_final':
            return f'{note["meta"]["task_id"]}:final'
        return f'{note["meta"]["round_id"]}:v{note["meta"]["variant"]}'

    def _row(self, note, excerpt=None):
        meta = note['meta']
        row = {'content_id': self._id(note), 'round_id': meta.get('round_id'), 'variant': meta.get('variant'),
                'title': note['title'], 'status': meta['tags'][0], 'source_ref': meta['source_ref'],
                'relative_path': note['path'].relative_to(self.vault).as_posix(),
                'excerpt': note['body'][:160] if excerpt is None else excerpt}
        if meta.get('record_type') == 'moments_task_final':
            row.update(record_type='moments_task_final', task_id=meta['task_id'],
                       selected_variant=meta.get('selected_variant'))
        return row

    def _finish(self, scan):
        coverage = self._coverage(scan)
        if not scan['owned_files'] and (scan['failed_file_count'] or scan['failed_directory_count']):
            raise HistoryError('HISTORY_READ_FAILED', '历史扫描或读取失败，不能把未读内容当成空库。', coverage)
        return coverage

    @staticmethod
    def _excerpt(body, terms):
        folded = normalized_text(body).casefold()
        positions = [folded.find(term) for term in terms if term in folded]
        if not positions:
            return body[:160]
        hit = min(positions)
        # Find an original-text offset using normalized prefix lengths. Output remains verbatim.
        lo, hi = 0, len(body)
        while lo < hi:
            mid = (lo + hi) // 2
            if len(normalized_text(body[:mid]).casefold()) < hit:
                lo = mid + 1
            else:
                hi = mid
        start = max(0, lo - 40)
        return body[start:start + 160]

    def resolve(self, content_id):
        _content_id(content_id)
        scan, matches = self._scan_state(), []
        for note in self._iter_notes(scan):
            if self._id(note) == content_id:
                row = self._row(note)
                if note['meta'].get('record_type') == 'moments_task_final':
                    row.update(body=note['body'], body_sha256=note['meta']['body_sha256'],
                               confirmed_at=note['meta'].get('confirmed_at'), published_at=note['meta'].get('published_at'),
                               publication_text_basis=note['meta'].get('publication_text_basis'))
                matches.append(row)
        coverage = self._finish(scan)
        if len(matches) > 1:
            raise HistoryError('AMBIGUOUS_CONTENT', '相同内容ID对应多份自有稿件，拒绝猜测。', coverage)
        if not coverage['coverage_complete']:
            raise HistoryError('HISTORY_INCOMPLETE', '历史读取不完整，不能确认内容ID唯一。', coverage)
        if not matches:
            raise HistoryError('CONTENT_NOT_FOUND', '没有找到该内容ID的自有稿件。', coverage)
        return {'status': 'OK', **coverage, 'item': matches[0]}

    def search(self, query, status=None, tag=None, limit=10, page=1):
        _limit(limit)
        max_page = (MAX_FILES + limit - 1) // limit
        if (not isinstance(page, int) or isinstance(page, bool)
                or not 1 <= page <= max_page):
            raise HistoryError('INVALID_INPUT', f'页码需为1至{max_page}的整数。')
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise HistoryError('INVALID_INPUT', '请提供不超过200字的字面检索词。')
        if status is not None and status not in ('待发布', '已发布'):
            raise HistoryError('INVALID_INPUT', '状态只能为待发布或已发布。')
        if tag is not None and (not isinstance(tag, str) or not tag.strip() or len(tag) > 80):
            raise HistoryError('INVALID_INPUT', '话题标签格式无效。')
        canonical = self.runner._canonical(tag) if tag is not None else None
        terms = tuple(dict.fromkeys(normalized_text(query).casefold().split()))
        if not terms:
            raise HistoryError('INVALID_INPUT', '检索词不能为空。')
        scan, rows, ids = self._scan_state(), [], {}
        for note in self._iter_notes(scan):
            cid = self._id(note)
            ids[cid] = ids.get(cid, 0) + 1
            meta = note['meta']
            if status is not None and meta['tags'][0] != status:
                continue
            if canonical is not None and canonical not in meta['tags']:
                continue
            title = normalized_text(note['title']).casefold()
            body = normalized_text(note['body']).casefold()
            if not all(term in title or term in body for term in terms):
                continue
            row = self._row(note, self._excerpt(note['body'], terms))
            row['matched_in'] = [name for name, value in (('title', title), ('body', body))
                                 if any(term in value for term in terms)]
            rows.append((sum(term in title for term in terms), row))
        coverage = self._finish(scan)
        rows.sort(key=lambda item: (-item[0], item[1]['content_id'], item[1]['relative_path']))
        offset = (page - 1) * limit
        items = [row for _, row in rows[offset:offset + limit]]
        for item in items:
            item['ambiguous_content_id'] = ids[item['content_id']] > 1
        return {'status': 'OK' if coverage['coverage_complete'] else 'PARTIAL', **coverage,
                'method': 'LITERAL_TERMS_V1', 'items': items, 'total_matches': len(rows),
                'page': page, 'has_more': offset + limit < len(rows), 'semantic_search': False}

    def check_duplicates(self, text, exclude_content_id=None, limit=10):
        _limit(limit)
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise HistoryError('INVALID_INPUT', '待比较正文需为不超过8000字的非空文本。')
        if exclude_content_id is not None:
            _content_id(exclude_content_id)
        wanted = normalized_text(text)
        scan, matches = self._scan_state(), []
        for note in self._iter_notes(scan):
            if self._id(note) == exclude_content_id:
                continue
            if normalized_text(note['body']) == wanted:
                matches.append(self._row(note))
        coverage = self._finish(scan)
        matches.sort(key=lambda row: (row['content_id'], row['relative_path']))
        return {'status': 'OK' if coverage['coverage_complete'] else 'PARTIAL', **coverage,
                'method': 'NFKC_WHITESPACE_EXACT_V1', 'items': matches[:limit],
                'total_matches': len(matches), 'has_more': len(matches) > limit,
                'semantic_duplicate_detection': False, 'advisory_only': True}
