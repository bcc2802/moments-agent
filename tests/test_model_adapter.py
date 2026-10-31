"""Model boundary tests with a synthetic CLI; no network or account calls."""
import importlib.util
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import model_adapter as adapter


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        version = patch.object(adapter, "_version", return_value=(0, 165, 0))
        version.start()
        self.addCleanup(version.stop)

    def output(self, result):
        def run(args, **kwargs):
            self.args, self.options = args, kwargs
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        return run

    def variants(self):
        return {"variants": [{"title": str(n), "angle": str(n), "body": "仅合成素材正文", "tags": ["实践"]}
                             for n in range(3)], "note": "合成"}

    def test_generation_is_structured_without_shell_or_user_material_in_argv(self):
        marker = "不可信材料$(do-not-execute)"
        with patch.object(adapter, "executable", return_value="synthetic-codex"), \
                patch.object(adapter.subprocess, "run", side_effect=self.output(self.variants())):
            result = adapter.generate({"demo": True}, marker)
        self.assertEqual(3, len(result["variants"]))
        self.assertNotIn(marker, " ".join(self.args))
        self.assertIn(marker, self.options["input"])
        self.assertFalse(self.options.get("shell", False))
        self.assertIn("--ignore-user-config", self.args)
        self.assertIn("--ephemeral", self.args)
        self.assertEqual("read-only", self.args[self.args.index("--sandbox") + 1])
        self.assertIn("features.shell_tool=false", self.args)
        self.assertIn("features.plugins=false", self.args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.args)

    def test_revision_returns_complete_body(self):
        with patch.object(adapter, "executable", return_value="synthetic-codex"), \
                patch.object(adapter.subprocess, "run", side_effect=self.output({"body": "合成修订正文"})):
            self.assertEqual("合成修订正文", adapter.revise({}, "合成旧稿", "删去套话")["body"])

    def test_missing_cli_and_oversized_context_fail_without_call(self):
        with patch.object(adapter, "executable", return_value=None), patch.object(adapter.subprocess, "run") as run:
            with self.assertRaises(adapter.ModelError) as caught:
                adapter.generate({}, "材料")
            self.assertEqual("MODEL_NOT_INSTALLED", caught.exception.code)
            run.assert_not_called()
        with self.assertRaises(adapter.ModelError) as caught:
            adapter.generate({"body": "长" * 50000}, "材料")
        self.assertEqual("CONTEXT_TOO_LARGE", caught.exception.code)

    def test_incomplete_variants_rejected(self):
        for value in ({"variants": [], "note": ""}, {"variants": [None] * 3, "note": ""}, {"body": "wrong"}):
            with self.subTest(value=value), patch.object(adapter, "executable", return_value="synthetic-codex"), \
                    patch.object(adapter.subprocess, "run", side_effect=self.output(value)):
                with self.assertRaises(adapter.ModelError) as caught:
                    adapter.generate({}, "材料")
                self.assertEqual("MODEL_FORMAT", caught.exception.code)

    def test_auth_limit_timeout_and_unknown_failures_are_redacted(self):
        for message, code in (("401 unauthorized secret-value", "MODEL_AUTH"),
                              ("429 usage_limit secret-value", "MODEL_LIMIT"),
                              ("unknown private-path secret-value", "MODEL_UNAVAILABLE")):
            with self.subTest(code=code), patch.object(adapter, "executable", return_value="synthetic-codex"), \
                    patch.object(adapter.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", message)):
                with self.assertRaises(adapter.ModelError) as caught:
                    adapter.generate({}, "材料")
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn("secret-value", str(caught.exception))
        with patch.object(adapter, "executable", return_value="synthetic-codex"), \
                patch.object(adapter.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 240)):
            with self.assertRaises(adapter.ModelError) as caught:
                adapter.generate({}, "材料")
            self.assertEqual("MODEL_TIMEOUT", caught.exception.code)

    def test_readiness_does_not_echo_account_output(self):
        with patch.object(adapter, "executable", return_value="synthetic-codex"), \
                patch.object(adapter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "private-id", "private-id")):
            value = adapter.readiness()
        self.assertTrue(value["authenticated"])
        self.assertNotIn("private-id", json.dumps(value))

    def test_old_or_unrecognized_cli_fails_before_sending_material(self):
        for version in ((0, 142, 2), None):
            with self.subTest(version=version), patch.object(adapter, "_version", return_value=version), \
                    patch.object(adapter, "executable", return_value="synthetic-codex"), \
                    patch.object(adapter.subprocess, "run") as run:
                self.assertEqual("CLI_UPDATE_REQUIRED", adapter.readiness()["code"])
                with self.assertRaises(adapter.ModelError) as caught:
                    adapter.generate({}, "不能发送的材料")
                self.assertEqual("CLI_UPDATE_REQUIRED", caught.exception.code)
                run.assert_not_called()

    def test_rewrite_presets_are_allowlisted_before_any_model_call(self):
        with patch.object(adapter.subprocess, "run") as run:
            for value in ("unknown", "professional;run-command", {}, [], 1):
                with self.subTest(value=value), self.assertRaises(adapter.ModelError) as caught:
                    adapter.revision_instruction(value, "保留事实")
                self.assertEqual("INVALID_REWRITE_PRESET", caught.exception.code)
            run.assert_not_called()

    def test_preset_keeps_fact_boundary_and_appends_user_preference(self):
        value = adapter.revision_instruction("authentic", "保留最后一个问题")
        self.assertIn("不新增客户", value)
        self.assertIn("不要为了生动补造", value)
        self.assertTrue(value.endswith("保留最后一个问题"))
        options = adapter.rewrite_preset_options()
        self.assertEqual({"colloquial", "authentic", "professional"}, {x["id"] for x in options})
        self.assertTrue(all(set(x) == {"id", "label", "description"} for x in options))

    def test_manual_revision_still_works_and_empty_or_large_input_is_rejected(self):
        self.assertEqual("  保留自己的分行\n", adapter.revision_instruction(None, "  保留自己的分行\n"))
        for preset, custom in ((None, ""), ("colloquial", "长" * 4001), ("professional", "长" * 3900)):
            with self.subTest(preset=preset), self.assertRaises(adapter.ModelError):
                adapter.revision_instruction(preset, custom)


class ZhipuAdapterTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(adapter.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = {"provider": "zhipu", "model": "glm-5.3-flash"}
        self.key = "synthetic-key-only-for-tests.0123456789"
        adapter.os.environ["ZHIPU_API_KEY"] = self.key

    def response(self, result=None, *, finish="stop", done=True, reasoning="PRIVATE_REASONING_NEVER_DISPLAY"):
        if result is None:
            result = {"body": "合成修订正文"}
        rows = [{"choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]},
                {"choices": [{"index": 0, "delta": {"content": json.dumps(result, ensure_ascii=False)}, "finish_reason": finish}]}]
        data = "\n\n".join("data: " + json.dumps(row, ensure_ascii=False) for row in rows)
        if done:
            data += "\n\ndata: [DONE]\n\n"
        stream = io.BytesIO(data.encode("utf-8"))
        stream.status = 200
        return stream

    def test_zhipu_uses_only_official_endpoint_and_no_codex_or_tools(self):
        events = []
        with patch.object(adapter.urllib.request, "build_opener") as opener, \
                patch.object(adapter.subprocess, "run") as cli:
            opener.return_value.open.return_value = self.response()
            result = adapter.revise({}, "合成旧稿", "删去套话", settings=self.settings,
                                    progress=lambda *args: events.append(args))
        cli.assert_not_called()
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(adapter.ZHIPU_ENDPOINT, request.full_url)
        self.assertEqual("POST", request.method)
        self.assertEqual("Bearer " + self.key, request.get_header("Authorization"))
        body = json.loads(request.data)
        self.assertEqual("glm-5.3-flash", body["model"])
        self.assertTrue(body["stream"])
        self.assertEqual("low", body["reasoning_effort"])
        self.assertEqual({"type": "enabled"}, body["thinking"])
        self.assertNotIn("tools", body)
        self.assertNotIn(self.key, request.data.decode())
        self.assertEqual("合成修订正文", result["body"])
        self.assertEqual(["requesting", "receiving", "validating"], [x[0] for x in events])
        self.assertNotIn("PRIVATE_REASONING_NEVER_DISPLAY", json.dumps(events))

    def test_zhipu_reuses_same_strict_three_variant_validation(self):
        variants = {"variants": [{"title": str(n), "angle": str(n), "body": "仅合成素材正文", "tags": ["实践"]}
                                  for n in range(3)], "note": "合成"}
        for value, valid in ((variants, True), ({"variants": [], "note": ""}, False), ({"body": "x"}, False)):
            with self.subTest(valid=valid), patch.object(adapter.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.response(value)
                if valid:
                    self.assertEqual(3, len(adapter.generate({}, "合成素材", settings=self.settings)["variants"]))
                else:
                    with self.assertRaises(adapter.ModelError) as caught:
                        adapter.generate({}, "合成素材", settings=self.settings)
                    self.assertEqual("MODEL_FORMAT", caught.exception.code)

    def test_partial_or_truncated_stream_is_rejected_without_retry(self):
        for finish, done in (("length", True), ("stop", False), (None, True)):
            with self.subTest(finish=finish, done=done), patch.object(adapter.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.response(finish=finish, done=done)
                with self.assertRaises(adapter.ModelError) as caught:
                    adapter.revise({}, "合成旧稿", "删套话", settings=self.settings)
                self.assertEqual("MODEL_FORMAT", caught.exception.code)
                self.assertEqual(1, opener.return_value.open.call_count)

    def test_http_failures_do_not_expose_vendor_text_or_fallback(self):
        for status, code in ((401, "MODEL_AUTH"), (403, "MODEL_ACCESS"), (429, "MODEL_LIMIT"), (500, "MODEL_UNAVAILABLE"), (302, "MODEL_UNAVAILABLE")):
            with self.subTest(status=status), patch.object(adapter.urllib.request, "build_opener") as opener, \
                    patch.object(adapter.subprocess, "run") as cli:
                opener.return_value.open.side_effect = urllib.error.HTTPError(adapter.ZHIPU_ENDPOINT, status, self.key, {}, io.BytesIO(self.key.encode()))
                with self.assertRaises(adapter.ModelError) as caught:
                    adapter.generate({}, "合成素材", settings=self.settings)
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn(self.key, str(caught.exception))
                self.assertEqual(1, opener.return_value.open.call_count)
                cli.assert_not_called()

    def test_timeout_is_redacted_and_does_not_retry(self):
        with patch.object(adapter.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError(self.key)
            with self.assertRaises(adapter.ModelError) as caught:
                adapter.generate({}, "合成素材", settings=self.settings)
        self.assertEqual("MODEL_TIMEOUT", caught.exception.code)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(1, opener.return_value.open.call_count)

    def test_readiness_has_no_network_or_secret_and_missing_key_is_clear(self):
        with patch.object(adapter.urllib.request, "build_opener") as opener:
            ready = adapter.readiness(self.settings)
            self.assertTrue(ready["available"])
            self.assertEqual("zhipu", ready["provider"])
            self.assertNotIn(self.key, json.dumps(ready))
            adapter.os.environ.pop("ZHIPU_API_KEY")
            self.assertEqual("MODEL_KEY_MISSING", adapter.readiness(self.settings)["code"])
            opener.assert_not_called()

    def test_labelled_key_file_is_backend_only_and_provider_switch_keeps_pointer(self):
        adapter.os.environ.pop("ZHIPU_API_KEY")
        with tempfile.TemporaryDirectory(prefix="moments-provider-test-") as temporary:
            root = Path(temporary)
            keyfile = root / "synthetic-key.txt"
            keyfile.write_text("智谱API\nAPIkey：" + self.key, encoding="utf-8-sig")
            (root / "model.local.json").write_text(json.dumps({"provider": "zhipu", "api_key_file": str(keyfile)}), encoding="utf-8")
            settings = adapter.load_settings(root)
            self.assertTrue(adapter.readiness(settings)["available"])
            public = json.dumps(adapter.readiness(settings))
            self.assertNotIn(str(keyfile), public)
            self.assertNotIn(self.key, public)
            selected = adapter.save_provider(root, "codex")
            self.assertEqual("codex", selected["provider"])
            self.assertEqual(str(keyfile), selected["api_key_file"])
            self.assertNotIn(self.key, (root / "model.local.json").read_text())
            self.assertEqual("zhipu", adapter.save_provider(root, "zhipu")["provider"])
            with self.assertRaises(adapter.ModelError):
                adapter.save_provider(root, "arbitrary-url")

    def test_ambiguous_key_file_and_raw_key_config_are_rejected(self):
        adapter.os.environ.pop("ZHIPU_API_KEY")
        with tempfile.TemporaryDirectory(prefix="moments-provider-test-") as temporary:
            root = Path(temporary)
            keyfile = root / "keys.txt"
            keyfile.write_text(self.key + "\nsecond-synthetic-key.0123456789", encoding="utf-8")
            self.assertEqual("MODEL_KEY_INVALID", adapter.readiness({**self.settings, "api_key_file": str(keyfile)})["code"])
            (root / "model.local.json").write_text(json.dumps({"provider": "zhipu", "api_key": self.key}), encoding="utf-8")
            with self.assertRaises(adapter.ModelError) as caught:
                adapter.load_settings(root)
            self.assertNotIn(self.key, str(caught.exception))

    def test_redirect_handler_refuses_cross_host_authorization(self):
        request = adapter.urllib.request.Request(adapter.ZHIPU_ENDPOINT, headers={"Authorization": "Bearer " + self.key})
        with self.assertRaises(urllib.error.HTTPError):
            adapter._NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://example.invalid/steal")


if __name__ == "__main__":
    unittest.main()
