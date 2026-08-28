# deep_integrity_checker.py
import os
import sys
import json
import time
from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

class DeepIntegrityChecker(QObject):
    """
    Runs the heavy security, structural, and network test suite
    in a completely separate OS process every 90-180 days.
    """
    log_output = pyqtSignal(str)
    finished_signal = pyqtSignal(int, str)  # (exit_code: int, summary: str)

    def __init__(self, test_dir: str):
        super().__init__()
        self.test_dir = test_dir

        self.weekly_files_to_ignore = [
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
        executable = sys.executable
        arguments = ["-m", "pytest", "-v", "-s", "-p", "no:cacheprovider", self.test_dir]

        for f in self.weekly_files_to_ignore:
            arguments.append(f"--ignore={f}")

        arguments.extend(["--timeout=120", "--timeout-method=thread"])

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(env)

        self.log_output.emit("Starting QUARTERLY deep security and structural checks...")
        self.process.start(executable, arguments)

    def _read_output(self):
        raw_data = self.process.readAllStandardOutput().data().decode('utf-8', errors='replace')
        for line in raw_data.splitlines():
            if line.strip():
                if "hmac check failed" in line or "error decrypting page" in line or "sqlcipher_codec_ctx_set_error" in line:
                    continue
                self.log_output.emit(line)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        self._read_output()

        if exit_status == QProcess.ExitStatus.CrashExit:
            summary = "DEEP CHECK CRASHED: Process terminated unexpectedly."
            self.log_output.emit(f"❌ {summary}")
            self.finished_signal.emit(-1, summary)
            return

        if exit_code == 0:
            summary = "DEEP CHECK SUCCESS: Security, MITM, and structural invariants verified."
            self.log_output.emit(f"✅ {summary}")
        else:
            summary = f"DEEP CHECK FAILURE: Exit code {exit_code}."
            self.log_output.emit(f"❌ {summary}")

        self.finished_signal.emit(exit_code, summary)

    def _on_process_error(self, error):
        error_msg = self.process.errorString()
        self.log_output.emit(f"❌ OS ERROR: {error_msg}")
        self.finished_signal.emit(-1, f"QProcess OS Error: {error_msg}")


# =====================================================================
# Deep Cooldown Management
# =====================================================================
def get_deep_cooldown_path(db_dir: str) -> str:
    return os.path.join(db_dir, ".deep_integrity_cooldown.json")

def should_run_deep_check(db_dir: str, cooldown_days: int = 90) -> bool:
    path = get_deep_cooldown_path(db_dir)
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r") as f:
            data = json.load(f)
            last_run = data.get("last_run", 0)
            return (time.time() - last_run) > (cooldown_days * 86400)
    except Exception:
        return True

def save_deep_cooldown_timestamp(db_dir: str):
    path = get_deep_cooldown_path(db_dir)
    try:
        os.makedirs(db_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"last_run": time.time()}, f)
    except Exception:
        pass
