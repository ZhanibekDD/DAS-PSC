from __future__ import annotations

import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import SESSION_COOKIE, SESSION_HOURS, Auth, LoginRejected
from app.control import Control
from app.control_routes import router as control_router
from app.security import GuardMiddleware, behind_https, client_address
from app.services.classifier import HIGH_CONFIDENCE, SERVICE, UNKNOWN
from app.services.zip_analyzer import DEFAULT_LIMITS, analyze_zip
from app.store import Conflict, NotFound, Store

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config/object_template.json").read_text(encoding="utf-8"))
APP_VERSION = "0.5.0"


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=100)
    customer: str = Field(default="", max_length=200)
    contractor: str = Field(default="", max_length=200)
    responsible: str = Field(default="", max_length=200)
    start_date: str = ""
    end_date: str = ""
    stage: str = Field(default="Не задан", max_length=100)
    progress: int | None = Field(default=None, ge=0, le=100)

    @field_validator("name", "code", "customer", "contractor", "responsible", "stage", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, value):
        if value:
            return date.fromisoformat(value).isoformat()
        return ""

    @model_validator(mode="after")
    def date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Дата окончания раньше начала")
        return self


class ProjectUpdate(ProjectInput):
    version: int = Field(ge=1)


class ReviewInput(BaseModel):
    category: str = Field(max_length=200)
    version: int = Field(ge=1)


class BulkReviewInput(BaseModel):
    min_score: float = Field(default=HIGH_CONFIDENCE, ge=HIGH_CONFIDENCE, le=1.0)
    category: str = Field(default="", max_length=200)


class ReclassifyInput(BaseModel):
    expected_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)
    next: str = Field(default="/", max_length=500)


class PasswordInput(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)
    repeat: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def same(self):
        if self.new != self.repeat:
            raise ValueError("Новый пароль и повтор не совпадают")
        return self


class UserInput(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    display_name: str = Field(default="", max_length=120)
    role: str = Field(default="user", pattern=r"^(admin|user)$")
    password: str = Field(min_length=1, max_length=200)


class ResetInput(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ActiveInput(BaseModel):
    active: bool


def safe_next(value: str) -> str:
    """Only same-site paths: no scheme, no //host, no backslashes."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"


def create_app(data_dir: Path | None = None, password: str | None = None) -> FastAPI:
    directory = Path(data_dir or os.environ.get("PSC_DATA_DIR", "data"))
    shared_password = os.environ.get("PSC_PASSWORD", "") if password is None else password
    if shared_password and len(shared_password) < 9:
        raise RuntimeError("PSC_PASSWORD должен содержать минимум 9 символов")

    @asynccontextmanager
    async def lifespan(application):
        application.state.store = Store(directory / "psc.sqlite3", CONFIG["required_categories"])
        application.state.control = Control(application.state.store, os.environ.get("PSC_TIMEZONE", "UTC"))
        application.state.control.initialize()
        application.state.auth = Auth(application.state.store)
        application.state.auth.initialize(shared_password)
        yield

    app = FastAPI(title="DAS-PSC", version=APP_VERSION, lifespan=lifespan)
    hosts = [v.strip() for v in os.environ.get("PSC_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if v.strip()]
    if not shared_password and any(h not in {"localhost", "127.0.0.1", "testserver"} for h in hosts):
        raise RuntimeError("Для сетевого адреса необходимо установить PSC_PASSWORD")
    app.add_middleware(GuardMiddleware, password=shared_password, max_body=DEFAULT_LIMITS.max_upload + 1024 * 1024)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    @app.exception_handler(ValueError)
    async def expected_error(request, exc):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    def store(request: Request) -> Store:
        return request.app.state.store

    def render(request, name, **context):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "categories": CONFIG["required_categories"],
                "unknown": UNKNOWN,
                "service": SERVICE,
                "app_version": APP_VERSION,
                "current_user": getattr(request.state, "user", None),
                **context,
            },
        )

    app.state.render = render  # routers outside this module get the same base context

    @app.get("/health")
    def health(request: Request):
        with store(request).connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "das-psc", "version": APP_VERSION}

    def address(request: Request) -> str:
        return client_address(request.scope, {k.lower(): v for k, v in request.scope["headers"]})

    def current(request: Request) -> dict:
        return getattr(request.state, "user", None) or {}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = Query(default="/", max_length=500)):
        return render(request, "login.html", next=safe_next(next))

    @app.post("/api/login")
    def login(request: Request, values: LoginInput):
        try:
            token, user = request.app.state.auth.login(values.username, values.password, address(request))
        except LoginRejected as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        response = JSONResponse({"url": "/account" if user["must_change"] else safe_next(values.next)})
        secure = behind_https(request.scope, {k.lower(): v for k, v in request.scope["headers"]})
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                            samesite="lax", secure=secure, path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            request.app.state.auth.logout(token, address(request))
        response = JSONResponse({"url": "/login"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request):
        return render(request, "account.html", user=current(request))

    @app.post("/api/account/password")
    def change_password(request: Request, values: PasswordInput):
        user = current(request)
        if not user.get("id"):
            raise HTTPException(400, "В локальном режиме пароль не используется")
        try:
            request.app.state.auth.change_password(user["id"], values.current, values.new, address(request))
        except LoginRejected as exc:
            raise HTTPException(400, str(exc)) from exc
        response = JSONResponse({"url": "/login"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        auth = request.app.state.auth
        return render(request, "users.html", users=auth.users(), events=auth.events(50), user=current(request))

    @app.get("/api/users")
    def list_users(request: Request):
        return request.app.state.auth.users()

    @app.post("/api/users", status_code=201)
    def add_user(request: Request, values: UserInput):
        created = request.app.state.auth.create_user(values.username, values.display_name, values.role,
                                                     values.password, current(request).get("username", ""))
        return {**created, "url": "/users"}

    @app.post("/api/users/{uid}/password")
    def reset_user_password(request: Request, uid: int, values: ResetInput):
        request.app.state.auth.reset_password(uid, values.password, current(request).get("username", ""))
        return {"url": "/users"}

    @app.post("/api/users/{uid}/active")
    def set_user_active(request: Request, uid: int, values: ActiveInput):
        user = current(request)
        request.app.state.auth.set_active(uid, values.active, user.get("id", 0), user.get("username", ""))
        return {"url": "/users"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        projects = [{**p, **store(request).summary(p["id"]), "control": request.app.state.control.summary(p["id"])} for p in store(request).projects()]
        return render(request, "dashboard.html", projects=projects)

    @app.get("/projects/{pid}", response_class=HTMLResponse)
    def project(request: Request, pid: str):
        db = store(request)
        return render(request, "project.html", project=db.project(pid), summary=db.summary(pid), control=request.app.state.control.summary(pid), **db.activity(pid))

    @app.get("/projects/{pid}/documents", response_class=HTMLResponse)
    def documents(request: Request, pid: str, q: str = Query(default="", max_length=200),
                  category: str = Query(default="", max_length=200), review: bool = False,
                  duplicates: bool = False, page: int = Query(default=1, ge=1, le=100000)):
        db = store(request)
        duplicate_result = db.duplicate_groups(pid, q, category, review, page) if duplicates else None
        result = db.documents(pid, q, category, review, False, page) if not duplicates else None
        return render(
            request,
            "documents.html",
            project=db.project(pid),
            result=result,
            duplicate_result=duplicate_result,
            q=q,
            category=category,
            review=review,
            duplicates=duplicates,
            summary=db.summary(pid),
            reclass=db.reclassification_preview(pid),
        )

    @app.get("/projects/{pid}/imports/{iid}", response_class=HTMLResponse)
    def import_preview(request: Request, pid: str, iid: str, page: int = Query(default=1, ge=1, le=100000)):
        db = store(request)
        job = db.import_info(pid, iid)
        rows = job["analysis"]["files"][(page - 1) * 100:page * 100]
        return render(request, "import.html", project=db.project(pid), job=job, rows=rows, page=page)

    @app.get("/api/projects")
    def list_projects(request: Request):
        return {"items": store(request).projects()}

    @app.post("/api/projects", status_code=201)
    def add_project(request: Request, values: ProjectInput):
        p = store(request).create_project(values.model_dump())
        return {**p, "url": f"/projects/{p['id']}"}

    @app.patch("/api/projects/{pid}")
    def edit_project(request: Request, pid: str, values: ProjectUpdate):
        return store(request).update_project(pid, values.model_dump(exclude={"version"}), values.version)

    @app.get("/api/projects/{pid}")
    def get_project(request: Request, pid: str):
        return {**store(request).project(pid), "summary": store(request).summary(pid)}

    @app.get("/api/projects/{pid}/documents")
    def get_documents(request: Request, pid: str, q: str = Query(default="", max_length=200),
                      category: str = Query(default="", max_length=200), review: bool = False,
                      duplicates: bool = False, page: int = Query(default=1, ge=1, le=100000),
                      page_size: int = Query(default=100, ge=1, le=200)):
        return store(request).documents(pid, q, category, review, duplicates, page, page_size)

    @app.get("/api/projects/{pid}/documents/reclassify-preview")
    def reclassify_preview(request: Request, pid: str):
        return store(request).reclassification_preview(pid)

    @app.post("/api/projects/{pid}/documents/reclassify")
    def reclassify_documents(request: Request, pid: str, values: ReclassifyInput):
        result = store(request).apply_reclassification(pid, values.expected_token)
        return {**result, "url": f"/projects/{pid}/documents?review=true"}

    @app.post("/api/projects/{pid}/documents/bulk-review")
    def bulk_review_documents(request: Request, pid: str, values: BulkReviewInput):
        result = store(request).bulk_review(pid, values.min_score, values.category)
        return {**result, "url": f"/projects/{pid}/documents?review=true"}

    @app.patch("/api/projects/{pid}/documents/{did}")
    def review_document(request: Request, pid: str, did: int, values: ReviewInput):
        store(request).review(pid, did, values.category, values.version)
        return {"status": "saved"}

    def analyze(file):
        try:
            if not file.filename or not file.filename.casefold().endswith(".zip"):
                raise HTTPException(400, "Нужен ZIP-архив")
            return analyze_zip(file.file)
        finally:
            file.file.close()

    @app.post("/api/analyze-zip")
    def analyze_only(file: UploadFile = File(...)):
        return analyze(file)

    @app.post("/api/projects/{pid}/imports", status_code=201)
    def prepare_import(request: Request, pid: str, file: UploadFile = File(...)):
        db = store(request)
        db.project(pid)
        job = db.stage_import(pid, file.filename or "ZIP", analyze(file))
        return {"id": job["id"], "status": job["status"], "url": f"/projects/{pid}/imports/{job['id']}"}

    @app.post("/api/projects/{pid}/imports/{iid}/confirm")
    def confirm(request: Request, pid: str, iid: str):
        return {**store(request).confirm_import(pid, iid), "url": f"/projects/{pid}/documents"}

    @app.post("/api/projects/{pid}/imports/{iid}/refresh")
    def refresh_import_analysis(request: Request, pid: str, iid: str, values: ReclassifyInput):
        result = store(request).apply_import_refresh(pid, iid, values.expected_token)
        return {**result, "url": f"/projects/{pid}/documents?review=true"}

    @app.post("/api/projects/{pid}/imports/{iid}/cancel")
    def cancel(request: Request, pid: str, iid: str):
        store(request).cancel_import(pid, iid)
        return {"url": f"/projects/{pid}"}

    @app.get("/api/projects/{pid}/documents.csv")
    def export_csv(request: Request, pid: str):
        rows = store(request).documents(pid, page_size=2147483647)["items"]
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "Путь", "Категория", "Категория проверена", "Уверенность правил", "Причина",
            "Контент-анализ", "Текстовых символов", "Нужен OCR", "Байт", "SHA-256",
            "Код (гипотеза)", "Ревизия (гипотеза)",
        ])

        def safe(value):
            text = str(value if value is not None else "")
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text

        for r in rows:
            values = [
                r["path"], r["category"], r["reviewed"], r["score"], r["reason"],
                r["content_status"], r["content_text_chars"], r["content_needs_ocr"],
                r["size"], r["sha256"], r["project_code"], r["revision"],
            ]
            writer.writerow([safe(value) for value in values])
        return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="psc-registry.csv"'})

    app.include_router(control_router)
    return app


app = create_app()
