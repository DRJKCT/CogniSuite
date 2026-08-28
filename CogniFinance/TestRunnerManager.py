# TestRunnerManager.py
import sys
import os
import json
from datetime import datetime
from PyQt6.QtCore import QObject, QProcess, pyqtSignal

from integrity_checker import IntegrityChecker, should_run_integrity_check, save_cooldown_timestamp
from deep_integrity_checker import DeepIntegrityChecker, should_run_deep_check, save_deep_cooldown_timestamp

DEEP_AUDIT_INTERVAL_DAYS = 90

class TestRunnerManager(QObject):
    audit_completed = pyqtSignal(bool, str)  # (success: bool, message: str)
    log_output = pyqtSignal(str)             # Streams test output to the UI

    def __init__(self, db_dir: str, test_dir: str, parent=None):
        super().__init__(parent)
        self.db_dir = db_dir
        self.test_dir = test_dir
        self.config_path = os.path.join(self.db_dir, ".cognifinance_audit.json")
        self.weekly_checker = None
        self.deep_checker = None

    def start_scheduled_audit(self):
        """Initial entry point triggered after app startup."""
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

        self._run_weekly_tests(run_deep_after=run_deep)

    def _run_weekly_tests(self, run_deep_after: bool):
        """Step 1: Launch weekly ledger invariant test."""
        self.log_output.emit("Starting Weekly Ledger Invariants...")
        self.weekly_checker = IntegrityChecker(self.test_dir)
        self.weekly_checker.log_output.connect(self.log_output.emit)

        if run_deep_after:
            self.weekly_checker.finished_signal.connect(self._on_weekly_finished_then_deep)
        else:
            self.weekly_checker.finished_signal.connect(self._on_weekly_finished_only)

        self.weekly_checker.start()

    def _on_weekly_finished_only(self, exit_code: int, summary: str):
        if exit_code == 0:
            save_cooldown_timestamp(self.db_dir)
            self.audit_completed.emit(True, "Weekly audit passed.")
        else:
            self.audit_completed.emit(False, f"Weekly audit failed: {summary}")

    def _on_weekly_finished_then_deep(self, exit_code: int, summary: str):
        if exit_code == 0:
            save_cooldown_timestamp(self.db_dir)
            self.log_output.emit("✅ Weekly audit passed. Transitioning to Deep audit...")
        else:
            self.log_output.emit(f"❌ Weekly audit failed: {summary}. Proceeding to Deep audit anyway...")

        self._run_deep_audit()

    def _run_deep_audit(self):
        """Step 2: Launch deep structural/crypto test suite."""
        self.log_output.emit("Starting Deep Structural/Security Suite...")
        self.deep_checker = DeepIntegrityChecker(self.test_dir)
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
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log_output.emit(f"Warning: Could not save failure counter: {e}")

    def _on_deep_audit_finished(self, exit_code: int, summary: str):
        """Step 3: Finalize deep audit based on exact exit code."""
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
        for checker in (self.weekly_checker, self.deep_checker):
            if checker and hasattr(checker, 'process') and checker.process.state() != QProcess.ProcessState.NotRunning:
                checker.process.terminate()
                if not checker.process.waitForFinished(2000):
                    checker.process.kill()
