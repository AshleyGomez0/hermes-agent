"""Stage a disabled, owner-scoped CP47 policy without touching live config.

This is an offline migration preflight. It never enables routing and never
mutates the shared Factory source or any gateway configuration.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from hermes_constants import get_process_hermes_home


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _owner_root(owner_home):
    raw = Path(owner_home)
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("CP47 owner home must be absolute and not a symlink")
    root = raw.resolve()
    process = get_process_hermes_home().resolve()
    effective = (process.parent.parent if process.parent.name.casefold() == "profiles"
                 else process)
    if os.path.normcase(str(root)) != os.path.normcase(str(effective)):
        raise ValueError("CP47 owner home differs from this Ashley process")
    return root


def _within(child, parent):
    return child == parent or parent in child.parents


def _validate_source(document, root):
    from hermes_cli.kanban_factory_policy import _eligible_routes, _is_task_enrolled

    if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
        raise ValueError("invalid policy schema")
    if document["schema_version"] != 1 or type(document.get("enabled")) is not bool:
        raise ValueError("unsupported policy schema or enabled flag")
    if not isinstance(document.get("owner_home"), str):
        raise ValueError("policy missing owner home")
    if os.path.normcase(str(Path(document["owner_home"]).resolve())) != os.path.normcase(str(root)):
        raise ValueError("policy owner is not the Ashley process owner")
    if not isinstance(document.get("board_db"), str) or not Path(document["board_db"]).is_absolute():
        raise ValueError("policy board_db must be absolute")
    if document.get("require_independent_reviewer") is not True:
        raise ValueError("independent reviewer required")
    if (document.get("qwen") != {"bounded_only": True, "tools": False}
            or document.get("deterministic") != "scripts"):
        raise ValueError("bounded Qwen and scripts must be preserved")
    _eligible_routes(document)
    enrolled = document.get("rollout")
    if not isinstance(enrolled, dict) or enrolled.get("mode") != "task_allowlist":
        raise ValueError("CP47 staging requires explicit task allowlist")
    ids = enrolled.get("task_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > 128
            or any(not isinstance(x, str) or not x.startswith("t_") for x in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("invalid task allowlist")
    owners = document.get("task_owners")
    roles = document.get("task_roles")
    profiles = document.get("dispatch_profiles")
    if (not isinstance(owners, dict) or not isinstance(roles, dict)
            or not isinstance(profiles, list) or not profiles
            or any(not isinstance(x, str) or not x.strip() for x in profiles)):
        raise ValueError("missing explicit owners, roles or profiles")
    for task_id in ids:
        if not _is_task_enrolled(document, task_id) or task_id not in roles:
            raise ValueError("missing enrolled task role")
        owner = owners.get(task_id)
        if (not isinstance(owner, str) or not Path(owner).is_absolute()
                or os.path.normcase(str(Path(owner).resolve())) != os.path.normcase(str(root))):
            raise ValueError("enrolled task belongs to another owner")


def stage_policy(source, owner_home, *, source_root, dry_run=True, allow_incomplete=False):
    """Dry-run by default; explicit staging only writes a disabled Ashley copy."""
    root = _owner_root(owner_home)
    path, allowed_root = Path(source), Path(source_root)
    if (not path.is_absolute() or not allowed_root.is_absolute()
            or path.is_symlink() or allowed_root.is_symlink()):
        raise ValueError("CP47 source paths must be absolute and not symlinks")
    path = path.resolve()
    allowed_root = allowed_root.resolve()
    if not _within(path, allowed_root) or not path.is_file():
        raise ValueError("CP47 policy source is outside the approved source root")
    original = path.read_bytes()
    if len(original) > 1024 * 1024:
        raise ValueError("CP47 policy source too large")
    doc = json.loads(original.decode("utf-8-sig"))
    if (not isinstance(doc, dict) or doc.get("schema_version") != 1
            or type(doc.get("enabled")) is not bool
            or not isinstance(doc.get("owner_home"), str)
            or not Path(doc["owner_home"]).is_absolute()
            or os.path.normcase(str(Path(doc["owner_home"]).resolve())) != os.path.normcase(str(root))):
        raise ValueError("CP47 legacy source is not an Ashley-owned v1 policy")
    blockers = []
    try:
        _validate_source(doc, root)
    except (ValueError, TypeError) as exc:
        # Legacy disabled policy may be copied for offline reconciliation only.
        # Do NOT auto-enroll shared tasks, synthesize owner ids, or enable it.
        if not allow_incomplete or doc["enabled"]:
            raise
        blockers = [str(exc)]
        blockers += [f"missing_{key}" for key in
                     ("rollout", "task_owners", "dispatch_profiles", "task_roles")
                     if not doc.get(key)]

    # Never copy an enabled policy into a place a gateway could execute as-is.
    staged = {**doc, "enabled": False}
    payload = (json.dumps(staged, sort_keys=True, indent=2) + "\n").encode("utf-8")
    folder = root / "factory" / "cp47"
    name = f"factory-routing-{_hash(original)[:16]}.staged.json"
    destination = folder / name
    receipt = {
        "source": str(path), "destination": str(destination),
        "owner_home": str(root), "source_sha256": _hash(original),
        "staged_sha256": _hash(payload), "ready_for_activation": False,
        "activation_blockers": blockers, "compatible": not blockers,
    }
    if dry_run:
        return receipt
    parent = root / "factory"
    if parent.exists() and (parent.is_symlink() or not _within(parent.resolve(), root)):
        raise ValueError("CP47 staging parent escapes owner home")
    if folder.exists() and (folder.is_symlink() or not _within(folder.resolve(), root)):
        raise ValueError("CP47 staging folder escapes owner home")
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or not _within(folder.resolve(), root) or destination.is_symlink():
        raise ValueError("CP47 staging destination is unsafe")
    if destination.exists():
        if _hash(destination.read_bytes()) != receipt["staged_sha256"]:
            raise ValueError("CP47 staged policy differs from immutable expected copy")
    else:
        with destination.open("xb") as f:
            f.write(payload)
    if _hash(destination.read_bytes()) != receipt["staged_sha256"]:
        raise ValueError("CP47 staged policy verification failed")
    return receipt


def rollback_stage(destination, owner_home, expected_sha256):
    """Delete ONLY a byte-verified staged artifact; do not touch live config."""
    root = _owner_root(owner_home)
    path = Path(destination)
    parent = root / "factory"
    folder = parent / "cp47"
    if (parent.is_symlink() or not _within(parent.resolve(), root)
            or folder.is_symlink() or not _within(folder.resolve(), root)):
        raise ValueError("CP47 rollback parent escapes owner home")
    if (not path.is_absolute() or path.is_symlink()
            or not re.fullmatch(r"factory-routing-[0-9a-f]{16}\.staged\.json", path.name)
            or path.resolve().parent != folder.resolve()
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError("not an owner-approved CP47 staged policy")
    if not path.is_file() or _hash(path.read_bytes()) != expected_sha256:
        raise ValueError("staged policy changed; refusing unsafe rollback")
    path.unlink()
