# SPDX-License-Identifier: MIT
"""3077：可搜索PDF逐字段oracle及真实本机HTTP条件同步验收。"""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

BASE = Path(__file__).resolve().parent
SOURCE = BASE / "3077_自造作业说明.pdf"
PAYLOAD_FIELDS = {"document_id", "revision", "step_id", "title", "instructions", "source_pages"}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TestHTTP:
    def __init__(self, body, *, status=200, marker="3077", location=None):
        self.requests = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                outer.requests.append(self.path)
                content = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                if marker is not None:
                    self.send_header("X-Demo-Marker", marker)
                if location is not None:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(content)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("验收HTTP线程未停止")


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load("engine3077_tests", "3077_提取同步引擎.py")
        cls.fixture = load("fixture3077_tests", "3077_自造文档.py")
        cls.mock = load("mock3077_tests", "3077_本地模拟接口.py")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="3077_验收暂存_"))
        self.handles = []
        self.journal = self.root / "3077_执行记录.json"

    def tearDown(self):
        for handle in reversed(self.handles):
            handle.stop()
        checked = self.root.resolve()
        if checked.parent != Path(tempfile.gettempdir()).resolve() or not checked.name.startswith("3077_验收暂存_"):
            raise RuntimeError("测试清理路径越出拥有范围")
        shutil.rmtree(checked)

    def source(self, pages=None, name="3077_边界说明.pdf"):
        path = self.root / name
        path.write_bytes(self.fixture.pdf_bytes(pages or self.fixture.page_lines()))
        return path

    def mock_start(self, initial=None):
        handle = self.mock.start_mock(self.fixture.initial_remote() if initial is None else initial)
        self.handles.append(handle)
        return handle

    def custom_start(self, *args, **kwargs):
        handle = TestHTTP(*args, **kwargs)
        self.handles.append(handle)
        return handle

    def put_requests(self, handle):
        return [record for record in handle.request_log if record["method"] == "PUT"]

    def plan(self, handle, source=SOURCE):
        return self.engine.build_plan(self.engine.extract_pdf(source), self.engine.get_remote(handle.base_url), handle.base_url)

    def assert_final(self, handle):
        expected = self.fixture.initial_remote()
        expected.update(self.fixture.desired_payloads())
        self.assertEqual(handle.snapshot(), expected)

    def header(self, document="DEMO-WI-001", revision="A"):
        return ["DEMO WORK INSTRUCTION", "SYNTHETIC TRAINING DATA", f"Document: {document}", f"Revision: {revision}"]

    def test_01_official_pdf_matches_all_independent_expected_fields(self):
        before = SOURCE.read_bytes()
        actual = self.engine.extract_pdf(SOURCE)
        self.assertEqual(SOURCE.read_bytes(), before)
        self.assertEqual(actual["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(actual["source_name"], SOURCE.name)
        self.assertEqual(actual["page_count"], 3)
        for key, value in self.fixture.expected_model().items():
            self.assertEqual(actual[key], value)

    def test_02_independent_pdf_reader_three_pages_four_searchable_steps_each(self):
        reader = PdfReader(SOURCE, strict=True)
        self.assertEqual(len(reader.pages), 3)
        for index, page in enumerate(reader.pages):
            text = page.extract_text()
            self.assertIn("DEMO WORK INSTRUCTION", text)
            self.assertIn("SYNTHETIC TRAINING DATA", text)
            self.assertIn(f"Page {index + 1} of 3", text)
            self.assertEqual(text.count("END STEP"), 4)
            for number in range(index * 4 + 1, index * 4 + 5):
                self.assertIn(f"STEP {number:03d}:", text)

    def test_03_cross_page_step_continuation_and_whitespace_join(self):
        pages = [self.header() + ["STEP 001: Training continuation", "Instruction: First   training line."],
                 self.header() + ["Second training line.", "END STEP"]]
        actual = self.engine.extract_pdf(self.source(pages))
        self.assertEqual(actual["steps"], [dict(step_id="001", title="Training continuation", instructions="First training line. Second training line.", source_pages=[1, 2])])

    def test_04_document_and_revision_must_match_across_pages(self):
        for field, replacement in [("Document: DEMO-WI-001", "Document: DEMO-WI-OTHER"), ("Revision: A", "Revision: B")]:
            pages = self.fixture.page_lines()
            pages[1][pages[1].index(field)] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source(pages, name="3077_跨页" + str(len(list(self.root.iterdir()))) + ".pdf"))

    def test_05_required_markers_cannot_be_missing_or_duplicated(self):
        for marker in ["Document: DEMO-WI-001", "Revision: A", "DEMO WORK INSTRUCTION", "SYNTHETIC TRAINING DATA"]:
            pages = self.fixture.page_lines()
            pages[0].remove(marker)
            with self.subTest(missing=marker), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source(pages, name=f"3077_缺标记{len(list(self.root.iterdir()))}.pdf"))
        for index, marker in enumerate(["Document: DEMO-WI-001", "Revision: A", "DEMO WORK INSTRUCTION", "SYNTHETIC TRAINING DATA"]):
            pages = self.fixture.page_lines()
            pages[0].insert(3, marker)
            with self.subTest(duplicated=marker), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source(pages, name=f"3077_重复标记{index}.pdf"))

    def test_06_step_ids_are_unique_nonzero_and_strictly_increasing(self):
        for bad_id in ["001", "000", "013"]:
            pages = self.fixture.page_lines()
            pages[0] = [line.replace("STEP 002:", f"STEP {bad_id}:") for line in pages[0]]
            with self.subTest(step_id=bad_id), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source(pages, name=f"3077_编号{bad_id}.pdf"))
        payload = copy.deepcopy(next(iter(self.fixture.desired_payloads().values())))
        payload["step_id"] = "٠٠١"
        with self.assertRaises(ValueError):
            self.engine.validate_payload(payload)

    def test_07_missing_end_instruction_and_nested_step_reject(self):
        cases = [["STEP 001: Demo", "Instruction: Sample body."], ["STEP 001: Demo", "Sample body.", "END STEP"],
                 ["STEP 001: Demo", "Instruction: Sample body.", "Instruction: Repeated.", "END STEP"],
                 ["STEP 001: Demo", "Instruction: Sample body.", "STEP 002: Nested", "Instruction: Other.", "END STEP"]]
        for index, body in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source([self.header() + body], name=f"3077_不完整{index}.pdf"))

    def test_08_unknown_outside_text_and_reserved_body_markers_reject(self):
        for body in [["Unstructured content"], ["STEP 001: Demo", "Instruction: Text.", "STEP broken", "END STEP"],
                     ["STEP 001: Demo", "Instruction: Text.", "Page invalid", "END STEP"]]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.engine.extract_pdf(self.source([self.header() + body], name=f"3077_未知{len(list(self.root.iterdir()))}.pdf"))

    def test_09_blank_scanned_encrypted_and_invalid_pdf_reject(self):
        # 无文字页模拟扫描输入：引擎须拒绝，而不能返回空步骤。
        writer = PdfWriter(); writer.add_blank_page(width=612, height=792)
        blank = self.root / "3077_无文字页.pdf"
        with blank.open("wb") as stream:
            writer.write(stream)
        writer.close()
        for password in ["3077-test-password", ""]:
            writer = PdfWriter(); writer.append(SOURCE); writer.encrypt(user_password=password, owner_password="3077-owner-only")
            path = self.root / ("3077_加密" + str(len(password)) + ".pdf")
            with path.open("wb") as stream:
                writer.write(stream)
            writer.close()
            with self.subTest(password_length=len(password)), self.assertRaises(ValueError):
                self.engine.extract_pdf(path)
        invalid = self.root / "3077_非法.pdf"; invalid.write_bytes(b"not a pdf")
        for path in [blank, invalid]:
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.engine.extract_pdf(path)

    def test_10_source_size_limit_and_unsupported_extension_reject(self):
        before = digest(SOURCE)
        with patch.object(self.engine, "MAX_PDF_BYTES", 100), self.assertRaises(ValueError):
            self.engine.extract_pdf(SOURCE)
        other = self.root / "3077_错误扩展.txt"; other.write_bytes(SOURCE.read_bytes())
        with self.assertRaises(ValueError):
            self.engine.extract_pdf(other)
        self.assertEqual(digest(SOURCE), before)

    def test_11_endpoint_validation_blocks_non_loopback_auth_query_and_paths(self):
        self.assertEqual(self.engine.endpoint("http://127.0.0.1:12345/"), "http://127.0.0.1:12345")
        for url in ["https://127.0.0.1:12345", "http://localhost:12345", "http://example.com:80", "http://127.0.0.2:12345", "http://u:p@127.0.0.1:12345", "http://127.0.0.1:12345/?q=1", "http://127.0.0.1:12345/#x", "http://127.0.0.1:12345/demo/steps", "http://127.0.0.1:0", "file:///tmp/test"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.engine.endpoint(url)

    def test_12_real_http_marker_and_redirect_refused_without_target_request(self):
        for marker in [None, "wrong"]:
            handle = self.custom_start(dict(demo_marker="3077", steps=[]), marker=marker)
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                self.engine.get_remote(handle.base_url)
        target = self.custom_start(dict(demo_marker="3077", steps=[]))
        redirect = self.custom_start({}, status=302, location=target.base_url + "/demo/steps")
        with self.assertRaises(ValueError):
            self.engine.get_remote(redirect.base_url)
        self.assertEqual(target.requests, [])

    def test_13_real_snapshot_etags_exact_payloads_and_invalid_records(self):
        handle = self.mock_start()
        snapshot = self.engine.get_remote(handle.base_url)
        self.assertEqual(snapshot["demo_marker"], "3077")
        self.assertEqual({r["key"]: r["payload"] for r in snapshot["steps"]}, self.fixture.initial_remote())
        for record in snapshot["steps"]:
            canonical = json.dumps(record["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertEqual(record["etag"], '"' + hashlib.sha256(canonical).hexdigest() + '"')
            self.assertEqual(set(record["payload"]), PAYLOAD_FIELDS)
        for change in ["etag", "duplicate", "marker"]:
            bad = copy.deepcopy(snapshot)
            if change == "etag": bad["steps"][0]["etag"] = '"wrong"'
            elif change == "duplicate": bad["steps"].append(copy.deepcopy(bad["steps"][0]))
            else: bad["demo_marker"] = "wrong"
            custom = self.custom_start(bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.engine.get_remote(custom.base_url)

    def test_14_default_plan_cli_has_create4_update4_unchanged4_and_zero_put(self):
        handle = self.mock_start()
        initial = handle.snapshot()
        output = self.root / "3077_计划.json"
        result = subprocess.run([sys.executable, "-B", str(BASE / "3077_提取同步引擎.py"), "plan", "--source", str(SOURCE), "--endpoint", handle.base_url, "--output", str(output)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([r["action"] for r in plan["operations"]], ["create"] * 4 + ["update"] * 4 + ["unchanged"] * 4)
        self.assertEqual(plan["unmodified_extra_keys"], ["DEMO-WI-001:900"])
        self.assertEqual(self.put_requests(handle), [])
        self.assertEqual(handle.snapshot(), initial)

    def test_15_apply_real_http_eight_verified_writes_and_preserved_extra(self):
        handle = self.mock_start(); before = digest(SOURCE)
        journal = self.engine.apply_plan(SOURCE, self.plan(handle), handle.base_url, self.journal)
        self.assertTrue(journal["complete"])
        self.assertEqual((journal["attempted_writes"], journal["verified_writes"], journal["unchanged"]), (8, 8, 4))
        self.assertEqual(len(self.put_requests(handle)), 8)
        self.assertTrue(all(record["committed"] for record in self.put_requests(handle)))
        self.assertEqual(json.loads(self.journal.read_text(encoding="utf-8")), journal)
        self.assertEqual(digest(SOURCE), before)
        self.assert_final(handle)

    def test_16_second_fresh_plan_is_twelve_unchanged_with_zero_new_write(self):
        handle = self.mock_start()
        self.engine.apply_plan(SOURCE, self.plan(handle), handle.base_url, self.journal)
        before = len(self.put_requests(handle)); plan = self.plan(handle)
        self.assertTrue(all(r["action"] == "unchanged" for r in plan["operations"]))
        result = self.engine.apply_plan(SOURCE, plan, handle.base_url, self.root / "3077_再次执行.json")
        self.assertEqual((result["attempted_writes"], result["unchanged"]), (0, 12))
        self.assertEqual(len(self.put_requests(handle)), before)
        self.assert_final(handle)

    def test_17_stale_source_rejected_before_journal_or_remote_write(self):
        handle = self.mock_start(); source = self.source(); plan = self.plan(handle, source)
        source.write_bytes(source.read_bytes() + b"\n% 3077 external change\n")
        with self.assertRaises(ValueError):
            self.engine.apply_plan(source, plan, handle.base_url, self.journal)
        self.assertEqual(self.put_requests(handle), [])
        self.assertFalse(self.journal.exists())

    def test_18_stale_remote_snapshot_even_extra_change_blocks_all_writes(self):
        handle = self.mock_start(); plan = self.plan(handle)
        payload = handle.snapshot()["DEMO-WI-001:900"]; payload["instructions"] = "Concurrent synthetic extra change."
        handle.mutate("DEMO-WI-001:900", payload); before = handle.snapshot()
        with self.assertRaises(ValueError):
            self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        self.assertEqual(handle.snapshot(), before)
        self.assertEqual(self.put_requests(handle), [])
        self.assertFalse(self.journal.exists())

    def test_19_tampered_plan_payload_actions_etag_and_endpoint_block_writes(self):
        handle = self.mock_start(); original = self.plan(handle)
        for change in ["payload", "action", "etag", "endpoint"]:
            plan = copy.deepcopy(original)
            if change == "payload": plan["operations"][0]["payload"]["instructions"] = "Forged plan content."
            elif change == "action": plan["operations"][0]["action"] = "unchanged"
            elif change == "etag": plan["operations"][4]["expected_etag"] = '"forged"'
            else: plan["endpoint"] = "http://127.0.0.1:1"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        self.assertEqual(self.put_requests(handle), [])
        self.assertFalse(self.journal.exists())

    def test_20_before_write_503_records_incomplete_without_committed_change(self):
        handle = self.mock_start(); plan = self.plan(handle); before = handle.snapshot()
        handle.fail_on_nth_write(1, after_commit=False)
        with self.assertRaises(self.engine.SyncError):
            self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertFalse(journal["complete"])
        self.assertEqual((journal["attempted_writes"], journal["verified_writes"]), (1, 0))
        self.assertTrue(journal["error"])
        self.assertEqual(handle.snapshot(), before)
        self.assertEqual(self.put_requests(handle)[0]["status"], 503)

    def test_21_after_write_503_is_recovered_only_by_matching_real_get(self):
        handle = self.mock_start(); plan = self.plan(handle)
        handle.fail_on_nth_write(1, after_commit=True)
        journal = self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        self.assertTrue(journal["complete"])
        self.assertEqual(journal["verified_writes"], 8)
        self.assertTrue(journal["writes"][0]["response_recovered"])
        self.assertTrue(self.put_requests(handle)[0]["committed"])
        self.assert_final(handle)

    def test_22_partial_failure_preserves_completed_steps_and_replan_resumes_safely(self):
        handle = self.mock_start(); plan = self.plan(handle)
        handle.fail_on_nth_write(3, after_commit=False)
        with self.assertRaises(self.engine.SyncError):
            self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        first = self.journal.read_bytes(); partial = json.loads(first)
        self.assertEqual((partial["attempted_writes"], partial["verified_writes"]), (3, 2))
        self.assertFalse(partial["complete"])
        new_plan = self.plan(handle)
        self.assertEqual(sum(r["action"] == "unchanged" for r in new_plan["operations"]), 6)
        completed = self.engine.apply_plan(SOURCE, new_plan, handle.base_url, self.root / "3077_接续执行.json")
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["verified_writes"], 6)
        self.assertEqual(self.journal.read_bytes(), first)
        self.assertEqual(sum(r["committed"] for r in self.put_requests(handle)), 8)
        self.assert_final(handle)

    def test_23_real_412_concurrent_creation_never_overwrites_other_payload(self):
        handle = self.mock_start(); plan = self.plan(handle); original = self.engine.request_json
        key = plan["operations"][0]["key"]
        concurrent = copy.deepcopy(plan["operations"][0]["payload"]); concurrent["instructions"] = "Concurrent distinct synthetic content."
        injected = False
        def race(base_url, method, path, *args, **kwargs):
            nonlocal injected
            if method == "PUT" and not injected:
                handle.mutate(key, concurrent); injected = True
            return original(base_url, method, path, *args, **kwargs)
        with patch.object(self.engine, "request_json", side_effect=race), self.assertRaises(self.engine.SyncError):
            self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        self.assertTrue(injected)
        self.assertEqual(handle.snapshot()[key], concurrent)
        self.assertEqual(self.put_requests(handle)[0]["status"], 412)
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(journal["verified_writes"], 0)
        self.assertFalse(journal["complete"])

    def test_24_existing_outputs_and_real_junction_paths_refuse_before_put(self):
        handle = self.mock_start(); plan = self.plan(handle)
        self.journal.write_bytes(b'{"3077_existing":true}')
        with self.assertRaises(ValueError):
            self.engine.apply_plan(SOURCE, plan, handle.base_url, self.journal)
        self.assertEqual(self.journal.read_bytes(), b'{"3077_existing":true}')
        with self.assertRaises((ValueError, FileExistsError)):
            self.engine.write_json(self.journal, {"changed": True}, new=True)
        self.assertEqual(self.put_requests(handle), [])
        if os.name != "nt":
            return
        real = self.root / "3077_真实目录"; real.mkdir()
        copied = real / "3077_副本.pdf"; copied.write_bytes(SOURCE.read_bytes())
        junction = self.root / "3077_连接入口"
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)], capture_output=True)
        if created.returncode:
            self.skipTest("系统未允许创建Windows目录连接点")
        try:
            with self.assertRaises(ValueError):
                self.engine.extract_pdf(junction / copied.name)
            with self.assertRaises(ValueError):
                self.engine.apply_plan(SOURCE, plan, handle.base_url, junction / "3077_执行记录.json")
            self.assertEqual(self.put_requests(handle), [])
        finally:
            if junction.parent != self.root or not junction.name.startswith("3077_"):
                raise RuntimeError("连接点越出拥有范围")
            junction.rmdir()


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.details = []
    def startTest(self, test):
        super().startTest(test); self.current = dict(test=test._testMethodName, status="running"); self.began = time.perf_counter()
    def addSuccess(self, test):
        super().addSuccess(test); self.current["status"] = "passed"
    def addFailure(self, test, error):
        super().addFailure(test, error); self.current["status"] = "failure"
    def addError(self, test, error):
        super().addError(test, error); self.current["status"] = "error"
    def addSkip(self, test, reason):
        super().addSkip(test, reason); self.current.update(status="skipped", reason=reason)
    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error: self.current["status"] = "failure" if issubclass(error[0], test.failureException) else "error"
    def stopTest(self, test):
        self.current["elapsed_seconds"] = round(time.perf_counter() - self.began, 6); self.details.append(self.current); super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(add_help=False); parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    names = ["3077_提取同步引擎.py", "3077_本地模拟接口.py", "3077_自造文档.py", "3077_验收测试.py"]
    before = {name: digest(BASE / name) for name in names}; source_sha = digest(SOURCE); began = time.perf_counter()
    program = unittest.main(argv=[sys.argv[0], *remaining], testRunner=unittest.TextTestRunner(verbosity=2, resultclass=TimedResult), exit=False)
    result = program.result; after = {name: digest(BASE / name) for name in names}
    if args.report:
        target = Path(os.path.abspath(args.report))
        if target.parent != BASE or not target.name.startswith("3077_") or target.suffix != ".json" or target.is_symlink():
            raise ValueError("验收报告须为本项目根目录下的3077前缀JSON普通文件")
        report = dict(schema_version=1, tests_run=result.testsRun, passed=sum(r["status"] == "passed" for r in result.details),
                      failures=sum(r["status"] == "failure" for r in result.details), errors=sum(r["status"] == "error" for r in result.details), skipped=len(result.skipped),
                      successful=result.wasSuccessful() and before == after and digest(SOURCE) == source_sha,
                      elapsed_seconds=round(time.perf_counter() - began, 6), per_test=result.details,
                      environment=dict(python=platform.python_version(), system=platform.system(), pypdf=__import__("pypdf").__version__, reportlab=__import__("reportlab").Version),
                      code_sha256=after, code_unchanged_during_tests=before == after, source_sha256=source_sha, source_unchanged=digest(SOURCE) == source_sha,
                      synthetic_only=True, real_http_loopback_tested=True, external_system_connected=False,
                      expected_flow=dict(extracted_steps=12, initial_remote_records=9, create=4, update=4, unchanged=4, preserved_extra=1, final_remote_records=13))
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() and before == after and digest(SOURCE) == source_sha else 1)


if __name__ == "__main__":
    main()
