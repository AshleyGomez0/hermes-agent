"""F10: bind same-card reviews to an owner-declared, real Git checkout.

Only enabled/enrolled CP47 cards opt in. The SQLite transition's caller holds
BEGIN IMMEDIATE; the repository is rechecked on handoff, reviewer admission and
final completion. No Git command writes or uses caller-provided shell text.
"""
import os
import re
import subprocess
from pathlib import Path


def _canonical_owned_checkout(task_path, approved_path, owner):
    if not all(isinstance(v, str) and v.strip() for v in
               (task_path, approved_path, owner)):
        return None
    raw, declared, home = Path(task_path), Path(approved_path), Path(owner)
    if not all(p.is_absolute() for p in (raw, declared, home)):
        return None
    try:
        owner_root = home.resolve(strict=True)
        checkout = raw.resolve(strict=True)
        approved = declared.resolve(strict=True)
        # Owner approval is the task's exact checkout, not an arbitrary tree
        # on a mixed-ownership Factory volume or another user's profile.
        if (not checkout.is_dir() or checkout == owner_root
                or owner_root not in checkout.parents
                or os.path.normcase(str(checkout)) != os.path.normcase(str(approved))):
            return None
        return checkout
    except (OSError, RuntimeError, ValueError):
        return None


def _checkout_at_sha(root, sha):
    from hermes_cli.kanban_db_workspace import _git

    def git(*argv):
        result = _git(root, "--no-optional-locks", "-c", "core.fsmonitor=false", *argv, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None

    # Require a repository ROOT, not a parent repo reached from an arbitrary
    # directory. Pin source to a real commit object, not a syntactic 40-hex id.
    top = git("rev-parse", "--show-toplevel")
    if not top:
        return False
    try:
        if Path(top).resolve(strict=True) != root:
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    if git("cat-file", "-t", sha) != "commit":
        return False
    if git("rev-parse", "--verify", "HEAD^{commit}") != sha:
        return False
    # No uncommitted source mutations, including untracked files.
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None or status:
        return False
    # Detect an ordinary HEAD update during validation. Later phases verify
    # again; the independent reviewer is restricted to its pinned checkout.
    return git("rev-parse", "--verify", "HEAD^{commit}") == sha


def checkout_commit_matches(conn, task_id, sha, phase, policy):
    """Fail closed unless task, owner and checkout match one immutable Git SHA."""
    if (not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or not isinstance(phase, dict) or not isinstance(policy, dict)):
        return False
    owner = policy.get("owner_home")
    if (not isinstance(owner, str) or
            policy.get("task_owners", {}).get(task_id) != owner):
        return False
    row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None:
        return False
    root = _canonical_owned_checkout(
        row["workspace_path"], phase.get("review_checkout"), owner)
    if root is None:
        return False
    try:
        return _checkout_at_sha(root, sha)
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return False


def active_writer_run_matches(conn, task_id, run_id, writer):
    """Trust only this card's native claimed, still-active SQLite writer run."""
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        return False
    row = conn.execute(
        "SELECT t.status,t.current_run_id,t.claim_lock,"
        "r.profile,r.claim_lock AS run_lock,r.ended_at,r.status AS run_status "
        "FROM tasks t JOIN task_runs r ON r.id=? AND r.task_id=t.id "
        "WHERE t.id=?", (run_id, task_id),
    ).fetchone()
    if (row is None or row["status"] != "running"
            or row["current_run_id"] != run_id
            or not row["claim_lock"] or row["claim_lock"] != row["run_lock"]
            or row["profile"] != writer or row["ended_at"] is not None
            or row["run_status"] != "running"):
        return False
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='claimed' LIMIT 1", (task_id, run_id),
    ).fetchone() is not None
