"""Editorial scoring and bounded menu composition over the existing public pool.

The current Codex model supplies editorial judgments for every candidate. This
module validates and computes them; it never pretends keyword hits are editorial
scores, verified news, user experience, or measured consultation likelihood.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
import uuid
from moments_storage import serialized

from moments_trends import PLATFORMS, SOURCE_IDS, Trends, TrendsError, _VERIFICATION
from moments_platforms import ARCHIVE_PLATFORMS, HISTORICAL_SOURCE_IDS


VERSION = "trend-selection-v0.2"
# Raw points, not old 0..5 ratings. No AI keyword dimension.
WEIGHTS = {"readability": 20, "story": 15, "resonance": 10, "trust": 10, "signal": 5}
LANE_WEIGHTS = {"problem": 25, "opportunity": 15}
LANES = ("A", "B", "C")
QUALITY_FLOOR = 75
CONTENT_FLOOR = 35
LANE_FLOOR = 25
BEAM_WIDTH = 256
CATEGORIES = ("A", "B", "C", "S")
SOFT_TARGETS = {"A": 4, "B": 3, "C": 2, "S": 1}
TECHNICAL_LIMIT = 1


def fail(code, message, details=None):
    raise TrendsError(code, message, details)


def text(value, field, limit=500):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value)):
        fail("INVALID_EDITORIAL", f"编辑评价的 {field} 需为有界非空单行文字。")
    return value.strip()


def stable_id(row):
    return hashlib.sha256((row["platform"] + "\n" + row["url"] + "\n" + row["title"]).encode("utf-8")).hexdigest()[:24]


def weighted_score(scores):
    """Sum the five content dimensions (max 60), rejecting old contracts."""
    if not isinstance(scores, dict) or set(scores) != set(WEIGHTS):
        fail("INVALID_EDITORIAL", "内容适配五维评分字段必须完整且不含额外维度。")
    for key, value in scores.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= WEIGHTS[key] or not math.isfinite(value)):
            fail("INVALID_EDITORIAL", "内容适配分项需为各自上限内的有限数字，禁止布尔值。")
    return sum(scores.values())


def lane_scores(lanes):
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        fail("INVALID_EDITORIAL", "需完整记录A/B/C三个独立赛道。")
    result = {}
    for lane in LANES:
        row = lanes[lane]
        if not isinstance(row, dict) or set(row) != {*LANE_WEIGHTS, "reason"}:
            fail("INVALID_EDITORIAL", "赛道需有problem、opportunity原始分及具体reason。")
        for key, maximum in LANE_WEIGHTS.items():
            value = row[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= maximum or not math.isfinite(value)):
                fail("INVALID_EDITORIAL", "赛道分项必须为上限内的有限数字。")
        result[lane] = {**row, "reason": text(row["reason"], "lane.reason"),
                        "score": sum(row[key] for key in LANE_WEIGHTS)}
    primary = max(LANES, key=lambda lane: result[lane]["score"])
    return result, primary


class Selection:
    def __init__(self, runner):
        self.runner = runner
        self.trends = Trends(runner)

    def prepare(self, crawl, *, historical=False):
        """Return all safe deduplicated public titles, with no persistence writes."""
        source_ids = HISTORICAL_SOURCE_IDS if historical else SOURCE_IDS
        platforms = ARCHIVE_PLATFORMS if historical else PLATFORMS
        data = self.trends._candidate_groups(crawl, legacy_scoring=False, historical=historical)
        candidates = []
        for group in data["groups"]:
            row = dict(group["item"])
            row.update(candidate_id=stable_id(row),
                       platform_name=platforms[row["platform"]][0],
                       occurrences=group["occurrences"],
                       content_published_at=None,
                       verification="TITLE_ONLY")
            candidates.append(row)
        # Bind every observed source row, including rows merged into another
        # representative. Otherwise changed secondary evidence could reuse old
        # editorial judgments. Canonical ordering ignores incoming source order.
        candidates.sort(key=lambda row: (source_ids.index(row["platform"]), row["rank"] or 100001,
                                         row["url"], row["title"]))
        source_fields = ("title", "platform", "url", "rank", "fetched_at")
        identity = []
        for row in candidates:
            entry = {key: row[key] for key in ("candidate_id", *source_fields)}
            occurrences = [{key: occurrence[key] for key in source_fields}
                           for occurrence in row["occurrences"]]
            entry["occurrences"] = sorted(occurrences, key=lambda occurrence:
                json.dumps(occurrence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            identity.append(entry)
        pool_identity = {"fetched_at": data["fetched_at"], "candidates": identity,
                         "source_reports": data["reports"]}
        pool_id = "pool-" + hashlib.sha256(json.dumps(pool_identity, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
        return {"status": "NEEDS_EDITORIAL_REVIEW" if candidates else "UNAVAILABLE",
                "selection_rule_version": VERSION, "pool_id": pool_id,
                "fetched_at": data["fetched_at"], "timezone": "Asia/Shanghai",
                "requested_sources": list(source_ids), "succeeded_sources": data["succeeded"],
                "failed_sources": data["failed"], "source_reports": data["reports"],
                "counts": {"received": sum(row["received_count"] for row in data["reports"]),
                           "valid_before_dedup": len(data["valid"]), "unique_after_dedup": len(candidates),
                           "duplicates_removed": len(data["valid"]) - len(candidates),
                           "rejected": sum(row["rejected_count"] for row in data["reports"])},
                "candidates": candidates,
                "notice": "尚未生成最终菜单；每个候选需编辑评价或有理由排除。仅标题信息，不代表事实或本人观点。"}

    @staticmethod
    def _evaluate(candidates, evaluations):
        if not isinstance(evaluations, list) or len(evaluations) != len(candidates):
            fail("INVALID_EDITORIAL", "需逐项覆盖完整候选池，不能只评价旧Top10。")
        known = {row["candidate_id"]: row for row in candidates}
        seen, eligible, audit = set(), [], []
        for value in evaluations:
            if not isinstance(value, dict) or not isinstance(value.get("candidate_id"), str):
                fail("INVALID_EDITORIAL", "编辑评价缺少候选ID。")
            cid = value["candidate_id"]
            if cid not in known or cid in seen:
                fail("INVALID_EDITORIAL", "评价包含未知或重复候选ID。")
            seen.add(cid)
            source = known[cid]
            evidence = {key: source[key] for key in ("title", "platform", "url", "rank", "fetched_at", "occurrences")}
            missing = ["content_published_at", "content_verification"]
            if source["rank"] is None:
                missing.append("rank")
            base = {"candidate_id": cid, "source_evidence": evidence,
                    "confidence": "TITLE_ONLY", "missing_fields": missing}
            if value.get("decision") == "EXCLUDE":
                if set(value) != {"candidate_id", "decision", "reason"}:
                    fail("INVALID_EDITORIAL", "排除项只接受候选ID、决定和具体理由。")
                audit.append({**base, "decision": "EXCLUDE", "reason": text(value.get("reason"), "reason")})
                continue
            required = {"candidate_id", "decision", "category", "non_ai", "technical", "scores", "lanes",
                        "why_read", "why_user", "angle", "event_key", "risk"}
            if (value.get("decision") != "ELIGIBLE" or not required <= set(value)
                    or set(value) - required - {"technical_exception"}):
                fail("INVALID_EDITORIAL", "评分项需五维原始分、三个赛道、分类、技术标志、理由、事件和风险。")
            if (not isinstance(value["category"], str) or value["category"] not in CATEGORIES
                    or not isinstance(value["non_ai"], bool) or not isinstance(value["technical"], bool)):
                fail("INVALID_EDITORIAL", "类别需A/B/C/S，非AI及技术性需独立布尔判断。")
            if not isinstance(value["risk"], str) or value["risk"] not in ("LOW", "NEEDS_PRIMARY"):
                fail("INVALID_EDITORIAL", "需独立记录事实风险，评分不是新闻事实认证。")
            exception = value.get("technical_exception", False)
            if not isinstance(exception, bool):
                fail("INVALID_EDITORIAL", "技术特例需明确布尔值。")
            content = weighted_score(value["scores"])
            lanes, primary = lane_scores(value["lanes"])
            if value["category"] != "S" and value["category"] != primary:
                fail("INVALID_EDITORIAL", "非跨界类别必须与最强主赛道一致，不能为配比重复归类。")
            primary_score = lanes[primary]["score"]
            item = {**source, **value, "lanes": lanes, "content_fit": content,
                    "primary_lane": primary, "primary_lane_score": primary_score,
                    "score": content + primary_score, "confidence": "TITLE_ONLY",
                    "missing_fields": missing}
            for key in ("why_read", "why_user", "angle", "event_key"):
                item[key] = text(value[key], key, 150 if key == "event_key" else 500)
            gates = {"total": item["score"] >= QUALITY_FLOOR,
                     "content": content >= CONTENT_FLOOR, "lane": primary_score >= LANE_FLOOR,
                     "risk": value["risk"] == "LOW", "technical_scene": not value["technical"] or exception}
            reasons = []
            if not gates["risk"]:
                reasons.append("重要主张缺少一手依据，阻止确定性转述，先核实再推荐")
            if not gates["total"]:
                reasons.append(f"总分低于{QUALITY_FLOOR}")
            if not gates["content"]:
                reasons.append(f"内容适配低于{CONTENT_FLOOR}/60")
            if not gates["lane"]:
                reasons.append(f"最强赛道低于{LANE_FLOOR}/40")
            if not gates["technical_scene"]:
                reasons.append("纯技术材料没有普通读者可理解的场景例外")
            detail = {**base, **{key: item[key] for key in ("category", "scores", "lanes", "content_fit",
                      "primary_lane", "primary_lane_score", "score", "technical", "non_ai", "risk",
                      "why_read", "why_user", "angle", "event_key")}, "thresholds": gates}
            if reasons:
                audit.append({**detail, "decision": "QUALITY_GATE_EXCLUDE", "reason": "；".join(reasons) + "。"})
            else:
                eligible.append(item)
                audit.append({**detail, "decision": "ELIGIBLE"})
        # Editorial event keys merge semantic duplicates after title/URL dedup.
        # Preserve every qualified source pointer instead of silently losing the
        # secondary platform when the composer gives the event a single slot.
        by_event = {}
        for item in eligible:
            refs = by_event.setdefault(item["event_key"], {})
            for ref in item["occurrences"]:
                refs[json.dumps(ref, ensure_ascii=False, sort_keys=True)] = ref
        for item in eligible:
            item["occurrences"] = [ref for _, ref in sorted(by_event[item["event_key"]].items())]
        return eligible, audit

    @staticmethod
    def _compose(items, cap):
        """Small bounded beam: hard safety/event/T/source gates, soft menu targets.

        Qualified scores dominate; each soft target slot adds two utility points
        and each source adds three. Thus nearby quality can yield a more varied
        menu, without admitting any below-threshold item. This bounded search
        does not claim global optimality.
        """
        ordered = sorted(items, key=lambda item: (-item["score"], item["candidate_id"]))
        # A menu has at most ten rows. Keep at most ten alternatives for each
        # source/category/non-AI signature; this bounds worst-case memory without
        # discarding any candidate's editorial evaluation or audit entry.
        signatures, bounded = {}, []
        for item in ordered:
            signature = (item["platform"], item["category"], item["technical"], item["non_ai"])
            events = signatures.setdefault(signature, set())
            if item["event_key"] not in events and len(events) < 10:
                bounded.append(item)
                events.add(item["event_key"])
        ordered = bounded
        # state: chosen tuple, source counts, category counts, technical, events, score
        initial = ((), (0,) * len(SOURCE_IDS), (0,) * len(CATEGORIES), 0, frozenset(), 0.0)
        beam = [initial]

        def key(state):
            chosen, sources, categories, technical, _, score = state
            coverage = sum(count > 0 for count in sources)
            targets = sum(min(categories[CATEGORIES.index(category)], target)
                          for category, target in SOFT_TARGETS.items())
            return (score + 2 * targets + 3 * coverage, score, targets, coverage, tuple(-i for i in chosen))

        best = initial
        for _ in range(10):
            next_states, seen = [], set()
            for state in beam:
                chosen, sources, categories, technical, events, score = state
                chosen_set = set(chosen)
                for index, item in enumerate(ordered):
                    source = SOURCE_IDS.index(item["platform"])
                    category = CATEGORIES.index(item["category"])
                    if (index in chosen_set or sources[source] >= cap or item["event_key"] in events
                            or (item["technical"] and technical >= TECHNICAL_LIMIT)):
                        continue
                    new_chosen = tuple(sorted((*chosen, index)))
                    if new_chosen in seen:
                        continue
                    seen.add(new_chosen)
                    sc, cc = list(sources), list(categories)
                    sc[source] += 1
                    cc[category] += 1
                    next_states.append((new_chosen, tuple(sc), tuple(cc), technical + item["technical"],
                                        events | {item["event_key"]}, score + item["score"]))
            if not next_states:
                break
            next_states.sort(key=key, reverse=True)
            beam = next_states[:BEAM_WIDTH]
            best = beam[0]
        chosen = [ordered[index] for index in best[0]]
        return sorted(chosen, key=lambda item: (-item["score"], item["candidate_id"]))

    @staticmethod
    def _feasible_source_coverage(items):
        """Maximum distinct sources jointly possible under event/T hard gates.

        The bounded source set makes a small source-to-event matching sufficient.
        Technical candidates are optional and may consume at most one slot.
        """
        nontechnical = [item for item in items if not item["technical"]]

        def matching(block_sources=frozenset(), block_events=frozenset()):
            by_source = {}
            for item in nontechnical:
                if item["platform"] not in block_sources and item["event_key"] not in block_events:
                    by_source.setdefault(item["platform"], set()).add(item["event_key"])
            owner = {}

            def augment(source, seen):
                for event in sorted(by_source[source]):
                    if event in seen:
                        continue
                    seen.add(event)
                    if event not in owner or augment(owner[event], seen):
                        owner[event] = source
                        return True
                return False

            return sum(augment(source, set()) for source in sorted(by_source))

        result = matching()
        technical = sorted({(item["platform"], item["event_key"])
                            for item in items if item["technical"]})
        for count in range(1, TECHNICAL_LIMIT + 1):
            for choice in combinations(technical, count):
                sources = frozenset(source for source, _ in choice)
                events = frozenset(event for _, event in choice)
                if len(sources) == count and len(events) == count:
                    result = max(result, count + matching(sources, events))
        return result

    def _choose(self, eligible):
        selected = self._compose(eligible, 2)
        cap, relaxations = 2, []
        available_sources = {item["platform"] for item in eligible}
        feasible_coverage = self._feasible_source_coverage(eligible)
        # Audience relevance and quality precede platform variety. Relax only the
        # per-source soft cap, to the smallest value that yields more safe items.
        if len(selected) < 10:
            for candidate_cap in range(3, 11):
                alternative = self._compose(eligible, candidate_cap)
                if len(alternative) > len(selected):
                    selected, cap = alternative, candidate_cap
                if len(selected) == 10:
                    break
            if cap > 2:
                relaxations.append({"rule": "SOURCE_CAP", "default": 2, "applied": cap,
                                    "reason": f"目标人群、风险与质量门槛后有{len(available_sources)}个候选平台；默认每源2条不足10条，按最小必要幅度放宽来源软上限，保留事件去重与技术最多{TECHNICAL_LIMIT}项，未降低质量。"})
        return selected, cap, relaxations, available_sources, feasible_coverage

    def _same_menu(self, previous, pool, selected):
        comparable = [{key: item[key] for key in ("title", "platform", "url", "rank", "fetched_at")}
                      for item in selected]
        previous_valid = (isinstance(previous, dict)
                          and set(previous) == {"batch_id", "fetched_at", "items", "selection_rule_version"}
                          and isinstance(previous.get("batch_id"), str)
                          and re.fullmatch(r"tr-[0-9a-f]{32}", previous["batch_id"])
                          and isinstance(previous.get("items"), list))
        if previous_valid and previous["items"]:
            try:
                # Reuse the existing complete index check, including bool numbers,
                # URL/rank/timestamp/identity checks. Re-curation repairs bad caches.
                self.trends.select(1, previous["batch_id"])
            except TrendsError:
                previous_valid = False
        return (previous_valid and previous.get("selection_rule_version") == VERSION
                and previous.get("fetched_at") == pool["fetched_at"]
                and isinstance(previous.get("items"), list)
                and [{key: row.get(key) for key in ("title", "platform", "url", "rank", "fetched_at")}
                     for row in previous["items"] if isinstance(row, dict)] == comparable)

    def _history_filter(self, pool, evaluations, eligible, audit, previous):
        """Enforce deterministic prior-recommendation exclusions before writing.

        Event equivalence remains editorial. Only exact observed URLs and an
        already-recorded exact event key are hard matches; no keyword heuristic
        pretends to resolve semantic similarity.
        """
        if not hasattr(self.runner, "project"):
            # Pure in-memory composition clients have no public artifact store.
            return eligible, {"status": "NO_PUBLIC_STORE", "excluded_count": 0}
        # Local import avoids a module cycle: Context also offers Workflow views.
        from moments_hot_context import Context, ContextError
        options = {"optional_legacy": not (Path(self.runner.project) / "CURRENT_STATUS.md").is_file()}
        if isinstance(previous, dict):
            def digest(value):
                encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                      allow_nan=False) + "\n").encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()
            options.update(exclude_index=previous, exclude_pool_sha256=digest(pool),
                           exclude_evaluations_sha256=digest(evaluations))
        try:
            history = Context(self.runner).history(**options)
        except ContextError as exc:
            fail("HOT_HISTORY_INCOMPLETE", "历史推荐记录未能完整校验，未更新菜单。",
                 {"history_error": exc.code})
        if not history.get("coverage_complete"):
            fail("HOT_HISTORY_INCOMPLETE", "历史推荐记录存在缺口，未将未读记录视为未推送；请先处理具体缺口。",
                 {"history_skipped": history.get("skipped", [])})
        seen_urls = {row["url"] for row in history["entries"]}
        seen_events = {key for row in history["entries"] for key in row["event_keys"]}
        excluded = {}
        for item in eligible:
            urls = sorted({row["url"] for row in item["occurrences"]} & seen_urls)
            event = item["event_key"] if item["event_key"] in seen_events else None
            if urls or event:
                excluded[item["candidate_id"]] = {"history_urls": urls, "history_event_key": event}
        for row in audit:
            if row["candidate_id"] in excluded:
                row.update(decision="HISTORY_DUPLICATE_EXCLUDE",
                           reason="该来源URL或已记录事件键已出现在正式推荐历史；不重复占据本轮名额。",
                           **excluded[row["candidate_id"]])
        return ([item for item in eligible if item["candidate_id"] not in excluded],
                {"status": "OK", "coverage_complete": True,
                 "menu_count": history["menu_count"],
                 "unique_url_count": history["unique_url_count"],
                 "excluded_count": len(excluded), "excluded": history["excluded"],
                 "basis": "HASH_VERIFIED_PUBLIC_HISTORY; EXACT_URL_OR_RECORDED_EVENT_KEY"})

    @serialized
    def recommend(self, crawl, evaluations, pool_id):
        pool = self.prepare(crawl)
        if not isinstance(pool_id, str) or pool_id != pool["pool_id"]:
            fail("POOL_MISMATCH", "评价池与本次完整候选不一致，请重新准备并评价。")
        eligible, audit = self._evaluate(pool["candidates"], evaluations)
        selected, cap, relaxations, available_sources, feasible_coverage = self._choose(eligible)
        state = self.runner._state()
        previous = state.get("hot_topics")
        eligible, history_gate = self._history_filter(pool, evaluations, eligible, audit, previous)
        if history_gate["excluded_count"]:
            selected, cap, relaxations, available_sources, feasible_coverage = self._choose(eligible)
        categories = Counter(item["category"] for item in selected)
        non_ai = sum(item["non_ai"] for item in selected)
        by_source = Counter(item["platform"] for item in selected)
        shortfalls = []
        if len(selected) < 10:
            shortfalls.append({"rule": "MENU_SIZE_10", "actual": len(selected),
                               "reason": "保留历史排重、事实、质量、事件去重和纯技术上限后不足10条。"})
        reused = self._same_menu(previous, pool, selected)
        batch_id = previous["batch_id"] if reused else "tr-" + uuid.uuid4().hex
        index, public = [], []
        for number, item in enumerate(selected, 1):
            row = {key: item[key] for key in ("title", "platform", "url", "rank", "fetched_at")}
            cid = hashlib.sha256((batch_id + "\n" + row["platform"] + "\n" + row["url"]).encode("utf-8")).hexdigest()[:24]
            row.update(number=number, candidate_id=cid, source_verification=dict(_VERIFICATION))
            index.append(row)
            entry = self.trends._public_item(row)
            entry.update(selection_rule_version=VERSION, editorial_candidate_id=item["candidate_id"],
                         category=item["category"], non_ai=item["non_ai"], score=item["score"],
                         scores=dict(item["scores"]), lanes=item["lanes"], content_fit=item["content_fit"],
                         primary_lane=item["primary_lane"], primary_lane_score=item["primary_lane_score"],
                         technical=item["technical"], confidence=item["confidence"], missing_fields=item["missing_fields"],
                         why=item["why_read"], why_read=item["why_read"],
                         why_user=item["why_user"], angle=item["angle"], event_key=item["event_key"],
                         content_published_at=None, publication_time_display="未知",
                         personal_material_basis="未读取私人经历；可表达空间仅为编辑提议。",
                         occurrences=item["occurrences"], risk=item["risk"])
            public.append(entry)
        if not reused:
            state["hot_topics"] = {"batch_id": batch_id, "fetched_at": pool["fetched_at"],
                                   "items": index, "selection_rule_version": VERSION}
            self.runner._write_state(state)
        for report in pool["source_reports"]:
            report["eligible_count"] = sum(item["platform"] == report["id"] for item in eligible)
            report["selected_count"] = by_source[report["id"]]
        selected_editorial_ids = {item["candidate_id"] for item in selected}
        for row in audit:
            if row["decision"] == "ELIGIBLE":
                row["menu_selected"] = row["candidate_id"] in selected_editorial_ids
                if not row["menu_selected"]:
                    row["menu_reason"] = "通过单条质量门槛；组合受事件/来源/技术上限与软类别目标约束，或其他候选更合适。"
        return {key: value for key, value in {
            **{key: pool[key] for key in ("pool_id", "fetched_at", "timezone", "requested_sources", "succeeded_sources", "failed_sources", "source_reports")},
            "status": "UNAVAILABLE" if not public else "PARTIAL" if pool["failed_sources"] or shortfalls else "OK",
            "selection_rule_version": VERSION, "batch_id": batch_id, "reused": reused,
            "items": public, "counts": {**pool["counts"], "editorial_eligible": len(eligible),
                                          "editorial_excluded": len(audit) - len(eligible),
                                          "history_excluded": history_gate["excluded_count"], "recommended": len(public)},
            "history_gate": history_gate,
            "selection_summary": {"categories": {key: categories[key] for key in CATEGORIES},
                                  "non_ai": non_ai, "technical": sum(item["technical"] for item in selected),
                                  "source_count": len(by_source), "by_source": dict(by_source),
                                  "eligible_source_count": len(available_sources),
                                  "joint_feasible_source_coverage": feasible_coverage,
                                  "source_cap": cap, "quality_floor": QUALITY_FLOOR,
                                  "technical_limit": TECHNICAL_LIMIT,
                                  "content_floor": CONTENT_FLOOR, "lane_floor": LANE_FLOOR,
                                  "audience": "由使用者的目标读者说明、表达偏好和本轮素材决定；不预设作者身份",
                                  "soft_targets": dict(SOFT_TARGETS),
                                  "weights": WEIGHTS, "lane_weights": LANE_WEIGHTS,
                                  "formula": "content_fit_60 + max(lane_A_40, lane_B_40, lane_C_40)",
                                  "scoring_basis": "Codex编辑提议；不代表事实核验或咨询效果实测。",
                                  "composition_method": f"每来源/类别/技术/非AI签名保留最高分10项，搜索保留{BEAM_WIDTH}种中间方案；质量分+软目标每格2分+来源每个3分，不宣称全局最优。"},
            "relaxations": relaxations, "constraint_shortfalls": shortfalls, "audit": audit,
            "notice": "NewsNow第三方热榜；采集时间不是发布时间。仅标题线索，正文/事实待核实，不代表本人立场。回复选热点3。"
        }.items()}
