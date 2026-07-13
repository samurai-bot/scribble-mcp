"""Lightweight HTTP server wrapping vault operations for n8n to call."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

# Reuse vault functions from the MCP server
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from scribble_mcp.server import (
    _vault_list,
    _vault_read,
    _vault_search,
    _vault_create,
    _vault_append,
    _vault_delete,
    VAULT_ROOT,
)

_LOG = logging.getLogger("vault_api")


# ── Handlers ──────────────────────────────────────────────────────

async def handle_list(request: Request) -> JSONResponse:
    note_type = request.query_params.get("type")
    try:
        notes = await _vault_list(note_type)
        return JSONResponse({"ok": True, "count": len(notes), "notes": notes})
    except Exception as e:
        _LOG.exception("list failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_read(request: Request) -> JSONResponse:
    path_glob = request.query_params.get("path", "")
    if not path_glob:
        return JSONResponse({"ok": False, "error": "Missing 'path' query param"}, status_code=400)
    try:
        results = await _vault_read(path_glob)
        return JSONResponse({"ok": True, "count": len(results), "notes": results})
    except Exception as e:
        _LOG.exception("read failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_search(request: Request) -> JSONResponse:
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"ok": False, "error": "Missing 'q' query param"}, status_code=400)
    try:
        max_results = int(request.query_params.get("max", "20"))
    except ValueError:
        max_results = 20
    try:
        results = await _vault_search(query, max_results)
        return JSONResponse({"ok": True, "count": len(results), "results": results})
    except Exception as e:
        _LOG.exception("search failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    title = body.get("title")
    note_type = body.get("type")
    content = body.get("content", "")
    tags = body.get("tags", [])
    sources = body.get("sources", [])

    if not title or not note_type:
        return JSONResponse(
            {"ok": False, "error": "Missing required fields: title, type"},
            status_code=400,
        )

    try:
        result = await _vault_create(title, note_type, content, tags, sources)
        if "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        _LOG.exception("create failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_append(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    path_glob = body.get("path")
    content = body.get("content", "")
    section = body.get("section")

    if not path_glob or not content:
        return JSONResponse(
            {"ok": False, "error": "Missing required fields: path, content"},
            status_code=400,
        )

    try:
        result = await _vault_append(path_glob, content, section)
        if "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        _LOG.exception("append failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_delete(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    path_glob = body.get("path")
    if not path_glob:
        return JSONResponse({"ok": False, "error": "Missing required field: path"}, status_code=400)

    try:
        result = await _vault_delete(path_glob)
        if "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        _LOG.exception("delete failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def handle_health(_request: Request) -> PlainTextResponse:
    """Health check — also reports vault stats."""
    try:
        notes = await _vault_list(None)
        return PlainTextResponse(
            f"OK | vault: {VAULT_ROOT} | notes: {len(notes)}"
        )
    except Exception as e:
        return PlainTextResponse(f"ERROR: {e}", status_code=500)


async def handle_legacy_post(request: Request) -> JSONResponse:
    """Handle legacy POST format used by the old n8n Vault Wiki - MCP workflow.
    Accepts: {"operation": "list|read|write|search", ...}
    """
    import traceback
    try:
        raw = await request.json()
    except Exception as e:
        _LOG.error("Invalid JSON from %s: %s", request.client, traceback.format_exc())
        return JSONResponse({"success": False, "error": f"Invalid JSON: {e}"}, status_code=400)

    _LOG.info("Legacy POST raw body: %s", json.dumps(raw)[:500])
    # n8n wraps the POST body in an array [{...}] or a {"body": ...} envelope
    if isinstance(raw, list):
        entry = raw[0] if raw else {}
        # n8n may also wrap array elements in {"body": ...}
        if isinstance(entry, dict) and "body" in entry:
            entry = entry["body"]
    elif isinstance(raw, dict):
        entry = raw.get("body", raw)
    else:
        entry = {}
    op = entry.get("operation", "")
    _LOG.info("Legacy POST operation: %s", op)

    if op == "list":
        target = entry.get("directory", "")
        if target:
            # Map directory names to note types (accept both plural and singular)
            dir_to_type = {
                "concepts": "concept", "concept": "concept",
                "entities": "entity", "entity": "entity",
                "comparisons": "comparison", "comparison": "comparison",
                "queries": "query", "query": "query",
                "raw": "raw",
            }
            note_type = dir_to_type.get(target)
            if note_type:
                notes = await _vault_list(note_type)
            else:
                return JSONResponse({"success": False, "error": f"Unknown directory '{target}'. Valid: concepts, entities, comparisons, queries, raw"})
        else:
            notes = await _vault_list(None)
        return JSONResponse({"success": True, "entries": notes, "count": len(notes)})
    elif op == "read":
        fp = entry.get("path", "")
        results = await _vault_read(fp)
        if not results or "error" in results[0]:
            return JSONResponse({"success": False, "error": results[0]["error"]}, status_code=404)
        return JSONResponse({"success": True, "content": results[0]["content"], "path": fp})
    elif op == "search":
        q = entry.get("query", "")
        results = await _vault_search(q)
        return JSONResponse({"success": True, "results": results, "count": len(results)})
    elif op == "write":
        fp = entry.get("path", "")
        content = entry.get("content", "")
        filepath = VAULT_ROOT / fp
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        return JSONResponse({"success": True, "path": fp, "write_ok": True})
    elif op == "delete":
        result = await _vault_delete(entry.get("path", ""))
        if "error" in result:
            return JSONResponse({"success": False, "error": result["error"]})
        return JSONResponse({"success": True, **result})

    return JSONResponse({"success": False, "error": f"Unknown operation: {op}"}, status_code=400)


async def handle_index(_request: Request) -> JSONResponse:
    """List available endpoints."""
    return JSONResponse({
        "service": "vault-api",
        "vault": str(VAULT_ROOT),
        "endpoints": {
            "GET  /": "This index",
            "GET  /health": "Health check",
            "GET  /list": "List notes (?type=concept|entity|comparison|query|raw)",
            "GET  /read": "Read note (?path=concepts/*marathon*)",
            "GET  /search": "Search notes (?q=query&max=20)",
            "POST /create": "Create note (JSON body)",
            "POST /append": "Append to note (JSON body)",
            "POST /delete": "Delete note (JSON body: {\"path\": \"...\"})",
        },
    })


# ── App ───────────────────────────────────────────────────────────

routes = [
    Route("/", endpoint=handle_index, methods=["GET"]),
    Route("/health", endpoint=handle_health),
    Route("/list", endpoint=handle_list),
    Route("/read", endpoint=handle_read),
    Route("/search", endpoint=handle_search),
    Route("/create", endpoint=handle_create, methods=["POST"]),
    Route("/append", endpoint=handle_append, methods=["POST"]),
    Route("/delete", endpoint=handle_delete, methods=["POST"]),
    Route("/", endpoint=handle_legacy_post, methods=["POST"]),
]

middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
]

app = Starlette(routes=routes, middleware=middleware)


# ── CLI entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import uvicorn
    port = int(os.environ.get("VAULT_API_PORT", "9003"))
    host = os.environ.get("VAULT_API_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")