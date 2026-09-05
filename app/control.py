"""Manual construction control. No engineering approvals or automatic source-file writes."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.classifier import SERVICE, UNKNOWN
from app.store import Conflict, NotFound, Store, now

STAGE_STATES = {"planned": "Запланирован", "in_progress": "В работе", "blocked": "Заблокирован", "done": "Завершен"}
ISSUE_STATES = {"open": "Открыто", "in_progress": "Устраняется", "resolved": "На проверке", "closed": "Закрыто"}
MAX_STAGES, MAX_ISSUES = 200, 2000


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("start_date", "due_date", check_fields=False)
    @classmethod
    def valid_date(cls, value):
        return date.fromisoformat(value).isoformat() if value else ""


class StageInput(Input):
    name: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=200)
    responsible: str = Field(min_length=1, max_length=200)
    start_date: str = Field(default="", max_length=10)
    due_date: str = Field(default="", max_length=10)
    status: Literal["planned", "in_progress", "blocked", "done"] = "planned"
    progress: int = Field(default=0, ge=0, le=100, strict=True)
    predecessor_id: int | None = Field(default=None, ge=1, strict=True)
    document_id: int | None = Field(default=None, ge=1, strict=True)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def coherent(self):
        if self.start_date and self.due_date and self.due_date < self.start_date:
            raise ValueError("Дата окончания раньше начала")
        if (self.status == "done" and self.progress != 100 or
                self.status == "planned" and self.progress != 0 or
                self.status not in {"planned", "done"} and self.progress == 100):
            raise ValueError("Запланирован: 0%; завершен: 100%; остальные статусы: 0–99%")
        if self.status == "blocked" and not self.note:
            raise ValueError("Укажите причину блокировки в примечании")
        return self


class StageUpdate(StageInput):
    version: int = Field(ge=1, strict=True)


class IssueInput(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    responsible: str = Field(min_length=1, max_length=200)
    due_date: str = Field(min_length=10, max_length=10)
    stage_id: int | None = Field(default=None, ge=1, strict=True)
    blocking: bool = Field(default=False, strict=True)
    status: Literal["open", "in_progress", "resolved", "closed"] = "open"
    resolution: str = Field(default="", max_length=4000)
    document_id: int | None = Field(default=None, ge=1, strict=True)
    verified_by: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def coherent(self):
        if self.status in {"resolved", "closed"} and not self.resolution:
            raise ValueError("Опишите, как устранено замечание")
        if self.status == "closed" and (not self.verified_by or self.document_id is None):
            raise ValueError("Для закрытия нужны проверивший и ID документа из реестра")
        if self.status != "closed" and self.verified_by:
            raise ValueError("Проверившего указывают только при закрытии")
        return self


class IssueUpdate(IssueInput):
    version: int = Field(ge=1, strict=True)


DDL = [
    "CREATE TABLE IF NOT EXISTS control_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS control_doc_scope ON documents(project_id,id)",
    """CREATE TABLE IF NOT EXISTS work_stages (
        id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, location TEXT NOT NULL, responsible TEXT NOT NULL,
        start_date TEXT NOT NULL, due_date TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('planned','in_progress','blocked','done')),
        progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
        predecessor_id INTEGER, document_id INTEGER, note TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,predecessor_id) REFERENCES work_stages(project_id,id),
        FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id))""",
    """CREATE TABLE IF NOT EXISTS prescriptions (
        id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        title TEXT NOT NULL, description TEXT NOT NULL, responsible TEXT NOT NULL, due_date TEXT NOT NULL,
        stage_id INTEGER, blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved','closed')),
        resolution TEXT NOT NULL, document_id INTEGER, verified_by TEXT NOT NULL,
        closed_at TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(project_id,stage_id) REFERENCES work_stages(project_id,id),
        FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id))""",
    "CREATE INDEX IF NOT EXISTS prescriptions_project_status ON prescriptions(project_id,status,due_date)",
    """CREATE TABLE IF NOT EXISTS control_changes (
        id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        entity TEXT NOT NULL, entity_id INTEGER NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
]


class Control:
    def __init__(self, store: Store, timezone_name: str = "UTC"):
        self.store = store
        self.timezone = timezone.utc if timezone_name == "UTC" else ZoneInfo(timezone_name)

    def today(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def initialize(self):
        # Separate version for additive module tables. Registry schema v1 is unchanged.
        with self.store.connect(True) as db:
            db.execute(DDL[0])
            version = db.execute("SELECT version FROM control_meta WHERE id=1").fetchone()
            if version and version[0] != 1:
                raise RuntimeError("Неподдерживаемая версия модуля контроля")
            for statement in DDL[1:]:
                db.execute(statement)
            db.execute("INSERT OR IGNORE INTO control_meta VALUES(1,1)")

    @staticmethod
    def _row(db, table: str, pid: str, ident: int) -> dict:
        # table names are internal constants, never request parameters.
        row = db.execute(f"SELECT * FROM {table} WHERE project_id=? AND id=?", (pid, ident)).fetchone()
        if row is None:
            raise NotFound("Запись не найдена в этом объекте")
        return dict(row)

    def _document(self, db, pid: str, did: int | None, closing: bool = False):
        if did is None:
            return
        row = self._row(db, "documents", pid, did)
        if not row["size"] or row["category"] == SERVICE:
            raise ValueError("Пустой или служебный файл нельзя привязать как документ")
        if closing and (not row["reviewed"] or row["category"] == UNKNOWN):
            raise ValueError("Перед закрытием подтвердите категорию документа в реестре")

    def _change(self, db, pid, table, ident, before, after):
        stamp = now()
        db.execute("INSERT INTO control_changes(project_id,entity,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                   (pid, table, ident, json.dumps(before, ensure_ascii=False) if before else None,
                    json.dumps(after, ensure_ascii=False), stamp))
        label = "Этап" if table == "work_stages" else "Предписание"
        labels = STAGE_STATES if table == "work_stages" else ISSUE_STATES
        previous = labels[before["status"]] if before else "Создано"
        self.store.event(db, pid, "control_changed",
                         f"{label} #{ident}: {previous} → {labels[after['status']]}; ответственный: {after['responsible']}; срок: {after['due_date'] or 'не указан'}")

    def _save(self, db, table, pid, values, ident=None, version=None):
        before = self._row(db, table, pid, ident) if ident is not None else None
        if before and before["version"] != version:
            raise Conflict("Запись изменена. Обновите страницу и повторите правку")
        stamp = now()
        fields = list(values)
        if before:
            db.execute(f"UPDATE {table} SET " + ",".join(f"{k}=?" for k in fields) +
                       ",version=version+1,updated_at=? WHERE project_id=? AND id=?",
                       [*values.values(), stamp, pid, ident])
        else:
            cursor = db.execute(f"INSERT INTO {table}(project_id,{','.join(fields)},created_at,updated_at) VALUES(" +
                                ",".join("?" for _ in range(len(fields) + 3)) + ")",
                                [pid, *values.values(), stamp, stamp])
            ident = cursor.lastrowid
        after = self._row(db, table, pid, ident)
        self._change(db, pid, table, ident, before, after)
        return after

    def save_stage(self, pid: str, values: StageInput, ident: int | None = None) -> dict:
        self.store.project(pid)
        data = values.model_dump(exclude={"version"})
        with self.store.connect(True) as db:
            stages = {r["id"]: dict(r) for r in db.execute("SELECT * FROM work_stages WHERE project_id=?", (pid,))}
            if ident is None and len(stages) >= MAX_STAGES:
                raise ValueError("В пилоте допускается не более 200 этапов на объект")
            self._document(db, pid, data["document_id"])
            if data["predecessor_id"] is not None and data["predecessor_id"] not in stages:
                raise NotFound("Предшествующий этап не найден в этом объекте")
            candidate = ident if ident is not None else -1
            stages[candidate] = {**data, "id": candidate}
            # Validate the whole graph; changing an ancestor cannot invalidate active successors.
            for sid, stage in stages.items():
                visited, cursor = set(), sid
                while cursor is not None:
                    if cursor in visited:
                        raise Conflict("Циклическая зависимость этапов запрещена")
                    visited.add(cursor)
                    cursor = stages[cursor]["predecessor_id"]
                pred = stage["predecessor_id"]
                if stage["status"] in {"in_progress", "done"} and pred is not None and stages[pred]["status"] != "done":
                    raise Conflict("Сначала завершите предшествующий этап либо заблокируйте зависимые этапы")
            if data["status"] in {"in_progress", "done"}:
                count = db.execute("""SELECT COUNT(*) FROM prescriptions WHERE project_id=? AND blocking=1
                    AND status!='closed' AND (stage_id IS NULL OR stage_id=?)""", (pid, candidate)).fetchone()[0]
                if count:
                    raise Conflict("Есть незакрытое блокирующее предписание. Проверьте раздел предписаний")
            return self._save(db, "work_stages", pid, data, ident, getattr(values, "version", None))

    def save_issue(self, pid: str, values: IssueInput, ident: int | None = None) -> dict:
        self.store.project(pid)
        data = values.model_dump(exclude={"version"})
        with self.store.connect(True) as db:
            old = self._row(db, "prescriptions", pid, ident) if ident is not None else None
            if old is None and db.execute("SELECT COUNT(*) FROM prescriptions WHERE project_id=?", (pid,)).fetchone()[0] >= MAX_ISSUES:
                raise ValueError("В пилоте допускается не более 2000 предписаний на объект")
            if data["stage_id"] is not None:
                self._row(db, "work_stages", pid, data["stage_id"])
            if data["status"] == "closed" and (not old or old["status"] not in {"resolved", "closed"}):
                raise Conflict("Сначала отправьте устранение на проверку, затем закройте предписание")
            if old and old["status"] == "closed" and data["status"] not in {"closed", "open"}:
                raise Conflict("Закрытое предписание сначала откройте повторно")
            if old and old["status"] == data["status"] == "closed" and any(data[k] != old[k] for k in data):
                raise Conflict("Для изменения закрытого предписания сначала откройте его повторно")
            self._document(db, pid, data["document_id"], closing=data["status"] == "closed")
            data["closed_at"] = (old["closed_at"] if old and old["closed_at"] else now()) if data["status"] == "closed" else None
            # Always permit recording a newly discovered defect, including after a stage is done.
            return self._save(db, "prescriptions", pid, data, ident, getattr(values, "version", None))

    def stages(self, pid: str) -> list[dict]:
        self.store.project(pid)
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("""SELECT s.*,p.name AS predecessor_name,p.status AS predecessor_status,
                (SELECT COUNT(*) FROM prescriptions i WHERE i.project_id=s.project_id AND i.blocking=1
                 AND i.status!='closed' AND (i.stage_id=s.id OR i.stage_id IS NULL)) AS blocking_issues
                FROM work_stages s LEFT JOIN work_stages p ON p.id=s.predecessor_id AND p.project_id=s.project_id
                WHERE s.project_id=? ORDER BY s.id""", (pid,))]
        for row in rows:
            row["overdue"] = bool(row["due_date"] and row["due_date"] < self.today() and row["status"] != "done")
            row["attention"] = bool(row["blocking_issues"] or row["status"] == "blocked" or
                                    row["predecessor_id"] and row["predecessor_status"] != "done")
        return rows

    def issues(self, pid: str, status: str = "", overdue: bool = False, page: int = 1) -> dict:
        self.store.project(pid)
        where, args = ["project_id=?"], [pid]
        if status:
            if status not in ISSUE_STATES:
                raise ValueError("Неизвестный статус предписания")
            where.append("status=?")
            args.append(status)
        if overdue:
            where.append("status!='closed' AND due_date<?")
            args.append(self.today())
        clause = " AND ".join(where)
        with self.store.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM prescriptions WHERE {clause}", args).fetchone()[0]
            rows = [dict(r) for r in db.execute(f"SELECT * FROM prescriptions WHERE {clause} ORDER BY (status='closed'),due_date,id LIMIT 50 OFFSET ?", [*args, (page-1)*50])]
        for row in rows:
            row["overdue"] = row["status"] != "closed" and row["due_date"] < self.today()
        return {"items": rows, "total": total, "page": page, "page_size": 50}

    def summary(self, pid: str) -> dict:
        stages = self.stages(pid)
        with self.store.connect() as db:
            issues = db.execute("SELECT status,due_date,blocking FROM prescriptions WHERE project_id=?", (pid,)).fetchall()
        return {"stages": len(stages), "done_stages": sum(s["status"] == "done" for s in stages),
                "attention_stages": sum(s["attention"] for s in stages), "overdue_stages": sum(s["overdue"] for s in stages),
                "open_issues": sum(i["status"] != "closed" for i in issues),
                "overdue_issues": sum(i["status"] != "closed" and i["due_date"] < self.today() for i in issues),
                "awaiting_review": sum(i["status"] == "resolved" for i in issues),
                "blocking_issues": sum(i["status"] != "closed" and bool(i["blocking"]) for i in issues),
                "today": self.today(), "timezone": str(self.timezone)}
