# TestRunnerManager.py ver 0.2
# CogniFinance ver 0.9.67

import sys
import os
import json
from datetime import datetime
from PyQt6.QtCore import QObject, QProcess, pyqtSignal

from integrity_checker import IntegrityChecker, should_run_integrity_check, save_cooldown_timestamp
from deep_integrity_checker import DeepIntegrityChecker, should_run_deep_check, save_deep_cooldown_timestamp
from config import AUDIT_CONFIG_PATH
from config import atomic_secure_write_json
from supply_chain_auditor import SupplyChainAuditor

DEEP_AUDIT_INTERVAL_DAYS = 90

class TestRunnerManager(QObject):
    audit_completed = pyqtSignal(bool, str)  # (success: bool, message: str)
    log_output = pyqtSignal(str)             # Streams test output to the UI
    supply_chain_warning = pyqtSignal(int, int, list) # (total, critical, details)


    def __init__(self, db_dir: str, test_dir: str, requirements_path, dek_hex: str, parent=None):
        super().__init__(parent)
        self.db_dir = db_dir
        self.test_dir = test_dir
        self.config_path = str(AUDIT_CONFIG_PATH)
        self.requirements_path = requirements_path
        self.dek_hex = dek_hex
        self.supply_chain_auditor = None
        self.weekly_checker = None
        self.deep_checker = None
        self._shutting_down = False

    def start_scheduled_audit(self):
        """Initial entry point triggered after app startup."""
        if self._shutting_down:
            return

        self.log_output.emit("=" * 50)
        self.log_output.emit("AUDIT SCHEDULER: Evaluating cooldowns...")

        run_weekly = should_run_integrity_check(self.db_dir, cooldown_days=7)
        run_deep = should_run_deep_check(self.db_dir, cooldown_days=DEEP_AUDIT_INTERVAL_DAYS)

        if not run_weekly and not run_deep:
            self.log_output.emit("No audits currently due. Exiting scheduler.")
            self.audit_completed.emit(True, "No audits currently due.")
            return

        if run_weekly:
            self.log_output.emit(" -> Weekly Ledger Invariants (Due)")
        if run_deep:
            self.log_output.emit(" -> Deep Structural/Security Suite (Due)")
        self.log_output.emit("=" * 50)

        if run_deep:
            # Supply chain audit only runs alongside the deep/quarterly suite,
            # not on every weekly cycle.
            self._run_supply_chain_audit(run_weekly_after=run_weekly)
        elif run_weekly:
            self._run_weekly_tests(run_deep_after=False)

    def _run_supply_chain_audit(self, run_weekly_after: bool):
        if self._shutting_down:
            return

        self.log_output.emit("Step 0: Running Supply Chain Vulnerability Audit...")
        self.supply_chain_auditor = SupplyChainAuditor(self.requirements_path)
        self.supply_chain_auditor.log_output.connect(self.log_output.emit)

        # Connect to a new handler that decides what to do next
        self.supply_chain_auditor.audit_finished.connect(
            lambda total, crit, details: self._on_supply_chain_finished(total, crit, details, run_weekly_after)
        )
        self.supply_chain_auditor.start()

    def _on_supply_chain_finished(self, total_vulns: int, critical_vulns: int, details: list, run_weekly: bool):
        if self._shutting_down:
            return

        if total_vulns == -1:
            self.log_output.emit("⚠️ Supply chain audit skipped or failed (offline?). Proceeding to code audits.")
        elif total_vulns > 0:
            self.log_output.emit(f"⚠️ WARNING: {total_vulns} vulnerable dependencies found. Please update requirements.txt.")
            self.supply_chain_warning.emit(total_vulns, critical_vulns, details)
        else:
            self.log_output.emit("✅ Supply chain is clean.")

        # Supply chain only ever runs when deep is due (start_scheduled_audit
        # gates it that way), so the next step is always weekly-then-deep or
        # straight to deep -- no run_deep parameter needed here.
        if run_weekly:
            self._run_weekly_tests(run_deep_after=True)
        else:
            self._run_deep_audit()

    def _run_weekly_tests(self, run_deep_after: bool):
        """Step 1: Launch weekly ledger invariant test."""
        if self._shutting_down:
            return

        self.log_output.emit("Starting Weekly Ledger Invariants...")
        self.weekly_checker = IntegrityChecker(self.test_dir)
        self.weekly_checker.log_output.connect(self.log_output.emit)

        if run_deep_after:
            self.weekly_checker.finished_signal.connect(self._on_weekly_finished_then_deep)
        else:
            self.weekly_checker.finished_signal.connect(self._on_weekly_finished_only)

        self.weekly_checker.start()

    def _on_weekly_finished_only(self, exit_code: int, summary: str):
        if self._shutting_down:
            return

        if exit_code == 0:
            save_cooldown_timestamp(self.db_dir)
            self.audit_completed.emit(True, "Weekly audit passed.")
        else:
            self.audit_completed.emit(False, f"Weekly audit failed: {summary}")

    def _on_weekly_finished_then_deep(self, exit_code: int, summary: str):
        if self._shutting_down:
            return

        if exit_code == 0:
            save_cooldown_timestamp(self.db_dir)
            self.log_output.emit("✅ Weekly audit passed. Transitioning to Deep audit...")
        else:
            self.log_output.emit(f"❌ Weekly audit failed: {summary}. Proceeding to Deep audit anyway...")

        self._run_deep_audit()

    def _run_deep_audit(self):
        """Step 2: Launch deep structural/crypto test suite."""
        if self._shutting_down:
            return

        self.log_output.emit("Starting Deep Structural/Security Suite...")
        self.deep_checker = DeepIntegrityChecker(self.test_dir,  self.db_dir, self.dek_hex)
        self.deep_checker.log_output.connect(self.log_output.emit)
        self.deep_checker.finished_signal.connect(self._on_deep_audit_finished)
        self.deep_checker.start()

    def _get_infra_failures(self) -> int:
        """Reads the consecutive infrastructure failure counter from disk."""
        if not os.path.exists(self.config_path):
            return 0
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return int(data.get("deep_infra_failures", 0))
        except Exception:
            return 0

    def _set_infra_failures(self, count: int):
        """Persists the consecutive infrastructure failure counter to disk."""
        data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        data["deep_infra_failures"] = count
        try:
            atomic_secure_write_json(self.config_path, data)
        except Exception as e:
            self.log_output.emit(f"Warning: Could not save failure counter: {e}")

    def _on_deep_audit_finished(self, exit_code: int, summary: str):
        """Step 3: Finalize deep audit based on exact exit code."""
        if self._shutting_down:
            return

        # SCENARIO A: Tests ran to completion (Exit 0 = pass, Exit 1 = test failure)
        if exit_code in (0, 1):
            self._update_deep_audit_timestamp()
            self._set_infra_failures(0)  # Reset failure counter

            if exit_code == 0:
                msg = "All tests passed successfully (Weekly + Deep Structural). 90-day cooldown reset."
                self.log_output.emit(f"✅ {msg}")
                self.audit_completed.emit(True, msg)
            else:
                msg = "Deep Structural tests ran, but some assertions failed. 90-day cooldown reset. Check logs for details."
                self.log_output.emit(f"⚠️ {msg}")
                self.audit_completed.emit(True, msg)

        # SCENARIO B: Infrastructure failure (Exit 2, 3, 4, 5, or -1 OS error)
        else:
            failures = self._get_infra_failures() + 1
            self._set_infra_failures(failures)

            if failures >= 3:
                # Limit hit: force cooldown reset to prevent endless retry loops on startup
                self._update_deep_audit_timestamp()
                self._set_infra_failures(0)

                error_msg = (
                    f"The automated security test suite crashed 3 consecutive times (Exit Code: {exit_code}) "
                    f"before it could even run the tests. This indicates a broken test environment, "
                    f"NOT a problem with your financial data.\n\n"
                    f"Actionable Advice:\n"
                    f"1. Open your terminal and run `pytest test/` manually to diagnose the issue.\n"
                    f"2. Ensure your virtual environment is activated and dependencies are installed.\n"
                    f"3. Check for syntax errors in recently added test files.\n\n"
                    f"The deep audit has been paused for 90 days to prevent startup spam."
                )
                self.log_output.emit(f"❌ {error_msg}")
                self.audit_completed.emit(False, error_msg)

            else:
                msg = (
                    f"Deep audit infrastructure failed (Exit Code: {exit_code}). "
                    f"This is retry {failures}/3. The cooldown will NOT reset, so it will retry on the next app startup."
                )
                self.log_output.emit(f"⚠️ {msg}")
                self.audit_completed.emit(True, msg)

    def _update_deep_audit_timestamp(self):
        """Persists successful deep audit timestamp to the db cooldown file."""
        save_deep_cooldown_timestamp(self.db_dir)

    def cleanup(self):
        """Ensures subprocesses don't orphan if the app closes mid-test."""
        # Order matters: flag and signal-blocking come BEFORE any terminate()/kill(),
        # because waitForFinished() can emit finished synchronously.
        self._shutting_down = True

        objs = (self.weekly_checker, self.deep_checker, self.supply_chain_auditor)
        for obj in objs:
            if obj is not None:
                try:
                    obj.blockSignals(True)
                except RuntimeError:
                    pass  # C++ object already deleted

        for obj in objs:
            proc = getattr(obj, 'process', None)
            if proc is None:
                continue
            try:
                if proc.state() != QProcess.ProcessState.NotRunning:
                    proc.terminate()
                    if not proc.waitForFinished(2000):
                        proc.kill()
                        proc.waitForFinished(1000)
            except RuntimeError:
                pass
