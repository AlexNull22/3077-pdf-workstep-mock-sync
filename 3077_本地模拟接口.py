# SPDX-License-Identifier: MIT
"""独立本地演示接口；不是 ION 官方 API，也不连接真实 MES。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEMO_MARKER = "3077"
MAX_BODY_BYTES = 64 * 1024
FIELDS = {"document_id", "revision", "step_id", "title", "instructions", "source_pages"}
DOCUMENT_RE = re.compile(r"DEMO-WI-[A-Z0-9]{1,20}\Z")
REVISION_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,15}\Z")
STEP_RE = re.compile(r"(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})\Z")
KEY_RE = re.compile(r"DEMO-WI-[A-Z0-9]{1,20}:(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _etag(payload: dict) -> str:
    return '"' + hashlib.sha256(_canonical(payload)).hexdigest() + '"'


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON字段重复")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError("JSON不允许非有限数值")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=reject_constant)


def _validate(key: str, payload: object) -> dict:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError("步骤key格式无效")
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("payload必须且只能包含6个约定字段")
    document_id, step_id = payload["document_id"], payload["step_id"]
    if not isinstance(document_id, str) or not DOCUMENT_RE.fullmatch(document_id):
        raise ValueError("document_id必须为DEMO-WI-加1至20个大写字母或数字")
    if not isinstance(step_id, str) or not STEP_RE.fullmatch(step_id):
        raise ValueError("step_id必须为001至999的三位数字字符串")
    if key != document_id + ":" + step_id:
        raise ValueError("key与payload的文档、步骤身份不一致")
    for field in ("revision", "title", "instructions"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(field + "必须为非空字符串")
    if not REVISION_RE.fullmatch(payload["revision"]):
        raise ValueError("revision须为1至16位大写字母、数字、点、下划线或连字符，首位须为字母或数字")
    if len(payload["title"]) > 100 or len(payload["instructions"]) > 5000:
        raise ValueError("title最多100字符，instructions最多5000字符")
    pages = payload["source_pages"]
    if not isinstance(pages, list) or not pages or any(type(page) is not int or not 1 <= page <= 20 for page in pages) or pages != sorted(set(pages)):
        raise ValueError("source_pages必须为1至20的非空升序去重整数列表")
    _canonical(payload)
    return copy.deepcopy(payload)


def _initial_payloads(initial: object) -> dict:
    if initial is None:
        return {}
    if isinstance(initial, dict) and "steps" in initial:
        if set(initial) - {"demo_marker", "steps"} or initial.get("demo_marker", DEMO_MARKER) != DEMO_MARKER:
            raise ValueError("初始状态的演示标记或字段无效")
        records = initial["steps"]
    elif isinstance(initial, list):
        records = initial
    elif isinstance(initial, dict):
        return {key: _validate(key, payload) for key, payload in initial.items()}
    else:
        raise ValueError("初始状态须为key到payload映射或steps记录列表")
    if not isinstance(records, list):
        raise ValueError("steps必须为数组")
    result = {}
    for record in records:
        if not isinstance(record, dict) or set(record) - {"key", "etag", "payload"} or not {"key", "payload"} <= set(record):
            raise ValueError("初始记录结构无效")
        key = record["key"]
        if not isinstance(key, str) or key in result:
            raise ValueError("初始步骤key无效或重复")
        payload = _validate(key, record["payload"])
        if "etag" in record and record["etag"] != _etag(payload):
            raise ValueError("初始记录ETag与payload不一致")
        result[key] = payload
    return result


class MockHandle:
    """管理内存状态及真实本机HTTP线程，stop可重复调用。"""

    def __init__(self, initial: object = None, port: int = 0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("端口必须为0至65535整数")
        self._lock = threading.RLock()
        self._state = _initial_payloads(initial)
        self._requests = []
        self._fault = None
        self._stopped = False
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.handle = self
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="3077_本地模拟接口", daemon=True)
        self._thread.start()

    @property
    def request_log(self) -> list:
        with self._lock:
            return copy.deepcopy(self._requests)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def fail_on_nth_write(self, n: int, after_commit: bool = False) -> None:
        if type(n) is not int or n < 1 or type(after_commit) is not bool:
            raise ValueError("n须为正整数，after_commit须为bool")
        with self._lock:
            self._fault = {"remaining": n, "after_commit": after_commit}

    def mutate(self, key: str, payload: dict) -> None:
        value = _validate(key, payload)
        with self._lock:
            self._state[key] = value

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("模拟接口线程未能停止")

    def _next_put_fault(self) -> dict | None:
        with self._lock:
            if self._fault is None:
                return None
            self._fault["remaining"] -= 1
            if self._fault["remaining"]:
                return None
            result = self._fault
            self._fault = None
            return result

    def _record(self, method: str, route: str, status: int, committed: bool) -> None:
        with self._lock:
            self._requests.append({"method": method, "path": route, "status": status, "committed": committed})

    def _item(self, key: str) -> dict | None:
        with self._lock:
            payload = self._state.get(key)
            if payload is None:
                return None
            return {"key": key, "etag": _etag(payload), "payload": copy.deepcopy(payload)}

    def _items(self) -> dict:
        with self._lock:
            return {"demo_marker": DEMO_MARKER, "steps": [self._item(key) for key in sorted(self._state)]}

    def _write(self, key: str, payload: dict, matches: list, none_matches: list, fault: dict | None) -> tuple:
        with self._lock:
            existing = key in self._state
            if existing:
                allowed = matches == [_etag(self._state[key])] and not none_matches
            else:
                allowed = none_matches == ["*"] and not matches
            if not allowed:
                return 412, {"error": "precondition_failed"}, False
            if fault is not None and not fault["after_commit"]:
                return 503, {"error": "injected_failure_before_commit"}, False
            self._state[key] = copy.deepcopy(payload)
            if fault is not None:
                return 503, {"error": "injected_failure_after_commit"}, True
            return (200 if existing else 201), self._item(key), True


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "3077LocalDemo/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args: object) -> None:
        pass

    @property
    def _mock(self) -> MockHandle:
        return self.server.handle

    def _respond(self, status: int, body: dict, committed: bool = False, etag: str | None = None) -> None:
        content = _canonical(body)
        self._mock._record(self.command, self.path, status, committed)
        self.send_response(status)
        self.send_header("X-Demo-Marker", DEMO_MARKER)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._respond(code, {"error": "http_error", "status": code})

    def _route(self) -> str | None:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        if parsed.path == "/demo/steps":
            return ""
        prefix = "/demo/steps/"
        if not parsed.path.startswith(prefix):
            return None
        try:
            key = unquote(parsed.path[len(prefix):], errors="strict")
        except UnicodeError:
            return None
        return key if KEY_RE.fullmatch(key) else None

    def do_GET(self) -> None:
        key = self._route()
        if key is None:
            self._respond(404, {"error": "not_found"})
        elif key == "":
            self._respond(200, self._mock._items())
        else:
            item = self._mock._item(key)
            if item is None:
                self._respond(404, {"error": "not_found"})
            else:
                self._respond(200, item, etag=item["etag"])

    def do_PUT(self) -> None:
        fault = self._mock._next_put_fault()
        key = self._route()
        if not key:
            self._respond(404, {"error": "not_found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            self._respond(400, {"error": "invalid_content_length"})
            return
        if len(lengths[0]) > 10:
            self._respond(413, {"error": "body_too_large", "limit_bytes": MAX_BODY_BYTES})
            return
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": "body_too_large", "limit_bytes": MAX_BODY_BYTES})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求正文长度不符")
            payload = _validate(key, _parse_json(raw))
        except (ValueError, UnicodeError, TimeoutError, RecursionError) as error:
            self._respond(400, {"error": "invalid_payload", "detail": str(error)})
            return
        status, record, committed = self._mock._write(key, payload, self.headers.get_all("If-Match", []), self.headers.get_all("If-None-Match", []), fault)
        self._respond(status, record, committed, record.get("etag"))

    def do_HEAD(self) -> None:
        self._respond(404, {"error": "not_found"})

    do_POST = do_HEAD
    do_DELETE = do_HEAD
    do_PATCH = do_HEAD
    do_OPTIONS = do_HEAD


def start_mock(initial: object = None) -> MockHandle:
    return MockHandle(initial)


def main() -> None:
    parser = argparse.ArgumentParser(description="3077独立本地模拟接口；不连接ION或MES")
    parser.add_argument("--state", type=Path, help="仅读取初始JSON，不会覆写或持久化")
    parser.add_argument("--port", type=int, default=0, help="本机端口，默认0自动分配")
    args = parser.parse_args()
    try:
        initial = _parse_json(args.state.read_bytes().removeprefix(b"\xef\xbb\xbf")) if args.state else None
        handle = MockHandle(initial, args.port)
    except (ValueError, OSError, UnicodeError) as error:
        parser.exit(2, "无法启动：" + str(error) + "\n")
    print(json.dumps({"demo_marker": DEMO_MARKER, "base_url": handle.base_url, "persistent": False}, ensure_ascii=False), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        handle.stop()


if __name__ == "__main__":
    main()
