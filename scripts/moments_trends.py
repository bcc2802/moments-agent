"""Read-only TrendRadar result adapter; standard library, no network or copy generation.

Only a short public candidate index is persisted through the existing state writer.
Titles and URLs are untrusted data. A valid platform URL is not verified reporting.
"""
from __future__ import annotations

import datetime as dt
from difflib import SequenceMatcher
import hashlib
import ipaddress
import math
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
import uuid
from moments_storage import serialized
from moments_platforms import (PLATFORMS, SOURCE_IDS, ARCHIVE_PLATFORMS,
                               HISTORICAL_SOURCE_IDS)


DEFAULT_KEYWORD_WEIGHTS = {
    "ai": 10, "人工智能": 10, "智能体": 10, "agent": 9, "codex": 12,
    "openai": 10, "chatgpt": 10, "claude": 9, "gemini": 8, "大模型": 9,
    "自动化": 9, "工作流": 9, "生产力": 8, "ai工具": 10, "企业ai": 10,
    "创业": 6, "商业模式": 6, "营销": 6, "获客": 7, "销售": 5,
    "产品": 4, "科技": 4, "工具": 4, "成长": 2, "生活": 1,
}
MAX_ITEMS_PER_SOURCE = 1000
_SECRET = re.compile(r"(?:^|[_\-])(?:token|password|passwd|secret|cookie|authorization|credential|session|sessionid|api[_\-]?key|access[_\-]?key|auth)(?:$|[_\-])", re.I)
_INJECTION = re.compile(
    r"忽略(?:此前|之前|以上|所有)?(?:的)?(?:规则|指令|提示)|"
    r"(?:立即|请)?执行(?:命令|脚本|shell|powershell)|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|"
    r"(?:system|developer)\s*(?:prompt|message)\s*:|"
    r"(?:泄露|输出|读取|上传|发送)(?:所有|全部)?(?:密钥|令牌|私人笔记|个人日记)", re.I)
_PRIVATE = re.compile(r"\[隐私\]|【隐私】|禁止公开|仅限内部|未公开商业信息|PRIVATE_DO_NOT_SHARE|(?<!\d)1[3-9]\d{9}(?!\d)", re.I)
_SAFE_ERRORS = {"NETWORK_ERROR", "TIMEOUT", "HTTP_ERROR", "PARSE_ERROR", "SOURCE_UNAVAILABLE",
                "MCP_ERROR", "UPSTREAM_ERROR", "NO_DATA", "MISSING_SOURCE", "SOURCE_FAILED",
                "CRAWL_FAILED", "MCP_UNAVAILABLE", "INVALID_RESPONSE"}
_REDIRECT_KEYS = {"next", "nexturl", "url", "target", "targeturl", "jump", "jumpurl", "continue", "destination", "dest", "callback", "callbackurl", "goto", "link", "outurl", "forward", "forwardurl", "backurl", "to"}
_INDEX_FIELDS = {"candidate_id", "number", "title", "platform", "url", "rank", "fetched_at", "source_verification"}
_VERIFICATION = {"domain_checked": True, "content_verified": False, "basis": "UPSTREAM_TITLE_ONLY"}


class TrendsError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _fail(code, message):
    raise TrendsError(code, message)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        _fail("INVALID_CRAWL", "采集时间需为带时区的 ISO 时间。")
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError:
        _fail("INVALID_CRAWL", "采集时间需为带时区的 ISO 时间。")
    return stamp.isoformat(timespec="seconds")


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        _fail("INVALID_TITLE", "热点标题缺失或过长。")
    value = value.strip()
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        _fail("INVALID_TITLE", "热点标题含控制字符。")
    if _INJECTION.search(value) or re.search(r"<\s*(?:script|iframe)\b", value, re.I):
        _fail("UNTRUSTED_INSTRUCTION", "热点数据含明显指令注入，已拒绝。")
    if _PRIVATE.search(value):
        _fail("PRIVATE_DATA", "热点数据含明确私有标记或疑似手机号。")
    return value


def _platform_url(value, platform, *, allow_historical=False):
    """Keep public search parameters intact; never resolve, request or strip them."""
    platforms = ARCHIVE_PLATFORMS if allow_historical else PLATFORMS
    if not isinstance(platform, str) or platform not in platforms:
        _fail("INVALID_PLATFORM", "热点来源不在允许的平台范围内。")
    if not isinstance(value, str) or not value or len(value) > 2048:
        _fail("INVALID_URL", "热点缺少可用的公开链接。")
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in value) or "\\" in value or "#" in value:
        _fail("INVALID_URL", "热点链接含不支持的字符。")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        _fail("INVALID_URL", "热点链接编码无效。")
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
        if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in decoded) or "\\" in decoded:
            _fail("INVALID_URL", "热点链接含编码控制字符。")
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c|25)", decoded, re.I):
        _fail("INVALID_URL", "热点链接编码层级过多。")
    if _PRIVATE.search(decoded):
        _fail("PRIVATE_DATA", "热点链接含明确私有标记。")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.netloc or parts.username is not None or parts.password is not None:
            raise ValueError("scheme or credentials")
        host = parts.hostname
        if not host or "%" in host or ":" in host:
            raise ValueError("host")
        host = host.encode("idna").decode("ascii").lower()
        if host.endswith("."):
            host = host[:-1]
        if parts.port not in (None, 443 if parts.scheme.lower() == "https" else 80):
            raise ValueError("port")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP literal")
        expected = platforms[platform][1]
        if host != expected and not host.endswith("." + expected):
            _fail("DOMAIN_MISMATCH", "热点链接不属于该平台的限定域名。")
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            raise ValueError("host labels")
        decoded_parts = urlsplit(decoded)
        if re.search(r"(?:^|/)(?:token|password|passwd|secret|cookie|session|credential|api[_-]?key|access[_-]?token)(?:/|=|:|$)", decoded_parts.path, re.I):
            _fail("CREDENTIAL_URL", "热点链接含凭证式路径。")
        for key, _ in parse_qsl(decoded_parts.query, keep_blank_values=True, max_num_fields=100):
            compact = re.sub(r"[^a-z]", "", key.casefold())
            if _SECRET.search(key) or compact in {"apikey", "accesstoken", "refreshtoken", "clientsecret", "sessionid", "authorization", "authkey", "jwt", "key"}:
                _fail("CREDENTIAL_URL", "热点链接含凭证式参数。")
            if compact in _REDIRECT_KEYS or compact.startswith(("redirect", "return")):
                _fail("REDIRECT_URL", "热点链接含显式跳转参数，请提供直接公开来源。")
        if re.search(r"(?:bearer\s|-----BEGIN|eyJ[A-Za-z0-9_-]{12,}\.)", decoded_parts.query, re.I):
            _fail("CREDENTIAL_URL", "热点链接含疑似凭证值。")
        # The exact original public query is retained, including order and escaping.
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        _fail("INVALID_URL", "热点链接格式无效。")


def _normalized_title(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def validate_hot_url(platform, url, *, allow_historical=False):
    """Public, offline URL gate for an explicitly matched current hot selection."""
    return _platform_url(url, platform, allow_historical=allow_historical)


def _near(left, right):
    if left == right:
        return True
    # Distinct years, figures and version numbers must not be merged by similarity.
    if min(len(left), len(right)) < 10 or re.findall(r"\d+", left) != re.findall(r"\d+", right):
        return False
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.9


def _safe_error(value):
    return value if isinstance(value, str) and (value in _SAFE_ERRORS or re.fullmatch(r"HTTP_[1-5]\d{2}", value)) else "SOURCE_FAILED"


class Trends:
    def __init__(self, runner, keyword_weights=None):
        self.runner = runner
        weights = DEFAULT_KEYWORD_WEIGHTS if keyword_weights is None else keyword_weights
        if not isinstance(weights, dict) or len(weights) > 100:
            _fail("INVALID_PREFERENCES", "热点关键词权重需为最多 100 项的对象。")
        self.keyword_weights = {}
        for word, weight in weights.items():
            if not isinstance(word, str) or not word.strip() or len(word) > 40 or any(unicodedata.category(c) in ("Cc", "Cf") for c in word):
                _fail("INVALID_PREFERENCES", "热点关键词格式无效。")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or not 0 <= weight <= 50:
                _fail("INVALID_PREFERENCES", "热点关键词权重需在 0–50 之间。")
            self.keyword_weights[unicodedata.normalize("NFKC", word.strip()).casefold()] = weight

    def _signals(self, title):
        normalized = unicodedata.normalize("NFKC", title).casefold()
        matched = []
        for word, weight in self.keyword_weights.items():
            found = bool(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", normalized)) if word.isascii() and word.isalpha() else word in normalized
            if found and weight > 0:
                matched.append((word, weight))
        matched.sort(key=lambda pair: (-pair[1], pair[0]))
        risk = bool(re.search(r"死亡|遇难|地震|战争|治愈|确诊|暴涨|暴跌|爆料|网传|突发|被捕", title))
        promotion = bool(re.search(r"限时优惠|点击领取|邀请码|返现|免费领|福利抽奖", title))
        return matched, risk, promotion

    def _score(self, item, cross_platform=1):
        matched, risk, promotion = self._signals(item["title"])
        relevance = min(45, sum(weight for _, weight in matched))
        rank_signal = 10 / math.sqrt(item["rank"] or 100)
        return round(relevance + rank_signal + min(8, (cross_platform - 1) * 3) - (12 if risk else 0) - (35 if promotion else 0), 4)

    def _public_item(self, item):
        matched, risk, promotion = self._signals(item["title"])
        why = "标题涉及关注关键词：" + "、".join(word for word, _ in matched[:4]) + "；可先核对具体事实再讨论。" if matched else "可作为公共话题观察；当前仅有榜单标题，需先确认事实与讨论价值。"
        if risk:
            why += "涉及重大或未核实主张，已降权，需一手资料复核。"
        if promotion:
            why += "含推广信号，已降权。"
        query = bool(urlsplit(item["url"]).query)
        return {**{key: item[key] for key in _INDEX_FIELDS}, "platform_name": ARCHIVE_PLATFORMS[item["platform"]][0],
                "fetched_at_display": dt.datetime.fromisoformat(item["fetched_at"]).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai"),
                "why": why, "angle": "这条消息的具体事实是什么，对实际使用或日常选择有什么启发？" if matched else "这件事有哪些值得核实的事实，普通人可以从哪个具体问题谈起？",
                "evidence": {"basis": "TITLE_ONLY", "content_verified": False,
                             "note": "仅标题信息；平台域名已离线校验，正文、事实与来源发布时间待核实。"},
                "requires_h1_review": query,
                "h1_review_note": "原始公开查询参数已保留，不能自动删参数；无当前批次校验上下文的原 H1 拒绝含参数链接，须通过当前 hot_selection 并实际核对来源。" if query else "进入 H1 前仍须实际回读链接、区分来源主张并确认本人观点或无立场模式。"}

    def _candidate_groups(self, crawl, legacy_scoring=True, *, historical=False):
        """Shared offline safety/shape gate and full de-duplicated candidate pool."""
        source_ids = HISTORICAL_SOURCE_IDS if historical else SOURCE_IDS
        platforms = ARCHIVE_PLATFORMS if historical else PLATFORMS
        if not isinstance(crawl, dict):
            _fail("INVALID_CRAWL", "需提供本次限定平台采集结果。")
        requested = crawl.get("requested_sources")
        if not isinstance(requested, list) or any(not isinstance(source, str) for source in requested) or len(requested) != len(source_ids) or set(requested) != set(source_ids):
            _fail("INVALID_CRAWL", f"每批必须且只能请求限定的{len(source_ids)}个平台。")
        fetched_at = _timestamp(crawl.get("fetched_at"))
        sources = crawl.get("sources")
        if not isinstance(sources, list) or len(sources) > len(source_ids):
            _fail("INVALID_CRAWL", "平台采集结果格式无效。")
        indexed = {}
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or source["id"] not in source_ids or source["id"] in indexed:
                _fail("INVALID_CRAWL", "采集结果包含重复或未授权平台。")
            if source.get("status") not in ("OK", "FAILED") or not isinstance(source.get("items", []), list) or len(source.get("items", [])) > MAX_ITEMS_PER_SOURCE:
                _fail("INVALID_CRAWL", "平台状态或候选条数无效。")
            if source["status"] == "FAILED" and source.get("items"):
                _fail("INVALID_CRAWL", "失败平台不能同时声明有可用候选。")
            indexed[source["id"]] = source
        reports, valid, succeeded, failed = [], [], [], []
        for platform in source_ids:
            source = indexed.get(platform, {"status": "FAILED", "error": "MISSING_SOURCE", "items": []})
            rows = source.get("items", [])
            report = {"id": platform, "platform_name": platforms[platform][0], "status": source["status"],
                      "received_count": len(rows), "valid_count": 0, "rejected_count": 0, "rejected_reasons": {}}
            if source["status"] == "FAILED":
                report["error"] = _safe_error(source.get("error"))
                failed.append({"id": platform, "error": report["error"]})
            else:
                succeeded.append(platform)
                for row in rows:
                    try:
                        if not isinstance(row, dict):
                            _fail("INVALID_ITEM", "候选格式无效。")
                        title = _title(row.get("title"))
                        url = _platform_url(row.get("url"), platform, allow_historical=historical)
                        rank = row.get("rank")
                        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 100000):
                            _fail("INVALID_RANK", "榜单排名格式无效。")
                        valid.append({"title": title, "platform": platform, "url": url, "rank": rank, "fetched_at": fetched_at})
                        report["valid_count"] += 1
                    except TrendsError as exc:
                        report["rejected_count"] += 1
                        report["rejected_reasons"][exc.code] = report["rejected_reasons"].get(exc.code, 0) + 1
            reports.append(report)
        # The compatibility route keeps its original scoring. New editorial pools
        # use a neutral representative order and never invoke keyword scoring.
        if legacy_scoring:
            valid.sort(key=lambda item: (-self._score(item), source_ids.index(item["platform"]), item["rank"] or 100001, item["url"]))
        else:
            valid.sort(key=lambda item: (source_ids.index(item["platform"]), item["rank"] or 100001, item["url"], item["title"]))
        groups = []
        for row in valid:
            norm = _normalized_title(row["title"])
            group = next((group for group in groups if row["url"] in group["urls"] or any(_near(norm, other) for other in group["titles"])), None)
            if group is None:
                group = {"item": row, "occurrences": [], "urls": set(), "titles": []}
                groups.append(group)
            group["occurrences"].append(row)
            group["urls"].add(row["url"])
            group["titles"].append(norm)
        if legacy_scoring:
            for group in groups:
                group["score"] = self._score(group["item"], len({row["platform"] for row in group["occurrences"]}))
            groups.sort(key=lambda group: (-group["score"], source_ids.index(group["item"]["platform"]), group["item"]["rank"] or 100001, group["item"]["url"]))
        else:
            for group in groups:
                group["score"] = 0
            groups.sort(key=lambda group: (source_ids.index(group["item"]["platform"]), group["item"]["rank"] or 100001, group["item"]["url"], group["item"]["title"]))
        return {"fetched_at": fetched_at, "groups": groups, "reports": reports,
                "valid": valid, "succeeded": succeeded, "failed": failed}

    @serialized
    def recommend(self, crawl):
        """Legacy keyword route retained for compatibility; new menus use Selection."""
        collected = self._candidate_groups(crawl)
        fetched_at, groups, reports = collected["fetched_at"], collected["groups"], collected["reports"]
        valid, succeeded, failed = collected["valid"], collected["succeeded"], collected["failed"]
        batch_id = "tr-" + uuid.uuid4().hex
        items, index = [], []
        for number, group in enumerate(groups[:10], 1):
            row = group["item"]
            candidate = {**row, "number": number, "candidate_id": hashlib.sha256((batch_id + "\n" + row["platform"] + "\n" + row["url"]).encode("utf-8")).hexdigest()[:24], "source_verification": dict(_VERIFICATION)}
            index.append(candidate)
            public = self._public_item(candidate)
            public["score"] = group["score"]
            public["occurrences"] = group["occurrences"]
            items.append(public)
        state = self.runner._state()
        # Do not alter schema, active_round, log topics or unknown future keys.
        state["hot_topics"] = {"batch_id": batch_id, "fetched_at": fetched_at, "items": index}
        self.runner._write_state(state)
        status = "UNAVAILABLE" if not items else "PARTIAL" if failed else "OK"
        return {"status": status, "batch_id": batch_id, "fetched_at": fetched_at,
                "timezone": "Asia/Shanghai", "requested_sources": list(SOURCE_IDS),
                "succeeded_sources": succeeded, "failed_sources": failed, "source_reports": reports,
                "counts": {"received": sum(report["received_count"] for report in reports),
                           "valid_before_dedup": len(valid), "unique_after_dedup": len(groups),
                           "duplicates_removed": len(valid) - len(groups),
                           "rejected": sum(report["rejected_count"] for report in reports), "recommended": len(items)},
                "items": items,
                "notice": "NewsNow 为第三方热榜聚合。采集时间不是来源发布时间；仅依据本次实际收到的标题推荐，不代表当日全网最热。" +
                          ("不足 10 条，按实际合格数量展示。" if len(items) < 10 else "") +
                          ("当前批次无可用候选，旧批次不再用于选择。" if not items else "回复选热点3可定位本批第3条；先核对来源再进入原三稿流程。")}

    def select(self, n, batch_id=None):
        group = self.runner._state().get("hot_topics")
        if group is None:
            _fail("NO_HOT_TOPICS", "没有最近热点批次，请先获取并展示当前热点清单。")
        if (not isinstance(group, dict) or not {"batch_id", "fetched_at", "items"} <= set(group)
                or set(group) - {"batch_id", "fetched_at", "items", "selection_rule_version"}
                or ("selection_rule_version" in group and group["selection_rule_version"] not in ("1.0", "2.0", "trend-selection-v0.2"))
                or not isinstance(group.get("batch_id"), str) or not re.fullmatch(r"tr-[0-9a-f]{32}", group["batch_id"])):
            _fail("HOT_STATE_INVALID", "热点索引格式损坏，请重新获取清单，不能猜测编号。")
        if batch_id is not None and (not isinstance(batch_id, str) or batch_id != group["batch_id"]):
            _fail("HOT_BATCH_MISMATCH", "批次不是当前最新清单，请提供当前批次或原始链接。")
        rows = group.get("items")
        if not isinstance(rows, list) or len(rows) > 10:
            _fail("HOT_STATE_INVALID", "热点索引条数无效，请重新获取清单。")
        try:
            fetched_at = _timestamp(group.get("fetched_at"))
            ids, urls = set(), set()
            for number, row in enumerate(rows, 1):
                if not isinstance(row, dict) or set(row) != _INDEX_FIELDS or row.get("number") != number or isinstance(row.get("number"), bool):
                    raise ValueError("fields or numbering")
                if row.get("platform") not in ARCHIVE_PLATFORMS or _title(row.get("title")) != row["title"] or _platform_url(row.get("url"), row["platform"], allow_historical=True) != row["url"]:
                    raise ValueError("candidate")
                if row.get("source_verification") != _VERIFICATION or row.get("fetched_at") != fetched_at:
                    raise ValueError("source validation")
                candidate_id = row.get("candidate_id")
                expected_id = hashlib.sha256((group["batch_id"] + "\n" + row["platform"] + "\n" + row["url"]).encode("utf-8")).hexdigest()[:24]
                if candidate_id != expected_id or candidate_id in ids or row["url"] in urls:
                    raise ValueError("candidate identity")
                rank = row.get("rank")
                if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 100000):
                    raise ValueError("rank")
                ids.add(candidate_id)
                urls.add(row["url"])
        except (TrendsError, ValueError, TypeError):
            _fail("HOT_STATE_INVALID", "热点索引内容损坏，请重新获取清单。")
        if not rows:
            _fail("NO_HOT_TOPICS", "当前最新批次无可用候选，请重新抓取；不能选择旧批次。")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(rows):
            _fail("INVALID_HOT_NUMBER", "热点编号超出当前清单范围。")
        public = self._public_item(rows[n - 1])
        if group.get("selection_rule_version") in ("1.0", "2.0", "trend-selection-v0.2"):
            description = ("内容60+最强赛道40、客户问题/项目实践/个人生活及组合多样性" if group["selection_rule_version"] == "trend-selection-v0.2"
                           else "企业AI流程价值、个人AI提效和来源可检验性" if group["selection_rule_version"] == "2.0"
                           else "大众可读性、生活工作价值和组合多样性")
            public.update(why=f"本批按{description}规则选入；编辑判断不等于事实核验或本人观点。",
                          angle="先回读来源，再确认本人可真实表达的问题或观察；不以命中技术关键词代替选题价值。",
                          selection_rule_version=group["selection_rule_version"])
        return {"status": "NEEDS_VERIFICATION", "batch_id": group["batch_id"], **public}
