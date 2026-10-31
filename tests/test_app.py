"""Loopback HTTP tests using a synthetic workspace and an explicit model double."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))
spec = importlib.util.spec_from_file_location("local_moments_app", PACKAGE / "scripts/app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class FakeModel:
    def __init__(self):
        self.contexts = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.invalid = False
        self.settings = []

    def readiness(self, settings=None):
        settings = settings or {}
        return {"available": True, "authenticated": True, "transport": "test-double",
                "provider": settings.get("provider"), "model": settings.get("model"), "label": "合成模型替身",
                "message": "Synthetic model double; no real model or login was tested."}

    def wait(self, context, settings=None, progress=None):
        self.contexts.append(context)
        self.settings.append(dict(settings or {}))
        if progress:
            progress("requesting", "Synthetic request submitted; not real model reasoning.")
        self.started.set()
        if self.block and not self.release.wait(5):
            raise RuntimeError("test timed out")
        if progress:
            progress("receiving", "Synthetic response received.")

    def generate(self, context, material, settings=None, progress=None):
        self.wait(context, settings, progress)
        return {"variants": [] if self.invalid else [
            {"title": "合成稿" + str(n), "angle": "合成角度" + str(n),
             "body": "这是测试替身返回的第" + str(n) + "稿。\n" + material, "tags": ["实践"]}
            for n in (1, 2, 3)]}

    def revise(self, context, body, instruction, settings=None, progress=None):
        self.wait(context, settings, progress)
        return {"body": body + "\n合成修改：" + instruction}


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="moments-app-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assets = self.root / "code"
        (self.assets / "templates").mkdir(parents=True)
        for filename, body in {
            "author.example.md": "# 个人说明书\n- 对外称呼：合成作者\n",
            "audience.example.md": "# 目标读者\n- 首要读者是谁：合成读者\n",
            "preferences.example.md": "---\nmoments_agent: moments-draft\nrecord_type: moments_style_profile\n---\n# 写作偏好\n\n已有案例证据应保留。\n",
        }.items():
            (self.assets / "templates" / filename).write_text(body, encoding="utf-8")
        web = self.assets / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><h1>本地测试界面</h1>", encoding="utf-8")
        (web / "app.js").write_text("'use strict';", encoding="utf-8")
        (web / "style.css").write_text("body{color:black}", encoding="utf-8")
        self.patch_assets = patch.object(app.workspace_tools, "CODE_ROOT", self.assets)
        self.patch_assets.start()
        self.addCleanup(self.patch_assets.stop)
        self.patch_web = patch.object(app, "CODE_ROOT", self.assets)
        self.patch_web.start()
        self.addCleanup(self.patch_web.stop)
        self.user = self.root / "private"
        app.workspace_tools.initialize(self.user)
        self.model = FakeModel()
        self.application = app.Application(self.user, demo=True, adapter=self.model)
        self.server = app.LocalServer(self.application, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.model.release.set()
        deadline = time.monotonic() + 3
        while self.application.active_job and time.monotonic() < deadline:
            time.sleep(0.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, data=None, *, authorized=True, headers=None, raw=None):
        values = {"Host": "127.0.0.1:" + str(self.server.server_address[1])}
        if authorized:
            values["Authorization"] = "Bearer " + self.application.token
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        body = response.read()
        result = json.loads(body.decode("utf-8")) if response.getheader("Content-Type", "").startswith("application/json") else body
        code, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return code, result, response_headers

    def wait_job(self, job_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            code, result, _ = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(code, 200)
            if result["status"] in ("DONE", "ERROR"):
                return result
            time.sleep(0.01)
        self.fail("Background test job did not finish")

    def generate(self, material="合成材料：我把开工问题写在一张清单里，不代表真人经历。"):
        code, queued, _ = self.request("POST", "/api/generate", {"material": material})
        self.assertEqual(code, 202, queued)
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "DONE", done)
        return done["result"]["round"]

    def test_authentication_host_and_origin_boundaries(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request("GET", "/api/state", authorized=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/state", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Origin": self.server.origin})[0], 200)
        self.assertEqual(self.request("OPTIONS", "/api/profile", authorized=False)[0], 405)

    def test_state_is_read_only_and_static_files_are_an_exact_allowlist(self):
        before = hashes(self.user)
        code, state, _ = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(state["demo"])
        self.assertIsNone(state["round"])
        self.assertEqual(state["library"], [])
        self.assertEqual(hashes(self.user), before)
        code, body, headers = self.request("GET", "/", authorized=False)
        self.assertEqual(code, 200)
        self.assertIn("本地测试界面", body.decode("utf-8"))
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ("/scripts/app.py", "/../config.json", "/%2e%2e/config.json", "/api/state?path=outside"):
            self.assertGreaterEqual(self.request("GET", path)[0], 400)

    def test_post_requires_json_bounded_body_and_known_fields(self):
        self.assertEqual(self.request("POST", "/api/generate", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/generate", raw=b"x", headers={"Content-Type": "application/json", "Content-Length": str(app.MAX_BODY_BYTES + 1)})[0], 413)
        self.assertEqual(self.request("POST", "/api/generate", raw=b"NaN", headers={"Content-Type": "application/json"})[0], 400)
        code, result, _ = self.request("POST", "/api/generate", {"material": "合成材料", "path": "../private"})
        self.assertEqual(code, 400)
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertFalse(self.model.contexts)

    def test_profile_writes_fixed_documents_backs_up_and_preserves_style_evidence(self):
        form = {kind: {key: "合成" + key for key in fields} for kind, fields in app._FIELDS.items()}
        code, result, _ = self.request("POST", "/api/profile", form)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["profiles"]["author"]["fields"]["name"], "合成name")
        preferences = (self.user / "archive/_style/00_写作偏好.md").read_text(encoding="utf-8")
        self.assertIn("已有案例证据应保留", preferences)
        self.assertIn("record_type: moments_style_profile", preferences)
        self.assertTrue(list((self.user / "backups/runtime").glob("*.bak")))
        before_files = set(hashes(self.user))
        form["author"]["name"] = "另一位合成作者"
        self.assertEqual(self.request("POST", "/api/profile", form)[0], 200)
        self.assertEqual((self.user / "archive/_style/00_写作偏好.md").read_text(encoding="utf-8").count(app._PREFERENCE_START), 1)
        self.assertFalse(any("personal" in name and name not in {"personal/author.md", "personal/audience.md"} for name in set(hashes(self.user)) - before_files))

    def test_profile_form_preserves_handwritten_extensions_and_only_replaces_unchanged_synthetic_starter(self):
        author = self.user / "personal/author.md"
        audience = self.user / "personal/audience.md"
        author.write_text("# 本人手写说明书\n\n- 对外称呼：旧称呼\n\n## 可引用经历的依据\n手写扩展、来源与确认日期：保留这段。\n", encoding="utf-8")
        audience.write_text("# 本人读者说明\n\n手写调研边界：尚未验证付费意愿。\n", encoding="utf-8")
        form = {kind: {key: "明确保存的" + key for key in fields} for kind, fields in app._FIELDS.items()}
        for name in ("第一称呼", "第二称呼"):
            form["author"]["name"] = name
            code, result, _ = self.request("POST", "/api/profile", form)
            self.assertEqual(code, 200, result)
            self.assertEqual(result["profiles"]["author"]["fields"]["name"], name)
        self.assertIn("手写扩展、来源与确认日期：保留这段。", author.read_text(encoding="utf-8"))
        self.assertIn("手写调研边界：尚未验证付费意愿。", audience.read_text(encoding="utf-8"))
        self.assertEqual(author.read_text(encoding="utf-8").count("<!-- moments-gui-profile:author:v1 -->"), 1)
        starter = "# 个人说明书\n\n状态：合成示例，非真人。\n- 对外称呼：合成默认名\n"
        (self.assets / "templates/author.example.md").write_text(starter, encoding="utf-8", newline="")
        author.write_text(starter + "\n手写追加：此句必须保留。\n", encoding="utf-8", newline="")
        self.assertEqual(self.request("POST", "/api/profile", form)[0], 200)
        saved = author.read_text(encoding="utf-8")
        self.assertNotIn("合成默认名", saved)
        self.assertNotIn("状态：合成示例，非真人", saved)
        self.assertIn("手写追加：此句必须保留。", saved)

    def test_url_only_material_is_rejected_and_mixed_material_records_no_fetch(self):
        for value in ("https://example.com/article", "请用这个链接帮我写朋友圈：https://example.com/article"):
            code, error, _ = self.request("POST", "/api/generate", {"material": value})
            self.assertEqual((code, error["code"]), (400, "SOURCE_TEXT_REQUIRED"))
        self.assertFalse(self.model.contexts)
        self.generate("参考链接：https://example.com/article，用户粘贴的合成要点：先明确资料、交付范围与确认人。我想表达的是开工前讲清楚能减少来回沟通。")
        access = self.model.contexts[0]["link_access"]
        self.assertEqual(access["status"], "NOT_FETCHED")
        self.assertEqual(access["link_count"], 1)
        self.assertIn("没有访问", access["notice"])

    def test_formal_mode_requires_explicit_profile_save_and_rejects_reserved_markers(self):
        self.application.demo = False
        self.assertFalse(self.request("GET", "/api/state")[1]["profile_ready"])
        code, error, _ = self.request("POST", "/api/generate", {"material": "合成材料"})
        self.assertEqual((code, error["code"]), (400, "PROFILE_REQUIRED"))
        self.assertFalse(self.model.contexts)
        form = {kind: {key: "" for key in fields} for kind, fields in app._FIELDS.items()}
        form["author"]["identity"] = "明确由测试填写的身份与业务"
        form["audience"]["people"] = "明确由测试填写的读者"
        form["preferences"]["tone"] = "平实口语，不补造经历"
        self.assertEqual(self.request("POST", "/api/profile", form)[0], 200)
        self.assertTrue(self.request("GET", "/api/state")[1]["profile_ready"])
        state = self.request("GET", "/api/state")[1]
        self.assertTrue(state["onboarding"]["ready"])
        self.assertFalse(state["onboarding"]["history_ready"])
        self.assertFalse(state["onboarding"]["history_required"])
        self.assertEqual(state["onboarding"]["history_count"], 0)
        self.assertFalse(self.generate("合成事实：尚未导入历史，仅用当次素材写作。")["notes"] == [])
        self.assertEqual(self.model.contexts[-1]["history"], [])
        self.assertEqual(self.request("POST", "/api/history", {"filename": "本人旧作.md", "body": "本人提供的合成写法参考，非真实发布案例。"})[0], 200)
        self.assertFalse(self.generate()["notes"] == [])
        form["preferences"]["tone"] = app._PREFERENCE_START
        self.assertEqual(self.request("POST", "/api/profile", form)[1]["code"], "RESERVED_MARKER")

    def test_three_required_profile_fields_are_enforced_and_partial_updates_keep_hidden_values(self):
        self.application.demo = False
        form = {"author": {"identity": "测试身份与业务"}, "audience": {"people": "测试读者"},
                "preferences": {"tone": "测试表达偏好"}}
        state = self.request("GET", "/api/state")[1]
        self.assertEqual({(r["group"], r["key"]) for r in state["onboarding"]["missing_profile_fields"]},
                         {("author", "identity"), ("audience", "people"), ("preferences", "tone")})
        for group, key in (("author", "identity"), ("audience", "people"), ("preferences", "tone")):
            invalid = json.loads(json.dumps(form))
            invalid[group][key] = "  "
            before = hashes(self.user)
            code, error, _ = self.request("POST", "/api/profile", invalid)
            self.assertEqual((code, error["code"]), (400, "REQUIRED_PROFILE_FIELD"))
            self.assertEqual(hashes(self.user), before)
        full = {kind: {key: "保留的" + key for key in fields} for kind, fields in app._FIELDS.items()}
        self.assertEqual(self.request("POST", "/api/profile", full)[0], 200)
        code, result, _ = self.request("POST", "/api/profile", form)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["profiles"]["author"]["fields"]["name"], "保留的name")
        self.assertEqual(result["profiles"]["author"]["fields"]["work"], "保留的work")
        self.assertEqual(result["profiles"]["author"]["fields"]["boundaries"], "保留的boundaries")
        self.assertEqual(result["profiles"]["audience"]["fields"]["problems"], "保留的problems")
        self.assertEqual(result["profiles"]["preferences"]["fields"]["avoid"], "保留的avoid")
        self.assertTrue(self.request("GET", "/api/state")[1]["onboarding"]["profile_ready"])

    def test_only_confirmed_published_final_enters_materials_and_future_context(self):
        original = "# 本人导入旧作\n这是合成的本人文风参考，原文不修改。"
        self.assertEqual(self.request("POST", "/api/history", {"filename": "旧作.md", "body": original})[0], 200)
        source_hash = hashes(self.user / "inputs")
        current = self.generate()
        self.assertTrue(all(not row["kept"] for row in current["notes"]))
        material = self.request("GET", "/api/state")[1]["materials"]
        self.assertEqual([row["kind"] for row in material["items"]], ["history"])
        rid = current["round_id"]
        final = "本人最终确认的合成文字。\n\n这不是实际发表案例。"
        self.assertEqual(self.request("POST", "/api/final", {"round_id": rid, "variant": 2, "body": final})[0], 200)
        self.assertEqual([row["kind"] for row in self.request("GET", "/api/state")[1]["materials"]["items"]], ["history"])
        self.assertEqual(self.request("POST", "/api/publish", {"round_id": rid, "variant": 2, "confirmed": True})[0], 200)
        material = self.request("GET", "/api/state")[1]["materials"]
        finals = [row for row in material["items"] if row["kind"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["body"], final)
        self.assertEqual(finals[0]["status"], "已发布")
        self.assertTrue(finals[0]["reference_eligible"])
        self.assertEqual(len(list((self.user / "archive").glob("*-v*.md"))), 1,
                         "Unselected temporary candidates retain core cleanup behavior")
        self.generate("下一轮合成事实材料，与上一轮不是相同经历。")
        future = self.model.contexts[-1]["history"]
        self.assertEqual(len(future), 2)
        self.assertEqual({row["kind"] for row in future}, {"history", "final"})
        self.assertIn(final, [row["body"] for row in future])
        self.assertEqual(hashes(self.user / "inputs"), source_hash)
        self.assertEqual(len([r for r in self.request("GET", "/api/state")[1]["materials"]["items"] if r["kind"] == "final"]), 1)

    def test_published_reference_eligibility_is_bound_to_current_canonical_body(self):
        current = self.generate()
        rid = current["round_id"]
        self.assertEqual(self.request("POST", "/api/publish", {"round_id": rid, "variant": 1, "confirmed": True})[0], 200)
        item = self.request("GET", "/api/state")[1]["materials"]["items"][0]
        path = Path(item["path"])
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'gui_reference_basis: "USER_REPORTED_PUBLISHED"', b'gui_reference_basis: "USER_SAVED_FINAL"'))
        self.assertEqual(self.request("GET", "/api/state")[1]["materials"]["items"], [],
                         "A saved final alone cannot establish publication adoption")
        path.write_bytes(raw.replace(item["body"].encode("utf-8"), "外部修改的合成正文，尚未再次确认。".encode("utf-8")))
        self.assertEqual(self.request("GET", "/api/state")[1]["materials"]["items"], [])
        self.assertEqual(self.request("POST", "/api/final", {"round_id": rid, "variant": 1, "body": "不应悄悄覆盖已发稿"})[1]["code"], "ALREADY_PUBLISHED")

    def test_automatic_unpublished_style_evidence_never_enters_model_preferences(self):
        form = {kind: {key: "本人明确填写的" + key for key in fields} for kind, fields in app._FIELDS.items()}
        self.assertEqual(self.request("POST", "/api/profile", form)[0], 200)
        current = self.generate()
        unpublished = "UNPUBLISHED_STYLE_EVIDENCE_7B19 未发表的合成回稿正文。"
        self.assertEqual(self.request("POST", "/api/final", {"round_id": current["round_id"],
                                                               "variant": 2, "body": unpublished})[0], 200)
        style = self.user / "archive/_style/00_写作偏好.md"
        with style.open("a", encoding="utf-8") as stream:
            stream.write("\n\n## 自动观察（未发回稿审计）\n" + unpublished + "\n")
        original_style = style.read_bytes()
        context = self.application.context("下一轮合成素材")
        self.assertNotIn("UNPUBLISHED_STYLE_EVIDENCE_7B19", json.dumps(context, ensure_ascii=False))
        self.assertEqual(context["profiles"]["preferences"]["fields"], form["preferences"])
        self.assertEqual(context["profiles"]["preferences"]["source"], "explicit_gui_preferences_only")
        self.assertEqual(style.read_bytes(), original_style, "Filtering model input must not delete local audit evidence")
        self.assertEqual(self.request("POST", "/api/publish", {"round_id": current["round_id"],
                                                                "variant": 2, "confirmed": True})[0], 200)
        adopted = self.application.context("下一轮合成素材")
        self.assertIn(unpublished, [row["body"] for row in adopted["history"]])

    def test_profile_size_and_ownership_are_checked_before_any_document_is_changed(self):
        path = self.user / "archive/_style/00_写作偏好.md"
        form = {kind: {key: "合成字段" for key in fields} for kind, fields in app._FIELDS.items()}
        path.write_text("# 非Agent偏好文件\n", encoding="utf-8")
        before = hashes(self.user)
        code, error, _ = self.request("POST", "/api/profile", form)
        self.assertEqual((code, error["code"]), (400, "NOT_OWNED"))
        self.assertEqual(hashes(self.user), before)
        header = "---\nmoments_agent: moments-draft\nrecord_type: moments_style_profile\n---\n"
        path.write_text(header + "x" * (64 * 1024 - len(header.encode("utf-8")) - 1), encoding="utf-8", newline="")
        before = hashes(self.user)
        code, error, _ = self.request("POST", "/api/profile", form)
        self.assertEqual((code, error["code"]), (400, "SOURCE_TOO_LARGE"))
        self.assertEqual(hashes(self.user), before)

    def test_history_import_is_exact_new_file_and_cannot_overwrite_or_escape(self):
        original = "# 本人导入的合成旧作\n\n原文、标点。\n"
        code, result, _ = self.request("POST", "/api/history", {"filename": "合成旧作.md", "body": original})
        self.assertEqual(code, 200, result)
        stable_id = result["id"]
        before = hashes(self.user)
        code, full, _ = self.request("GET", "/api/history/" + stable_id)
        self.assertEqual(code, 200)
        self.assertEqual(full["item"]["body"], original)
        self.assertEqual(hashes(self.user), before)
        self.assertEqual(self.request("POST", "/api/history", {"filename": "合成旧作.md", "body": "不能覆盖"})[0], 409)
        for name in ("../outside.md", "secret.md", ".env.md", "sub/file.md", "CON.md", "a?.md"):
            self.assertEqual(self.request("POST", "/api/history", {"filename": name, "body": "不应保存"})[0], 400)
        self.assertEqual((self.user / "inputs/history/合成旧作.md").read_bytes(), original.encode("utf-8"))
        self.assertFalse(self.model.contexts)

    def test_async_generation_saves_three_pending_drafts_and_bounded_context(self):
        for number in range(4):
            (self.user / "inputs/history" / f"合成{number}.md").write_text("明确合成的历史文风示例", encoding="utf-8")
        before_inputs = hashes(self.user / "inputs")
        current = self.generate()
        self.assertEqual(len(current["notes"]), 3)
        self.assertTrue(all(row["status"] == "待发布" for row in current["notes"]))
        self.assertEqual(len(self.model.contexts[0]["history"]), 2)
        self.assertTrue(self.model.contexts[0]["demo"])
        self.assertEqual(hashes(self.user / "inputs"), before_inputs)
        self.assertEqual(len(self.request("GET", "/api/state")[1]["library"]), 3)

    def test_private_history_is_not_sent_and_large_accepted_history_is_explicitly_truncated(self):
        (self.user / "inputs/history/00-private.md").write_text("PRIVATE_DO_NOT_SHARE 不可送模型的内容", encoding="utf-8")
        large = "合成旧作正文。" * 4000
        self.assertLess(len(large.encode("utf-8")), app.workspace_tools.MAX_DOCUMENT_BYTES)
        (self.user / "inputs/history/01-large.md").write_text(large, encoding="utf-8")
        current = self.generate()
        self.assertEqual(len(current["notes"]), 3)
        context = self.model.contexts[0]
        self.assertEqual([row["relative_path"] for row in context["history"]], ["01-large.md"])
        self.assertTrue(context["history"][0]["truncated"])
        self.assertEqual(context["context_warnings"][0]["code"], "PRIVACY_BLOCKED")
        self.assertNotIn("不可送模型的内容", json.dumps(context, ensure_ascii=False))

    def test_user_final_keep_and_explicit_publish_remain_separate(self):
        current = self.generate()
        rid = current["round_id"]
        body = "本人提供的合成终稿！\n\n内部分行、标点都保留。"
        code, saved, _ = self.request("POST", "/api/final", {"round_id": rid, "variant": 2, "body": body})
        self.assertEqual(code, 200, saved)
        second = next(row for row in saved["round"]["notes"] if row["variant"] == 2)
        self.assertEqual(second["body"], body)
        self.assertEqual(second["status"], "待发布")
        for invalid in (False, 1, "true"):
            self.assertEqual(self.request("POST", "/api/publish", {"round_id": rid, "variant": 2, "confirmed": invalid})[0], 400)
        self.assertEqual(self.request("POST", "/api/keep", {"round_id": rid, "variant": 1, "keep": True})[0], 200)
        code, published, _ = self.request("POST", "/api/publish", {"round_id": rid, "variant": 2, "confirmed": True})
        self.assertEqual(code, 200, published)
        rows = {row["variant"]: row for row in published["round"]["notes"]}
        self.assertEqual(set(rows), {1, 2})
        self.assertTrue(rows[1]["kept"])
        self.assertEqual(rows[2]["status"], "已发布")
        self.assertIsNone(rows[2]["published_at"])
        self.assertEqual(len(self.model.contexts), 1, "Publishing must not call the model or WeChat")

    def test_revision_changes_only_target_and_rejects_stale_round(self):
        current = self.generate()
        rid = current["round_id"]
        protected = {row["variant"]: Path(row["path"]).read_bytes() for row in current["notes"] if row["variant"] != 2}
        code, queued, _ = self.request("POST", "/api/revise", {"round_id": rid, "variant": 2, "instruction": "再口语一点，明确只是合成演示"})
        self.assertEqual(code, 202)
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "DONE", done)
        for row in done["result"]["round"]["notes"]:
            if row["variant"] in protected:
                self.assertEqual(Path(row["path"]).read_bytes(), protected[row["variant"]])
        self.generate("第二份合成材料：这次讨论确认人，不是上一轮内容。")
        code, error, _ = self.request("POST", "/api/keep", {"round_id": rid, "variant": 2, "keep": True})
        self.assertEqual(code, 409)
        self.assertEqual(error["code"], "STALE_ROUND")

    def test_rewrite_presets_are_whitelisted_compiled_and_do_not_touch_other_variants(self):
        state = self.request("GET", "/api/state")[1]
        self.assertEqual({row["id"] for row in state["rewrite_presets"]}, {"colloquial", "authentic", "professional"})
        self.assertTrue(all(set(row) == {"id", "label", "description"} for row in state["rewrite_presets"]))
        current = self.generate()
        rid = current["round_id"]
        before = hashes(self.user)
        calls = len(self.model.contexts)
        code, error, _ = self.request("POST", "/api/revise", {"round_id": rid, "variant": 2,
                                                             "preset": "invent-a-fact", "instruction": ""})
        self.assertEqual((code, error["code"]), (400, "INVALID_REWRITE_PRESET"))
        self.assertEqual(len(self.model.contexts), calls)
        self.assertEqual(hashes(self.user), before)
        protected = {row["variant"]: Path(row["path"]).read_bytes() for row in current["notes"] if row["variant"] != 2}
        code, queued, _ = self.request("POST", "/api/revise", {"round_id": rid, "variant": 2,
                                                              "preset": "authentic", "instruction": "删掉最后一句"})
        self.assertEqual(code, 202, queued)
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "DONE", done)
        second = next(row for row in done["result"]["round"]["notes"] if row["variant"] == 2)
        self.assertIn("真实意味着克制", second["body"])
        self.assertIn("删掉最后一句", second["body"])
        for row in done["result"]["round"]["notes"]:
            if row["variant"] in protected:
                self.assertEqual(Path(row["path"]).read_bytes(), protected[row["variant"]])
        self.assertEqual(self.request("GET", "/api/state")[1]["materials"]["items"], [])

    def test_simultaneous_generation_has_one_job_and_busy_rejection(self):
        self.model.block = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.request("POST", "/api/generate", {"material": "合成并发材料"}), range(2)))
        self.assertEqual(sorted(result[0] for result in results), [202, 409])
        self.assertTrue(self.model.started.wait(1))
        code, error, _ = self.request("POST", "/api/history", {"filename": "blocked.md", "body": "合成"})
        self.assertEqual((code, error["code"]), (409, "BUSY"))
        self.model.release.set()
        queued = next(result[1] for result in results if result[0] == 202)
        self.assertEqual(self.wait_job(queued["job_id"])["status"], "DONE")
        self.assertEqual(len(self.model.contexts), 1)

    def test_provider_selection_is_validated_persisted_and_not_a_credential_endpoint(self):
        before = hashes(self.user)
        for data in ({"provider": "untrusted-provider"}, {"provider": "zhipu", "api_key": "not-a-real-key"},
                     {"provider": "zhipu", "api_key_file": "../untrusted.txt"}):
            code, _, _ = self.request("POST", "/api/model", data)
            self.assertEqual(code, 400)
            self.assertEqual(hashes(self.user), before)
        code, chosen, _ = self.request("POST", "/api/model", {"provider": "zhipu"})
        self.assertEqual(code, 200, chosen)
        self.assertEqual(chosen["model"]["provider"], "zhipu")
        self.assertEqual(chosen["model"]["model"], "glm-5.3-flash")
        self.assertEqual({row["id"] for row in chosen["model_options"]}, {"codex", "zhipu"})
        config = json.loads((self.user / "model.local.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"provider": "zhipu"})
        self.application.ready_value = None
        self.assertEqual(self.request("GET", "/api/state")[1]["model"]["provider"], "zhipu")
        self.assertEqual(self.request("POST", "/api/model", {"provider": "codex"})[0], 200)
        self.assertEqual(json.loads((self.user / "model.local.json").read_text(encoding="utf-8")), {"provider": "codex"})

    def test_running_job_reports_real_stages_target_elapsed_origin_and_fixed_model_snapshot(self):
        current = self.generate()
        self.assertEqual(self.request("POST", "/api/model", {"provider": "zhipu"})[0], 200)
        self.model.block = True
        self.model.started.clear()
        code, queued, _ = self.request("POST", "/api/revise", {"round_id": current["round_id"], "variant": 2,
                                                                 "preset": "colloquial"})
        self.assertEqual(code, 202)
        self.assertTrue(self.model.started.wait(1))
        running = self.request("GET", "/api/jobs/" + queued["job_id"])[1]
        self.assertEqual(running["status"], "RUNNING")
        self.assertEqual(running["phase"], "requesting")
        self.assertEqual(running["target"], 2)
        self.assertEqual(running["kind"], "revise")
        self.assertEqual(running["provider"], "zhipu")
        self.assertEqual(running["model"], "glm-5.3-flash")
        self.assertIsNotNone(running["started_at"])
        self.assertTrue(running["started_at"].endswith("+00:00"))
        self.assertIsNone(running["finished_at"])
        self.assertNotIn("api_key", json.dumps(running))
        self.assertEqual(self.request("GET", "/api/state")[1]["active_job"], queued["job_id"])
        before = hashes(self.user)
        code, error, _ = self.request("POST", "/api/model", {"provider": "codex"})
        self.assertEqual((code, error["code"]), (409, "BUSY"))
        self.assertEqual(hashes(self.user), before)
        self.assertEqual(self.request("POST", "/api/revise", {"round_id": current["round_id"], "variant": 2,
                                                                 "preset": "colloquial"})[0], 409)
        self.application.progress(queued["job_id"], "invented-progress", "not allowed")
        self.application.progress(queued["job_id"], "requesting", "PRIVATE_PROMPT_OR_REASONING must not appear")
        visible = self.request("GET", "/api/jobs/" + queued["job_id"])[1]
        self.assertEqual(visible["message"], app._JOB_PHASE_MESSAGES["requesting"])
        self.assertNotIn("PRIVATE_PROMPT_OR_REASONING", json.dumps(visible))
        self.model.release.set()
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "DONE", done)
        self.assertEqual(done["phase"], "done")
        self.assertIsNotNone(done["finished_at"])
        self.assertEqual(self.model.settings[-1]["provider"], "zhipu")
        self.assertIsNone(self.request("GET", "/api/state")[1]["active_job"])

    def test_external_round_change_while_model_runs_cannot_be_overwritten(self):
        self.model.block = True
        _, queued, _ = self.request("POST", "/api/generate", {"material": "合成慢任务"})
        self.assertTrue(self.model.started.wait(1))
        runner = self.application.runner()
        external = runner.create_round("另一个合成轮", "用户当次提供素材：外部合成进程", [
            {"angle": str(n), "title": "外部合成稿", "body": "外部合成正文" + str(n), "tags": ["实践"]}
            for n in (1, 2, 3)])
        before = {row["path"]: Path(row["path"]).read_bytes() for row in external["notes"]}
        self.model.release.set()
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "ERROR")
        self.assertEqual(done["error"]["code"], "STALE_ROUND")
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertEqual(self.request("GET", "/api/state")[1]["round"]["round_id"], external["round_id"])

    def test_external_same_round_edit_cannot_be_overwritten_by_slow_ai_revision(self):
        current = self.generate()
        self.model.block = True
        self.model.started.clear()
        _, queued, _ = self.request("POST", "/api/revise", {"round_id": current["round_id"], "variant": 2,
                                                             "instruction": "合成慢改稿"})
        self.assertTrue(self.model.started.wait(1))
        runner = self.application.runner()
        runner.handle("修改2：外部进程保存的合成新正文。", expected_round_id=current["round_id"])
        target = next(Path(row["path"]) for row in current["notes"] if row["variant"] == 2)
        before = target.read_bytes()
        self.model.release.set()
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "ERROR")
        self.assertEqual(done["error"]["code"], "STALE_CONTENT")
        self.assertEqual(target.read_bytes(), before)

    def test_invalid_model_output_cannot_create_partial_drafts(self):
        self.model.invalid = True
        _, queued, _ = self.request("POST", "/api/generate", {"material": "合成无效输出测试"})
        done = self.wait_job(queued["job_id"])
        self.assertEqual(done["status"], "ERROR")
        self.assertIsNone(self.request("GET", "/api/state")[1]["round"])
        self.assertEqual(list((self.user / "archive").glob("*v*.md")), [])

    def test_history_link_cannot_write_or_read_outside_workspace(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.md").write_text("NEVER_SEND", encoding="utf-8")
        history = self.user / "inputs/history"
        history.rmdir()
        try:
            os.symlink(outside, history, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Account cannot create symlinks: " + type(exc).__name__)
        code, error, _ = self.request("POST", "/api/history", {"filename": "new.md", "body": "合成"})
        self.assertEqual(code, 400)
        self.assertEqual(error["code"], "UNSAFE_PATH")
        self.assertFalse((outside / "new.md").exists())
        self.assertNotIn("NEVER_SEND", json.dumps(error))


if __name__ == "__main__":
    unittest.main()
