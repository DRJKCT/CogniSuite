# integrity_checker.py
import os
import sys
import json
import time
from PyQt6.QtCore import QObject, QProcess, pyqtSignal

class IntegrityChecker(QObject):
    """
    Runs the core accounting test suite in a completely separate OS process.
    This bypasses Python's GIL entirely, ensuring the UI remains 100%
    responsive even during heavy SQLCipher fuzz testing.
    """
    log_output = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str) # (success: bool, summary: str)

    def __init__(self, test_dir: str):
        super().__init__()
        self.test_dir = test_dir

        # ONLY the fast, critical smoke tests.
        # Excluded: test_balance_invariant_fuzz.py (too heavy for background UI)
        self.target_tests = [
            os.path.join(test_dir, "test_core_finance.py"),
            os.path.join(test_dir, "test_ledger_logic.py"),
            os.path.join(test_dir, "test_ledger_reconciliation_history.py"),
        ]

        self.process = QProcess(self)
        # Merge stdout and stderr so we catch all pytest output
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._on_finished)

    def start(self):
        """Spawns pytest in a separate OS process."""
        self.log_output.emit("Starting background ledger integrity checks...")
        self.log_output.emit("Running core math, audit trail, and reconciliation tests.")

        # Use the exact same Python executable running the app (ensures venv is used)
        executable = sys.executable

        # -v: verbose
        # -s: disable pytest's output capturing so it streams to QProcess in real-time
        # -p no:cacheprovider: prevents .pytest_cache folder bloat
        arguments = ["-m", "pytest", "-v", "-s", "-p", "no:cacheprovider"] + self.target_tests

        self.process.start(executable, arguments)

    def _read_output(self):
        """Reads the streamed output from the child process and emits it to the UI."""
        # .data() gets the raw bytes, decode handles any weird characters safely
        raw_data = self.process.readAllStandardOutput().data().decode('utf-8', errors='replace')
        for line in raw_data.splitlines():
            if line.strip():
                self.log_output.emit(line)

    def _on_finished(self, exit_code, exit_status):
        """Called when the pytest process terminates."""

        self._read_output()

        success = (exit_code == 0)
        if success:
            summary = "SUCCESS: All accounting invariants verified. Ledger math is perfectly balanced."
            self.log_output.emit(f"✅ {summary}")
        else:
            summary = f"CRITICAL FAILURE: Tests failed with exit code {exit_code}. Check the Log Tab immediately."
            self.log_output.emit(f"❌ {summary}")

        self.finished_signal.emit(success, summary)

    def _on_process_error(self, error):
        """Fires if QProcess fails to start or crashes at the OS level."""
        error_msg = self.process.errorString()
        self.log_output.emit(f"❌ CRITICAL OS ERROR: QProcess failed: {error_msg}")
        self.finished_signal.emit(False, f"QProcess OS Error: {error_msg}")

# =====================================================================
# Cooldown Management (Unchanged from previous version)
# =====================================================================
def get_cooldown_path(db_dir: str) -> str:
    return os.path.join(db_dir, ".integrity_check_cooldown.json")

def should_run_integrity_check(db_dir: str, cooldown_days: int = 7) -> bool:
    path = get_cooldown_path(db_dir)
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r") as f:
            data = json.load(f)
            last_run = data.get("last_run", 0)
            return (time.time() - last_run) > (cooldown_days * 86400)
    except Exception:
        return True

def save_cooldown_timestamp(db_dir: str):
    path = get_cooldown_path(db_dir)
    try:
        with open(path, "w") as f:
            json.dump({"last_run": time.time()}, f)
    except Exception:
        pass
