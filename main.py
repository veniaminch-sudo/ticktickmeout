import os
import json
import time
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse

app = FastAPI()

CLIENT_ID = os.environ["TICKTICK_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICKTICK_CLIENT_SECRET"]
REDIRECT_URI = os.environ["TICKTICK_REDIRECT_URI"]
TOKENS_FILE = "/data/tokens.json"

AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
API_BASE = "https://api.ticktick.com/open/v1"


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
    # Token expires_in is ~180 days, no refresh_token from TickTick
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
async def get_tasks():
    token = get_valid_token()
    today_date = datetime.now().date()
    headers = {"Authorization": f"Bearer {token}"}

    # Get all projects
    projects_resp = httpx.get(f"{API_BASE}/project", headers=headers)
    projects = {}
    open_project_ids = []
    if projects_resp.status_code == 200:
        for p in projects_resp.json():
            projects[p["id"]] = p["name"]
            if not p.get("closed") and p.get("kind") == "TASK":
                open_project_ids.append(p["id"])

    tasks_today = []
    overdue = []
    no_date = []

    # Fetch tasks from each open project
    for pid in open_project_ids:
        resp = httpx.get(f"{API_BASE}/project/{pid}/data", headers=headers)
        if resp.status_code != 200:
            continue
        data = resp.json()
        for task in data.get("tasks", []):
            if task.get("status") != 0:
                continue
            due_date = task.get("dueDate")
            task_info = {
                "title": task.get("title"),
                "project": projects.get(task.get("projectId"), "Inbox"),
                "priority": task.get("priority", 0),
                "due": due_date,
            }
            if due_date:
                due = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                if due.date() == today_date:
                    tasks_today.append(task_info)
                elif due.date() < today_date:
                    overdue.append(task_info)
            else:
                no_date.append(task_info)

    # Sort by priority (higher first)
    tasks_today.sort(key=lambda t: t["priority"], reverse=True)
    overdue.sort(key=lambda t: t["priority"], reverse=True)

    return JSONResponse({
        "today": tasks_today,
        "overdue": overdue,
        "no_date": no_date,
        "date": str(today_date),
    })


@app.get("/ticktick/status")
async def status():
    tokens = load_tokens()
    if not tokens:
        return JSONResponse({"status": "not_authorized"})
    return JSONResponse({
        "status": "authorized",
        "saved_at": tokens.get("saved_at"),
    })
