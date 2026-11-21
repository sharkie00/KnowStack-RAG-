"""知栈 Demo HTTP 服务。纯标准库，启动即用：

    python server.py
    python server.py --port 9000 --reset

API:
    GET  /api/state?course=os
    GET  /api/courses
    POST /api/courses        {name, description}
    GET  /api/coverage?course=os
    POST /api/ask            {question, courseId}
    POST /api/ingest         {title, text, authority, courseId}
    POST /api/eval/run       {courseId}
    POST /api/settings       {enabled, base_url, model, api_key}
    POST /api/reset
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import rag

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

LLM = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
}

SUGGESTIONS = {
    "os": [
        "进程和线程有什么区别？",
        "为什么需要 TLB？",
        "进程切换会换页表吗？",
        "LRU 怎么实现 O(1)？",
        "缺页中断的完整流程是什么？",
        "什么是 zswap？",
    ],
    "ds": [
        "栈和队列有什么区别？",
        "快速排序最坏为什么是 O(n²)？",
        "怎么判断链表有环？",
        "堆和优先队列是什么关系？",
        "哈希冲突怎么处理？",
        "什么是 B+ 树？",
    ],
}


def mode() -> str:
    return "llm" if LLM.get("enabled") and LLM.get("api_key") else "offline"


def eval_count(course_id: str) -> int:
    try:
        questions = json.loads(rag.QUESTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(1 for q in questions if q.get("courseId") == course_id)


def state_payload(course_id: str | None = None) -> dict:
    conn = rag.connect()
    course = rag.course_row(conn, course_id)
    if course is None:
        conn.close()
        return {"error": "没有可用课程"}
    cid = course["id"]
    payload = rag.coverage_payload(conn, cid)
    docs = conn.execute(
        "SELECT COUNT(*) AS n FROM docs WHERE course_id=?", (cid,)
    ).fetchone()["n"]
    chunks = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE d.course_id=?",
        (cid,),
    ).fetchone()["n"]
    courses = rag.list_courses(conn)
    conn.close()
    return {
        "courseId": cid,
        "course": course["name"],
        "description": course["description"] or "",
        "courses": courses,
        "mode": mode(),
        "hasKey": bool(LLM.get("api_key")),
        "model": LLM.get("model"),
        "stats": {
            "docs": docs,
            "chunks": chunks,
            "concepts": len(payload["concepts"]),
            "coverage": payload["overall"],
        },
        "concepts": payload["concepts"],
        "gaps": payload["gaps"][:5],
        "suggestions": SUGGESTIONS.get(
            cid,
            [
                "这门课最薄的地方在哪？",
                "帮我解释一个概念，并指出笔记缺什么。",
            ],
        ),
        "evalCount": eval_count(cid),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "KnowStackDemo/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- helpers ----------
    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        target = (STATIC / rel).resolve()
        base = STATIC.resolve()
        if not target.is_relative_to(base) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        self.send_response(200)
        self.send_header("Content-Type", (mime or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- routes ----------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._json({"ok": True, "mode": mode()})
            elif path == "/api/state":
                self._json(state_payload(query.get("course", [None])[0]))
            elif path == "/api/courses":
                conn = rag.connect()
                self._json({"courses": rag.list_courses(conn)})
                conn.close()
            elif path == "/api/coverage":
                conn = rag.connect()
                course = rag.course_row(conn, query.get("course", [None])[0])
                payload = rag.coverage_payload(conn, course["id"])
                conn.close()
                self._json(payload)
            elif path == "/api/notes":
                concept_id = query.get("conceptId", [""])[0]
                conn = rag.connect()
                self._json({"notes": rag.list_notes(conn, concept_id)})
                conn.close()
            elif path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
            else:
                self._static(path)
        except Exception as exc:  # noqa: BLE001 - demo 场景统一返回可读错误
            self._json({"error": str(exc)}, 500)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/notes/"):
                doc_id = path.rsplit("/", 1)[-1]
                body = self._read_json()
                result = rag.update_note(
                    doc_id, body.get("title") or "", body.get("text") or ""
                )
                result["state"] = state_payload(result["courseId"])
                self._json(result)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/ask":
                question = (body.get("question") or "").strip()
                if not question:
                    self._json({"error": "问题不能为空"}, 400)
                    return
                self._json(
                    rag.ask(question, course_id=body.get("courseId"), settings=LLM)
                )
            elif path == "/api/courses":
                name = (body.get("name") or "").strip()
                if not name:
                    self._json({"error": "课程名不能为空"}, 400)
                    return
                created = rag.create_course(name, body.get("description") or "")
                self._json({"ok": True, "course": created, "state": state_payload(created["id"])})
            elif path == "/api/concepts":
                cid = body.get("courseId")
                result = rag.add_concept(
                    cid, body.get("name") or "", body.get("definition") or ""
                )
                result["state"] = state_payload(result["courseId"])
                self._json(result)
            elif path.startswith("/api/concepts/") and path.endswith("/fill"):
                concept_id = path.split("/")[-2]
                result = rag.fill_concept(
                    concept_id,
                    body.get("example") or "",
                    body.get("derivation") or "",
                    body.get("practice") or "",
                )
                result["state"] = state_payload(result["courseId"])
                self._json(result)
            elif path == "/api/ingest":
                title = (body.get("title") or "").strip() or "未命名笔记"
                text = (body.get("text") or "").strip()
                if len(text) < 20:
                    self._json({"error": "内容太短，至少 20 字"}, 400)
                    return
                authority = int(body.get("authority") or 1)
                authority = max(1, min(3, authority))
                cid = body.get("courseId")
                result = rag.ingest_text(title[:80], text[:20000], authority, cid)
                result["state"] = state_payload(result["courseId"])
                self._json(result)
            elif path == "/api/eval/run":
                cid = body.get("courseId")
                result = rag.run_eval(cid)
                conn = rag.connect()
                conn.execute(
                    "INSERT INTO eval_runs(course_id,created_at,metrics_json) VALUES(?,?,?)",
                    (
                        cid,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        json.dumps(result["metrics"], ensure_ascii=False),
                    ),
                )
                conn.commit()
                conn.close()
                self._json(result)
            elif path == "/api/settings":
                LLM["enabled"] = bool(body.get("enabled"))
                if body.get("base_url"):
                    LLM["base_url"] = str(body["base_url"]).strip()
                if body.get("model"):
                    LLM["model"] = str(body["model"]).strip()
                if body.get("api_key"):
                    LLM["api_key"] = str(body["api_key"]).strip()
                if body.get("clear_key"):
                    LLM["api_key"] = ""
                self._json({"ok": True, "mode": mode(), "hasKey": bool(LLM.get("api_key"))})
            elif path == "/api/reset":
                stats = rag.rebuild()
                self._json({"ok": True, "stats": stats, "state": state_payload()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset:
        rag.rebuild()
    else:
        rag.seed_if_empty()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"KnowStack demo: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
