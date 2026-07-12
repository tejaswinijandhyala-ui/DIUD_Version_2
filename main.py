"""
main.py — the application's entry point.

Run this file to start the server: `uvicorn main:app --reload`

Everything this file does is startup wiring — creating the FastAPI app
and attaching the routes defined in api/routes.py. No business logic
lives here, on purpose, for the same reason api/routes.py stays thin:
if this file starts growing real logic, that logic belongs in graph/,
agents/, or tools/ instead.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from api.routes import router as chat_router
from api.admin_routes import router as admin_router
from api.export_routes import router as export_router

app = FastAPI(title="Revenue Intelligence Copilot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(admin_router, tags=["admin"])
app.include_router(export_router, tags=["export"])


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """
    Serves the chat UI directly from this backend, at the same origin.
    This matters more than it might look like: every fetch() call in
    frontend/index.html uses a relative path like '/api/chat', not a
    full URL. Those only resolve correctly if the HTML is served from
    this exact same domain — deploying the HTML separately (e.g. as a
    Render Static Site) would break every one of those calls silently,
    since they'd hit the static site's own origin instead of this API.
    """
    with open("frontend/index.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/health")
def health_check():
    """
    A simple endpoint for uptime monitoring. Confirms the server process
    is alive without touching ClickHouse or Claude at all — so a
    monitoring check never fails just because the database is briefly
    slow, only if the server itself is actually down.
    """
    return {"status": "ok"}
