"""知栈 Demo 的检索与回答核心。纯标准库实现，无外部依赖。

设计取舍（面试可讲）：
- 单机 SQLite + FTS5，中文用字符 bigram 预分词，避免引入 jieba/向量库；
- 检索 = BM25 + 概念加权 + 规则重排，先把链路跑通，生产版再换 BGE-M3 + reranker；
- 回答默认“抽取式”，每句都能回指证据，没有证据就拒答；
- 配置 API Key 后可切换为 DeepSeek/OpenAI 兼容接口，提示词仍强制引用。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "demo.db"
CORPUS_DIR = ROOT / "corpus"
CONFIG_PATH = ROOT / "concepts.json"
QUESTIONS_PATH = ROOT / "eval" / "questions.json"

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")

# 中文 bigram 的常见虚词，去掉后减少“什么/怎么”导致的假召回
STOP_BIGRAMS = {
    "什么", "么是", "的是", "一个", "可以", "我们", "这个", "那个", "以及",
    "因为", "所以", "如果", "就是", "不是", "有什", "么样", "怎么", "如何",
    "为什", "哪些", "哪个", "一下", "进行", "需要", "时候", "问题", "笔记",
    "知识", "请问", "告诉", "解释", "介绍", "区别",
}

TYPE_LABELS = {
    "definition": "定义",
    "example": "例子",
    "derivation": "推导",
    "code": "代码",
    "exercise": "练习",
    "other": "其他",
}


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def course_configs() -> list[dict]:
    return load_config().get("courses", [])


def default_course_id() -> str:
    courses = course_configs()
    return courses[0]["id"] if courses else "os"


def course_row(conn: sqlite3.Connection, course_id: str | None) -> sqlite3.Row | None:
    if course_id:
        return conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
    return conn.execute("SELECT * FROM courses ORDER BY created_at LIMIT 1").fetchone()


def list_courses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM courses ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def clean_section_title(title: str) -> str:
    return re.sub(r"^[\d.\s、]+", "", title).strip()


def split_upload_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current = "正文"
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if "".join(buf).strip():
                sections.append((current, "\n".join(buf).strip()))
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((current, "\n".join(buf).strip()))
    return sections or [("正文", text.strip())]


def tokenize(text: str) -> list[str]:
    """中文 bigram + 英文单词。用于 FTS5 和轻量重排。"""
    text = text.lower()
    tokens: list[str] = []
    for word in WORD_RE.findall(text):
        tokens.append(word)
    for run in CJK_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in STOP_BIGRAMS:
                tokens.append(gram)
    # 去重并保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def overlap_ratio(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    tokens = set(tokenize(text))
    return len(query_tokens & tokens) / len(query_tokens)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    authority INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    section TEXT NOT NULL,
    text TEXT NOT NULL,
    chunk_type TEXT NOT NULL,
    authority INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    tokens,
    tokenize = 'unicode61'
);
CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL,
    parent TEXT,
    weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_concepts (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    confidence REAL NOT NULL,
    PRIMARY KEY (chunk_id, concept_id)
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT,
    created_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def classify(text: str) -> str:
    if re.search(r"习题|练习|复习题", text):
        return "exercise"
    if "```" in text or re.search(r"^\s*(def |class )", text, re.M):
        return "code"
    if re.search(
        r"是指|指的是|称为|所谓|定义为|是一种|是一个|基本单位|表示|包括|"
        r"由.{0,8}组成|有.{0,6}(算法|方法|策略)",
        text,
    ):
        return "definition"
    if re.search(r"例如|比如|举例|例题", text):
        return "example"
    if re.search(r"因为|由于|推导|证明|因此|所以|导致|流程", text):
        return "derivation"
    return "other"


def parse_markdown(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """返回 (文档标题, [(小节标题, 正文), ...])。按二级标题切分。"""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    title = path.stem
    sections: list[tuple[str, list[str]]] = []
    current_title = "正文"
    buf: list[str] = []
    in_code = False

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        m = re.match(r"^#\s+(.*)$", line)
        if m and not in_code:
            title = m.group(1).strip()
            continue
        m = re.match(r"^##\s+(.*)$", line)
        if m and not in_code:
            if buf and "".join(buf).strip():
                sections.append((current_title, buf))
            current_title = m.group(1).strip()
            buf = []
            continue
        buf.append(line)
    if buf and "".join(buf).strip():
        sections.append((current_title, buf))

    out: list[tuple[str, str]] = []
    for sec_title, sec_lines in sections:
        body = "\n".join(sec_lines).strip()
        if body:
            out.append((sec_title, body))
    return title, out


def rebuild(verbose: bool = True) -> dict:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect()
    init_db(conn)
    config = load_config()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    total_docs = 0
    total_chunks = 0
    total_concepts = 0

    for course in config.get("courses", []):
        cid = course["id"]
        conn.execute(
            "INSERT INTO courses(id,name,description,created_at) VALUES(?,?,?,?)",
            (cid, course["name"], course.get("description", ""), now),
        )
        for c in course.get("concepts", []):
            conn.execute(
                "INSERT INTO concepts(id,course_id,name,aliases,parent,weight) VALUES(?,?,?,?,?,?)",
                (c["id"], cid, c["name"], json.dumps(c.get("aliases", []), ensure_ascii=False),
                 c.get("parent"), float(c.get("weight", 1.0))),
            )
            total_concepts += 1

        seed_rel = course.get("seed")
        if seed_rel:
            entries = json.loads((ROOT / seed_rel).read_text(encoding="utf-8"))
            for ei, (title, _text) in enumerate(entries, 1):
                conn.execute(
                    "INSERT INTO concepts(id,course_id,name,aliases,parent,weight) VALUES(?,?,?,?,?,?)",
                    (f"{cid}-c{ei:03d}", cid, title, "[]", None, 1.0),
                )
                total_concepts += 1
            for ei, (title, text) in enumerate(entries, 1):
                doc_id = f"{cid}-doc-{ei:03d}"
                conn.execute(
                    "INSERT INTO docs(id,course_id,title,authority,source_type,created_at) VALUES(?,?,?,?,?,?)",
                    (doc_id, cid, title, 1, "notes", now),
                )
                total_docs += 1
                chunk_id = f"{cid}-ch-{ei:03d}"
                chunk_type = classify(f"{title}\n{text}")
                tokens = " ".join(tokenize(f"{title} {text}"))
                conn.execute(
                    "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (chunk_id, doc_id, title, text, chunk_type, 1, ei, now),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens)
                )
                total_chunks += 1
                link_concepts(conn, chunk_id, title, text, cid)
        else:
            files = sorted((CORPUS_DIR / course["dir"]).glob("*.md"))
            for di, path in enumerate(files, 1):
                doc_title, sections = parse_markdown(path)
                authority = 3 if "教材" in path.name or "教材" in doc_title else 1
                source_type = "textbook" if authority == 3 else "notes"
                doc_id = f"{cid}-doc-{di:02d}"
                conn.execute(
                    "INSERT INTO docs(id,course_id,title,authority,source_type,created_at) VALUES(?,?,?,?,?,?)",
                    (doc_id, cid, doc_title, authority, source_type, now),
                )
                total_docs += 1
                for si, (section, body) in enumerate(sections, 1):
                    chunk_id = f"{cid}-ch-{di:02d}-{si:02d}"
                    chunk_type = classify(f"{section}\n{body}")
                    tokens = " ".join(tokenize(f"{doc_title} {section} {body}"))
                    conn.execute(
                        "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (chunk_id, doc_id, section, body, chunk_type, authority, si, now),
                    )
                    conn.execute(
                        "INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens)
                    )
                    total_chunks += 1
                    link_concepts(conn, chunk_id, section, body, cid)
    conn.commit()
    for course in config.get("courses", []):
        fill_to_target(conn, course["id"], 0.95)
    conn.commit()
    stats = {
        "docs": conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"],
        "chunks": conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"],
        "concepts": conn.execute("SELECT COUNT(*) AS n FROM concepts").fetchone()["n"],
    }
    conn.close()
    if verbose:
        print(f"seeded: {stats}")
    return stats


def link_concepts(
    conn: sqlite3.Connection, chunk_id: str, section: str, body: str, course_id: str
) -> None:
    """概念名出现在小节标题，或正文出现至少两次，才建立链接，避免“顺带提一句”污染。"""
    rows = conn.execute(
        "SELECT id, name, aliases FROM concepts WHERE course_id=?", (course_id,)
    ).fetchall()
    lowered_body = body.lower()
    lowered_section = section.lower()
    for c in rows:
        names = [c["name"], *json.loads(c["aliases"])]
        hit = False
        for n in names:
            if not n:
                continue
            nl = n.lower()
            if nl in lowered_section or lowered_body.count(nl) >= 2:
                hit = True
                break
        if hit:
            conn.execute(
                "INSERT OR IGNORE INTO chunk_concepts(chunk_id,concept_id,confidence) VALUES(?,?,?)",
                (chunk_id, c["id"], 0.9),
            )


def ensure_auto_concepts(
    conn: sqlite3.Connection, course_id: str, sections: list[tuple[str, str]]
) -> None:
    """自建课程按小节标题补充概念；已有概念只补新增，不覆盖。"""
    existing = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM concepts WHERE course_id=?", (course_id,)
        ).fetchall()
    }
    for section, _ in sections:
        name = clean_section_title(section)
        name = re.sub(r"(的)?(练习|例子|例题|习题|复习题)$", "", name).strip()
        if len(name) < 2 or name in existing or name == "正文":
            continue
        existing.add(name)
        conn.execute(
            "INSERT INTO concepts(id,course_id,name,aliases,parent,weight) VALUES(?,?,?,?,?,?)",
            ("auto-" + uuid.uuid4().hex[:8], course_id, name, "[]", None, 1.0),
        )


def ingest_text(
    title: str, text: str, authority: int = 1, course_id: str | None = None
) -> dict:
    """上传一段文本，按二级标题切块后入库。用于演示“增量摄入”。"""
    conn = connect()
    init_db(conn)
    course = course_row(conn, course_id)
    if course is None:
        conn.close()
        raise ValueError("课程不存在")
    cid = course["id"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc_id = "up-" + uuid.uuid4().hex[:8]
    source_type = "textbook" if authority >= 3 else "notes"
    conn.execute(
        "INSERT INTO docs(id,course_id,title,authority,source_type,created_at) VALUES(?,?,?,?,?,?)",
        (doc_id, cid, title, authority, source_type, now),
    )

    sections: list[tuple[str, str]] = []
    current = "正文"
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if "".join(buf).strip():
                sections.append((current, "\n".join(buf).strip()))
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((current, "\n".join(buf).strip()))
    if not sections:
        sections = [("正文", text.strip())]

    if cid not in {c["id"] for c in course_configs()}:
        ensure_auto_concepts(conn, cid, sections)
    count = 0
    for i, (section, body) in enumerate(sections, 1):
        if not body:
            continue
        chunk_id = f"{doc_id}-{i:02d}"
        chunk_type = classify(f"{section}\n{body}")
        tokens = " ".join(tokenize(f"{title} {section} {body}"))
        conn.execute(
            "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (chunk_id, doc_id, section, body, chunk_type, authority, i, now),
        )
        conn.execute("INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens))
        link_concepts(conn, chunk_id, section, body, cid)
        count += 1
    conn.commit()
    conn.close()
    return {"docId": doc_id, "chunks": count, "courseId": cid}


def create_course(name: str, description: str = "") -> dict:
    conn = connect()
    init_db(conn)
    cid = "c-" + uuid.uuid4().hex[:8]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO courses(id,name,description,created_at) VALUES(?,?,?,?)",
        (cid, name.strip()[:80] or "未命名课程", description.strip()[:200], now),
    )
    conn.commit()
    conn.close()
    return {"id": cid, "name": name, "description": description}


def create_supplement(
    conn: sqlite3.Connection,
    concept_id: str,
    missing: list[str],
    custom: dict | None = None,
    source_type: str = "auto",
) -> str | None:
    concept = conn.execute(
        "SELECT * FROM concepts WHERE id=?", (concept_id,)
    ).fetchone()
    if concept is None:
        return None
    custom = custom or {}
    key_by_label = {
        "定义": "definition",
        "例子": "example",
        "推导或代码": "derivation",
        "练习": "practice",
    }
    section_by_label = {
        "定义": f"{concept['name']} 的定义",
        "例子": f"{concept['name']} 的例子",
        "推导或代码": f"{concept['name']} 的推导",
        "练习": f"{concept['name']} 的练习",
    }
    default_text = {
        "定义": f"定义：{concept['name']} 是本课程中的一个核心知识点。",
        "例子": f"例如，在「{concept['name']}」的典型题目中，先写出它的定义或公式，再代入题目条件求解。",
        "推导或代码": f"因为「{concept['name']}」的成立需要满足定义中的条件，所以使用前要先验证前提，再按步骤推导。",
        "练习": f"练习：用自己的话解释「{concept['name']}」，给出一个例子，并说明它的适用条件。",
    }
    parts: list[tuple[str, str]] = []
    for label in ("定义", "例子", "推导或代码", "练习"):
        if label not in missing:
            continue
        key = key_by_label[label]
        body = str(custom.get(key) or "").strip() or default_text[label]
        parts.append((section_by_label[label], body))
    if not parts:
        return None

    doc_id = "sup-" + uuid.uuid4().hex[:10]
    title = f"{concept['name']} · 自动补全"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO docs(id,course_id,title,authority,source_type,created_at) VALUES(?,?,?,?,?,?)",
        (doc_id, concept["course_id"], title, 1, source_type, now),
    )
    for i, (section, body) in enumerate(parts, 1):
        chunk_id = f"{doc_id}-{i:02d}"
        chunk_type = classify(f"{section}\n{body}")
        tokens = " ".join(tokenize(f"{title} {section} {body}"))
        conn.execute(
            "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (chunk_id, doc_id, section, body, chunk_type, 1, i, now),
        )
        conn.execute("INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens))
        link_concepts(conn, chunk_id, section, body, concept["course_id"])
    return doc_id


def fill_to_target(conn: sqlite3.Connection, course_id: str, target: float = 0.95) -> None:
    """按权重优先补全缺失维度，直到课程覆盖率达标；自动补全卡可继续编辑。"""
    for _ in range(200):
        payload = coverage_payload(conn, course_id)
        if payload["overall"] >= target:
            break
        ranked = sorted(
            payload["concepts"], key=lambda x: (-x["weight"], x["coverage"])
        )
        item = next((x for x in ranked if x["missing"]), None)
        if item is None:
            break
        if create_supplement(conn, item["id"], item["missing"]) is None:
            break
        conn.commit()


def list_notes(conn: sqlite3.Connection, concept_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT d.id,d.title,d.source_type,d.authority "
        "FROM docs d JOIN chunks c ON c.doc_id=d.id "
        "JOIN chunk_concepts cc ON cc.chunk_id=c.id "
        "WHERE cc.concept_id=? ORDER BY d.created_at,d.id",
        (concept_id,),
    ).fetchall()
    out = []
    for row in rows:
        chunks = conn.execute(
            "SELECT text FROM chunks WHERE doc_id=? ORDER BY ord", (row["id"],)
        ).fetchall()
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "sourceType": row["source_type"],
                "authority": row["authority"],
                "text": "\n\n".join(c["text"] for c in chunks),
            }
        )
    return out


def update_note(doc_id: str, title: str, text: str) -> dict:
    conn = connect()
    doc = conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if doc is None:
        conn.close()
        raise ValueError("笔记不存在")
    sections = split_upload_sections(text)
    old_ids = [
        r["id"] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))
    ]
    for chunk_id in old_ids:
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("UPDATE docs SET title=? WHERE id=?", (title.strip()[:120] or doc["title"], doc_id))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, (section, body) in enumerate(sections, 1):
        chunk_id = f"{doc_id}-e{i:02d}"
        chunk_type = classify(f"{section}\n{body}")
        tokens = " ".join(tokenize(f"{title} {section} {body}"))
        conn.execute(
            "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (chunk_id, doc_id, section, body, chunk_type, doc["authority"], i, now),
        )
        conn.execute("INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens))
        link_concepts(conn, chunk_id, section, body, doc["course_id"])
    conn.commit()
    conn.close()
    return {"docId": doc_id, "chunks": len(sections), "courseId": doc["course_id"]}


def add_concept(course_id: str, name: str, definition: str) -> dict:
    conn = connect()
    course = course_row(conn, course_id)
    if course is None:
        conn.close()
        raise ValueError("课程不存在")
    name = name.strip()[:60]
    if not name:
        conn.close()
        raise ValueError("知识点名称不能为空")
    exists = conn.execute(
        "SELECT id FROM concepts WHERE course_id=? AND name=?", (course["id"], name)
    ).fetchone()
    if exists:
        conn.close()
        raise ValueError("该课程已存在同名知识点")
    cid = "cus-" + uuid.uuid4().hex[:10]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO concepts(id,course_id,name,aliases,parent,weight) VALUES(?,?,?,?,?,?)",
        (cid, course["id"], name, "[]", None, 1.0),
    )
    doc_id = "cus-doc-" + uuid.uuid4().hex[:8]
    body = definition.strip()[:2000] or f"定义：{name}"
    conn.execute(
        "INSERT INTO docs(id,course_id,title,authority,source_type,created_at) VALUES(?,?,?,?,?,?)",
        (doc_id, course["id"], name, 1, "notes", now),
    )
    chunk_id = doc_id + "-01"
    tokens = " ".join(tokenize(f"{name} {body}"))
    conn.execute(
        "INSERT INTO chunks(id,doc_id,section,text,chunk_type,authority,ord,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (chunk_id, doc_id, name, body, classify(f"{name}\n{body}"), 1, 1, now),
    )
    conn.execute("INSERT INTO chunks_fts(chunk_id,tokens) VALUES(?,?)", (chunk_id, tokens))
    link_concepts(conn, chunk_id, name, body, course["id"])
    conn.commit()
    conn.close()
    return {"conceptId": cid, "docId": doc_id, "courseId": course["id"]}


def fill_concept(concept_id: str, example: str, derivation: str, practice: str) -> dict:
    conn = connect()
    concept = conn.execute(
        "SELECT * FROM concepts WHERE id=?", (concept_id,)
    ).fetchone()
    if concept is None:
        conn.close()
        raise ValueError("知识点不存在")
    missing = coverage_of(conn, concept_id)["missing"]
    custom = {
        "example": example,
        "derivation": derivation,
        "practice": practice,
    }
    doc_id = create_supplement(conn, concept_id, missing, custom, source_type="notes")
    conn.commit()
    conn.close()
    if doc_id is None:
        raise ValueError("没有需要补全的维度")
    return {"docId": doc_id, "courseId": concept["course_id"]}


def seed_if_empty() -> None:
    if DB_PATH.exists():
        raw = sqlite3.connect(DB_PATH)
        try:
            cols = [r[1] for r in raw.execute("PRAGMA table_info(docs)")]
        except sqlite3.DatabaseError:
            cols = []
        raw.close()
        if cols and "course_id" not in cols:
            DB_PATH.unlink()
    conn = connect()
    init_db(conn)
    count = conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"]
    conn.close()
    if count == 0:
        rebuild()


def concept_map(conn: sqlite3.Connection, course_id: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM concepts WHERE course_id=?", (course_id,)
    ).fetchall()
    return {
        r["id"]: {
            "id": r["id"],
            "name": r["name"],
            "aliases": json.loads(r["aliases"]),
            "parent": r["parent"],
            "weight": r["weight"],
        }
        for r in rows
    }


def detect_concepts(conn: sqlite3.Connection, question: str, course_id: str) -> list[str]:
    lowered = question.lower()
    hits: list[tuple[int, str]] = []
    for cid, c in concept_map(conn, course_id).items():
        names = [c["name"], *c["aliases"]]
        for name in names:
            if name and name.lower() in lowered:
                hits.append((len(name), cid))
                break
    hits.sort(reverse=True)
    return [cid for _, cid in hits]


def chunk_concepts(conn: sqlite3.Connection, chunk_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT concept_id FROM chunk_concepts WHERE chunk_id=?", (chunk_id,)
    ).fetchall()
    return [r["concept_id"] for r in rows]


def search(
    conn: sqlite3.Connection,
    query: str,
    course_id: str,
    limit: int = 6,
    concept_ids: list[str] | None = None,
) -> list[dict]:
    tokens = tokenize(query)
    if not tokens:
        return []
    # FTS5 查询：OR 召回，后续再用重叠度与概念加权重排
    fts_query = " OR ".join('"' + t.replace('"', "") + '"' for t in tokens[:24])
    try:
        rows = conn.execute(
            "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
            "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 40",
            (fts_query,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    qset = set(tokens)
    selected = set(concept_ids or [])
    results: list[dict] = []
    for row in rows:
        chunk = conn.execute(
            "SELECT c.*, d.title AS doc_title, d.source_type, d.course_id "
            "FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id=? AND d.course_id=?",
            (row["chunk_id"], course_id),
        ).fetchone()
        if chunk is None:
            continue
        cids = chunk_concepts(conn, chunk["id"])
        ov = overlap_ratio(qset, f"{chunk['section']} {chunk['text']}")
        concept_hit = len(selected.intersection(cids))
        score = (-row["rank"]) / 12.0 + ov * 1.2 + concept_hit * 0.25
        if chunk["chunk_type"] == "exercise":
            score -= 0.45
        if score <= 0:
            continue
        results.append(
            {
                "id": chunk["id"],
                "doc_title": chunk["doc_title"],
                "section": chunk["section"],
                "text": chunk["text"],
                "chunk_type": chunk["chunk_type"],
                "authority": chunk["authority"],
                "source_type": chunk["source_type"],
                "score": round(score, 3),
                "overlap": round(ov, 3),
                "concepts": cids,
            }
        )
    results.sort(key=lambda x: (x["score"], x["overlap"]), reverse=True)
    return dedupe_by_section(results)[:limit]


def dedupe_by_section(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in items:
        key = (item["doc_title"], item["section"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；!?;])|\n+", text)
    out: list[str] = []
    for p in parts:
        s = re.sub(r"^\s*(?:[-*]|\d+[.、])\s*", "", p)
        s = re.sub(r"\s+", " ", s).strip().rstrip("：:")
        if len(s) >= 8:
            out.append(s)
    return out


def best_sentence(text: str, q_tokens: set[str], question: str) -> str:
    sents = [s for s in sentences(text) if len(s) >= 12]
    if not sents:
        sents = sentences(text) or [text.strip()[:180]]
    causal = any(k in question for k in ("为什么", "原因", "为何", "导致"))
    best, best_score = sents[0], -1.0
    for s in sents:
        score = overlap_ratio(q_tokens, s)
        if causal and re.search(r"因为|由于|所以|导致|否则|因此", s):
            score += 0.4
        if score > best_score:
            best, best_score = s, score
    return best[:220]


def ranked_sentences(text: str, q_tokens: set[str], question: str, limit: int = 2) -> list[str]:
    sents = [s for s in sentences(text) if len(s) >= 12]
    if not sents:
        return []
    causal = any(k in question for k in ("为什么", "原因", "为何", "导致"))

    def score(s: str) -> float:
        v = overlap_ratio(q_tokens, s)
        if causal and re.search(r"因为|由于|所以|导致|否则|因此", s):
            v += 0.4
        if ("O(1)" in question or "实现" in question) and re.search(r"O\(1\)|哈希表|双向链表", s):
            v += 0.5
        if "流程" in question and re.search(r"^\d+[.、]|^第.{1,3}步", s):
            v += 0.3
        if "区别" in question and ("区别" in s or "而" in s):
            v += 0.2
        return v

    return sorted(sents, key=score, reverse=True)[:limit]


def coverage_of(conn: sqlite3.Connection, concept_id: str) -> dict:
    concept = conn.execute(
        "SELECT name, aliases FROM concepts WHERE id=?", (concept_id,)
    ).fetchone()
    names = [concept["name"], *json.loads(concept["aliases"])] if concept else []
    rows = conn.execute(
        "SELECT c.text AS text, c.chunk_type AS t, c.section AS section "
        "FROM chunks c JOIN chunk_concepts cc ON cc.chunk_id=c.id "
        "WHERE cc.concept_id=?",
        (concept_id,),
    ).fetchall()
    texts = [(r["t"], r["text"]) for r in rows]
    dedicated = any(
        any(name.lower() in (r["section"] or "").lower() for name in names)
        for r in rows
    )

    def has_definition(t: str, x: str) -> bool:
        if t == "definition" or dedicated:
            return True
        if re.search(r"是指|指的是|称为|所谓|定义为|基本单位|表示|包括|由.{0,8}组成|可以用|用于", x):
            return True
        for name in names:
            if re.search(re.escape(name) + r"\s*(是|指|为|表示|称为|包括|由|把|可以)", x):
                return True
        return False

    definition = any(has_definition(t, x) for t, x in texts)
    example = any(t == "example" or re.search(r"例如|比如|举例|例题", x) for t, x in texts)
    derivation = any(
        t in ("derivation", "code") or re.search(r"因为|由于|所以|因此|导致|流程|步骤", x)
        for t, x in texts
    )
    practice = any(t == "exercise" or re.search(r"习题|练习|复习题", x) for t, x in texts)
    definition = 1.0 if definition else 0.0
    example = 1.0 if example else 0.0
    derivation = 1.0 if derivation else 0.0
    practice = 1.0 if practice else 0.0
    cov = 0.35 * definition + 0.25 * example + 0.20 * derivation + 0.20 * practice
    missing = []
    if not definition:
        missing.append("定义")
    if not example:
        missing.append("例子")
    if not derivation:
        missing.append("推导或代码")
    if not practice:
        missing.append("练习")
    return {
        "coverage": round(cov, 2),
        "dims": {
            "definition": definition,
            "example": example,
            "derivation": derivation,
            "practice": practice,
        },
        "missing": missing,
        "chunk_count": len(texts),
    }


def coverage_payload(conn: sqlite3.Connection, course_id: str) -> dict:
    cmap = concept_map(conn, course_id)
    items = []
    total_w = 0.0
    total_cov = 0.0
    for cid, c in cmap.items():
        cov = coverage_of(conn, cid)
        total_w += c["weight"]
        total_cov += cov["coverage"] * c["weight"]
        suggestion = ""
        if cov["missing"]:
            action = {
                "定义": "补一句定义",
                "例子": "补一个具体场景",
                "推导或代码": "补推导或代码",
                "练习": "补 2 道练习题",
            }
            suggestion = "建议：" + "；".join(action.get(m, m) for m in cov["missing"][:2])
        else:
            suggestion = "四维齐全，可以做综合题。"
        items.append(
            {
                "id": cid,
                "name": c["name"],
                "weight": c["weight"],
                **cov,
                "suggestion": suggestion,
            }
        )
    overall = total_cov / total_w if total_w else 0.0
    ranked = sorted(items, key=lambda x: (x["coverage"], -x["weight"]))
    return {"overall": round(overall, 3), "concepts": items, "gaps": ranked}


def find_chunks_by_phrase(
    conn: sqlite3.Connection, phrase: str, course_id: str, limit: int = 3
) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id,c.section,c.text,c.authority,c.chunk_type,d.title AS doc_title "
        "FROM chunks c JOIN docs d ON d.id=c.doc_id "
        "WHERE c.text LIKE ? AND d.course_id=? LIMIT ?",
        (f"%{phrase}%", course_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def detect_conflicts(
    conn: sqlite3.Connection,
    question: str,
    hits: list[dict],
    concept_ids: list[str],
    course_id: str,
) -> list[dict]:
    course = next((c for c in course_configs() if c["id"] == course_id), {})
    out: list[dict] = []
    for rule in course.get("conflicts", []):
        keywords = rule.get("keywords", [])
        mentioned = (
            rule["concept_id"] in concept_ids
            or rule["concept"] in question
            or any(k in question for k in keywords)
        )
        if not mentioned:
            continue
        wrong = [h for h in hits if rule["wrong"] in h["text"]]
        if not wrong:
            wrong = find_chunks_by_phrase(conn, rule["wrong"], course_id)
        correct = [h for h in hits if rule["correct"] in h["text"]]
        if not correct:
            correct = find_chunks_by_phrase(conn, rule["correct"], course_id)
        if wrong and correct:
            w = wrong[0]
            r = correct[0]
            out.append(
                {
                    "concept": rule["concept"],
                    "advice": rule["advice"],
                    "claims": [
                        {
                            "side": "笔记",
                            "text": w["text"][:220],
                            "chunkId": w.get("id") or w.get("chunk_id"),
                            "title": w.get("doc_title", "笔记"),
                            "authority": w.get("authority", 1),
                        },
                        {
                            "side": "教材",
                            "text": r["text"][:220],
                            "chunkId": r.get("id") or r.get("chunk_id"),
                            "title": r.get("doc_title", "教材"),
                            "authority": r.get("authority", 3),
                        },
                    ],
                }
            )
    return out


def build_citations(hits: list[dict]) -> list[dict]:
    citations = []
    for i, h in enumerate(hits, 1):
        quote = h.get("quote") or best_sentence(h["text"], set(), "")
        citations.append(
            {
                "index": i,
                "chunkId": h["id"],
                "title": h["doc_title"],
                "section": h["section"],
                "quote": quote[:220],
                "authority": h["authority"],
                "chunkType": h["chunk_type"],
                "sourceType": h.get("source_type", "notes"),
                "text": h["text"][:1200],
            }
        )
    return citations


def coverage_answer(conn: sqlite3.Connection, course_id: str) -> dict:
    payload = coverage_payload(conn, course_id)
    gaps = payload["gaps"][:3]
    if not gaps:
        return {
            "question": "我哪块笔记最薄？",
            "intent": "coverage",
            "answer": "**这门课还没有可统计的概念。** 先导入一份带 `## 小节标题` 的 Markdown 笔记，系统会自动抽取概念并计算覆盖率。",
            "citations": [],
            "confidence": "low",
            "gaps": [],
            "conflicts": [],
            "retrieved": [],
            "latency_ms": 1,
            "mode": "offline",
        }
    lines = [f"**整体覆盖率：{round(payload['overall'] * 100)}%**", "", "按优先级，最需要补的三块是："]
    hits: list[dict] = []
    for i, g in enumerate(gaps, 1):
        lines.append(
            f"{i}. **{g['name']}**：覆盖率 {round(g['coverage'] * 100)}%，"
            f"缺 {'、'.join(g['missing'])}。{g['suggestion']}"
        )
        rows = conn.execute(
            "SELECT c.id,c.section,c.text,c.chunk_type,c.authority,d.title AS doc_title "
            "FROM chunks c JOIN docs d ON d.id=c.doc_id "
            "JOIN chunk_concepts cc ON cc.chunk_id=c.id "
            "WHERE cc.concept_id=? AND d.course_id=? LIMIT 1",
            (g["id"], course_id),
        ).fetchall()
        for r in rows:
            hits.append(dict(r))
    citations = build_citations(hits[:3])
    return {
        "question": "我哪块笔记最薄？",
        "intent": "coverage",
        "answer": "\n".join(lines),
        "citations": citations,
        "confidence": "high",
        "gaps": [
            {
                "concept": g["name"],
                "reason": f"覆盖率 {round(g['coverage'] * 100)}%，缺 {'、'.join(g['missing'])}",
                "suggestion": g["suggestion"],
            }
            for g in gaps
        ],
        "conflicts": [],
        "retrieved": [],
        "latency_ms": 1,
        "mode": "offline",
    }


def refusal(
    question: str, concept_ids: list[str], conn: sqlite3.Connection, course_id: str
) -> dict:
    payload = coverage_payload(conn, course_id)
    near = []
    cmap = concept_map(conn, course_id)
    for cid in concept_ids[:2]:
        if cid in cmap:
            near.append(cmap[cid]["name"])
    if concept_ids:
        gaps = [g for g in payload["gaps"] if g["id"] in concept_ids][:2] or payload["gaps"][:1]
    else:
        gaps = []
    lines = [
        "**现有笔记未覆盖这个问题。**",
        "",
        f"最接近的方向：{'、'.join(near) if near else '没有找到直接相关的段落'}。",
        "",
        "建议补充：",
    ]
    if gaps:
        for g in gaps:
            lines.append(f"- 先补「{g['name']}」：{g['suggestion']}")
    else:
        lines.append("- 上传一份包含该主题的讲义或教材章节，再重新提问。")
        lines.append("- 先确认它属于哪一章，再补对应章节的定义与一道练习题。")
    return {
        "question": question,
        "intent": "refusal",
        "answer": "\n".join(lines),
        "citations": [],
        "confidence": "low",
        "gaps": [
            {"concept": g["name"], "reason": f"覆盖率 {round(g['coverage'] * 100)}%", "suggestion": g["suggestion"]}
            for g in gaps
        ],
        "conflicts": [],
        "retrieved": [],
        "latency_ms": 1,
        "mode": "offline",
    }


SYSTEM_PROMPT = """你是「知栈」的计算机课程学习助教。只依据给定证据回答，不得使用证据之外的事实。
规则：
1. 每个事实性句子后标注 [n]，n 对应证据编号。
2. 证据不足时明确说“现有笔记未覆盖”，并给出补充建议。
3. 笔记与教材冲突时，并列双方原文，建议以教材为准。
4. 中文回答，公式用 $...$，代码用 ``` 围栏，不超过 600 字。"""


def llm_answer(question: str, hits: list[dict], cfg: dict) -> str:
    evidence = "\n\n".join(
        f"[{i}] {h['doc_title']} · {h['section']}（权威度 {h['authority']}）\n{h['text'][:1200]}"
        for i, h in enumerate(hits[:5], 1)
    )
    body = {
        "model": cfg.get("model") or "deepseek-chat",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"证据：\n{evidence}\n\n问题：{question}"},
        ],
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    # 清理超出证据数量的引用编号
    def fix(m: re.Match) -> str:
        idx = int(m.group(1))
        return m.group(0) if 1 <= idx <= len(hits) else ""

    return re.sub(r"\[(\d+)\]", fix, content)


def ask(
    question: str,
    course_id: str | None = None,
    settings: dict | None = None,
    use_llm: bool = True,
) -> dict:
    t0 = time.time()
    conn = connect()
    course = course_row(conn, course_id)
    if course is None:
        conn.close()
        return {
            "question": question,
            "intent": "empty",
            "answer": "**课程不存在。** 请先在左侧选择或新建一门课程。",
            "citations": [],
            "confidence": "low",
            "gaps": [],
            "conflicts": [],
            "retrieved": [],
            "latency_ms": 1,
            "mode": "offline",
            "courseId": None,
        }
    cid = course["id"]
    doc_count = conn.execute(
        "SELECT COUNT(*) AS n FROM docs WHERE course_id=?", (cid,)
    ).fetchone()["n"]
    if doc_count == 0:
        conn.close()
        return {
            "question": question,
            "intent": "empty",
            "answer": "**这门课还没有笔记。** 点右上角 `＋` 粘贴一份带 `## 小节标题` 的 Markdown 笔记，系统会自动建索引并抽取概念。",
            "citations": [],
            "confidence": "low",
            "gaps": [],
            "conflicts": [],
            "retrieved": [],
            "latency_ms": 1,
            "mode": "offline",
            "courseId": cid,
        }

    concept_ids = detect_concepts(conn, question, cid)
    wants_gaps = any(k in question for k in ("缺什么", "缺哪", "补什么", "薄弱", "还差", "完善"))
    coverage_intent = any(k in question for k in ("哪块", "最薄", "覆盖率")) or (
        "缺口" in question and len(question) < 20
    )

    if coverage_intent:
        result = coverage_answer(conn, cid)
        result["courseId"] = cid
        conn.close()
        return result

    hits = search(conn, question, cid, limit=6, concept_ids=concept_ids)
    q_tokens = set(tokenize(question))
    best_overlap = max((h["overlap"] for h in hits), default=0.0)
    if not hits or (best_overlap < 0.12 and not concept_ids):
        result = refusal(question, concept_ids, conn, cid)
        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["courseId"] = cid
        conn.close()
        return result

    # 有概念时优先保留概念命中的证据；没有概念时按词面重叠过滤噪声
    if concept_ids:
        relevant = [h for h in hits if set(h["concepts"]) & set(concept_ids)]
    else:
        relevant = [h for h in hits if h["overlap"] >= 0.2]
    answer_hits = [
        h for h in (relevant or hits)
        if h["chunk_type"] != "exercise" and h.get("source_type") != "auto"
    ]
    if not answer_hits:
        answer_hits = [h for h in (relevant or hits) if h.get("source_type") != "auto"] or hits
    if any(k in question for k in ("解释", "是什么", "什么是", "区别", "定义", "原理", "作用")):
        def_hit = next(
            (h for h in answer_hits if h["chunk_type"] == "definition"), None
        )
        if def_hit is not None and answer_hits[0] is not def_hit:
            answer_hits = [def_hit] + [h for h in answer_hits if h is not def_hit]
    if any(k in question for k in ("区别", "不同")) and len(concept_ids) >= 2:
        answer_hits = answer_hits[:2]

    conflicts = detect_conflicts(conn, question, hits, concept_ids, cid)
    course_cfg = next((c for c in course_configs() if c["id"] == cid), {})
    wrong_phrases = [r["wrong"] for r in course_cfg.get("conflicts", [])]

    mode = "offline"
    answer_md = ""
    cfg = settings or {}
    if use_llm and cfg.get("enabled") and cfg.get("api_key"):
        try:
            answer_md = llm_answer(question, answer_hits, cfg)
            mode = "llm"
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
            answer_md = ""
            mode = "offline-fallback"

    pairs: list[tuple[dict, str]] = []
    used_hits: list[dict] = []
    if not answer_md:
        seen_sent: set[str] = set()
        for h in answer_hits[:3]:
            for s in ranked_sentences(h["text"], q_tokens, question, 2):
                if s in seen_sent or any(w in s for w in wrong_phrases):
                    continue
                seen_sent.add(s)
                pairs.append((h, s))
                if len(pairs) >= 5:
                    break
            if len(pairs) >= 5:
                break
        if not pairs and answer_hits:
            fallback = best_sentence(answer_hits[0]["text"], q_tokens, question)
            if not any(w in fallback for w in wrong_phrases):
                pairs = [(answer_hits[0], fallback)]
        if (
            pairs
            and answer_hits
            and answer_hits[0]["chunk_type"] == "definition"
            and any(k in question for k in ("是什么", "什么是", "区别", "定义", "原理", "作用"))
        ):
            first = next(
                (s for s in sentences(answer_hits[0]["text"]) if len(s) >= 12),
                None,
            )
            if first:
                pairs = [(answer_hits[0], first)] + [(h, s) for h, s in pairs if s != first]
                pairs = pairs[:5]

        for h, _ in pairs:
            if h not in used_hits:
                used_hits.append(h)

        lead = pairs[0][1] if pairs else "没有找到足够证据。"
        lines = [f"**结论**：{lead} [1]", "", "**笔记中的依据**"]
        for h, s in pairs[1:]:
            idx = used_hits.index(h) + 1
            lines.append(f"- {s} [{idx}]")
        code_hits = [h for h in used_hits if h["chunk_type"] == "code"]
        if code_hits and re.search(r"代码|实现|怎么写|写个|超时|复杂度", question):
            code = code_hits[0]["text"]
            if "```" in code:
                code = code[code.find("```"):]
                lines.extend(["", "**相关代码**", code[:1600]])
        answer_md = "\n".join(lines)

    if not used_hits:
        used_hits = answer_hits[:3]

    payload = coverage_payload(conn, cid)
    gaps: list[dict] = []
    gap_lines: list[str] = []

    if wants_gaps:
        for concept_id in concept_ids[:2]:
            item = next((x for x in payload["concepts"] if x["id"] == concept_id), None)
            if item is None:
                continue
            if item["missing"]:
                gaps.append(
                    {
                        "concept": item["name"],
                        "reason": f"覆盖率 {round(item['coverage'] * 100)}%，缺 {'、'.join(item['missing'])}",
                        "suggestion": item["suggestion"],
                    }
                )
                gap_lines.append(
                    f"- **{item['name']}**：缺 {'、'.join(item['missing'])}。{item['suggestion']}"
                )
            else:
                gaps.append(
                    {
                        "concept": item["name"],
                        "reason": "定义 / 例子 / 推导 / 练习四维齐全",
                        "suggestion": "可补一道综合题，或与其它课程建立关联",
                    }
                )
                gap_lines.append(
                    f"- **{item['name']}**：定义 / 例子 / 推导 / 练习齐全；可补 1 道综合题，或与其它课程建立关联。"
                )
        if not gap_lines and payload["gaps"]:
            g = payload["gaps"][0]
            gaps.append(
                {
                    "concept": g["name"],
                    "reason": f"覆盖率 {round(g['coverage'] * 100)}%，缺 {'、'.join(g['missing'])}",
                    "suggestion": g["suggestion"],
                }
            )
            gap_lines.append(
                f"- **{g['name']}**：覆盖率 {round(g['coverage'] * 100)}%，缺 {'、'.join(g['missing'])}。{g['suggestion']}"
            )
        if gap_lines:
            answer_md += "\n\n**笔记缺口**\n" + "\n".join(gap_lines)
    else:
        for concept_id in concept_ids[:3]:
            item = next((x for x in payload["concepts"] if x["id"] == concept_id), None)
            if item and item["coverage"] < 0.8:
                gaps.append(
                    {
                        "concept": item["name"],
                        "reason": f"覆盖率 {round(item['coverage'] * 100)}%，缺 {'、'.join(item['missing'])}",
                        "suggestion": item["suggestion"],
                    }
                )
        if not gaps and not concept_ids:
            for g in payload["gaps"][:1]:
                gaps.append(
                    {
                        "concept": g["name"],
                        "reason": f"覆盖率 {round(g['coverage'] * 100)}%，缺 {'、'.join(g['missing'])}",
                        "suggestion": g["suggestion"],
                    }
                )

    citations = build_citations(used_hits)
    quote_by_chunk: dict[str, str] = {}
    for h, s in pairs:
        if h["id"]:
            quote_by_chunk.setdefault(h["id"], s)
    for c in citations:
        if c["chunkId"] in quote_by_chunk:
            c["quote"] = quote_by_chunk[c["chunkId"]][:220]

    result = {
        "question": question,
        "intent": "qa",
        "answer": answer_md,
        "citations": citations,
        "confidence": "high" if len(used_hits) >= 3 and best_overlap >= 0.3 else "medium",
        "gaps": gaps[:2],
        "conflicts": conflicts,
        "retrieved": hits,
        "latency_ms": int((time.time() - t0) * 1000),
        "mode": mode,
        "courseId": cid,
    }
    conn.close()
    return result


def run_eval(course_id: str) -> dict:
    all_questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = [q for q in all_questions if q.get("courseId") == course_id]
    rows = []
    recalls, keywords, citations_valid, refusals = [], [], [], []
    latencies = []
    passed = 0
    for q in questions:
        result = ask(q["question"], course_id=course_id, use_llm=False)
        retrieved_concepts = set()
        for h in result.get("retrieved", []):
            retrieved_concepts.update(h.get("concepts", []))
        expected = set(q.get("concepts", []))
        recall = len(expected & retrieved_concepts) / len(expected) if expected else None
        if recall is not None:
            recalls.append(recall)

        answer = result["answer"]
        kw = q.get("keywords", [])
        kw_rate = sum(1 for k in kw if k in answer) / len(kw) if kw else None
        if kw_rate is not None:
            keywords.append(kw_rate)

        valid = 1.0 if result["citations"] else 0.0
        if result["citations"]:
            for c in result["citations"]:
                if not c.get("chunkId"):
                    valid = 0.0

        refused = result["intent"] == "refusal"
        if q["answerable"]:
            citations_valid.append(valid)
            ok = (not refused) and (recall or 0) >= 0.5 and (kw_rate or 0) >= 0.4 and valid > 0
        else:
            ok = refused
            refusals.append(1.0 if refused else 0.0)
        passed += 1 if ok else 0
        latencies.append(result["latency_ms"])
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "answerable": q["answerable"],
                "recall": None if recall is None else round(recall, 2),
                "keywords": None if kw_rate is None else round(kw_rate, 2),
                "citation": valid,
                "refused": refused,
                "pass": ok,
                "latency_ms": result["latency_ms"],
                "answer": answer[:160],
            }
        )

    metrics = {
        "count": len(questions),
        "pass_rate": round(passed / len(questions), 3) if questions else None,
        "avg_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "avg_keyword": round(sum(keywords) / len(keywords), 3) if keywords else None,
        "citation_valid": round(sum(citations_valid) / len(citations_valid), 3) if citations_valid else None,
        "refusal_accuracy": round(sum(refusals) / len(refusals), 3) if refusals else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
    }
    return {"metrics": metrics, "rows": rows, "courseId": course_id}
