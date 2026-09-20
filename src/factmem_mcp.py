#!/usr/bin/env python3
"""factmem_mcp.py — MCP stdio server wrapping the factmem.py HTTP service.

Implements the MCP JSON-RPC stdio protocol directly (no third-party deps).
Reads configuration from:
  FACTMEM_URL   — base URL of the factmem service  (e.g. http://100.x.y.z:7700)
  FACTMEM_TOKEN — bearer token for the service

Exposed tools: mem_write, mem_search, mem_get, mem_correct, mem_forget, mem_health
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_URL: str = os.environ.get("FACTMEM_URL", "http://127.0.0.1:7700").rstrip("/")
_TOKEN: str = os.environ.get("FACTMEM_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    url = _BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            payload = {"error": exc.reason}
        return exc.code, payload
    except Exception as exc:
        return 0, {"error": str(exc)}


def _get(path: str) -> tuple[int, Any]:
    return _request("GET", path)


def _post(path: str, body: dict) -> tuple[int, Any]:
    return _request("POST", path, body)


def _put(path: str, body: dict) -> tuple[int, Any]:
    return _request("PUT", path, body)


def _delete(path: str, body: dict) -> tuple[int, Any]:
    return _request("DELETE", path, body)


def _get_qs(path: str, params: dict) -> tuple[int, Any]:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    full = path + ("?" + qs if qs else "")
    return _get(full)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_mem_write(args: dict) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}

    ftype = (args.get("type") or "").strip()
    valid_types = ("entity", "preference", "decision", "operational")
    if ftype not in valid_types:
        return {"error": f"type must be one of: {', '.join(valid_types)}"}

    source = (args.get("source") or "").strip()
    if not source:
        return {"error": "source is required (format: host or host+agent)"}

    confidence = args.get("confidence")
    if confidence is None:
        return {"error": "confidence is required"}
    try:
        confidence = float(confidence)
        assert 0.0 <= confidence <= 1.0
    except (TypeError, ValueError, AssertionError):
        return {"error": "confidence must be a float between 0.0 and 1.0"}

    body: dict[str, Any] = {
        "text": text,
        "type": ftype,
        "source": source,
        "confidence": confidence,
    }

    ttl_days = args.get("ttl_days")
    if ttl_days is not None:
        try:
            body["ttl_days"] = float(ttl_days)
        except (TypeError, ValueError):
            return {"error": "ttl_days must be a positive number"}

    tags = args.get("tags")
    if tags is not None:
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        body["tags"] = tags

    status, data = _post("/facts", body)
    if status in (200, 201):
        return data
    return {"error": data.get("error", f"service returned {status}"), "status": status}


def _tool_mem_search(args: dict) -> dict:
    params: dict[str, Any] = {}
    q = (args.get("q") or "").strip()
    if q:
        params["q"] = q
    ftype = (args.get("type") or "").strip()
    if ftype:
        params["type"] = ftype
    tag = (args.get("tag") or "").strip()
    if tag:
        params["tag"] = tag
    limit = args.get("limit")
    if limit is not None:
        try:
            params["limit"] = int(limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}

    status, data = _get_qs("/facts", params)
    if status == 200:
        return {"facts": data, "count": len(data)}
    return {"error": data.get("error", f"service returned {status}"), "status": status}


def _tool_mem_get(args: dict) -> dict:
    fact_id = (args.get("id") or "").strip()
    if not fact_id:
        return {"error": "id is required"}

    status, data = _get(f"/facts/{fact_id}")
    if status == 200:
        return data
    if status == 404:
        return {"error": "Fact not found", "id": fact_id}
    return {"error": data.get("error", f"service returned {status}"), "status": status}


def _tool_mem_correct(args: dict) -> dict:
    fact_id = (args.get("id") or "").strip()
    if not fact_id:
        return {"error": "id is required"}

    new_text = (args.get("text") or "").strip()
    if not new_text:
        return {"error": "text (corrected text) is required"}

    reason = (args.get("reason") or "").strip()
    if not reason:
        return {"error": "reason is required — state why the previous fact was wrong"}

    source = (args.get("source") or "").strip()
    if not source:
        return {"error": "source is required (format: host or host+agent)"}

    status, data = _put(f"/facts/{fact_id}", {
        "text": new_text,
        "reason": reason,
        "source": source,
    })
    if status == 200:
        return data
    if status == 404:
        return {"error": "Fact not found", "id": fact_id}
    if status == 409:
        return {"error": "Fact has already been superseded; look up its replacement first", "id": fact_id}
    if status == 410:
        return {"error": "Fact has been hard-deleted; it cannot be corrected", "id": fact_id}
    return {"error": data.get("error", f"service returned {status}"), "status": status}


def _tool_mem_forget(args: dict) -> dict:
    fact_id = (args.get("id") or "").strip()
    if not fact_id:
        return {"error": "id is required"}

    reason = (args.get("reason") or "").strip()
    if not reason:
        return {"error": "reason is required — state why the fact should be permanently removed"}

    status, data = _delete(f"/facts/{fact_id}", {"reason": reason})
    if status == 200:
        return data
    if status == 404:
        return {"error": "Fact not found", "id": fact_id}
    if status == 410:
        return {"error": "Fact already deleted", "id": fact_id}
    return {"error": data.get("error", f"service returned {status}"), "status": status}


def _tool_mem_health(_args: dict) -> dict:
    status, data = _get("/health")
    if status == 200:
        return data
    return {"error": data.get("error", f"service returned {status}"), "status": status}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "mem_write",
        "description": (
            "Store a durable fact or decision in the persistent fact memory service. "
            "Use this for facts that must survive across agent sessions and be available "
            "to any agent on any host — architectural decisions, licence constraints, "
            "operator-confirmed findings, compliance rules, vendor rejections with rationale, "
            "and hard deadlines converted to absolute dates. "
            "Do NOT use for transcript chunks, session notes, ephemeral task state, or anything "
            "derivable from current code or git history. "
            "If a stored fact is wrong, call mem_correct on the existing id rather than "
            "writing a new fact — duplicate or conflicting writes pollute the store."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact or decision, written as a self-contained declarative sentence."
                },
                "type": {
                    "type": "string",
                    "enum": ["entity", "preference", "decision", "operational"],
                    "description": (
                        "entity — a named thing and its properties (e.g. a library, service, host). "
                        "preference — a standing operator or project preference. "
                        "decision — a choice made with stated rationale (use for architectural/vendor decisions). "
                        "operational — a runtime constraint, port, limit, or observed system fact."
                    )
                },
                "source": {
                    "type": "string",
                    "description": "Origin of this fact. Format: 'host' or 'host+agent'. E.g. 'app-server+worker' or 'laptop+agent'."
                },
                "confidence": {
                    "type": "number",
                    "description": "How certain the source is, from 0.0 (guess) to 1.0 (verified). Use 0.9+ only for operator-confirmed facts.",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "ttl_days": {
                    "type": "number",
                    "description": "Optional. Days until this fact expires automatically. Omit for permanent facts. Use for operational state that becomes stale (e.g. a measured RAM figure)."
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tags for grouping (e.g. ['licence', 'app-server', 'memory-design'])."
                }
            },
            "required": ["text", "type", "source", "confidence"]
        }
    },
    {
        "name": "mem_search",
        "description": (
            "Search the persistent fact memory store for durable facts and decisions. "
            "Call this before starting any task that involves choosing a library, tool, host, "
            "or architecture — the store may contain constraints or decisions that narrow the "
            "solution space. Also call it before writing a new fact to avoid duplicates. "
            "Returns only live (non-deleted, non-superseded, non-expired) facts by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Full-text search query. Searches the fact text. Leave blank to list recent facts."
                },
                "type": {
                    "type": "string",
                    "enum": ["entity", "preference", "decision", "operational"],
                    "description": "Filter to a single fact type. Omit to search all types."
                },
                "tag": {
                    "type": "string",
                    "description": "Filter to facts with this tag. Omit to search all tags."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of facts to return (1–1000). Default 100.",
                    "minimum": 1,
                    "maximum": 1000
                }
            },
            "required": []
        }
    },
    {
        "name": "mem_get",
        "description": (
            "Retrieve a single fact by its id, including its full audit chain: the complete "
            "history of corrections from the original write through every superseding version. "
            "Use when you have an id from mem_search or mem_write and need the full lineage, "
            "or when you want to verify whether a fact has been corrected or superseded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "UUID of the fact to retrieve."
                },
                "with_audit_chain": {
                    "type": "boolean",
                    "description": "Always true — the audit chain is always returned. This parameter is accepted for clarity but has no effect."
                }
            },
            "required": ["id"]
        }
    },
    {
        "name": "mem_correct",
        "description": (
            "Correct a wrong or outdated fact by superseding it with a new version. "
            "The old fact is marked superseded but preserved in the audit chain — the history "
            "is never destroyed. Use this instead of mem_write when a fact already exists and "
            "is incorrect: writing a duplicate creates ambiguity; superseding preserves clarity. "
            "Requires the id of the fact being corrected, the corrected text, the reason the "
            "old fact was wrong, and the source performing the correction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "UUID of the fact to supersede."
                },
                "text": {
                    "type": "string",
                    "description": "The corrected fact text, written as a complete self-contained sentence."
                },
                "reason": {
                    "type": "string",
                    "description": "Why the previous fact was wrong or outdated. This is stored in the audit chain."
                },
                "source": {
                    "type": "string",
                    "description": "Who is making the correction. Format: 'host' or 'host+agent'."
                }
            },
            "required": ["id", "text", "reason", "source"]
        }
    },
    {
        "name": "mem_forget",
        "description": (
            "Erase a fact's text from the memory store, leaving only an id/timestamp tombstone. "
            "The text is removed from both the facts table and the full-text search index and "
            "will not appear in any future backup. "
            "Reserve for PII, clear fabrications with no audit value, or facts whose existence "
            "itself is harmful. For a fact that is merely wrong or outdated, use mem_correct "
            "instead — it preserves the audit trail while removing the fact from active results. "
            "Erasure cannot be undone. Requires a stated reason."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "UUID of the fact to delete."
                },
                "reason": {
                    "type": "string",
                    "description": "Why this fact must be erased rather than corrected. Required."
                }
            },
            "required": ["id", "reason"]
        }
    },
    {
        "name": "mem_health",
        "description": (
            "Check whether the fact memory service is reachable and healthy. "
            "Returns service status, uptime, schema version, and counts of total vs live facts. "
            "Call this to verify the service is up before a batch write, or to diagnose "
            "connection failures. No authentication parameters needed — the health endpoint "
            "is unauthenticated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
]

_TOOL_MAP = {t["name"]: t for t in _TOOLS}

_TOOL_FNS = {
    "mem_write": _tool_mem_write,
    "mem_search": _tool_mem_search,
    "mem_get": _tool_mem_get,
    "mem_correct": _tool_mem_correct,
    "mem_forget": _tool_mem_forget,
    "mem_health": _tool_mem_health,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC protocol
# ---------------------------------------------------------------------------

def _rpc_ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle(msg: dict) -> dict | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications (no id) are silently ignored per spec
    if req_id is None and method != "initialize":
        return None

    if method == "initialize":
        return _rpc_ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "factmem-mcp", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _rpc_ok(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        fn = _TOOL_FNS.get(name)
        if fn is None:
            return _rpc_err(req_id, -32601, f"Unknown tool: {name!r}")
        tool_args = params.get("arguments") or {}
        try:
            result = fn(tool_args)
        except Exception as exc:
            result = {"error": f"Internal error: {exc}"}
        # MCP tool result: list of content items
        text = json.dumps(result, indent=2, default=str)
        return _rpc_ok(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": "error" in result and isinstance(result, dict),
        })

    if method == "ping":
        return _rpc_ok(req_id, {})

    return _rpc_err(req_id, -32601, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# stdio loop
# ---------------------------------------------------------------------------

def _write(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def main() -> None:
    # Validate config on startup; warn to stderr, not stdout (stdout is the protocol channel)
    if not _TOKEN:
        print(
            "WARNING: FACTMEM_TOKEN not set — requests to the service will be unauthenticated",
            file=sys.stderr,
        )
    print(f"factmem_mcp: connecting to {_BASE_URL}", file=sys.stderr)

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _write(_rpc_err(None, -32700, f"Parse error: {exc}"))
            continue
        try:
            response = _handle(msg)
        except Exception as exc:
            _write(_rpc_err(msg.get("id"), -32603, f"Internal error: {exc}"))
            continue
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
