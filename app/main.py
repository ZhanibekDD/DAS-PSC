from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.completeness import build_completeness
from app.services.zip_analyzer import UnsafeArchive, analyze_zip

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "object_template.json"
TEMPLATE_CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

app = FastAPI(title="DAS-PSC", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

DEMO_FILES = [
    "Проект/DEMO-001-ГЧ-001_2.pdf",
    "Проект/DEMO-001-ОД-001_0.pdf",
    "Сертификаты и паспорта/Сертификат трубы.pdf",
    "Геодезическая съемка/Исполнительная съемка.pdf",
    "Фото объекта/Фото 2026-09-05.jpg",
    "Журналы ведения работ/Общий журнал работ.pdf",
]


def demo_project() -> dict:
    from app.services.classifier import classify_path

    categories = [classify_path(path).category for path in DEMO_FILES]
    completeness = build_completeness(categories, TEMPLATE_CONFIG["required_categories"])
    return {
        "id": "demo",
        "name": "Демо строительного объекта",
        "code": "DEMO-001",
        "status": "В работе",
        "progress": 42,
        "documents": len(DEMO_FILES),
        "review": 0,
        "completeness": completeness,
        "categories": Counter(categories),
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "das-psc", "version": "0.1.0"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"project": demo_project()})


@app.get("/projects/demo", response_class=HTMLResponse)
def project_card(request: Request):
    return templates.TemplateResponse(request=request, name="project.html", context={"project": demo_project()})


@app.post("/api/analyze-zip")
def analyze_archive(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Нужен ZIP-архив")
    try:
        analysis = analyze_zip(file.file)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Поврежденный ZIP") from None
    except UnsafeArchive as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    categories = [row["classification"]["category"] for row in analysis["files"]]
    analysis["completeness"] = build_completeness(categories, TEMPLATE_CONFIG["required_categories"])
    return analysis
