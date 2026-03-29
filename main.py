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

ALLOWED_PROJECTS = {
    "66ed773f99c75168c1d3d420",
    "679e38696067d16f8b792da4",
    "699327944c495119bf8610d9",
    "699afac358b951c08d3e671e",
    "68aad622f99291ab56312d51",
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
        resp = httpx.get(f"{API_BASE}/project/{pid}/data", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # Process ALL tasks from /data (including completed ones)
            for task in data.get("tasks", []):
                _process_task(task, projects, today_date, from_dt, to_dt,
                              tasks_today, overdue, no_date, completed,
                              include_completed=includeCompleted)
            # Also check completedItems if present
            for task in data.get("completedItems", []):
                _process_task(task, projects, today_date, from_dt, to_dt,
                              tasks_today, overdue, no_date, completed,
                              include_completed=True, force_completed=True)

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


@app.get("/ticktick/debug/completed")
async def debug_completed(projectId: str):
    """Debug endpoint to test different completed tasks APIs."""
    token = get_valid_token()
    headers = {"Authorization": f"Bearer {token}"}
    results = {}

    # 1. Official /data endpoint - check all keys
    try:
        resp = httpx.get(f"{API_BASE}/project/{projectId}/data", headers=headers, timeout=10)
        results["v1_data_status"] = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            results["v1_data_keys"] = list(data.keys())
            results["v1_tasks_count"] = len(data.get("tasks", []))
            # Show statuses of tasks
            statuses = {}
            for t in data.get("tasks", []):
                s = t.get("status", "none")
                statuses[str(s)] = statuses.get(str(s), 0) + 1
            results["v1_task_statuses"] = statuses
    except Exception as e:
        results["v1_data_error"] = str(e)

    # 2. Try v2 completed endpoint
    try:
        resp = httpx.get(f"{API_V2_BASE}/project/{projectId}/completed/",
                         headers=headers, timeout=10)
        results["v2_completed_status"] = resp.status_code
        results["v2_completed_body"] = resp.text[:500]
    except Exception as e:
        results["v2_completed_error"] = str(e)

    # 3. Try official completed with params
    try:
        resp = httpx.get(f"{API_BASE}/project/{projectId}/completed",
                         headers=headers, timeout=10,
                         params={"from": "2026-03-01T00:00:00+0000",
                                 "to": "2026-04-30T23:59:59+0000"})
        results["v1_completed_status"] = resp.status_code
        results["v1_completed_body"] = resp.text[:500]
    except Exception as e:
        results["v1_completed_error"] = str(e)

    # 4. Try getting individual task with completed status
    try:
        resp = httpx.get(f"{API_BASE}/project/{projectId}/task",
                         headers=headers, timeout=10)
        results["v1_task_list_status"] = resp.status_code
        results["v1_task_list_body"] = resp.text[:300]
    except Exception as e:
        results["v1_task_list_error"] = str(e)

    return JSONResponse(results)


def _process_task(task, projects, today_date, from_dt, to_dt,
                  tasks_today, overdue, no_date, completed,
                  include_completed=False, force_completed=False):
    status = task.get("status", 0)
    is_completed = force_completed or status != 0

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


@app.get("/ticktick/tasks/raw")
async def get_tasks_raw(
    projectId: str = Query(...),
):
    """Return ALL raw task data from TickTick API for a project, including
    descriptions, checklists, recurrence, subtasks, and every other field."""
    token = get_valid_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.get(f"{API_BASE}/project/{projectId}/data", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Also try to get individual task details for richer data (content/desc)
    raw_tasks = data.get("tasks", [])
    enriched = []
    for task in raw_tasks:
        task_id = task.get("id")
        if task_id:
            try:
                detail = httpx.get(
                    f"{API_BASE}/project/{projectId}/task/{task_id}",
                    headers=headers, timeout=10
                )
                if detail.status_code == 200:
                    enriched.append(detail.json())
                else:
                    enriched.append(task)
            except Exception:
                enriched.append(task)
        else:
            enriched.append(task)

    return JSONResponse({
        "project_id": projectId,
        "tasks": enriched,
        "columns": data.get("columns", []),
        "raw_keys": list(data.keys()),
    })


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
    for field in ("content", "desc", "priority", "dueDate", "startDate", "columnId", "isAllDay", "timeZone"):
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
