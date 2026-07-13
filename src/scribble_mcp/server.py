"""MCP server for a markdown wiki vault — scribble, read, search, and manage knowledge notes.

Tools:
- vault_list      — list notes (filter by type or path prefix)
- vault_read      — read a note by file name (supports glob)
- vault_search    — full-text grep across the vault
- vault_create    — create a new note with YAML frontmatter
- vault_append    — append content to an existing note, bumping updated date
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool
from mcp.server.stdio import stdio_server

_LOG = logging.getLogger("vault_mcp")

# ── Vault location ────────────────────────────────────────────────
VAULT_ROOT = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/vault/wiki")))
assert VAULT_ROOT.is_dir(), f"VAULT_ROOT {VAULT_ROOT} is not a directory"

# Valid top-level sub-directories
TYPE_DIRS = {
    "concept": "concepts",
    "entity": "entities",
    "comparison": "comparisons",
    "query": "queries",
}
ALL_TYPE_DIRS = set(TYPE_DIRS.values()) | {"raw"}

# ── Helpers ───────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _slugify(title: str) -> str:
    """Turn a title into a lowercase-hyphenated file name."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s\-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def _frontmatter(title: str, note_type: str, tags: list[str], sources: list[str]) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"created: {_today()}\n"
        f"updated: {_today()}\n"
        f"type: {note_type}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"sources: [{', '.join(sources)}]\n"
        "---\n"
    )


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _update_frontmatter_date(content: str, field: str, value: str) -> str:
    """Update a frontmatter date field (created/updated) in-place."""
    pattern = re.compile(rf"^({field}:)\s*\S.*$", re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(rf"\1 {value}", content)
    return content


# ── Tool implementations ──────────────────────────────────────────

async def _vault_list(note_type: str | None) -> list[dict]:
    """List notes in the vault."""
    if note_type and note_type in TYPE_DIRS:
        target = VAULT_ROOT / TYPE_DIRS[note_type]
    elif note_type and note_type == "raw":
        target = VAULT_ROOT / "raw"
    elif note_type:
        return [{"error": f"Unknown type '{note_type}'. Valid: concept, entity, comparison, query, raw"}]
    else:
        target = VAULT_ROOT

    notes = []
    if target.is_dir():
        for f in sorted(target.rglob("*.md")):
            # Skip hidden files (._filename.md) and dotfiles
            if f.name.startswith("."):
                continue
            rel = f.relative_to(VAULT_ROOT)
            notes.append({
                "path": str(rel),
                "name": f.stem,
                "type": rel.parts[0] if len(rel.parts) > 1 else "root",
            })
    return notes


async def _vault_read(path_glob: str) -> list[dict]:
    """Read note(s) matching a path glob."""
    matches = list(VAULT_ROOT.glob(path_glob))
    if not matches:
        return [{"error": f"No notes matching '{path_glob}'"}]

    results = []
    for f in sorted(matches):
        if f.name.startswith("."):
            continue
        rel = f.relative_to(VAULT_ROOT)
        content = _read_file(f)
        results.append({"path": str(rel), "content": content})
    return results


async def _vault_search(query: str, max_results: int = 20) -> list[dict]:
    """Full-text grep across the vault."""
    results = []
    for f in VAULT_ROOT.rglob("*.md"):
        if f.name.startswith("."):
            continue
        content = _read_file(f)
        if query.lower() in content.lower():
            rel = f.relative_to(VAULT_ROOT)
            # Find the first line with a match
            snippet_lines = []
            for i, line in enumerate(content.splitlines()):
                if query.lower() in line.lower():
                    snippet_lines.append(line.strip()[:200])
                    if len(snippet_lines) >= 3:
                        break
            results.append({
                "path": str(rel),
                "match_count": content.lower().count(query.lower()),
                "snippets": snippet_lines,
            })
            if len(results) >= max_results:
                break
    return results


async def _vault_create(
    title: str,
    note_type: str,
    content: str,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict:
    """Create a new note in the vault."""
    note_type = note_type.lower()
    if note_type not in TYPE_DIRS:
        return {"error": f"Invalid type '{note_type}'. Valid: concept, entity, comparison, query"}

    subdir = TYPE_DIRS[note_type]
    slug = _slugify(title)
    today = _today()
    filename = f"{slug}.md"
    # Year subdir for concepts and raw; entities/comparisons/queries stay flat
    if note_type in ("concept", "query"):
        year = today.split("-")[0]
        filepath = VAULT_ROOT / subdir / year / filename
    else:
        filepath = VAULT_ROOT / subdir / filename

    if filepath.exists():
        return {"error": f"Note already exists: {filepath.relative_to(VAULT_ROOT)}"}

    fm = _frontmatter(title, note_type, tags or [], sources or [])
    full_content = fm + "\n" + content.strip() + "\n"

    filepath.parent.mkdir(parents=True, exist_ok=True)
    _write_file(filepath, full_content)

    return {
        "path": str(filepath.relative_to(VAULT_ROOT)),
        "status": "created",
    }


async def _vault_append(path_glob: str, content: str, section: str | None = None) -> dict:
    """Append content to an existing note, bumping the updated date."""
    matches = list(VAULT_ROOT.glob(path_glob))
    if not matches:
        return {"error": f"No notes matching '{path_glob}'"}
    if len(matches) > 1:
        paths = [str(m.relative_to(VAULT_ROOT)) for m in sorted(matches)]
        return {"error": f"Pattern matches multiple notes", "matches": paths}

    filepath = matches[0]
    original = _read_file(filepath)

    # Split frontmatter and body (frontmatter is between --- ... ---)
    parts = original.split("---", 2)
    if len(parts) < 3:
        return {"error": "Note has no valid frontmatter"}

    frontmatter_raw = parts[1]
    body = parts[2].strip()

    # Bump updated date in frontmatter
    new_fm = re.sub(
        r"^updated:\s*\S.*$",
        f"updated: {_today()}",
        frontmatter_raw,
        count=1,
        flags=re.MULTILINE,
    )

    # Append
    new_section = f"\n\n## {section}\n\n{content.strip()}" if section else f"\n\n{content.strip()}"
    new_body = body + new_section

    new_content = f"---{new_fm}---\n{new_body}\n"
    _write_file(filepath, new_content)

    return {
        "path": str(filepath.relative_to(VAULT_ROOT)),
        "status": "updated",
    }


async def _vault_delete(path_glob: str) -> dict:
    """Delete a note by path glob. Must match exactly one file."""
    matches = list(VAULT_ROOT.glob(path_glob))
    if not matches:
        return {"error": f"No notes matching '{path_glob}'"}
    if len(matches) > 1:
        paths = [str(m.relative_to(VAULT_ROOT)) for m in sorted(matches)]
        return {"error": f"Pattern matches multiple files", "matches": paths}

    filepath = matches[0]
    rel = str(filepath.relative_to(VAULT_ROOT))
    filepath.unlink()
    return {"path": rel, "status": "deleted"}


# ── MCP server wiring ─────────────────────────────────────────────

def _build_server() -> Server:
    server = Server("vault-mcp")

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name="vault_list",
                description="List notes in the vault. Optionally filter by type (concept, entity, comparison, query, raw).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "note_type": {
                            "type": "string",
                            "description": "Filter by type: concept, entity, comparison, query, raw",
                        }
                    },
                },
            ),
            Tool(
                name="vault_read",
                description="Read one or more notes by path glob (e.g. 'concepts/*marathon*').",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path_glob": {
                            "type": "string",
                            "description": "Glob pattern relative to vault root, e.g. 'concepts/*.md' or 'entities/2026-04-10-singapore.md'",
                        }
                    },
                    "required": ["path_glob"],
                },
            ),
            Tool(
                name="vault_search",
                description="Full-text search across all markdown files in the vault. Returns matching file paths and content snippets.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (case-insensitive)",
                        },
                        "max_results": {
                            "type": "number",
                            "description": "Maximum number of matching files to return (default 20)",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="vault_create",
                description="Create a new note in the vault with proper YAML frontmatter, date-prefixed filename, and correct directory placement.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Note title (used to generate the slug/filename)",
                        },
                        "note_type": {
                            "type": "string",
                            "description": "Type of note: concept, entity, comparison, query",
                        },
                        "content": {
                            "type": "string",
                            "description": "Markdown body content",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags from the vault taxonomy",
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Source file names (relative to raw/ dir)",
                        },
                    },
                    "required": ["title", "note_type", "content"],
                },
            ),
            Tool(
                name="vault_append",
                description="Append content to an existing note, bumping the updated date in frontmatter.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path_glob": {
                            "type": "string",
                            "description": "Glob pattern to match the target note (must match exactly one file)",
                        },
                        "content": {
                            "type": "string",
                            "description": "Markdown content to append",
                        },
                        "section": {
                            "type": "string",
                            "description": "Optional section heading (creates a new ## section)",
                        },
                    },
                    "required": ["path_glob", "content"],
                },
            ),
            Tool(
                name="vault_delete",
                description="Delete a note by path glob. Must match exactly one file.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path_glob": {
                            "type": "string",
                            "description": "Glob pattern to match the target note (must match exactly one file)",
                        },
                    },
                    "required": ["path_glob"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            if name == "vault_list":
                result = await _vault_list(arguments.get("note_type"))
            elif name == "vault_read":
                result = await _vault_read(arguments["path_glob"])
            elif name == "vault_search":
                result = await _vault_search(
                    arguments["query"],
                    arguments.get("max_results", 20),
                )
            elif name == "vault_create":
                result = await _vault_create(
                    arguments["title"],
                    arguments["note_type"],
                    arguments["content"],
                    arguments.get("tags"),
                    arguments.get("sources"),
                )
            elif name == "vault_append":
                result = await _vault_append(
                    arguments["path_glob"],
                    arguments["content"],
                    arguments.get("section"),
                )
            elif name == "vault_delete":
                result = await _vault_delete(arguments["path_glob"])
            else:
                result = {"error": f"Unknown tool: {name}"}

            import json
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        except Exception as e:
            _LOG.exception("Error handling tool %s", name)
            return [TextContent(type="text", text=f"Error: {e}")]

    return server


async def run_stdio() -> None:
    """Run the Vault MCP server over stdio."""
    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )