"""
supply_chain_auditor.py ver 1.0
For CogniFinance ver 1.9
Runs pip-audit in a background QProcess to check for known CVEs
in the project's dependencies.
"""
import sys
import os
import json
import logging
from PyQt6.QtCore import QObject, QProcess, pyqtSignal

logger = logging.getLogger(__name__)

class SupplyChainAuditor(QObject):
    """Runs pip-audit and emits the results."""
    log_output = pyqtSignal(str)
    # Emits (vulnerability_count, critical_count, json_results)
    audit_finished = pyqtSignal(int, int, list)

    def __init__(self, requirements_path: str, parent=None):
        super().__init__(parent)
        self.requirements_path = requirements_path
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._on_finished)

        # Buffer to hold the JSON output
        self._output_buffer = ""

    def start(self):
        """Spawns pip-audit in a separate OS process."""
        if not os.path.exists(self.requirements_path):
            self.log_output.emit(f"Supply Chain Audit skipped: {self.requirements_path} not found.")
            self.audit_finished.emit(0, 0, [])
            return

        self.log_output.emit("Starting Supply Chain Vulnerability Audit (pip-audit)...")

        # Use sys.executable to ensure we audit the exact venv the app is running in
        executable = sys.executable

        # --format json: Easy to parse programmatically
        # --progress-spinner off: Prevents terminal spinner chars from corrupting the output
        # --desc: Includes the vulnerability description in the output
        arguments = [
            "-m", "pip_audit",
            "-r", self.requirements_path,
            "--format", "json",
            "--progress-spinner", "off",
            "--desc"
        ]

        self.process.start(executable, arguments)

    def _read_output(self):
        raw_data = self.process.readAllStandardOutput().data().decode('utf-8', errors='replace')
        self._output_buffer += raw_data

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        if exit_status == QProcess.ExitStatus.CrashExit:
            self.log_output.emit("❌ Supply Chain Audit process crashed.")
            self.audit_finished.emit(-1, 0, [])
            return

        # pip-audit exits with 1 if vulnerabilities are found, 0 if clean.
        # We don't treat exit code 1 as a process failure.

        vuln_count = 0
        critical_count = 0
        vulnerabilities = []

        try:
            # pip-audit JSON output is a list of dictionaries
            results = json.loads(self._output_buffer)
            if isinstance(results, list):
                vulnerabilities = results
                vuln_count = len(vulnerabilities)

                # Count critical/high severity
                for v in vulnerabilities:
                    # pip-audit uses 'fix_versions' and 'description'
                    # We'll count anything that doesn't have an immediate fix as critical
                    if not v.get('fix_versions'):
                        critical_count += 1

                self.log_output.emit(f"✅ Supply Chain Audit complete. Found {vuln_count} known vulnerabilities.")
            else:
                self.log_output.emit("✅ Supply Chain Audit complete. No vulnerabilities found.")
        except json.JSONDecodeError:
            # If it's not valid JSON, pip-audit might have thrown an error (e.g., network issue)
            self.log_output.emit(f"⚠️ Supply Chain Audit could not parse results. Output: {self._output_buffer[:200]}")
            # Emit -1 to indicate a network/parse failure, not a vulnerability
            self.audit_finished.emit(-1, 0, [])
            return

        self.audit_finished.emit(vuln_count, critical_count, vulnerabilities)
