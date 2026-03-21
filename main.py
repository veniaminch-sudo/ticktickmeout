import os
import json
import time
import httpx
from datetime import datetime, timezone, date
from fastapi import FastAPI, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse
from typing import Optional

app = FastAPI()

CLIENT_ID = os.environ["TICKTICK_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICKTICK_CLIENT_SECRET"]
REDIRECT_URI = os.environ["TICKTICK_REDIRECT_URI"]
TOKENS_FILE = "/data/tokens.json"

AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
API_BASE = "https://api.ticktick.com/open/v1"
API_V2_BASE = "https://api.ticktick.com/api/v2"

# Only fetch tasks from these projects
ALLOWED_PROJECTS = {
    "66ed773f99c75168c1d3d420",  # 🗓Стратегия
    "679e38696067d16f8b792da4",  # 🎴Тактика
    "699327944c495119bf8610d9",  # 🐧Наш список
    "699afac358b951c08d3e671e",  # Плэнсы с планерки
    "68aad622f99291ab56312d51",  # 🌇Высоко
}


def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return None


def save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def get_valid_token():
    tokens = load_tokens()
    if not tokens:
        raise Exception("Not authorized. Visit /ticktick/auth first.")
    expires_in = tokens.get("expires_in", 3600)
    saved_at = tokens.get("saved_at", 0)
    if time.time() - saved_at > expires_in - 300:
        raise Exception("Token expired. Re-authorize at /ticktick/auth")
    return tokens["access_token"]


@app.get("/ticktick/auth")
def auth():
    url = (
        f"{AUTH_URL}"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=tasks:read tasks:write"
    )
    return RedirectResponse(url)


@app.get("/ticktick/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "No code received"}, status_code=400)

    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
    )
    response.raise_for_status()
    tokens = response.json()
    tokens["saved_at"] = time.time()
    save_tokens(tokens)
    return JSONResponse({"status": "authorized", "message": "TickTick connected successfully!"})


@app.get("/ticktick/tasks")
async def get_tasks(
    projectId: Optional[str] = Query(None),
    includeCompleted: bool = Query(False),
    fromDate: Optional[str] = Query(None),
    toDate: Optional[str] = Query(None),
):
    token = get_valid_token()
    today_date = datetime.now().date()
    headers = {"Authorization": f"Bearer {token}"}

    from_dt = date.fromisoformat(fromDate) if fromDate else None
    to_dt = date.fromisoformat(toDate) if toDate else None

    # Get all projects
    projects_resp = httpx.get(f"{API_BASE}/project", headers=headers)
    projects = {}
    open_project_ids = []
    if projects_resp.status_code == 200:
        for p in projects_resp.json():
            projects[p["id"]] = p["name"]
            if not p.get("closed") and p.get("kind") == "TASK":
                if projectId:
                    if p["id"] == projectId:
                        open_project_ids.append(p["id"])
                elif p["id"] in ALLOWED_PROJECTS:
                    open_project_ids.append(p["id"])

    tasks_today = []
    overdue = []
    no_date = []
    completed = []

    for pid in open_project_ids:
        # Fetch active tasks
        resp = httpx.get(f"{API_BASE}/project/{pid}/data", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            for task in data.get("tasks", []):
                _process_task(task, projects, today_date, from_dt, to_dt,
                              tasks_today, overdue, no_date, completed,
                              include_completed=includeCompleted)

        # Fetch completed tasks via unofficial v2 API
        if includeCompleted:
            _fetch_completed_v2(pid, headers, projects, today_date,
                                from_dt, to_dt, completed)

    tasks_today.sort(key=lambda t: t["priority"], reverse=True)
    overdue.sort(key=lambda t: t["priority"], reverse=True)
    completed.sort(key=lambda t: t.get("completedTime", ""), reverse=True)

    result = {
        "today": tasks_today,
        "overdue": overdue,
        "no_date": no_date,
        "date": str(today_date),
    }
    if includeCompleted:
        result["completed"] = completed

    return JSONResponse(result)


def _fetch_completed_v2(project_id, headers, projects, today_date,
                        from_dt, to_dt, completed):
    """Fetch completed tasks using unofficial TickTick v2 API with pagination."""
    seen_ids = set()
    # Try multiple endpoints
    endpoints = [
        f"{API_V2_BASE}/project/{project_id}/completed/",
        f"{API_BASE}/project/{project_id}/completed",
    ]
    for url in endpoints:
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            task_list = data if isinstance(data, list) else data.get("tasks", data.get("completedItems", []))
            for task in task_list:
                task_id = task.get("id", task.get("title"))
                if task_id in seen_ids:
                    continue
                seen_ids.add(task_id)

                due_date = task.get("dueDate")
                completed_time = task.get("completedTime")

                # Date filter on completedTime or dueDate
                filter_date = None
                for dt_str in [completed_time, due_date]:
                    if dt_str:
                        try:
                            filter_date = datetime.fromisoformat(
                                dt_str.replace("Z", "+00:00")).date()
                            break
                        except (ValueError, TypeError):
                            pass

                if from_dt and filter_date and filter_date < from_dt:
                    continue
                if to_dt and filter_date and filter_date > to_dt:
                    continue

                task_info = {
                    "title": task.get("title"),
                    "project": projects.get(task.get("projectId"), "Inbox"),
                    "priority": task.get("priority", 0),
                    "due": due_date,
                    "status": "completed",
                }
                if completed_time:
                    task_info["completedTime"] = completed_time
                completed.append(task_info)
            if task_list:
                break  # Got data from this endpoint, skip others
        except Exception:
            continue


def _process_task(task, projects, today_date, from_dt, to_dt,
                  tasks_today, overdue, no_date, completed,
                  include_completed=False):
    status = task.get("status", 0)
    is_completed = status != 0

    if not include_completed and is_completed:
        return

    due_date = task.get("dueDate")
    completed_time = task.get("completedTime")
    task_info = {
        "title": task.get("title"),
        "project": projects.get(task.get("projectId"), "Inbox"),
        "priority": task.get("priority", 0),
        "due": due_date,
        "status": "completed" if is_completed else "active",
    }
    if completed_time:
        task_info["completedTime"] = completed_time

    task_date = None
    if due_date:
        try:
            task_date = datetime.fromisoformat(due_date.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            pass

    if from_dt and task_date and task_date < from_dt:
        return
    if to_dt and task_date and task_date > to_dt:
        return

    if is_completed:
        completed.append(task_info)
    elif due_date and task_date:
        if task_date == today_date:
            tasks_today.append(task_info)
        elif task_date < today_date:
            overdue.append(task_info)
        else:
            tasks_today.append(task_info)
    else:
        no_date.append(task_info)


@app.get("/ticktick/projects")
async def get_projects():
    token = get_valid_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(f"{API_BASE}/project", headers=headers)
    resp.raise_for_status()
    return JSONResponse([
        {"id": p["id"], "name": p["name"], "kind": p.get("kind")}
        for p in resp.json()
        if not p.get("closed")
    ])


@app.post("/ticktick/tasks/create")
async def create_task(request: Request):
    token = get_valid_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = await request.json()
    payload = {
        "title": body["title"],
        "projectId": body["projectId"],
    }
    for field in ("content", "desc", "priority", "dueDate", "startDate"):
        if field in body:
            payload[field] = body[field]
    resp = httpx.post(f"{API_BASE}/task", headers=headers, json=payload)
    resp.raise_for_status()
    return JSONResponse(resp.json())


@app.get("/ticktick/status")
async def status():
    tokens = load_tokens()
    if not tokens:
        return JSONResponse({"status": "not_authorized"})
    return JSONResponse({
        "status": "authorized",
        "saved_at": tokens.get("saved_at"),
    })
