#!/usr/bin/env python3
"""factmem.py — persistent fact memory service (stdlib + sqlite3 only).
Env: FACTMEM_TOKEN, FACTMEM_BIND, FACTMEM_PORT (7700), FACTMEM_DB (factmem.db)."""

import argparse, ipaddress, json, os, socket, sqlite3, sys, time, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCHEMA_VERSION = 1
_START = time.monotonic()

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS facts (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    type          TEXT NOT NULL
                  CHECK(type IN ('entity','preference','decision','operational')),
    source_host   TEXT NOT NULL,
    source_agent  TEXT NOT NULL,
    confidence    REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    created_at    TEXT NOT NULL,
    expires_at    TEXT,
    superseded_by TEXT,
    superseded_at TEXT,
    deleted_at    TEXT,
    delete_reason TEXT,
    tags          TEXT NOT NULL DEFAULT '[]'
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, content=facts, content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL, applied_at TEXT NOT NULL
);
"""

# ------------------------------------------------------------------ DB helpers

def _open(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    return c

def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    with conn:
        if not conn.execute("SELECT 1 FROM schema_meta LIMIT 1").fetchone():
            conn.execute("INSERT INTO schema_meta VALUES (?,?)", (SCHEMA_VERSION, _now()))

def _now() -> str: return datetime.now(timezone.utc).isoformat()
def _uid() -> str: return str(uuid.uuid4())

def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d

def _expired(d: dict) -> bool:
    exp = d.get("expires_at")
    if not exp: return False
    try: return datetime.fromisoformat(exp) < datetime.now(timezone.utc)
    except ValueError: return False

def _superseded(d: dict) -> bool: return bool(d.get("superseded_by"))
def _deleted(d: dict) -> bool: return bool(d.get("deleted_at"))

def _live(d: dict, inc_sup=False, inc_exp=False) -> bool:
    return not (_deleted(d) or (not inc_sup and _superseded(d)) or (not inc_exp and _expired(d)))

# ----------------------------------------------------------------- Bind/network

_TS_NET = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")

def _find_tailscale_ip() -> str | None:
    # Try ioctl on each interface (Linux); fall back to hostname lookup
    try:
        import fcntl, struct
        SIOCGIFADDR = 0x8915
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, name in socket.if_nameindex():
                try:
                    req = struct.pack("16sH14s", name.encode()[:15], socket.AF_INET, b"\x00"*14)
                    ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                    if ipaddress.ip_address(ip) in _TS_NET:
                        return ip
                except (OSError, struct.error):
                    continue
    except (ImportError, OSError):
        pass
    try:
        for _, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = addr[0]
            if ipaddress.ip_address(ip) in _TS_NET:
                return ip
    except OSError:
        pass
    return None

def _resolve_bind() -> str:
    addr = os.environ.get("FACTMEM_BIND", "").strip()
    if addr: return addr
    ts = _find_tailscale_ip()
    return ts if ts else "127.0.0.1"

def _check_bind(addr: str) -> None:
    if addr in ("0.0.0.0", "::"):
        sys.exit(f"FATAL: refusing to bind on {addr!r} — public interface is forbidden")
    try:
        resolved = socket.gethostbyname(addr)
    except socket.gaierror as exc:
        sys.exit(f"FATAL: cannot resolve bind address {addr!r}: {exc}")
    try:
        ip = ipaddress.ip_address(resolved)
    except ValueError as exc:
        sys.exit(f"FATAL: cannot parse resolved address {resolved!r}: {exc}")
    if not (ip in _TS_NET or ip in _LOOPBACK):
        sys.exit(
            f"FATAL: {addr!r} resolves to {resolved!r} — only loopback (127.0.0.0/8) "
            f"or tailnet (100.64.0.0/10) addresses are permitted"
        )

# ---------------------------------------------------------------- HTTP handler

_MAX_BODY = 1 * 1024 * 1024  # 1 MiB hard cap on request bodies
_BODY_ERR = object()          # sentinel: _body already sent the error response


class _H(BaseHTTPRequestHandler):
    server: "MemServer"

    def log_message(self, *_):  # suppress default access-log noise
        pass

    def _db(self) -> sqlite3.Connection: return _open(self.server.db_path)

    def _ok(self) -> bool:
        tok = self.server.token
        if not tok:
            return True
        h = self.headers.get("Authorization", "").split(None, 1)
        return len(h) == 2 and h[0].lower() in ("bearer", "token") and h[1] == tok

    def _send(self, status: int, data) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, s: int, msg: str) -> None: self._send(s, {"error": msg})

    def _body(self) -> dict | None:
        raw_cl = self.headers.get("Content-Length", "0")
        try:
            n = int(raw_cl)
        except (ValueError, TypeError):
            self._err(400, "Invalid Content-Length")
            return _BODY_ERR
        if n < 0:
            self._err(400, "Invalid Content-Length")
            return _BODY_ERR
        if n > _MAX_BODY:
            self._err(413, "Request body too large")
            return _BODY_ERR
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # -------------------------------------------------------------- routing

    def do_GET(self):
        p = urlparse(self.path)
        path, qs = p.path.rstrip("/"), parse_qs(p.query)
        if path == "/health":
            return self._health()
        if not self._ok():
            return self._err(401, "Unauthorized")
        if path == "/facts":
            self._list(qs)
        elif path.startswith("/facts/"):
            self._get(path[7:])
        elif path == "/export":
            self._export()
        else:
            self._err(404, "Not found")

    def do_POST(self):
        if not self._ok():
            return self._err(401, "Unauthorized")
        if urlparse(self.path).path.rstrip("/") == "/facts":
            self._post()
        else:
            self._err(404, "Not found")

    def do_PUT(self):
        if not self._ok():
            return self._err(401, "Unauthorized")
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/facts/"):
            self._put(path[7:])
        else:
            self._err(404, "Not found")

    def do_DELETE(self):
        if not self._ok():
            return self._err(401, "Unauthorized")
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/facts/"):
            self._delete(path[7:])
        else:
            self._err(404, "Not found")

    # -------------------------------------------------------------- handlers

    def _health(self):
        db = self._db()
        try:
            now = _now()
            total = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            live = db.execute(
                "SELECT COUNT(*) FROM facts WHERE deleted_at IS NULL "
                "AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at>?)", (now,)
            ).fetchone()[0]
            ver = db.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1").fetchone()
            self._send(200, {
                "status": "ok",
                "uptime_seconds": round(time.monotonic() - _START, 1),
                "schema_version": ver[0] if ver else SCHEMA_VERSION,
                "facts_total": total, "facts_live": live,
                "db_bytes": os.path.getsize(self.server.db_path),
            })
        finally:
            db.close()

    def _list(self, qs: dict):
        q = (qs.get("q") or [None])[0]
        ftype = (qs.get("type") or [None])[0]
        tag = (qs.get("tag") or [None])[0]
        try:
            limit = max(1, min(int((qs.get("limit") or ["100"])[0]), 1000))
        except ValueError:
            limit = 100
        inc_sup = (qs.get("include_superseded") or ["false"])[0].lower() == "true"
        inc_exp = (qs.get("include_expired") or ["false"])[0].lower() == "true"

        db = self._db()
        try:
            pool = limit * 5
            if q:
                fts_q = '"' + q.replace('"', '""') + '"'  # phrase-quote for FTS5 safety
                try:
                    rows = db.execute(
                        "SELECT f.* FROM facts f JOIN facts_fts ON facts_fts.rowid=f.rowid "
                        "WHERE facts_fts MATCH ? ORDER BY f.created_at DESC LIMIT ?",
                        (fts_q, pool),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = db.execute(
                        "SELECT * FROM facts WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                        (f"%{q}%", pool),
                    ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (pool,)
                ).fetchall()

            out = []
            for r in rows:
                d = _row(r)
                if not _live(d, inc_sup, inc_exp):
                    continue
                if ftype and d["type"] != ftype:
                    continue
                if tag and tag not in d["tags"]:
                    continue
                out.append(d)
                if len(out) >= limit:
                    break
            self._send(200, out)
        finally:
            db.close()

    def _get(self, fact_id: str):
        db = self._db()
        try:
            r = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if r is None:
                return self._err(404, "Fact not found")
            fact = _row(r)

            # Assemble full lineage oldest→newest.
            # superseded_by on OLD fact points to NEW fact; walk backwards then forwards.
            chain: list[dict] = [fact]
            head = fact_id
            while True:  # walk backward to root
                prev = db.execute("SELECT * FROM facts WHERE superseded_by=?", (head,)).fetchone()
                if prev is None or len(chain) > 200:
                    break
                d = _row(prev)
                chain.insert(0, d)
                head = d["id"]
            tail = fact
            while tail.get("superseded_by") and len(chain) < 200:  # walk forward
                nxt = db.execute("SELECT * FROM facts WHERE id=?", (tail["superseded_by"],)).fetchone()
                if nxt is None:
                    break
                tail = _row(nxt)
                if tail["id"] not in {c["id"] for c in chain}:
                    chain.append(tail)

            self._send(200, {"fact": fact, "audit_chain": chain})
        finally:
            db.close()

    def _post(self):
        body = self._body()
        if body is _BODY_ERR:
            return
        if body is None:
            return self._err(400, "Invalid JSON")

        text = (body.get("text") or "").strip()
        ftype = (body.get("type") or "").strip()
        source = (body.get("source") or "").strip()
        confidence = body.get("confidence")
        ttl_days = body.get("ttl_days")
        tags = body.get("tags", [])

        if not text:
            return self._err(400, "text is required")
        if ftype not in ("entity", "preference", "decision", "operational"):
            return self._err(400, "type must be: entity|preference|decision|operational")
        if not source:
            return self._err(400, "source is required")
        if confidence is None:
            return self._err(400, "confidence is required")
        try:
            confidence = float(confidence)
            assert 0.0 <= confidence <= 1.0
        except (TypeError, ValueError, AssertionError):
            return self._err(400, "confidence must be a float 0.0–1.0")

        parts = source.split("+", 1)
        src_host, src_agent = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "unknown")

        expires_at = None
        if ttl_days is not None:
            try:
                days = float(ttl_days)
                assert days > 0
                expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            except (TypeError, ValueError, AssertionError):
                return self._err(400, "ttl_days must be a positive number")

        if not isinstance(tags, list):
            return self._err(400, "tags must be a list")

        db = self._db()
        try:
            with db:
                dup = db.execute(
                    "SELECT id FROM facts WHERE text=? AND type=? AND deleted_at IS NULL",
                    (text, ftype),
                ).fetchone()
                if dup:
                    return self._send(200, {"id": dup["id"], "deduplicated": True})
                fid = _uid()
                db.execute(
                    "INSERT INTO facts "
                    "(id,text,type,source_host,source_agent,confidence,"
                    " created_at,expires_at,superseded_by,superseded_at,"
                    " deleted_at,delete_reason,tags) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
                    (fid, text, ftype, src_host, src_agent, confidence, _now(), expires_at,
                     json.dumps(tags)),
                )
            self._send(201, {"id": fid})
        finally:
            db.close()

    def _put(self, fact_id: str):
        body = self._body()
        if body is _BODY_ERR:
            return
        if body is None:
            return self._err(400, "Invalid JSON")

        new_text = (body.get("text") or "").strip()
        reason = (body.get("reason") or "").strip()
        source = (body.get("source") or "").strip()

        if not new_text:
            return self._err(400, "text is required")
        if not reason:
            return self._err(400, "reason is required")
        if not source:
            return self._err(400, "source is required")

        parts = source.split("+", 1)
        src_host, src_agent = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "unknown")

        db = self._db()
        _committed = False
        try:
            # BEGIN IMMEDIATE takes the write lock at read time so two concurrent
            # corrections cannot both see the row as un-superseded and both succeed.
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if r is None:
                return self._err(404, "Fact not found")
            old = _row(r)
            if _deleted(old):
                return self._err(410, "Fact has been deleted")
            if _superseded(old):
                return self._err(409, "Fact already superseded")

            new_id, now = _uid(), _now()
            db.execute(
                "INSERT INTO facts "
                "(id,text,type,source_host,source_agent,confidence,"
                " created_at,expires_at,superseded_by,superseded_at,"
                " deleted_at,delete_reason,tags) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
                (new_id, new_text, old["type"], src_host, src_agent,
                 old["confidence"], now, old.get("expires_at"),
                 json.dumps(old.get("tags") or [])),
            )
            db.execute(
                "UPDATE facts SET superseded_by=?, superseded_at=? WHERE id=?",
                (new_id, now, fact_id),
            )
            db.commit()
            _committed = True
            self._send(200, {"id": new_id, "supersedes": fact_id, "reason": reason})
        finally:
            if not _committed:
                try:
                    db.rollback()
                except Exception:
                    pass
            db.close()

    def _delete(self, fact_id: str):
        body = self._body()
        if body is _BODY_ERR:
            return
        if body is None:
            return self._err(400, "Invalid JSON")
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self._err(400, "reason is required")

        db = self._db()
        try:
            with db:
                r = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
                if r is None:
                    return self._err(404, "Fact not found")
                if _deleted(_row(r)):
                    return self._err(410, "Fact already deleted")
                # Erase the plaintext from both facts and facts_fts (via the facts_au trigger),
                # keeping only the tombstone columns so the text cannot be recovered from a backup.
                db.execute(
                    "UPDATE facts SET text='', deleted_at=?, delete_reason=? WHERE id=?",
                    (_now(), reason, fact_id),
                )
            self._send(200, {"id": fact_id, "deleted": True})
        finally:
            db.close()

    def _export(self):
        db = self._db()
        try:
            rows = db.execute(
                "SELECT * FROM facts WHERE deleted_at IS NULL AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at",
                (_now(),),
            ).fetchall()
            self._send(200, [_row(r) for r in rows])
        finally:
            db.close()


# --------------------------------------------------------------------- Server

class MemServer(ThreadingHTTPServer):
    def __init__(self, bind: str, port: int, db_path: str, token: str):
        self.db_path = db_path
        self.token = token
        super().__init__((bind, port), _H)

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


# ------------------------------------------------------------------- Selftest

def _selftest() -> int:
    import tempfile

    fails = 0

    def chk(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {label}" + (f"  [{detail}]" if not ok and detail else ""))
        if not ok:
            fails += 1

    with tempfile.TemporaryDirectory() as tmp:
        db = _open(os.path.join(tmp, "selftest.db"))
        _migrate(db)

        _I = ("INSERT INTO facts (id,text,type,source_host,source_agent,confidence,"
              " created_at,expires_at,superseded_by,superseded_at,"
              " deleted_at,delete_reason,tags) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,'[]')")

        fid = _uid()
        with db: db.execute(_I, (fid, "Sky is blue", "entity", "h", "a", 0.9, _now(), None))
        r = _row(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
        chk("write fact", r and r["text"] == "Sky is blue")

        dup = db.execute(
            "SELECT id FROM facts WHERE text=? AND type=? AND deleted_at IS NULL",
            ("Sky is blue", "entity"),
        ).fetchone()
        chk("dedup returns existing id", dup and dup["id"] == fid)

        new_id, now = _uid(), _now()
        with db:
            db.execute(_I, (new_id, "Sky is grey", "entity", "h", "a", 0.95, now, None))
            db.execute("UPDATE facts SET superseded_by=?, superseded_at=? WHERE id=?",
                       (new_id, now, fid))

        old = _row(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
        chk("old fact marked superseded", _superseded(old))
        chk("old fact text preserved (no mutation)", old["text"] == "Sky is blue")
        new_f = _row(db.execute("SELECT * FROM facts WHERE id=?", (new_id,)).fetchone())
        chk("new fact is live", new_f and not _superseded(new_f))

        live_ids = {_row(r)["id"] for r in db.execute(
            "SELECT * FROM facts WHERE deleted_at IS NULL AND superseded_by IS NULL").fetchall()}
        chk("superseded excluded from default live set", fid not in live_ids)
        chk("new fact in live set", new_id in live_ids)
        all_ids = {_row(r)["id"] for r in db.execute(
            "SELECT * FROM facts WHERE deleted_at IS NULL").fetchall()}
        chk("superseded visible with include_superseded", fid in all_ids)

        exp_id = _uid()
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        with db: db.execute(_I, (exp_id, "Expired", "operational", "h", "a", 0.5, _now(), past))
        exp_row = _row(db.execute("SELECT * FROM facts WHERE id=?", (exp_id,)).fetchone())
        chk("_expired() detects past expires_at", _expired(exp_row))

        base = [_row(r) for r in db.execute(
            "SELECT * FROM facts WHERE deleted_at IS NULL AND superseded_by IS NULL").fetchall()]
        chk("expired excluded from default list (filter on read)",
            not any(d["id"] == exp_id for d in base if not _expired(d)))
        chk("expired visible with include_expired", any(d["id"] == exp_id for d in base))

        # FTS5
        try:
            fts_ids = {_row(r)["id"] for r in db.execute(
                "SELECT f.* FROM facts f JOIN facts_fts ON facts_fts.rowid=f.rowid "
                "WHERE facts_fts MATCH ? LIMIT 50",
                ('"Sky is grey"',),
            ).fetchall()}
            chk("FTS5 phrase query finds new fact", new_id in fts_ids)
        except sqlite3.OperationalError as exc:
            chk("FTS5 index functional", False, str(exc))

        # ---- forget / tombstone test (fix #3) --------------------------------
        forget_id = _uid()
        with db:
            db.execute(_I, (forget_id, "PII name John Doe SSN 123-45-6789",
                            "entity", "h", "a", 0.9, _now(), None))
        try:
            fts_pre = {_row(r)["id"] for r in db.execute(
                "SELECT f.* FROM facts f JOIN facts_fts ON facts_fts.rowid=f.rowid "
                "WHERE facts_fts MATCH ? LIMIT 10", ('"John Doe"',),
            ).fetchall()}
            chk("forget pre-check: text present in FTS before delete", forget_id in fts_pre)
        except sqlite3.OperationalError as exc:
            chk("forget pre-check: FTS available", False, str(exc))

        with db:
            db.execute(
                "UPDATE facts SET text='', deleted_at=?, delete_reason=? WHERE id=?",
                (_now(), "PII removal selftest", forget_id),
            )
        row_del = _row(db.execute("SELECT * FROM facts WHERE id=?", (forget_id,)).fetchone())
        chk("forget: plaintext erased from facts", row_del["text"] == "")
        chk("forget: tombstone has deleted_at", bool(row_del.get("deleted_at")))
        try:
            fts_post = {_row(r)["id"] for r in db.execute(
                "SELECT f.* FROM facts f JOIN facts_fts ON facts_fts.rowid=f.rowid "
                "WHERE facts_fts MATCH ? LIMIT 10", ('"John Doe"',),
            ).fetchall()}
            chk("forget: plaintext gone from facts_fts", forget_id not in fts_post)
        except sqlite3.OperationalError as exc:
            chk("forget: FTS post-delete query", False, str(exc))

        db.close()

        # ---- concurrent-correction test (fix #2) -----------------------------
        import threading as _threading

        db_path_c = os.path.join(tmp, "concurrent.db")
        db_c = _open(db_path_c)
        _migrate(db_c)
        fid_c = _uid()
        with db_c:
            db_c.execute(_I, (fid_c, "Concurrent test fact", "entity", "h", "a", 0.9, _now(), None))
        db_c.close()

        _winner_ids: list = []
        _c_errors: list = []
        _c_lock = _threading.Lock()
        _c_barrier = _threading.Barrier(2)

        def _try_correct(label: str) -> None:
            conn = _open(db_path_c)
            try:
                _c_barrier.wait()
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM facts WHERE id=?", (fid_c,)).fetchone()
                if row is None:
                    conn.rollback()
                    with _c_lock: _c_errors.append(f"{label}: not found")
                    return
                d = _row(row)
                if _superseded(d) or _deleted(d):
                    conn.rollback()
                    with _c_lock: _c_errors.append(f"{label}: 409")
                    return
                time.sleep(0.02)
                nid = _uid()
                n2 = _now()
                conn.execute(_I, (nid, f"Correction {label}", "entity", "h", "a", 0.9, n2, None))
                conn.execute(
                    "UPDATE facts SET superseded_by=?, superseded_at=? WHERE id=?",
                    (nid, n2, fid_c),
                )
                conn.commit()
                with _c_lock: _winner_ids.append(nid)
            except Exception as exc:
                try: conn.rollback()
                except Exception: pass
                with _c_lock: _c_errors.append(f"{label}: {exc}")
            finally:
                conn.close()

        t1 = _threading.Thread(target=_try_correct, args=("T1",), daemon=True)
        t2 = _threading.Thread(target=_try_correct, args=("T2",), daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=12); t2.join(timeout=12)

        chk("concurrent correction: exactly one winner",
            len(_winner_ids) == 1,
            f"winners={_winner_ids} errors={_c_errors}")
        db_c2 = _open(db_path_c)
        orig_c = _row(db_c2.execute("SELECT * FROM facts WHERE id=?", (fid_c,)).fetchone())
        db_c2.close()
        chk("concurrent correction: chain intact",
            bool(_winner_ids) and orig_c is not None
            and orig_c.get("superseded_by") in set(_winner_ids))

        # ---- bind-guard test (fix #4) ----------------------------------------
        try:
            _check_bind("8.8.8.8")
            chk("bind guard: rejects routable public IP", False, "expected SystemExit")
        except SystemExit:
            chk("bind guard: rejects routable public IP", True)

        # ---- oversized-body test (fix #6) ------------------------------------
        import threading as _threading2
        import urllib.request as _urlreq
        import urllib.error as _urlerr

        db_srv_path = os.path.join(tmp, "srv.db")
        conn_srv = _open(db_srv_path)
        _migrate(conn_srv)
        conn_srv.close()

        srv = MemServer("127.0.0.1", 0, db_srv_path, "selftest-tok")
        srv_port = srv.server_address[1]
        srv_thread = _threading2.Thread(target=srv.serve_forever, daemon=True)
        srv_thread.start()
        time.sleep(0.05)

        try:
            big = (b'{"text": "' + b"x" * (2 * 1024 * 1024)
                   + b'", "type": "entity", "source": "h", "confidence": 0.9}')
            req = _urlreq.Request(
                f"http://127.0.0.1:{srv_port}/facts",
                data=big,
                headers={
                    "Authorization": "Bearer selftest-tok",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(big)),
                },
                method="POST",
            )
            try:
                _urlreq.urlopen(req, timeout=5)
                chk("body-size cap: 413 for oversized body", False, "no error raised")
            except _urlerr.HTTPError as e:
                chk("body-size cap: 413 for oversized body", e.code == 413, f"got {e.code}")
            except Exception as e:
                chk("body-size cap: 413 for oversized body", False, str(e))
        finally:
            srv.shutdown()

    print()
    print("All checks PASSED" if not fails else f"{fails} check(s) FAILED")
    return 1 if fails else 0


# ----------------------------------------------------------------- Entry point

def main() -> None:
    ap = argparse.ArgumentParser(description="persistent fact memory service")
    ap.add_argument("--db", default=os.environ.get("FACTMEM_DB", "factmem.db"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FACTMEM_PORT", "7700")))
    ap.add_argument("--selftest", action="store_true", help="Run self-tests and exit")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())

    bind = _resolve_bind()
    _check_bind(bind)

    token = os.environ.get("FACTMEM_TOKEN", "").strip()
    if not token:
        print("WARNING: FACTMEM_TOKEN not set — service accepts unauthenticated requests",
              file=sys.stderr)

    conn = _open(args.db)
    _migrate(conn)
    conn.close()

    srv = MemServer(bind, args.port, args.db, token)
    print(f"factmem listening on http://{bind}:{args.port}/  db={args.db}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)


if __name__ == "__main__":
    main()
