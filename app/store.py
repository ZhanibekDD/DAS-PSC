from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.services.classifier import UNKNOWN, SERVICE
from app.services.completeness import build_completeness


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL,
 customer TEXT NOT NULL, contractor TEXT NOT NULL, responsible TEXT NOT NULL,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, stage TEXT NOT NULL,
 progress INTEGER CHECK(progress BETWEEN 0 AND 100),
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 source TEXT NOT NULL, digest TEXT NOT NULL, manifest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled')),
 created_at TEXT NOT NULL, UNIQUE(project_id,digest)
);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 path TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 project_code TEXT, revision TEXT, suggested_category TEXT NOT NULL,
 category TEXT NOT NULL, score REAL NOT NULL, reason TEXT NOT NULL,
 reviewed INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, UNIQUE(project_id,path,sha256)
);
CREATE INDEX IF NOT EXISTS docs_project_hash ON documents(project_id,sha256);
CREATE TABLE IF NOT EXISTS import_documents (
 import_id TEXT NOT NULL REFERENCES imports(id),
 document_id INTEGER NOT NULL REFERENCES documents(id),
 PRIMARY KEY(import_id,document_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path: Path, categories: list[str]):
        self.path = Path(path)
        self.categories = list(dict.fromkeys(categories))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise RuntimeError("База создана более новой версией ПСК")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("casefold", 1, lambda s: str(s).casefold(), deterministic=True)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, project_id: str, kind: str, detail: str):
        db.execute("INSERT INTO events(project_id,kind,detail,created_at) VALUES(?,?,?,?)",
                   (project_id, kind, detail, now()))

    def create_project(self, values: dict) -> dict:
        pid = uuid4().hex
        with self.connect(True) as db:
            db.execute("""INSERT INTO projects
                (id,name,code,customer,contractor,responsible,start_date,end_date,stage,progress,created_at)
                VALUES(:id,:name,:code,:customer,:contractor,:responsible,:start_date,:end_date,:stage,:progress,:created_at)""",
                       {**values, "id": pid, "created_at": now()})
            self.event(db, pid, "project_created", "Создан объект")
        return self.project(pid)

    def project(self, pid: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise NotFound("Объект не найден")
        return dict(row)

    def update_project(self, pid: str, values: dict, version: int) -> dict:
        self.project(pid)
        with self.connect(True) as db:
            updated = db.execute("""UPDATE projects SET name=:name,code=:code,customer=:customer,
                contractor=:contractor,responsible=:responsible,start_date=:start_date,end_date=:end_date,
                stage=:stage,progress=:progress,version=version+1 WHERE id=:id AND version=:version""",
                                 {**values, "id": pid, "version": version}).rowcount
            if not updated:
                raise Conflict("Объект изменен другим пользователем. Обновите страницу")
            self.event(db, pid, "project_updated", "Обновлены паспорт или ручной ход работ")
        return self.project(pid)

    def projects(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_at DESC,id").fetchall()
        return [dict(r) for r in rows]

    def stage_import(self, pid: str, source: str, analysis: dict) -> dict:
        self.project(pid)
        payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True)
        # Independent of ZIP ordering, compression, or client filename.
        identity = sorted((r["path"], r["sha256"]) for r in analysis["files"])
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        iid = uuid4().hex
        with self.connect(True) as db:
            previous = db.execute("SELECT id,status FROM imports WHERE project_id=? AND digest=?", (pid, digest)).fetchone()
            if previous:
                iid = previous["id"]
                if previous["status"] == "cancelled":
                    db.execute("UPDATE imports SET status='pending',manifest=? WHERE id=?", (payload, iid))
            else:
                db.execute("INSERT INTO imports VALUES(?,?,?,?,?,'pending',?)",
                           (iid, pid, source[:200], digest, payload, now()))
                self.event(db, pid, "import_staged", f"Анализ: {len(identity)} файлов. Ожидает подтверждения")
        return self.import_info(pid, iid)

    def import_info(self, pid: str, iid: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM imports WHERE id=? AND project_id=?", (iid, pid)).fetchone()
        if row is None:
            raise NotFound("Импорт не найден в этом объекте")
        result = dict(row)
        result["analysis"] = json.loads(result.pop("manifest"))
        return result

    def confirm_import(self, pid: str, iid: str) -> dict:
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM imports WHERE id=? AND project_id=?", (iid, pid)).fetchone()
            if row is None:
                raise NotFound("Импорт не найден в этом объекте")
            if row["status"] == "cancelled":
                raise Conflict("Импорт отменен. Загрузите архив заново")
            if row["status"] == "confirmed":
                return {"added": 0, "already_confirmed": True}
            added = 0
            for item in json.loads(row["manifest"])["files"]:
                c = item["classification"]
                added += db.execute("""INSERT OR IGNORE INTO documents
                    (project_id,path,name,size,sha256,project_code,revision,suggested_category,category,score,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (pid, item["path"], item["name"], item["size"], item["sha256"],
                                     item["project_code"], item["revision"], c["category"], c["category"], c["confidence"], c["reason"], now())).rowcount
                did = db.execute("SELECT id FROM documents WHERE project_id=? AND path=? AND sha256=?",
                                 (pid, item["path"], item["sha256"])).fetchone()[0]
                db.execute("INSERT OR IGNORE INTO import_documents VALUES(?,?)", (iid, did))
            db.execute("UPDATE imports SET status='confirmed' WHERE id=?", (iid,))
            self.event(db, pid, "import_confirmed", f"Реестр сохранен: добавлено {added}. Исходники не изменялись")
            return {"added": added, "already_confirmed": False}

    def cancel_import(self, pid: str, iid: str):
        with self.connect(True) as db:
            row = db.execute("SELECT status FROM imports WHERE id=? AND project_id=?", (iid, pid)).fetchone()
            if row is None:
                raise NotFound("Импорт не найден")
            if row[0] == "confirmed":
                raise Conflict("Подтвержденный импорт отменить нельзя")
            db.execute("UPDATE imports SET status='cancelled' WHERE id=?", (iid,))
            self.event(db, pid, "import_cancelled", "Анализ отменен без изменения реестра")

    def documents(self, pid: str, q: str = "", category: str = "", review: bool = False,
                  duplicates: bool = False, page: int = 1, page_size: int = 100) -> dict:
        self.project(pid)
        where, params = ["project_id=?"], [pid]
        if q:
            where.append("instr(casefold(path),?)>0")
            params.append(q.casefold())
        if category:
            where.append("category=?")
            params.append(category)
        if review:
            where.append("reviewed=0 AND category!=?")
            params.append(SERVICE)
        if duplicates:
            where.append("sha256 IN (SELECT sha256 FROM documents WHERE project_id=? GROUP BY sha256 HAVING COUNT(*)>1)")
            params.append(pid)
        clause = " AND ".join(where)
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM documents WHERE {clause}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM documents WHERE {clause} ORDER BY path,id LIMIT ? OFFSET ?",
                              [*params, page_size, (page - 1) * page_size]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}

    def review(self, pid: str, did: int, category: str, version: int):
        if category not in [*self.categories, UNKNOWN, SERVICE]:
            raise ValueError("Неизвестная категория")
        with self.connect(True) as db:
            row = db.execute("SELECT category FROM documents WHERE project_id=? AND id=?", (pid, did)).fetchone()
            if row is None:
                raise NotFound("Документ не найден в этом объекте")
            changed = db.execute("UPDATE documents SET category=?,reviewed=?,version=version+1 WHERE project_id=? AND id=? AND version=?",
                                 (category, int(category != UNKNOWN), pid, did, version)).rowcount
            if not changed:
                raise Conflict("Документ изменен другим пользователем. Обновите страницу")
            self.event(db, pid, "document_reviewed", f"Документ #{did}: {row[0]} → {category}")

    def summary(self, pid: str) -> dict:
        self.project(pid)
        with self.connect() as db:
            rows = db.execute("SELECT category,reviewed,size,sha256 FROM documents WHERE project_id=?", (pid,)).fetchall()
            duplicate_groups = db.execute("SELECT COUNT(*) FROM (SELECT sha256 FROM documents WHERE project_id=? GROUP BY sha256 HAVING COUNT(*)>1)", (pid,)).fetchone()[0]
        meaningful = [r for r in rows if r["category"] != SERVICE and r["size"] > 0]
        report = build_completeness([r["category"] for r in meaningful], self.categories)
        for item in report["items"]:
            item["reviewed"] = sum(r["category"] == item["category"] and r["reviewed"] for r in meaningful)
        return {"documents": len(rows), "review": sum(not r["reviewed"] for r in meaningful),
                "unknown": sum(r["category"] == UNKNOWN for r in meaningful),
                "service": sum(r["category"] == SERVICE for r in rows),
                "empty_files": sum(r["size"] == 0 for r in rows),
                "unique_hashes": len({r["sha256"] for r in rows}),
                "duplicate_groups": duplicate_groups, "coverage": report}

    def activity(self, pid: str) -> dict:
        with self.connect() as db:
            events = db.execute("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 30", (pid,)).fetchall()
            imports = db.execute("SELECT id,source,status,created_at FROM imports WHERE project_id=? ORDER BY created_at DESC LIMIT 30", (pid,)).fetchall()
        return {"events": [dict(r) for r in events], "imports": [dict(r) for r in imports]}
