from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.globals import (
    templates,
    get_node_status,
    ip_privacy_state,
    streaming_status_cache
)

# Use the centralized templates object
# If you prefer to declare it here locally:
# templates = Jinja2Templates(directory="templates") 
# But better to use the one from globals if we are unifying.
# However, for now, let's assume we import the shared one to start using it.

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Render global dashboard (SPA entry point).
    """
    return templates.TemplateResponse("index.html", {
        "request": request
    })

@router.get("/tests", response_class=HTMLResponse)
async def tests_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    return templates.TemplateResponse("schedules.html", {"request": request})

@router.get("/trace", response_class=HTMLResponse)
    return templates.TemplateResponse("trace.html", {"request": request})

@router.get("/whitelist", response_class=HTMLResponse)
async def whitelist_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/redis", response_class=HTMLResponse)
async def redis_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

