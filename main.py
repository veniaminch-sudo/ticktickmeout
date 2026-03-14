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
API_BASE = "https://api.ticktick.com/api/v2"


def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return None


def save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def refresh_access_token(tokens: dict):
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
    )
    response.raise_for_status()
    new_tokens = response.json()
    new_tokens["saved_at"] = time.time()
    save_tokens(new_tokens)
    return new_tokens


def get_valid_token():
    tokens = load_tokens()
    if not tokens:
        raise Exception("Not authorized. Visit /ticktick/auth first.")
    # Refresh if expires in less than 5 minutes
    expires_in = tokens.get("expires_in", 3600)
    saved_at = tokens.get("saved_at", 0)
    if time.time() - saved_at > expires_in - 300:
        tokens = refresh_access_token(tokens)
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
    today = datetime.now().strftime("%Y-%m-%d")

    response = httpx.get(
        f"{API_BASE}/task/closed",
        headers={"Authorization": f"Bearer {token}"},
        params={"from": today, "to": today, "limit": 50},
    )

    # Get all projects to find today's tasks
    projects_resp = httpx.get(
        f"{API_BASE}/projects",
        headers={"Authorization": f"Bearer {token}"},
    )

    tasks_today = []
    overdue = []

    if projects_resp.status_code == 200:
        projects = {p["id"]: p["name"] for p in projects_resp.json()}
    else:
        projects = {}

    # Get uncompleted tasks
    all_tasks_resp = httpx.get(
        f"{API_BASE}/batch/check/0",
        headers={"Authorization": f"Bearer {token}"},
    )

    if all_tasks_resp.status_code == 200:
        data = all_tasks_resp.json()
        sync_tasks = data.get("syncTaskBean", {}).get("update", [])

        now = datetime.now(timezone.utc)
        today_date = datetime.now().date()

        for task in sync_tasks:
            if task.get("status") != 0:
                continue
            due_date = task.get("dueDate")
            project_name = projects.get(task.get("projectId"), "Inbox")
            task_info = {
                "title": task.get("title"),
                "project": project_name,
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
                tasks_today.append(task_info)

    return JSONResponse({
        "today": tasks_today,
        "overdue": overdue,
        "date": today,
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
