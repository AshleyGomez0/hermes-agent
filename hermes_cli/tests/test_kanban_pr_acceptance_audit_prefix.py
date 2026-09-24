"""
Regression tests for gate-A fix: kanban_pr_acceptance.py should treat
'audit:OWNER/REPO' as a local-only-equivalent completion contract for READ_ONLY audits.

Backstory: prior to gate A, any task with completion_contract='OWNER/REPO' (a valid contract
by itself) but no published_pr in metadata could not close — it blocked on the gh PR-acceptance
collection path. For READ_ONLY audits, there is no PR and there will never be one. The audit-
local equivalent path allows these tasks to durably terminate through the local-only semantics
while preserving the documentary OWNER/REPO metadata for reviewers.

This test was introduced as part of the STRICT-VnV final sanitation gate, gated by
directive ASHLEY-ORCA-OWNER-OS-SUINI-OBSIDIAN-FIRST-20260923-01, durable review at
panorama-mission-control#2 comment 5820880131 (external anchor) and the closure
comment now being written.
"""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path


# Locatable in-process: hermes_cli package under hermes-agent.
HERE = Path(__file__).resolve().parent.parent  # hermes_cli/
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class AuditPrefixAcceptanceTest(unittest.TestCase):
    """Direct tests of validate_contract + collect_acceptance (gate A)."""

    def setUp(self):
        # Re-import the module freshly so any prior test runtime doesn't leak
        if "kanban_pr_acceptance" in sys.modules:
            del sys.modules["kanban_pr_acceptance"]
        self.mod = importlib.import_module("kanban_pr_acceptance")

    def test_validate_local_only_passes(self):
        self.assertEqual(self.mod.validate_contract("local-only"), "local-only")
        self.assertEqual(self.mod.validate_contract(None), "local-only")

    def test_validate_owner_repo_passes_through(self):
        # OWNER/REPO without audit: prefix is the original behaviour — still works,
        # still requires published_pr to pass collect_acceptance.
        self.assertEqual(
            self.mod.validate_contract("neokyhurtado-cmd/panorama-mission-control"),
            "neokyhurtado-cmd/panorama-mission-control",
        )

    def test_validate_audit_prefix_accepts_valid_suffix(self):
        # audit:OWNER/REPO returns 'local-only' (acceptance-side equivalence)
        # but the OWNER/REPO is preserved in collect_acceptance's audit_target_repo
        # for documentary purposes.
        self.assertEqual(
            self.mod.validate_contract("audit:neokyhurtado-cmd/panorama-mission-control"),
            "local-only",
        )

    def test_validate_audit_prefix_rejects_invalid_suffix(self):
        with self.assertRaises(ValueError):
            self.mod.validate_contract("audit:notarepo")

    def test_collect_acceptance_audit_prefix_returns_ok(self):
        r = self.mod.collect_acceptance(
            "audit:neokyhurtado-cmd/panorama-mission-control",
            published_pr=None,
        )
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("classification"), "audit-local")
        self.assertEqual(r.get("audit_target_repo"), "neokyhurtado-cmd/panorama-mission-control")
        self.assertIsNone(r.get("pr_url"))

    def test_collect_acceptance_audit_prefix_with_pr_still_ok(self):
        # If a PR is provided for an audit: contract, we still treat it as audit-local
        # (don't escalate audit to PR-acceptance — that would re-introduce the loop).
        r = self.mod.collect_acceptance(
            "audit:neokyhurtado-cmd/panorama-mission-control",
            published_pr="https://github.com/some/repo/pull/1",
        )
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("classification"), "audit-local")

    def test_collect_acceptance_owner_repo_without_pr_still_missing(self):
        # Regression check: the original behaviour for OWNER/REPO without published_pr
        # must remain ok=False, classification=missing. The gate-A addition must NOT
        # degrade the original guard.
        r = self.mod.collect_acceptance(
            "neokyhurtado-cmd/panorama-mission-control",
            published_pr=None,
        )
        self.assertFalse(r.get("ok"))
        self.assertEqual(r.get("classification"), "missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
