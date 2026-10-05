"""Durable capacity primitive; dispatch hooks pending, never an auth store.
Owner/provider identify shared quota; board is attribution only. Disabled by
 default and performs no SQLite I/O until explicitly enabled by a future hook.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import sqlite3
import uuid


@dataclass(frozen=True)
class Permit:
    allowed: bool
    state: str
    probe_token: str | None = None
    eligible_at: float | None = None
    reason: str = ""


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # Integers outside float range are malformed timestamps, not permits.
        return False
    return finite and value >= 0


class CapacityBreaker:
    def __init__(self, path, *, enabled=False):
        self.path = str(path)
        self.enabled = enabled

    def _valid(self, owner, provider, now):
        return self.enabled is True and isinstance(owner, str) and bool(owner.strip()) and isinstance(provider, str) and bool(provider.strip()) and _finite(now)

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS capacity (
                owner TEXT NOT NULL, provider TEXT NOT NULL,
                state TEXT NOT NULL, eligible_at REAL NOT NULL,
                failures INTEGER NOT NULL, board TEXT NOT NULL,
                generation INTEGER NOT NULL, token TEXT,
                lease_until REAL NOT NULL,
                PRIMARY KEY(owner, provider))""")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row(self, conn, owner, provider):
        row = conn.execute("SELECT * FROM capacity WHERE owner=? AND provider=?", (owner, provider)).fetchone()
        if row is not None:
            if row["state"] not in ("CLOSED", "OPEN", "HALF_OPEN") or not _finite(row["eligible_at"]) or not _finite(row["lease_until"]) or not isinstance(row["generation"], int) or row["generation"] < 1 or not isinstance(row["failures"], int) or row["failures"] < 0:
                raise ValueError("Malformed capacity state")
            if row["state"] == "HALF_OPEN" and (not isinstance(row["token"], str) or not row["token"] or row["lease_until"] <= 0):
                raise ValueError("Malformed probe lease")
            if row["state"] != "HALF_OPEN" and (row["token"] is not None or row["lease_until"] != 0):
                raise ValueError("Unexpected probe lease")
        return row

    def acquire(self, owner, provider, *, now):
        if self.enabled is False:
            return Permit(True, "DISABLED")
        if not self._valid(owner, provider, now):
            return Permit(False, "INVALID", reason="malformed_config_or_key")
        try:
            with self._transaction() as conn:
                row = self._row(conn, owner, provider)
                if row is None or row["state"] == "CLOSED":
                    return Permit(True, "CLOSED")
                if now < row["eligible_at"] or (row["state"] == "HALF_OPEN" and now < row["lease_until"]):
                    return Permit(False, row["state"], eligible_at=max(row["eligible_at"], row["lease_until"]))
                generation = row["generation"] + 1
                token = f"{generation}:{uuid.uuid4().hex}"
                conn.execute("UPDATE capacity SET state='HALF_OPEN',generation=?,token=?,lease_until=? WHERE owner=? AND provider=?", (generation, token, now + 60, owner, provider))
                return Permit(True, "HALF_OPEN", token, now + 60)
        except (sqlite3.Error, ValueError, OSError):
            return Permit(False, "INVALID", reason="capacity_state_unavailable")

    def rate_limited(self, owner, provider, *, now, board="", reset_at=None):
        if not self._valid(owner, provider, now) or not isinstance(board, str) or (reset_at is not None and not _finite(reset_at)):
            return False
        try:
            with self._transaction() as conn:
                row = self._row(conn, owner, provider)
                failures = (row["failures"] if row else 0) + 1
                generation = (row["generation"] if row else 0) + 1
                backoff = min(3600, 300 * (2 ** min(failures - 1, 4)))
                eligible = max(now + backoff, reset_at or 0, row["eligible_at"] if row else 0)
                conn.execute("""INSERT INTO capacity VALUES (?,?,'OPEN',?,?,?, ?,NULL,0)
                    ON CONFLICT(owner,provider) DO UPDATE SET state='OPEN',
                    eligible_at=excluded.eligible_at, failures=excluded.failures,
                    board=excluded.board,generation=excluded.generation,
                    token=NULL,lease_until=0""", (owner, provider, eligible, failures, board, generation))
                return True
        except (sqlite3.Error, ValueError, OSError):
            return False

    def success(self, owner, provider, probe_token, *, now):
        if not self._valid(owner, provider, now) or not isinstance(probe_token, str) or not probe_token:
            return False
        try:
            with self._transaction() as conn:
                row = self._row(conn, owner, provider)
                if row is None or row["state"] != "HALF_OPEN" or row["token"] != probe_token or now >= row["lease_until"]:
                    return False
                cur = conn.execute("""UPDATE capacity SET state='CLOSED', eligible_at=0,
                    failures=0, token=NULL,lease_until=0 WHERE owner=? AND provider=?
                    AND generation=? AND token=? AND state='HALF_OPEN'""", (owner, provider, row["generation"], probe_token))
                return cur.rowcount == 1
        except (sqlite3.Error, ValueError, OSError):
            return False
