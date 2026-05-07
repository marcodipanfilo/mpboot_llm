from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

DEFAULT_HOST = os.getenv("ANTHROPIC_MOCK_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("ANTHROPIC_MOCK_PORT", "8000"))
DEFAULT_MODE = os.getenv("ANTHROPIC_MOCK_MODE", "cache-first")
DEFAULT_DB = Path(os.getenv("ANTHROPIC_MOCK_DB", "anthropic_mock_server/cache.sqlite3"))
DEFAULT_REAL_BASE_URL = os.getenv("ANTHROPIC_REAL_BASE_URL", "https://api.anthropic.com")
DEFAULT_REAL_API_KEY = os.getenv("ANTHROPIC_REAL_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_LOG_LEVEL = os.getenv("ANTHROPIC_MOCK_LOG_LEVEL", "INFO").upper()

LOGGER = logging.getLogger("anthropic_mock_server")

VALID_MODES = {"cache-first", "record", "replay", "mock-only", "passthrough"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def debug_log(message: str) -> None:
    LOGGER.debug(message)


def ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anthropic_cache (
                request_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_headers TEXT NOT NULL,
                response_body TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def normalize_request(payload: Dict[str, Any], version: str, beta: str) -> str:
    key_obj = {
        "anthropic-version": version,
        "anthropic-beta": beta,
        "payload": payload,
    }
    return json.dumps(key_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def request_hash(payload: Dict[str, Any], version: str, beta: str) -> str:
    normalized = normalize_request(payload, version, beta)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_cache(db_path: Path, key: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT response_status, response_headers, response_body
            FROM anthropic_cache
            WHERE request_hash = ?
            """,
            (key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return {
        "status": int(row[0]),
        "headers": json.loads(row[1]),
        "body": json.loads(row[2]),
    }


def save_cache(db_path: Path, key: str, payload: Dict[str, Any], status: int, headers: Dict[str, str], body: Dict[str, Any]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO anthropic_cache (
                request_hash, created_at, request_json, response_status, response_headers, response_body
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                utc_now(),
                json.dumps(payload, ensure_ascii=False),
                int(status),
                json.dumps(headers, ensure_ascii=False),
                json.dumps(body, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def extract_prompt_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    parts = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(part.strip() for part in parts if part).strip()


def build_mock_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    prompt_text = extract_prompt_text(payload) or "Mock response"
    text = f"[mock] {prompt_text[:500]}"
    max_tokens = payload.get("max_tokens", 128)
    model = payload.get("model", "claude-mock")
    return {
        "id": f"msg_mock_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(1, len(prompt_text) // 4),
            "output_tokens": min(int(max_tokens), max(1, len(text) // 4)),
        },
    }


def forward_to_anthropic(payload: Dict[str, Any], version: str, beta: str, real_base_url: str, api_key: str) -> tuple[int, Dict[str, str], Dict[str, Any]]:
    target_url = real_base_url.rstrip("/") + "/v1/messages"
    req_headers = {
        "content-type": "application/json",
        "anthropic-version": version or "2023-06-01",
        "x-api-key": api_key,
    }
    if beta:
        req_headers["anthropic-beta"] = beta

    request = Request(
        target_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=req_headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=300) as response:
            raw = response.read().decode("utf-8")
            body = json.loads(raw)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), headers, body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"type": "error", "error": {"type": "http_error", "message": raw}}
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return int(exc.code), headers, body
    except URLError as exc:
        raise RuntimeError(f"Failed to reach Anthropic upstream: {exc}") from exc


class AnthropicMockHandler(BaseHTTPRequestHandler):
    server_version = "AnthropicMock/0.1"

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            self._write_json(
                HTTPStatus.OK,
                {
                    "service": "anthropic-mock-server",
                    "mode": self.server.mode,
                    "db_path": str(self.server.db_path),
                    "upstream": self.server.real_base_url,
                },
            )
            return

        if self.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"ok": True})
            return

        if self.path == "/v1/cache/stats":
            conn = sqlite3.connect(self.server.db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM anthropic_cache").fetchone()[0]
            finally:
                conn.close()
            self._write_json(HTTPStatus.OK, {"entries": count, "mode": self.server.mode})
            return

        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/messages":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})
            return

        version = self.headers.get("anthropic-version", "2023-06-01")
        beta = self.headers.get("anthropic-beta", "")
        key = request_hash(payload, version, beta)
        model = payload.get("model", "")
        debug_log(f"POST /v1/messages model={model} mode={self.server.mode} hash={key[:16]}")

        cached = load_cache(self.server.db_path, key)
        if self.server.mode in {"cache-first", "replay"} and cached is not None:
            debug_log(f"Cache HIT for hash={key[:16]} model={model}")
            self._write_json(cached["status"], cached["body"], extra_headers={"x-cache": "HIT"})
            return

        if self.server.mode == "replay":
            debug_log(f"Replay MISS for hash={key[:16]} model={model}")
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"type": "error", "error": {"type": "cache_miss", "message": "No cached response for request"}},
                extra_headers={"x-cache": "MISS"},
            )
            return

        if self.server.mode == "mock-only":
            debug_log(f"Mock-only response for hash={key[:16]} model={model}")
            body = build_mock_response(payload)
            save_cache(self.server.db_path, key, payload, HTTPStatus.OK, {}, body)
            self._write_json(HTTPStatus.OK, body, extra_headers={"x-cache": "MOCK"})
            return

        api_key = DEFAULT_REAL_API_KEY.strip()
        if not api_key:
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "type": "error",
                    "error": {
                        "type": "missing_upstream_key",
                        "message": "ANTHROPIC_REAL_API_KEY is required for live forwarding modes",
                    },
                },
            )
            return

        try:
            debug_log(f"Calling Anthropic upstream for hash={key[:16]} model={model}")
            status, headers, body = forward_to_anthropic(
                payload=payload,
                version=version,
                beta=beta,
                real_base_url=self.server.real_base_url,
                api_key=api_key,
            )
            debug_log(f"Anthropic upstream returned status={status} for hash={key[:16]} model={model}")
        except RuntimeError as exc:
            debug_log(f"Anthropic upstream failed for hash={key[:16]} model={model}: {exc}")
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
            )
            return

        if self.server.mode in {"cache-first", "record"} and 200 <= status < 300:
            save_cache(self.server.db_path, key, payload, status, headers, body)
            debug_log(f"Cached upstream response for hash={key[:16]} model={model}")

        self._write_json(status, body, extra_headers={"x-cache": "MISS"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _write_json(self, status: int, body: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


class AnthropicMockServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], *, db_path: Path, mode: str, real_base_url: str):
        super().__init__(server_address, handler_class)
        self.db_path = db_path
        self.mode = mode
        self.real_base_url = real_base_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Anthropic-compatible mock/cache server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mode", choices=sorted(VALID_MODES), default=DEFAULT_MODE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--real-base-url", default=DEFAULT_REAL_BASE_URL)
    parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    ensure_db(args.db)

    server = AnthropicMockServer(
        (args.host, args.port),
        AnthropicMockHandler,
        db_path=args.db,
        mode=args.mode,
        real_base_url=args.real_base_url,
    )
    LOGGER.info("Anthropic mock server listening on http://%s:%s", args.host, args.port)
    LOGGER.info("Mode: %s", args.mode)
    LOGGER.info("Cache DB: %s", args.db)
    LOGGER.info("Upstream: %s", args.real_base_url)
    LOGGER.info("Log level: %s", args.log_level.upper())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
