#!/usr/bin/env python3
"""Minimal direct HTTP API for the Ubuntu-mounted iCloud Obsidian vault."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

VAULT = Path("/home/ck/icloud-linux-mount/Obsidian/Ck's Vault")
MOUNT = Path("/home/ck/icloud-linux-mount")
SERVICE = "icloud.service"
MAX_BODY = 20 * 1024 * 1024


def service_ready() -> tuple[bool, str]:
    if not MOUNT.is_mount():
        return False, "iCloud mount is unavailable"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", SERVICE],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return False, "icloud.service is not active"
    return True, "ok"


def safe_path(value: str, *, allow_empty: bool = False) -> Path:
    value = unquote(value or "")
    if not value and allow_empty:
        return VAULT
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("relative vault path required")
    candidate = (VAULT / value).resolve()
    try:
        candidate.relative_to(VAULT.resolve())
    except ValueError as exc:
        raise ValueError("path traversal rejected") from exc
    return candidate


def relative(path: Path) -> str:
    return path.relative_to(VAULT.resolve()).as_posix()


def json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length > MAX_BODY:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length) if length else b"{}"
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def write_text(path: Path, content: str, *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultAPI/1.0"

    def reply(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def error(self, code: int, message: str) -> None:
        self.reply(code, message + "\n", "text/plain; charset=utf-8")

    def require_ready(self) -> None:
        ready, reason = service_ready()
        if not ready:
            raise RuntimeError(reason)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/health":
                ready, reason = service_ready()
                self.reply(200 if ready else 503, reason + "\n")
                return
            self.require_ready()
            if parsed.path == "/vault/read":
                path = safe_path(query.get("path", [""])[0])
                self.reply(200, path.read_text(encoding="utf-8", errors="replace"))
            elif parsed.path == "/vault/list":
                path = safe_path(query.get("path", [""])[0], allow_empty=True)
                if not path.is_dir():
                    raise ValueError("directory required")
                entries = []
                for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                    suffix = "/" if child.is_dir() else ""
                    entries.append(relative(child) + suffix)
                self.reply(200, "\n".join(entries) + ("\n" if entries else ""))
            elif parsed.path == "/vault/search":
                needle = query.get("q", [""])[0]
                if not needle:
                    raise ValueError("search query required")
                base = safe_path(query.get("path", [""])[0], allow_empty=True)
                pattern = re.compile(needle)
                roots = [base] if base.is_file() else base.rglob("*")
                matches = []
                for candidate in roots:
                    if not candidate.is_file() or candidate.name.startswith("."):
                        continue
                    try:
                        text = candidate.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if pattern.search(text):
                        matches.append(relative(candidate))
                self.reply(200, "\n".join(sorted(matches)) + ("\n" if matches else ""))
            else:
                self.error(404, "not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error(400, str(exc))
        except (FileNotFoundError, NotADirectoryError) as exc:
            self.error(404, str(exc))
        except PermissionError as exc:
            self.error(403, str(exc))
        except RuntimeError as exc:
            self.error(503, str(exc))
        except Exception as exc:
            self.error(500, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.require_ready()
            data = json_body(self)
            if parsed.path == "/vault/write":
                path = safe_path(str(data["path"]))
                write_text(path, str(data["content"]))
                self.reply(200, "ok\n")
            elif parsed.path == "/vault/append":
                path = safe_path(str(data["path"]))
                write_text(path, str(data["content"]), append=True)
                self.reply(200, "ok\n")
            elif parsed.path == "/vault/delete":
                path = safe_path(str(data["path"]))
                path.unlink()
                self.reply(200, "ok\n")
            elif parsed.path == "/vault/move":
                source = safe_path(str(data["from"]))
                destination = safe_path(str(data["to"]))
                if destination.exists():
                    raise FileExistsError("destination already exists")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                self.reply(200, "ok\n")
            else:
                self.error(404, "not found")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self.error(400, str(exc))
        except (FileNotFoundError, NotADirectoryError) as exc:
            self.error(404, str(exc))
        except FileExistsError as exc:
            self.error(409, str(exc))
        except PermissionError as exc:
            self.error(403, str(exc))
        except RuntimeError as exc:
            self.error(503, str(exc))
        except Exception as exc:
            self.error(500, str(exc))

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("vault-api: " + (fmt % args) + "\n")


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VAULT_API_HOST", "127.0.0.1")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("VAULT_API_PORT", "8765"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
