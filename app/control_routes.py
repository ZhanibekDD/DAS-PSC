"""Construction-control endpoints share the existing authentication and same-origin guard."""
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.control import Control, IssueInput, IssueUpdate, StageInput, StageUpdate, STAGE_STATES, ISSUE_STATES

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def control(request: Request) -> Control:
    return request.app.state.control


@router.get("/projects/{pid}/control", response_class=HTMLResponse)
def board(request: Request, pid: str, status: str = Query(default="", max_length=20),
          overdue: bool = False, page: int = Query(default=1, ge=1, le=100000)):
    service = control(request)
    return templates.TemplateResponse(request=request, name="control.html", context={
        "project": service.store.project(pid), "stages": service.stages(pid),
        "issues": service.issues(pid, status, overdue, page), "control": service.summary(pid),
        "stage_states": STAGE_STATES, "issue_states": ISSUE_STATES, "status": status, "overdue": overdue})


@router.get("/api/projects/{pid}/control")
def overview(request: Request, pid: str):
    return control(request).summary(pid)


@router.get("/api/projects/{pid}/stages")
def stages(request: Request, pid: str):
    return {"items": control(request).stages(pid)}


@router.post("/api/projects/{pid}/stages", status_code=201)
def new_stage(request: Request, pid: str, values: StageInput):
    return control(request).save_stage(pid, values)


@router.patch("/api/projects/{pid}/stages/{sid}")
def edit_stage(request: Request, pid: str, sid: int, values: StageUpdate):
    return control(request).save_stage(pid, values, sid)


@router.get("/api/projects/{pid}/prescriptions")
def issues(request: Request, pid: str, status: str = Query(default="", max_length=20),
           overdue: bool = False, page: int = Query(default=1, ge=1, le=100000)):
    return control(request).issues(pid, status, overdue, page)


@router.post("/api/projects/{pid}/prescriptions", status_code=201)
def new_issue(request: Request, pid: str, values: IssueInput):
    return control(request).save_issue(pid, values)


@router.patch("/api/projects/{pid}/prescriptions/{iid}")
def edit_issue(request: Request, pid: str, iid: int, values: IssueUpdate):
    return control(request).save_issue(pid, values, iid)
