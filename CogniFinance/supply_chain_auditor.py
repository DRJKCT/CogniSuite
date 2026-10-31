"""
supply_chain_auditor.py ver 1.0
For CogniFinance ver 0.9.67
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
        # Keep stderr separate: warnings/retries on stderr would otherwise be
        # mixed into stdout and break JSON parsing.
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._read_error)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

        # Buffers: stdout holds the JSON, stderr is kept for diagnostics only
        self._output_buffer = ""
        self._error_buffer = ""
        self._done = False  # ensures audit_finished is emitted exactly once

    def _emit_finished(self, total: int, critical: int, details: list):
        if self._done:
            return
        self._done = True
        self.audit_finished.emit(total, critical, details)

    def start(self):
        """Spawns pip-audit in a separate OS process."""
        if not os.path.exists(self.requirements_path):
            self.log_output.emit(f"Supply Chain Audit skipped: {self.requirements_path} not found.")
            self._emit_finished(0, 0, [])
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

    def _read_error(self):
        raw_data = self.process.readAllStandardError().data().decode('utf-8', errors='replace')
        self._error_buffer += raw_data

    def _on_error(self, error: QProcess.ProcessError):
        # If the process can't launch at all, `finished` never fires. Without this
        # the audit chain would hang and the weekly/deep audits would never start.
        if error == QProcess.ProcessError.FailedToStart:
            self.log_output.emit(
                f"⚠️ Supply Chain Audit could not start ({self.process.errorString()})."
            )
            self._emit_finished(-1, 0, [])
        # Crashed is handled by _on_finished (CrashExit); other errors are
        # reported through finished or are non-fatal.

    @staticmethod
    def _flatten_results(results) -> list:
        """Normalizes pip-audit JSON into a flat list of one dict per vulnerability.

        Newer pip-audit emits {"dependencies": [{"name", "version", "vulns": [...]}]}.
        Older versions emit a bare list of vulnerability dicts.
        """
        if isinstance(results, dict):
            flat = []
            for dep in results.get("dependencies", []) or []:
                for vuln in dep.get("vulns", []) or []:
                    entry = dict(vuln)
                    entry["name"] = dep.get("name")
                    entry["version"] = dep.get("version")
                    flat.append(entry)
            return flat
        if isinstance(results, list):
            return [v for v in results if isinstance(v, dict)]
        raise ValueError("Unrecognized pip-audit output shape")

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
        # Drain anything still buffered in the process pipes.
        self._read_output()
        self._read_error()

        if exit_status == QProcess.ExitStatus.CrashExit:
            self.log_output.emit("❌ Supply Chain Audit process crashed.")
            self._emit_finished(-1, 0, [])
            return

        # pip-audit exits with 1 if vulnerabilities are found, 0 if clean.
        # We don't treat exit code 1 as a process failure; parsing decides.
        try:
            results = json.loads(self._output_buffer)
            vulnerabilities = self._flatten_results(results)
        except (json.JSONDecodeError, ValueError):
            detail = (self._error_buffer or self._output_buffer).strip()[:200]
            self.log_output.emit(
                f"⚠️ Supply Chain Audit could not parse results (exit {exit_code}). Output: {detail}"
            )
            # -1 signals a network/parse failure, not a vulnerability.
            self._emit_finished(-1, 0, [])
            return

        vuln_count = len(vulnerabilities)
        # "Critical" here means no fixed version is available, not a CVSS severity.
        critical_count = sum(1 for v in vulnerabilities if not v.get("fix_versions"))

        if vuln_count:
            self.log_output.emit(
                f"✅ Supply Chain Audit complete. Found {vuln_count} known vulnerabilities "
                f"({critical_count} with no fix available)."
            )
        else:
            self.log_output.emit("✅ Supply Chain Audit complete. No vulnerabilities found.")

        self._emit_finished(vuln_count, critical_count, vulnerabilities)
