"""Pure, offline checks for the upstream MCP response and config boundary."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import sys
from pathlib import Path
import unittest


RUNTIME = Path(__file__).resolve().parents[2] / "scripts/trendradar_client.py"
sys.path.insert(0, str(RUNTIME.parent))
SPEC = importlib.util.spec_from_file_location("trendradar_client_pure_tests", RUNTIME)
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)
IDS = tuple(CLIENT.SOURCES)


class TrendRadarClientTests(unittest.TestCase):
    def test_current_policy_is_exactly_the_seven_requested_platforms(self):
        self.assertEqual(IDS, ("baidu", "nowcoder", "hupu", "juejin", "sspai", "weibo", "zhihu"))
        self.assertEqual(CLIENT.SOURCES["nowcoder"], "nowcoder.com")

    def test_runtime_mcp_sources_match_current_policy_and_reject_old_or_bad_lists(self):
        self.assertIsNone(CLIENT.validate_runtime_sources(list(IDS)))
        self.assertIsNone(CLIENT.validate_runtime_sources(list(reversed(IDS))))
        for values in (list(IDS[:-1]), list(IDS) + ["douyin"],
                       ["douyin", "baidu", "bilibili-hot-search", "hupu", "juejin", "sspai", "weibo", "zhihu"],
                       list(IDS[:-1]) + [IDS[0]], "baidu", None, list(IDS[:-1]) + [[]]):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "MCP_CONFIGURATION_MISMATCH"):
                    CLIENT.validate_runtime_sources(values)

    def test_removed_sources_are_rejected_in_live_config_and_response(self):
        for platform, domain in (("douyin", "douyin.com"), ("bilibili-hot-search", "bilibili.com")):
            with self.subTest(platform=platform):
                config = self.config()
                config["platforms"]["sources"][1] = {"id": platform, "expected_domain": domain}
                with self.assertRaisesRegex(ValueError, "SOURCE_CONFIGURATION_MISMATCH"):
                    CLIENT.validate_config(config)
                raw = self.raw()
                raw["summary"]["platforms"][1] = platform
                self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")

    def raw(self):
        return {"success": True, "summary": {"platforms": list(IDS), "failed_platforms": [],
            "crawl_time": "2026-10-20 23:30:00"}, "data": [
                {"platform_id": key, "title": "合成标题 " + key,
                 "url": "https://" + CLIENT.SOURCES[key] + "/synthetic", "ranks": [index, index + 1]}
                for index, key in enumerate(IDS, 1)]}

    def config(self):
        return {"platforms": {"enabled": True, "sources": [
            {"id": key, "expected_domain": domain, "enabled": True}
            for key, domain in CLIENT.SOURCES.items()]},
            **{key: {"enabled": False} for key in
               ("rss", "schedule", "notification", "ai_analysis", "ai_translation")},
            "app": {"timezone": "Asia/Shanghai"},
            "storage": {"backend": "local", "pull": {"enabled": False}, "remote": {"enabled": False}}}

    def assert_all_failed(self, value, error):
        self.assertEqual(value["requested_sources"], list(IDS))
        self.assertEqual(len(value["sources"]), len(IDS))
        self.assertEqual({s["id"] for s in value["sources"]}, set(IDS))
        for source in value["sources"]:
            self.assertEqual(source["status"], "FAILED")
            self.assertEqual(source["items"], [])
            self.assertEqual(source["error"], error)

    def test_all_current_sources_normalize_actual_rows_and_first_observed_rank(self):
        value = CLIENT.normalize_crawl(self.raw())
        self.assertEqual(value["requested_sources"], list(IDS))
        self.assertEqual(len(value["sources"]), len(IDS))
        for index, source in enumerate(value["sources"], 1):
            self.assertEqual(source["status"], "OK")
            self.assertEqual(len(source["items"]), 1)
            self.assertEqual(source["items"][0]["rank"], index)
            self.assertNotIn("error", source)

    def test_shanghai_collection_clock_is_not_host_timezone_or_publication_time(self):
        value = CLIENT.normalize_crawl(self.raw())
        self.assertEqual(value["fetched_at"], "2026-10-20T23:30:00+08:00")
        stamp = datetime.fromisoformat(value["fetched_at"])
        self.assertEqual(stamp.astimezone(timezone.utc).isoformat(), "2026-10-20T15:30:00+00:00")
        self.assertNotIn("content_published_at", value)
        self.assertNotIn("event_at", value)

    def test_partial_failure_preserves_other_rows_without_fabricating_failed_rows(self):
        raw = self.raw()
        raw["summary"]["platforms"] = list(IDS[:-1])
        raw["summary"]["failed_platforms"] = [IDS[-1]]
        raw["data"] = raw["data"][:-1]
        value = CLIENT.normalize_crawl(raw)
        self.assertEqual(sum(s["status"] == "OK" for s in value["sources"]), len(IDS) - 1)
        last = value["sources"][-1]
        self.assertEqual(last, {"id": IDS[-1], "status": "FAILED", "error": "SOURCE_FAILED", "items": []})

    def test_all_reported_failures_yield_no_cached_or_invented_rows(self):
        raw = self.raw()
        raw["summary"].update(platforms=[], failed_platforms=list(IDS))
        raw["data"] = []
        self.assert_all_failed(CLIENT.normalize_crawl(raw), "SOURCE_FAILED")

    def test_missing_upstream_platform_is_explicit_missing_source(self):
        raw = self.raw()
        raw["summary"]["platforms"] = list(IDS[:-1])
        raw["data"] = raw["data"][:-1]
        value = CLIENT.normalize_crawl(raw)
        self.assertEqual(value["sources"][-1]["error"], "MISSING_SOURCE")
        self.assertEqual(value["sources"][-1]["items"], [])

    def test_upstream_error_details_do_not_leak_to_normalized_failure(self):
        value = CLIENT.normalize_crawl({"success": False, "error": "SYNTHETIC_PRIVATE_TOKEN_SENTINEL"})
        self.assert_all_failed(value, "UPSTREAM_ERROR")
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN_SENTINEL", json.dumps(value))

    def test_response_shape_drift_fails_closed(self):
        for mutate in (
            lambda raw: raw.pop("summary"),
            lambda raw: raw.update(summary=[]),
            lambda raw: raw.update(data={}),
            lambda raw: raw["summary"].update(platforms="baidu"),
            lambda raw: raw["summary"].update(failed_platforms=None),
            lambda raw: raw["summary"].update(crawl_time="unknown"),
            lambda raw: raw["summary"].pop("crawl_time"),
            lambda raw: raw["data"].append(None),
        ):
            with self.subTest(mutation=mutate):
                raw = self.raw()
                mutate(raw)
                self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")

    def test_ninth_source_in_summary_or_data_is_rejected(self):
        raw = self.raw()
        raw["summary"]["platforms"].append("unauthorized-ninth")
        self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")
        raw = self.raw()
        raw["data"].append({"platform_id": "unauthorized-ninth", "title": "extra"})
        self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")

    def test_ambiguous_success_failure_or_duplicate_platform_is_rejected(self):
        for field in ("platforms", "failed_platforms"):
            raw = self.raw()
            raw["summary"][field].append(IDS[0])
            self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")

    def test_rows_from_failed_source_are_not_claimed_as_success(self):
        raw = self.raw()
        raw["summary"]["platforms"] = list(IDS[:-1])
        raw["summary"]["failed_platforms"] = [IDS[-1]]
        self.assert_all_failed(CLIENT.normalize_crawl(raw), "PARSE_ERROR")

    def test_missing_rank_stays_unknown(self):
        raw = self.raw()
        raw["data"][0].pop("ranks")
        self.assertIsNone(CLIENT.normalize_crawl(raw)["sources"][0]["items"][0]["rank"])

    def test_exact_current_sources_all_disabled_features_and_timezone_pass(self):
        result = CLIENT.validate_config(self.config())
        self.assertEqual(result["sources"], list(IDS))
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(set(result["disabled"]), {"rss", "schedule", "notification", "ai_analysis", "ai_translation"})

    def test_ninth_missing_duplicate_or_disabled_source_configuration_refuses(self):
        base = self.config()
        cases = []
        extra = deepcopy(base)
        extra["platforms"]["sources"].append({"id": "ninth", "expected_domain": "example.com"})
        cases.append(extra)
        missing = deepcopy(base)
        missing["platforms"]["sources"].pop()
        cases.append(missing)
        duplicate = deepcopy(base)
        duplicate["platforms"]["sources"][-1] = deepcopy(duplicate["platforms"]["sources"][0])
        cases.append(duplicate)
        disabled = deepcopy(base)
        disabled["platforms"]["sources"][0]["enabled"] = False
        cases.append(disabled)
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "SOURCE_CONFIGURATION_MISMATCH"):
                    CLIENT.validate_config(config)

    def test_domain_drift_or_platform_feature_disabled_refuses(self):
        config = self.config()
        config["platforms"]["sources"][0]["expected_domain"] = "example.com"
        with self.assertRaisesRegex(ValueError, "SOURCE_CONFIGURATION_MISMATCH"):
            CLIENT.validate_config(config)
        config = self.config()
        config["platforms"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "SOURCE_CONFIGURATION_MISMATCH"):
            CLIENT.validate_config(config)

    def test_forbidden_features_enabled_or_missing_refuse(self):
        for section in ("rss", "schedule", "notification", "ai_analysis", "ai_translation"):
            for value in (True, None):
                config = self.config()
                config[section]["enabled"] = value
                with self.subTest(section=section, value=value):
                    with self.assertRaisesRegex(ValueError, "DISABLED_FEATURE_REQUIRED"):
                        CLIENT.validate_config(config)

    def test_timezone_or_remote_upload_change_refuses(self):
        config = self.config()
        config["app"]["timezone"] = "UTC"
        with self.assertRaisesRegex(ValueError, "TIMEZONE_MISMATCH"):
            CLIENT.validate_config(config)
        config = self.config()
        config["storage"]["remote"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "REMOTE_STORAGE_BLOCKED"):
            CLIENT.validate_config(config)

    def test_actual_storage_remote_backend_refuses(self):
        config = self.config()
        config["storage"]["backend"] = "remote"
        with self.assertRaisesRegex(ValueError, "REMOTE_STORAGE_BLOCKED"):
            CLIENT.validate_config(config)

    def test_actual_storage_pull_enabled_or_missing_refuses(self):
        for enabled in (True, None):
            config = self.config()
            config["storage"]["pull"]["enabled"] = enabled
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ValueError, "REMOTE_STORAGE_BLOCKED"):
                    CLIENT.validate_config(config)


if __name__ == "__main__":
    unittest.main()
