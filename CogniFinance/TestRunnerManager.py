import sys
import os
import json
from datetime import datetime, timedelta
from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

CONFIG_PATH = os.path.expanduser("~/.cognifinance_audit.json")
DEEP_AUDIT_INTERVAL_DAYS = 90  # 3 months

class TestRunnerManager(QObject):
    audit_completed = pyqtSignal(bool, str) # success, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.weekly_passed = False

    def start_scheduled_audit(self):
        """Initial entry point triggered after app startup."""
        self._run_weekly_tests()

    def _run_weekly_tests(self):
        """Step 1: Launch weekly ledger invariant test."""
        self.process = QProcess(self)
        self.process.finished.connect(self._on_weekly_finished)

        # Path to your test executable or python module
        self.process.start("python3", ["-m", "cogni_tests.weekly_invariants"])

    def _on_weekly_finished(self, exit_code, exit_status):
        self.process.disconnect()  # Clean up signal bindings

        if exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            self.weekly_passed = False
            self.audit_completed.emit(False, "Weekly ledger invariant tests failed. Aborting deep audit.")
            return

        self.weekly_passed = True

        # Step 2: Evaluate if 3-6 month deep audit threshold is met
        if self._should_run_deep_audit():
            self._run_deep_audit()
        else:
            self.audit_completed.emit(True, "Weekly invariants passed. Deep audit not due yet.")

    def _should_run_deep_audit(self) -> bool:
        """Checks local config for the last successful deep audit timestamp."""
        if not os.path.exists(CONFIG_PATH):
            return True
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
                last_run_str = data.get("last_deep_audit")
                if not last_run_str:
                    return True

                last_run = datetime.fromisoformat(last_run_str)
                return datetime.now() - last_run >= timedelta(days=DEEP_AUDIT_INTERVAL_DAYS)
        except Exception:
            return True  # Fallback to running if config is unreadable

    def _run_deep_audit(self):
        """Step 3: Launch deep structural/crypto test suite (MITM, Rekey, SPAKE2)."""
        self.process = QProcess(self)
        self.process.finished.connect(self._on_deep_audit_finished)

        self.process.start("python3", ["-m", "cogni_tests.deep_structural_suite"])

    def _on_deep_audit_finished(self, exit_code, exit_status):
        self.process.disconnect()

        if exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            self._update_deep_audit_timestamp()
            self.audit_completed.emit(True, "All tests passed successfully (Weekly + Deep Structural).")
        else:
            self.audit_completed.emit(False, "Weekly invariants passed, but Deep Structural tests failed.")

    def _update_deep_audit_timestamp(self):
        """Persists successful deep audit timestamp."""
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    data = json.load(f)
            except Exception:
                pass

        data["last_deep_audit"] = datetime.now().isoformat()
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def cleanup(self):
        """Ensures subprocesses don't orphan if the app closes mid-test."""
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
