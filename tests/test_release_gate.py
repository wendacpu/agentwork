"""发布门禁回归：subprocess 隔离模拟，不调用模型或外部服务。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_system.release_gate import (
    CHECKLIST_KEYS,
    LEDGER_NAME,
    ReleaseGateError,
    approve_release,
    check_active_release,
    record_proposal_verification,
    revoke_release,
    source_fingerprint,
    validate_release,
)


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.out = Path(self.temp.name) / "evidence"
        for directory in ("agent_system", "rules", "rulepacks", "tests"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "agent_system" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests" / "test_core.py").write_text("def test_core():\n    assert True\n", encoding="utf-8")
        self.checklist = {key: {"verified": True, "evidence": f"{key}: case-1 期望/实际一致，人工核对"}
                          for key in CHECKLIST_KEYS}

    def validate(self, proposal_ids=None, returncode=0, stdout="3 passed in 0.02s\n", **kwargs):
        result = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=kwargs.get("stderr", ""))
        with patch("agent_system.release_gate.subprocess.run", return_value=result) as runner:
            evidence = validate_release(self.root, proposal_ids or ["p1"], self.out)
        return evidence, runner

    def reviewed(self, proposal_ids=None):
        evidence, _ = self.validate(proposal_ids=proposal_ids)
        for proposal_id in evidence["proposal_ids"]:
            record_proposal_verification(self.root, evidence["validation_path"], proposal_id,
                                         "human-reviewer", self.checklist, self.out, confirmed=True)
        return evidence

    def approve(self, evidence):
        return approve_release(self.root, evidence["validation_path"], "release-reviewer", self.out, confirmed=True)

    def test_fingerprint_stable_scoped_and_sensitive_to_path_and_content(self):
        original = source_fingerprint(self.root)
        self.assertEqual(len(original), 64)
        for path in ("agent_system/__pycache__/core.py", "rules/cache/a.py", "rulepacks/x.txt", "unrelated.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("irrelevant", encoding="utf-8")
        self.assertEqual(source_fingerprint(self.root), original)
        (self.root / "rules" / "new.py").write_text("a=1", encoding="utf-8")
        changed = source_fingerprint(self.root)
        self.assertNotEqual(changed, original)
        (self.root / "rulepacks" / "pack.md").write_text("rule", encoding="utf-8")
        self.assertNotEqual(source_fingerprint(self.root), changed)
        self.assertEqual(source_fingerprint(str(self.root / ".")), source_fingerprint(self.root))

    def test_real_nested_rules_and_verification_are_fingerprinted(self):
        for directory in ("agent_system/rules", "agent_system/verification"):
            before = source_fingerprint(self.root)
            path = self.root / directory / "logic.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("VALUE=1", encoding="utf-8")
            self.assertNotEqual(source_fingerprint(self.root), before)

    def test_validation_exact_safe_command_and_immutable_unique_evidence(self):
        evidence, runner = self.validate(stderr="real stderr")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "pytest", "-q", "tests"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["cwd"], str(self.root.resolve()))
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["returncode"], 0)
        self.assertEqual(evidence["stdout"], "3 passed in 0.02s\n")
        self.assertEqual(evidence["stderr"], "real stderr")
        saved = Path(evidence["validation_path"]).read_bytes()
        second, _ = self.validate()
        self.assertNotEqual(evidence["validation_id"], second["validation_id"])
        self.assertEqual(Path(evidence["validation_path"]).read_bytes(), saved)
        with sqlite3.connect(self.out / LEDGER_NAME) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
        self.assertEqual(source_fingerprint(self.root), evidence["fingerprint"])

    def test_invalid_proposal_ids_fail_before_subprocess(self):
        for value in ([], "p1", [""], [1], ["p1", "p1"], None):
            with self.subTest(value=value), patch("agent_system.release_gate.subprocess.run") as runner:
                with self.assertRaises(ReleaseGateError):
                    validate_release(self.root, value, self.out)
                runner.assert_not_called()

    def test_all_empty_skipped_selected_or_failed_runs_rejected(self):
        for code, summary in ((5, "no tests ran in 0.01s"), (0, "no tests ran in 0.01s"),
                              (0, "2 skipped in 0.01s"), (0, "2 passed, 1 skipped in 0.01s"),
                              (0, "1 passed, 1 deselected in 0.01s"),
                              (0, "1 passed, 1 xfailed in 0.01s"),
                              (1, "1 failed, 1 passed in 0.01s"), (0, "arbitrary output")):
            with self.subTest(code=code, summary=summary):
                evidence, _ = self.validate(returncode=code, stdout=summary)
                self.assertFalse(evidence["passed"])
                self.assertEqual(evidence["returncode"], code)
                with self.assertRaises(ReleaseGateError):
                    self.approve(evidence)

    def test_timeout_and_launch_failure_preserve_real_output_without_fake_exit_code(self):
        for error in (subprocess.TimeoutExpired(["pytest"], 120, output=b"partial", stderr=b"timeout stderr"),
                      OSError("cannot launch")):
            with self.subTest(error=type(error).__name__):
                with patch("agent_system.release_gate.subprocess.run", side_effect=error):
                    evidence = validate_release(self.root, ["p1"], self.out)
                self.assertIsNone(evidence["returncode"])
                self.assertFalse(evidence["passed"])
                self.assertTrue(evidence["error"])
                if isinstance(error, subprocess.TimeoutExpired):
                    self.assertTrue(evidence["timed_out"])
                    self.assertEqual(evidence["stdout"], "partial")
                    self.assertEqual(evidence["stderr"], "timeout stderr")

    def test_source_drift_during_subprocess_invalidates_validation(self):
        def run(*args, **kwargs):
            (self.root / "rules" / "changed.py").write_text("changed=True", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "1 passed in 0.01s", "")
        with patch("agent_system.release_gate.subprocess.run", side_effect=run):
            evidence = validate_release(self.root, ["p1"], self.out)
        self.assertFalse(evidence["passed"])
        self.assertNotEqual(evidence["fingerprint"], evidence["fingerprint_after"])

    def test_full_state_machine_does_not_change_source(self):
        fingerprint = source_fingerprint(self.root)
        self.assertEqual(check_active_release(self.root, self.out)["status"], "unconfigured")
        self.assertFalse(self.out.exists())
        evidence = self.reviewed(["p1", "p2"])
        self.assertEqual(check_active_release(self.root, self.out)["status"], "unconfigured")
        manifest = self.approve(evidence)
        status = check_active_release(self.root, self.out)
        self.assertTrue(status["allowed"])
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["manifest_id"], manifest["manifest_id"])
        self.assertEqual(status["proposal_ids"], ["p1", "p2"])
        self.assertEqual(source_fingerprint(self.root), fingerprint)
        revoke = revoke_release(self.root, manifest["manifest_path"], "reviewer", "回归遗漏", self.out, confirmed=True)
        self.assertEqual(revoke["reason"], "回归遗漏")
        self.assertTrue(Path(revoke["revocation_path"]).is_file())
        status = check_active_release(self.root, self.out)
        self.assertFalse(status["allowed"])
        self.assertEqual(status["status"], "revoked")
        with self.assertRaises(ReleaseGateError):
            self.approve(evidence)
        with self.assertRaises(ReleaseGateError):
            revoke_release(self.root, manifest["manifest_path"], "reviewer", "again", self.out, confirmed=True)
        new_manifest = self.approve(self.reviewed())
        self.assertNotEqual(manifest["manifest_id"], new_manifest["manifest_id"])
        self.assertTrue(check_active_release(self.root, self.out)["allowed"])
        self.assertEqual(source_fingerprint(self.root), fingerprint)

    def test_explicit_confirmation_and_reviewer_required(self):
        evidence = self.reviewed()
        for reviewer, confirmed in (("human", False), ("", True), (" ", True), (None, True), ("human", 1)):
            with self.subTest(reviewer=reviewer, confirmed=confirmed):
                with self.assertRaises(ReleaseGateError):
                    approve_release(self.root, evidence["validation_path"], reviewer, self.out, confirmed=confirmed)
        with self.assertRaises(ReleaseGateError):
            record_proposal_verification(self.root, evidence["validation_path"], "p1", "human", self.checklist, self.out)

    def test_manual_checklist_required_for_every_proposal(self):
        evidence, _ = self.validate(proposal_ids=["p1", "p2"])
        with self.assertRaisesRegex(ReleaseGateError, "checklist"):
            self.approve(evidence)
        record_proposal_verification(self.root, evidence["validation_path"], "p1", "human", self.checklist,
                                     self.out, confirmed=True)
        with self.assertRaisesRegex(ReleaseGateError, "p2"):
            self.approve(evidence)
        invalid_lists = ({}, {key: True for key in CHECKLIST_KEYS},
                         {key: {"verified": False, "evidence": "case"} for key in CHECKLIST_KEYS},
                         {key: {"verified": True, "evidence": " "} for key in CHECKLIST_KEYS})
        for checklist in invalid_lists:
            with self.subTest(checklist=checklist), self.assertRaises(ReleaseGateError):
                record_proposal_verification(self.root, evidence["validation_path"], "p2", "human", checklist,
                                             self.out, confirmed=True)
        with self.assertRaises(ReleaseGateError):
            record_proposal_verification(self.root, evidence["validation_path"], "unrelated", "human", self.checklist,
                                         self.out, confirmed=True)

    def test_forged_or_tampered_validation_json_rejected_against_ledger(self):
        evidence = self.reviewed()
        path = Path(evidence["validation_path"])
        fake_path = self.out / "forged.json"
        fake_path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseGateError, "ledger"):
            approve_release(self.root, fake_path, "human", self.out, confirmed=True)
        original = json.loads(path.read_text(encoding="utf-8"))
        original["stdout"] = "pretend success"
        path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseGateError, "篡改"):
            self.approve(evidence)

    def test_failed_test_json_cannot_be_forged_into_success(self):
        evidence, _ = self.validate(returncode=1, stdout="1 failed in 0.01s")
        path = Path(evidence["validation_path"])
        forged = json.loads(path.read_text(encoding="utf-8"))
        forged.update(passed=True, returncode=0, stdout="1 passed in 0.01s")
        path.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseGateError, "篡改"):
            self.approve(evidence)

    def test_stale_validation_and_active_source_are_blocked(self):
        evidence = self.reviewed()
        manifest = self.approve(evidence)
        (self.root / "agent_system" / "core.py").write_text("VALUE=2", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseGateError, "漂移"):
            self.approve(evidence)
        status = check_active_release(self.root, self.out)
        self.assertFalse(status["allowed"])
        self.assertIn("漂移", status["reason"])
        # 源码漂移不妨碍人工撤销，但不能以旧验证重新批准。
        revoke_release(self.root, manifest["manifest_path"], "human", "源码漂移", self.out, confirmed=True)
        self.assertEqual(check_active_release(self.root, self.out)["status"], "revoked")

    def test_test_suite_change_invalidates_approval(self):
        evidence = self.reviewed()
        self.approve(evidence)
        (self.root / "tests" / "test_core.py").write_text("def test_other():\n    assert True\n", encoding="utf-8")
        status = check_active_release(self.root, self.out)
        self.assertFalse(status["allowed"])
        self.assertIn("测试", status["reason"])

    def test_manifest_or_validation_tampering_after_approval_is_blocked(self):
        for which in ("manifest", "validation"):
            with self.subTest(which=which):
                evidence = self.reviewed()
                manifest = self.approve(evidence)
                path = Path(manifest["manifest_path"] if which == "manifest" else evidence["validation_path"])
                data = json.loads(path.read_text(encoding="utf-8"))
                data["reviewer"] = "forged"
                path.write_text(json.dumps(data), encoding="utf-8")
                status = check_active_release(self.root, self.out)
                self.assertFalse(status["allowed"])
                self.assertIn("篡改", status["reason"])

    def test_validation_cannot_be_reused_in_other_project_or_ledger(self):
        evidence = self.reviewed()
        other = self.root.parent / "other"
        other.mkdir()
        with self.assertRaises(ReleaseGateError):
            approve_release(other, evidence["validation_path"], "human", self.out, confirmed=True)
        with self.assertRaises(ReleaseGateError):
            approve_release(self.root, evidence["validation_path"], "human", self.out / "new-ledger", confirmed=True)

    def test_superseded_release_never_becomes_fallback_after_revoke(self):
        first = self.approve(self.reviewed())
        second = self.approve(self.reviewed())
        self.assertNotEqual(first["manifest_id"], second["manifest_id"])
        self.assertEqual(check_active_release(self.root, self.out)["manifest_id"], second["manifest_id"])
        revoke_release(self.root, second["manifest_path"], "human", "revoke new", self.out, confirmed=True)
        self.assertFalse(check_active_release(self.root, self.out)["allowed"])

    def test_approved_review_cannot_be_modified(self):
        evidence = self.reviewed()
        self.approve(evidence)
        with self.assertRaises(ReleaseGateError):
            record_proposal_verification(self.root, evidence["validation_path"], "p1", "another reviewer",
                                         self.checklist, self.out, confirmed=True)

    def test_missing_ledger_with_existing_release_blocks(self):
        self.out.mkdir()
        (self.out / "release-orphan.json").write_text("{}", encoding="utf-8")
        status = check_active_release(self.root, self.out)
        self.assertFalse(status["allowed"])
        self.assertEqual(status["status"], "invalid")

    def test_env_cannot_supply_pytest_addopts_or_plugins(self):
        with patch.dict("os.environ", {"PYTEST_ADDOPTS": "--collect-only", "PYTEST_PLUGINS": "malicious"}):
            _, runner = self.validate()
        env = runner.call_args.kwargs["env"]
        self.assertNotIn("PYTEST_ADDOPTS", env)
        self.assertNotIn("PYTEST_PLUGINS", env)


if __name__ == "__main__":
    unittest.main()
