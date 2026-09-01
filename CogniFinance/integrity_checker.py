# integrity_checker.py ver 0.1
# For CogniFinance ver 1.8

import os
import sys
import json
import time
from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

class IntegrityChecker(QObject):
    """
    Runs the core accounting test suite in a completely separate OS process.
    This bypasses Python's GIL entirely, ensuring the UI remains 100%
    responsive even during heavy SQLCipher fuzz testing.
    """
    log_output = pyqtSignal(str)
    finished_signal = pyqtSignal(int, str)  # (exit_code: int, summary: str)

    def __init__(self, test_dir: str):
        super().__init__()
        self.test_dir = test_dir

        self.target_tests = [
            os.path.join(test_dir, "test_core_finance.py"),
            os.path.join(test_dir, "test_ledger_logic.py"),
            os.path.join(test_dir, "test_ledger_reconciliation_history.py"),
            os.path.join(test_dir, "test_balance_invariant_fuzz.py"),
        ]

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)

    def start(self):
        """Spawns pytest in a separate OS process."""
        self.log_output.emit("Starting background ledger integrity checks...")

        executable = sys.executable
        arguments = [
            "-m", "pytest", "-v", "-s", "-p", "no:cacheprovider",
            "--timeout=120", "--timeout-method=thread"
        ] + self.target_tests

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(env)
        self.process.start(executable, arguments)

    def _read_output(self):
        """Reads the streamed output from the child process and emits it to the UI."""
        raw_data = self.process.readAllStandardOutput().data().decode('utf-8', errors='replace')
        for line in raw_data.splitlines():
            if line.strip():
                if "hmac check failed" in line or "error decrypting page" in line or "sqlcipher_codec_ctx_set_error" in line:
                    continue
                self.log_output.emit(line)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        """Called when the pytest process terminates."""
        self._read_output()

        if exit_status == QProcess.ExitStatus.CrashExit:
            summary = "CRITICAL FAILURE: Process crashed or was terminated unexpectedly."
            self.log_output.emit(f"❌ {summary}")
            self.finished_signal.emit(-1, summary)
            return

        if exit_code == 0:
            summary = "SUCCESS: All accounting invariants verified. Ledger math is perfectly balanced."
            self.log_output.emit(f"✅ {summary}")
        else:
            summary = f"CRITICAL FAILURE: Tests failed with exit code {exit_code}. Check the Log Tab immediately."
            self.log_output.emit(f"❌ {summary}")

        self.finished_signal.emit(exit_code, summary)

    def _on_process_error(self, error):
        """Fires if QProcess fails to start or crashes at the OS level."""
        error_msg = self.process.errorString()
        self.log_output.emit(f"❌ CRITICAL OS ERROR: QProcess failed: {error_msg}")
        self.finished_signal.emit(-1, f"QProcess OS Error: {error_msg}")


# =====================================================================
# Cooldown Management
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
        os.makedirs(db_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"last_run": time.time()}, f)
    except Exception:
        pass
