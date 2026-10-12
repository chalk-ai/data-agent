"""A small, read-only stand-in for the Notion or Linear API, served from fixtures.

One file serves either app; WORKSPACE_KIND picks which. Every call must carry
`Authorization: Bearer $WORKSPACE_TOKEN`, every call is logged as one JSON line (the audit
trail), and nothing can be written.

Notion-shaped (WORKSPACE_KIND=notion, fixtures in notion.json):
    POST /v1/search                    {"query": "..."} -> pages and databases, best match first
    GET  /v1/pages/{id}                page title and content as markdown
    POST /v1/databases/{id}/query      all rows of a database

Linear-shaped (WORKSPACE_KIND=linear, fixtures in linear.json):
    GET  /v1/issues?query=...          issues, best match first
    GET  /v1/issues/{identifier}       one issue with its description and comments
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

KIND = os.environ.get("WORKSPACE_KIND", "notion")
TOKEN = os.environ.get("WORKSPACE_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
FIXTURES = json.loads((Path(__file__).parent / f"{KIND}.json").read_text())

logging.basicConfig(level=logging.INFO, format="%(message)s")
audit = logging.getLogger("audit")

app = FastAPI(title=f"fake-{KIND}")


def authorize(request: Request) -> None:
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    ok = bool(TOKEN) and hmac.compare_digest(supplied, TOKEN)
    audit.info(
        json.dumps(
            {
                "ts": round(time.time(), 3),
                "app": KIND,
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "authorized": ok,
            }
        )
    )
    if not ok:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def score(query: str, *texts: str) -> int:
    """Keyword overlap, with title matches weighted up. Good enough for a dozen documents."""
    words = [w for w in re.findall(r"[a-z0-9\-]+", query.lower()) if len(w) > 2]
    title, *rest = texts
    body = " ".join(rest).lower()
    return sum(3 * title.lower().count(w) + body.count(w) for w in words)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "app": KIND}


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


if KIND == "notion":
    PAGES = {p["id"]: p for p in FIXTURES["pages"]}
    DATABASES = {d["id"]: d for d in FIXTURES["databases"]}

    @app.post("/v1/search", dependencies=[Depends(authorize)])
    async def notion_search(req: SearchRequest) -> JSONResponse:
        hits = []
        for p in PAGES.values():
            hits.append((score(req.query, p["title"], p["markdown"]), "page", p))
        for d in DATABASES.values():
            text = d["description"] + " " + " ".join(json.dumps(r) for r in d["rows"])
            hits.append((score(req.query, d["title"], text), "database", d))
        hits.sort(key=lambda h: -h[0])
        results = [
            {"object": kind, "id": o["id"], "title": o["title"], "last_edited_time": o["last_edited_time"]}
            for s, kind, o in hits
            if s > 0
        ]
        return JSONResponse({"results": results[:8]})

    @app.get("/v1/pages/{page_id}", dependencies=[Depends(authorize)])
    async def notion_page(page_id: str) -> JSONResponse:
        page = PAGES.get(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail=f"no page {page_id}")
        return JSONResponse(page)

    @app.post("/v1/databases/{database_id}/query", dependencies=[Depends(authorize)])
    async def notion_database(database_id: str) -> JSONResponse:
        db = DATABASES.get(database_id)
        if db is None:
            raise HTTPException(status_code=404, detail=f"no database {database_id}")
        return JSONResponse(
            {"id": db["id"], "title": db["title"], "description": db["description"], "results": db["rows"]}
        )

else:
    ISSUES = {i["identifier"]: i for i in FIXTURES["issues"]}

    @app.get("/v1/issues", dependencies=[Depends(authorize)])
    async def linear_search(query: str = "") -> JSONResponse:
        ranked = sorted(
            ISSUES.values(),
            key=lambda i: -score(query, i["title"], i["description"], *(c["body"] for c in i["comments"])),
        )
        summary = [
            {k: i[k] for k in ("identifier", "title", "team", "state", "updatedAt")}
            for i in ranked
            if not query or score(query, i["title"], i["description"], *(c["body"] for c in i["comments"])) > 0
        ]
        return JSONResponse({"issues": summary[:8]})

    @app.get("/v1/issues/{identifier}", dependencies=[Depends(authorize)])
    async def linear_issue(identifier: str) -> JSONResponse:
        issue = ISSUES.get(identifier.upper())
        if issue is None:
            raise HTTPException(status_code=404, detail=f"no issue {identifier}")
        return JSONResponse(issue)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)
