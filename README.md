# Scribble MCP Vault API

The repository name is historical. The implementation is current: a small, dependency-free HTTP API used by Claude's n8n MCP vault workflows.

## Production architecture

```text
Claude MCP
  → n8n on tech-vm
  → HTTP over private Tailscale
  → vault-api.service on agentic-vm
  → /home/ck/icloud-linux-mount/Obsidian/Ck's Vault
  → iCloud
```

The Mac mini and SSH are not part of the vault path.

## Production service

The user service runs `vault_api.py` from this repository:

```text
/home/ck/repo/scribble-mcp/vault_api.py
/home/ck/.config/systemd/user/vault-api.service
```

It binds to `100.78.128.119:8765` in production. The API checks that both the mount and `icloud.service` are active before every vault operation. If either check fails, it returns an error and does not write.

Start or inspect it on `agentic-vm`:

```bash
systemctl --user daemon-reload
systemctl --user enable --now vault-api.service
systemctl --user status vault-api.service
curl http://100.78.128.119:8765/health
```

For a local development process, the defaults bind to loopback:

```bash
python3 vault_api.py
```

To bind explicitly:

```bash
python3 vault_api.py 100.78.128.119 8765
```

## API contract

All paths are relative to the vault root. Path traversal and absolute paths are rejected.

| Method | Endpoint | Request | Purpose |
|---|---|---|---|
| GET | `/health` | none | Check mount and service health |
| GET | `/vault/read?path=wiki/SCHEMA.md` | none | Read a file |
| GET | `/vault/list?path=wiki/` | none | List a directory |
| GET | `/vault/search?q=frontmatter&path=wiki/` | none | Case-sensitive regex search |
| POST | `/vault/write` | `{path, content}` | Write or overwrite a file |
| POST | `/vault/append` | `{path, content}` | Append to a file |
| POST | `/vault/delete` | `{path}` | Delete a file |
| POST | `/vault/move` | `{from, to}` | Move or rename a file |

The n8n MCP workflow parameter names are part of the contract:

- Search uses `q`
- Move uses `from` and `to`
- Write and append use `path` and `content`
- Delete uses `path`

## Write safety

- The mount and `icloud.service` are checked before every operation.
- Writes use direct file operations through the FUSE mount.
- Existing files are not replaced through a generic temp-file-plus-rename workflow.
- After writes, inspect the iCloud journal for `file-sync-complete`:

```bash
journalctl --user -u icloud.service -n 80 --no-pager
```

Do not add an SSH fallback or a second vault copy.

## n8n verification

From `tech-vm`:

```bash
docker exec n8n node -e 'fetch("http://100.78.128.119:8765/health").then(async r => console.log(r.status, await r.text()))'
```

The seven live workflows are:

- `Vault Read - MCP`
- `Vault Write - MCP`
- `Vault Append - MCP`
- `Vault Delete - MCP`
- `Vault Move - MCP`
- `Vault List - MCP`
- `Vault Search - MCP`

For an end-to-end check, use a temporary file under `wiki/`, exercise write, read, append, move, and delete, then confirm the file is gone. Also exercise list and search. Never leave test files in the vault.
