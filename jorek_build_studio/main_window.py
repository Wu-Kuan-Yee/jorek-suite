"""Minimal Qt desktop interface for the JOREK suite builder."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .backend import (
    BackendError, cli_help, default_build_script, doctor_arguments, external_exec_runner,
    inspect_checkout, parse_doctor_output, resolve_build_script,
)
from .config import (
    build_arguments, default_config_path, load_config, normalize_config,
    save_config, validate_config,
)
from .legacy import import_legacy_text
from .stage_logs import StageLogDialog
from .remote import (
    RemoteProfileError, build_remote_command, connection_test_command,
    parse_remote_overrides, remote_artifact_path, remote_repo_path,
    scp_arguments, ssh_arguments, validate_connection_profile, validate_profile,
)


APP_TITLE = "JOREK Build Studio"


def _button(text: str, slot, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.clicked.connect(slot)
    return button


def _path_row(edit: QLineEdit, browse_slot, parent: QWidget) -> QWidget:
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    layout.addWidget(_button("Browse…", browse_slot, row))
    return row


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1120, 780)
        self.setMinimumSize(860, 600)
        self.config_path = default_config_path()
        try:
            self.config = load_config(self.config_path)
        except ValueError as exc:
            self.config = normalize_config()
            QMessageBox.warning(self, APP_TITLE, str(exc))

        script = self.config.get("build_script") or str(default_build_script())
        if Path(script).is_file():
            self.config["build_script"] = str(Path(script).resolve())
        if not self.config.get("work_dir"):
            self.config["work_dir"] = str(Path.home() / "jorek-build-work")

        self.catalog: Dict[str, Any] = {}
        self.feature_checks: Dict[str, QCheckBox] = {}
        self.family_checks: Dict[str, QCheckBox] = {}
        self.extra_checks: Dict[str, QCheckBox] = {}
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process_output)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)
        self.task_output = ""
        self.task_kind = ""
        self.task_log_path: Optional[Path] = None
        self.task_log = None
        self.task_started_at: Optional[datetime] = None
        self.current_command: List[str] = []
        self.task_stage_log_dir: Optional[Path] = None
        self.stage_records: Dict[str, Dict[str, Any]] = {}
        self.stage_log_handles: Dict[str, Any] = {}
        self.stage_stack: List[str] = []
        self.stage_dialogs: Dict[str, StageLogDialog] = {}
        self.history_stage_dialogs: List[QDialog] = []
        self.process_output_buffer = ""
        self.current_phase = ""
        self._cancel_requested = False
        self._stage_view_auto_selected = False
        self.stage_elapsed_timer = QTimer(self)
        self.stage_elapsed_timer.setInterval(1000)
        self.stage_elapsed_timer.timeout.connect(self._refresh_running_stage_rows)

        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)
        self._build_overview_tab()
        self._build_configuration_tab()
        self._build_advanced_tab()
        self._build_remote_tab()
        self._build_dependencies_tab()
        self._build_build_tab()
        self._build_history_tab()
        self._build_help_tab()
        self._build_status_bar()
        self._load_config_into_widgets()
        self.refresh_catalog(show_message=False)
        self.refresh_history()
        self._load_remote_profiles()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_overview_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        paths = QGroupBox("Paths")
        form = QFormLayout(paths)

        self.build_script_edit = QLineEdit()
        self.root_edit = QLineEdit()
        self.work_edit = QLineEdit()
        form.addRow("Build driver (build.py)", _path_row(
            self.build_script_edit, self._browse_build_script, paths))
        form.addRow("JOREK source tree", _path_row(
            self.root_edit, self._browse_jorek_root, paths))
        form.addRow("Work directory", _path_row(
            self.work_edit, self._browse_work_dir, paths))
        outer.addWidget(paths)

        actions = QHBoxLayout()
        actions.addWidget(_button("Inspect checkout", self.refresh_catalog, page))
        actions.addWidget(_button("Check environment", self.run_doctor, page))
        actions.addWidget(_button("Import legacy build.sh…", self.import_legacy, page))
        actions.addStretch(1)
        actions.addWidget(_button("Save configuration", self.save_current_config, page))
        outer.addLayout(actions)

        self.overview_text = QPlainTextEdit()
        self.overview_text.setReadOnly(True)
        self.overview_text.setPlaceholderText(
            "Select a build.py and JOREK source tree, then inspect the checkout "
            "or run the environment diagnostic."
        )
        self.overview_text.setMaximumBlockCount(5000)
        outer.addWidget(self.overview_text, 1)
        self.tabs.addTab(page, "Overview")

    def _build_configuration_tab(self) -> None:
        # The configuration page contains many controls.  Keeping the whole
        # page in a scroll area prevents QSplitter from shrinking QFormLayout
        # rows below their usable height on smaller windows / high-DPI screens.
        page = QScrollArea(self)
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        general = QGroupBox("Build configuration")
        form = QFormLayout(general)
        self.command_combo = QComboBox()
        self.command_combo.addItem("Build executables only", "build")
        self.command_combo.addItem("Build and package a single-file suite", "dist")
        self.command_combo.addItem("Build the full solver suite", "full")
        self.matrix_combo = QComboBox()
        for value, label in (
            ("one", "One model / one variant"),
            ("models", "One variant per model"),
            ("maximal", "All extensions enabled per model"),
            ("complete", "All model-extension combinations"),
        ):
            self.matrix_combo.addItem(label, value)
        self.model_combo = QComboBox()
        self.programs_combo = QComboBox()
        self.programs_combo.addItems(["main", "most", "all"])
        self.solver_combo = QComboBox()
        self.solver_combo.addItems([
            "mumps", "pastix6", "pastix5", "strumpack", "all", "none", "wsmp", "hips"
        ])
        self.deps_combo = QComboBox()
        self.deps_combo.addItems(["auto", "source", "system", "none"])
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["minimal", "standard", "full", "complete"])
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 4096)
        self.compression_spin = QSpinBox()
        self.compression_spin.setRange(0, 9)
        self.output_edit = QLineEdit()
        self.cc_edit = QLineEdit()
        self.cxx_edit = QLineEdit()
        self.fc_edit = QLineEdit()
        self.mpi_prefix_edit = QLineEdit()
        self.dep_prefixes_edit = QPlainTextEdit()
        self.dep_prefixes_edit.setMaximumHeight(58)

        form.addRow("Action", self.command_combo)
        form.addRow("Variant matrix", self.matrix_combo)
        form.addRow("JOREK model", self.model_combo)
        form.addRow("Programs", self.programs_combo)
        form.addRow("Direct solver", self.solver_combo)
        form.addRow("Dependency policy", self.deps_combo)
        form.addRow("Dependency profile", self.profile_combo)
        form.addRow("Parallel jobs", self.jobs_spin)
        form.addRow("C compiler (optional)", self.cc_edit)
        form.addRow("C++ compiler (optional)", self.cxx_edit)
        form.addRow("Fortran compiler (optional)", self.fc_edit)
        form.addRow("MPI prefix (optional)", self.mpi_prefix_edit)
        form.addRow("Additional dependency prefixes", self.dep_prefixes_edit)
        form.addRow("Suite output path (optional)", self.output_edit)
        form.addRow("Archive compression", self.compression_spin)

        feature_group = QGroupBox("Compile-time features")
        feature_layout = QGridLayout(feature_group)
        self.feature_container = feature_layout
        self.model_combo.currentIndexChanged.connect(self._update_model_toggle_controls)

        self.family_group = QGroupBox("Model extensions")
        self.family_layout = QGridLayout(self.family_group)
        self.family_hint = QLabel(
            "Select a single model to configure its model-family extensions."
        )
        self.family_layout.addWidget(self.family_hint, 0, 0, 1, 3)

        parameters = QGroupBox("Hard-coded integer parameters")
        parameter_form = QFormLayout(parameters)
        self.param_grid_edit = QPlainTextEdit()
        self.param_max_edit = QPlainTextEdit()
        self.set_params_edit = QPlainTextEdit()
        for editor in (self.param_grid_edit, self.param_max_edit, self.set_params_edit):
            editor.setMaximumHeight(64)
            editor.setPlaceholderText("One NAME=VALUE expression per line")
        self.param_grid_edit.setPlaceholderText("n_tor=1,3,5\nn_plane=8,16,32")
        self.param_max_edit.setPlaceholderText("n_tor=17\nn_plane=64")
        self.set_params_edit.setPlaceholderText("n_order=5")
        parameter_form.addRow("Cartesian parameter grid", self.param_grid_edit)
        parameter_form.addRow("Maximum-value expansion", self.param_max_edit)
        parameter_form.addRow("Fixed overrides", self.set_params_edit)

        switches = QGroupBox("Build policy")
        switch_layout = QGridLayout(switches)
        self.option_checks: Dict[str, QCheckBox] = {}
        switch_defs = [
            ("offline", "Offline: use cached sources only"),
            ("force", "Force dependency rebuild"),
            ("no_checksum", "Skip source checksum verification"),
            ("allow_shared", "Allow non-system shared libraries"),
            ("require_full_static", "Require full static linking"),
            ("no_full_static", "Do not request -static"),
            ("keep_going", "Continue after a failed variant"),
            ("verbose", "Show every executed command"),
            ("dry_run", "Dry run: print plan without building"),
        ]
        for index, (key, label) in enumerate(switch_defs):
            check = QCheckBox(label)
            self.option_checks[key] = check
            switch_layout.addWidget(check, index // 2, index % 2)

        # Use one page-level scrollbar instead of squeezing two splitter panes.
        # Each group therefore retains its natural size and remains fully usable.
        for widget in (general, feature_group, self.family_group, parameters, switches):
            outer.addWidget(widget)
        outer.addStretch(1)
        page.setWidget(content)
        self.tabs.addTab(page, "Configure")

    def _build_advanced_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        source_group = QGroupBox("Advanced source and toolchain options")
        form = QFormLayout(source_group)
        self.jorek_url_edit = QLineEdit()
        self.toolchain_prefix_edit = QLineEdit()
        self.source_dir_edit = QLineEdit()
        self.sources_edit = QPlainTextEdit()
        self.urls_edit = QPlainTextEdit()
        self.mirrors_edit = QPlainTextEdit()
        self.imas_root_edit = QLineEdit()
        self.catalyst_root_edit = QLineEdit()
        self.solver_prefix_edit = QLineEdit()
        for editor in (self.sources_edit, self.urls_edit, self.mirrors_edit):
            editor.setMaximumHeight(58)
            editor.setPlaceholderText("One NAME=VALUE mapping per line")
        form.addRow("JOREK Git URL", self.jorek_url_edit)
        form.addRow("Private toolchain prefix", self.toolchain_prefix_edit)
        form.addRow("Extracted dependency source directory", self.source_dir_edit)
        form.addRow("Per-dependency source trees", self.sources_edit)
        form.addRow("Primary source URL overrides", self.urls_edit)
        form.addRow("Fallback mirrors", self.mirrors_edit)
        form.addRow("IMAS installation prefix", self.imas_root_edit)
        form.addRow("ParaView Catalyst prefix", self.catalyst_root_edit)
        form.addRow("Full-suite solver prefix", self.solver_prefix_edit)

        flags = QGroupBox("Advanced build switches")
        flag_layout = QGridLayout(flags)
        definitions = [
            ("fetch_source", "Clone JOREK automatically if the source tree is absent"),
            ("everything", "Everything preset: complete matrix, all solvers and diagnostics"),
            ("no_verify", "Skip static dependency verification (not recommended)"),
            ("keep_source", "Keep staged per-variant source trees"),
            ("debug", "Debug build with runtime checks and symbols"),
            ("no_build_solvers", "Full build: use an existing solver prefix"),
        ]
        for index, (key, label) in enumerate(definitions):
            check = QCheckBox(label)
            self.extra_checks[key] = check
            flag_layout.addWidget(check, index // 2, index % 2)

        run_group = QGroupBox("Run an existing suite")
        run_form = QFormLayout(run_group)
        self.suite_path_edit = QLineEdit()
        self.stdin_file_edit = QLineEdit()
        self.run_model_edit = QLineEdit()
        self.run_variant_edit = QLineEdit()
        self.run_program_edit = QLineEdit()
        self.run_args_edit = QLineEdit()
        self.run_args_edit.setPlaceholderText("--mpi -np 8")
        run_form.addRow("Suite executable (optional)", _path_row(
            self.suite_path_edit, lambda: self._browse_file_for(
                self.suite_path_edit, "Select suite executable"), run_group))
        run_form.addRow("Model selector", self.run_model_edit)
        run_form.addRow("Variant name", self.run_variant_edit)
        run_form.addRow("Embedded program", self.run_program_edit)
        run_form.addRow("Standard input file", _path_row(
            self.stdin_file_edit, lambda: self._browse_file_for(
                self.stdin_file_edit, "Select namelist input"), run_group))
        run_form.addRow("Arguments passed to the suite", self.run_args_edit)

        outer.addWidget(source_group)
        outer.addWidget(flags)
        outer.addWidget(run_group)
        outer.addStretch(1)
        self.tabs.addTab(page, "Advanced")

    def _browse_directory_for(self, edit: QLineEdit, title: str) -> None:
        path = QFileDialog.getExistingDirectory(self, title, edit.text())
        if path:
            edit.setText(path)

    def _browse_file_for(self, edit: QLineEdit, title: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, title, edit.text(), "All files (*)")
        if path:
            edit.setText(path)

    def _build_remote_tab(self) -> None:
        page = QScrollArea()
        page.setWidgetResizable(True)
        content = QWidget()
        outer = QVBoxLayout(content)
        profile_row = QHBoxLayout()
        self.remote_profile_combo = QComboBox()
        self.remote_profile_combo.currentIndexChanged.connect(self._load_selected_remote_profile)
        profile_row.addWidget(QLabel("Saved profile"))
        profile_row.addWidget(self.remote_profile_combo, 1)
        profile_row.addWidget(_button("Save profile", self._save_remote_profile, page))
        profile_row.addWidget(_button("Remove", self._remove_remote_profile, page))
        outer.addLayout(profile_row)

        group = QGroupBox("Native POSIX remote builder")
        form = QFormLayout(group)
        self.remote_name_edit = QLineEdit("Linux x86_64")
        self.remote_destination_edit = QLineEdit()
        self.remote_destination_edit.setPlaceholderText("user@host or SSH config alias")
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1, 65535)
        self.remote_port_spin.setValue(22)
        self.remote_identity_edit = QLineEdit()
        self.remote_root_edit = QLineEdit("/tmp/jorek-build-studio")
        self.remote_work_edit = QLineEdit("/tmp/jorek-build-work")
        self.remote_jorek_root_edit = QLineEdit()
        self.remote_jorek_root_edit.setPlaceholderText(
            "Optional path to an existing JOREK checkout on the remote host")
        self.remote_python_edit = QLineEdit("python3")
        self.remote_repo_url_edit = QLineEdit(
            "https://github.com/Wu-Kuan-Yee/jorek-suite.git")
        self.remote_branch_edit = QLineEdit(
            self._local_git_branch() or "feature/build-studio-gui")
        self.remote_commit_edit = QLineEdit(self._local_git_commit())
        self.remote_artifact_dir_edit = QLineEdit(
            str(Path.home() / "JOREK-artifacts"))
        self.remote_host_key_combo = QComboBox()
        self.remote_host_key_combo.addItems(["yes", "accept-new", "no"])
        self.remote_ssh_options_edit = QPlainTextEdit()
        self.remote_ssh_options_edit.setMaximumHeight(55)
        self.remote_ssh_options_edit.setPlaceholderText(
            "Optional OpenSSH -o options, one per line")
        self.remote_overrides_edit = QPlainTextEdit()
        self.remote_overrides_edit.setMaximumHeight(95)
        self.remote_overrides_edit.setPlaceholderText(
            '{"toolchain_prefix": "/opt/jorek-toolchain", "mpi_prefix": "/opt/mpi"}')
        form.addRow("Profile name", self.remote_name_edit)
        form.addRow("SSH destination", self.remote_destination_edit)
        form.addRow("SSH port", self.remote_port_spin)
        form.addRow("Identity file (optional)", _path_row(
            self.remote_identity_edit, lambda: self._browse_file_for(
                self.remote_identity_edit, "Select SSH identity file"), group))
        form.addRow("Remote repository parent", self.remote_root_edit)
        form.addRow("Remote build work directory", self.remote_work_edit)
        form.addRow("Remote JOREK source (optional)", self.remote_jorek_root_edit)
        form.addRow("Remote Python executable", self.remote_python_edit)
        form.addRow("jorek-suite repository URL", self.remote_repo_url_edit)
        form.addRow("Repository branch", self.remote_branch_edit)
        form.addRow("Pinned repository commit", self.remote_commit_edit)
        form.addRow("Local artifact directory", _path_row(
            self.remote_artifact_dir_edit, lambda: self._browse_directory_for(
                self.remote_artifact_dir_edit, "Select artifact directory"), group))
        form.addRow("Host key policy", self.remote_host_key_combo)
        form.addRow("Additional SSH options", self.remote_ssh_options_edit)
        form.addRow("Remote build overrides (JSON)", self.remote_overrides_edit)
        outer.addWidget(group)

        note = QLabel(
            "Remote builds run natively on the remote OS and CPU. The remote host "
            "needs SSH access, Git, Python 3, a C/C++/Fortran toolchain and the "
            "required build utilities. Leave the JOREK source path empty to let "
            "the remote build fetch JOREK itself. Local absolute paths are not "
            "reused remotely; enter remote-specific compiler and prefix paths in "
            "the JSON override field. Supported keys include cc, cxx, fc, "
            "toolchain_prefix, mpi_prefix, solver_prefix, source_dir, imas_root, "
            "catalyst_root and dep_prefixes.")
        note.setWordWrap(True)
        outer.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addWidget(_button("Test SSH connection", self._test_remote_connection, page))
        buttons.addWidget(_button("Preview remote configuration", self.preview_remote_build, page))
        buttons.addWidget(_button("Build, verify and download", self._build_remote, page))
        buttons.addStretch(1)
        outer.addLayout(buttons)
        self.remote_preview = QPlainTextEdit()
        self.remote_preview.setReadOnly(True)
        self.remote_preview.setMaximumHeight(105)
        self.remote_preview.setPlaceholderText("The remote command preview appears here.")
        outer.addWidget(self.remote_preview)
        outer.addStretch(1)
        page.setWidget(content)
        self.tabs.addTab(page, "Remote")

    def _local_git_branch(self) -> str:
        try:
            script = Path(default_build_script()).resolve()
            result = subprocess.run(
                ["git", "-C", str(script.parent), "branch", "--show-current"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False, timeout=5)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _local_git_commit(self) -> str:
        try:
            script = Path(default_build_script()).resolve()
            result = subprocess.run(
                ["git", "-C", str(script.parent), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False, timeout=5)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _remote_profile_from_widgets(self) -> Dict[str, Any]:
        return {
            "name": self.remote_name_edit.text().strip(),
            "destination": self.remote_destination_edit.text().strip(),
            "port": self.remote_port_spin.value(),
            "identity_file": self.remote_identity_edit.text().strip(),
            "remote_root": self.remote_root_edit.text().strip(),
            "remote_work": self.remote_work_edit.text().strip(),
            "remote_jorek_root": self.remote_jorek_root_edit.text().strip(),
            "python": self.remote_python_edit.text().strip() or "python3",
            "repo_url": self.remote_repo_url_edit.text().strip(),
            "branch": self.remote_branch_edit.text().strip(),
            "commit": self.remote_commit_edit.text().strip(),
            "artifact_dir": self.remote_artifact_dir_edit.text().strip(),
            "host_key_policy": self.remote_host_key_combo.currentText(),
            "ssh_options": [
                line.strip() for line in self.remote_ssh_options_edit.toPlainText().splitlines()
                if line.strip()
            ],
            "remote_overrides": self.remote_overrides_edit.toPlainText().strip(),
        }

    def _apply_remote_profile(self, profile: Dict[str, Any]) -> None:
        self.remote_name_edit.setText(str(profile.get("name", "")))
        self.remote_destination_edit.setText(str(profile.get("destination", "")))
        self.remote_port_spin.setValue(int(profile.get("port", 22)))
        self.remote_identity_edit.setText(str(profile.get("identity_file", "")))
        self.remote_root_edit.setText(str(profile.get("remote_root", "/tmp/jorek-build-studio")))
        self.remote_work_edit.setText(str(profile.get("remote_work", "/tmp/jorek-build-work")))
        self.remote_jorek_root_edit.setText(str(profile.get("remote_jorek_root", "")))
        self.remote_python_edit.setText(str(profile.get("python", "python3")))
        self.remote_repo_url_edit.setText(str(profile.get("repo_url", "")))
        self.remote_branch_edit.setText(str(profile.get("branch", "")))
        self.remote_commit_edit.setText(str(profile.get("commit", "")))
        self.remote_artifact_dir_edit.setText(str(profile.get("artifact_dir", "")))
        self.remote_host_key_combo.setCurrentText(str(profile.get("host_key_policy", "yes")))
        self.remote_ssh_options_edit.setPlainText(
            "\n".join(profile.get("ssh_options", []) or []))
        self.remote_overrides_edit.setPlainText(str(profile.get("remote_overrides", "")))

    def _load_remote_profiles(self) -> None:
        profiles = self.config.get("remote_profiles", [])
        current = str(self.config.get("active_remote_profile", ""))
        self.remote_profile_combo.blockSignals(True)
        self.remote_profile_combo.clear()
        for profile in profiles:
            self.remote_profile_combo.addItem(str(profile.get("name", "")))
        index = self.remote_profile_combo.findText(current)
        if index >= 0:
            self.remote_profile_combo.setCurrentIndex(index)
        self.remote_profile_combo.blockSignals(False)
        if self.remote_profile_combo.count():
            self._load_selected_remote_profile(self.remote_profile_combo.currentIndex())

    def _load_selected_remote_profile(self, index: int) -> None:
        if index < 0:
            return
        name = self.remote_profile_combo.itemText(index)
        for profile in self.config.get("remote_profiles", []):
            if profile.get("name") == name:
                self._apply_remote_profile(profile)
                self.config["active_remote_profile"] = name
                return

    def _save_remote_profile(self) -> bool:
        profile = self._remote_profile_from_widgets()
        errors = validate_profile(profile)
        if errors:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errors))
            return False
        profiles = list(self.config.get("remote_profiles", []))
        replaced = False
        for index, old in enumerate(profiles):
            if old.get("name") == profile["name"]:
                profiles[index] = profile
                replaced = True
                break
        if not replaced:
            profiles.append(profile)
        self.config["remote_profiles"] = profiles
        self.config["active_remote_profile"] = profile["name"]
        try:
            save_config(self.config, self.config_path)
        except OSError as exc:
            QMessageBox.warning(self, APP_TITLE, "Could not save remote profile:\n%s" % exc)
            return False
        self._load_remote_profiles()
        self.statusBar().showMessage("Remote profile saved", 5000)
        return True

    def _remove_remote_profile(self) -> None:
        name = self.remote_profile_combo.currentText()
        if not name:
            return
        self.config["remote_profiles"] = [
            item for item in self.config.get("remote_profiles", [])
            if item.get("name") != name
        ]
        self.config["active_remote_profile"] = ""
        save_config(self.config, self.config_path)
        self._load_remote_profiles()

    def _test_remote_connection(self) -> None:
        profile = self._remote_profile_from_widgets()
        errors = validate_connection_profile(profile)
        if errors:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errors))
            return
        ssh = shutil.which("ssh")
        if not ssh:
            QMessageBox.warning(self, APP_TITLE, "OpenSSH client (ssh) was not found in PATH.")
            return
        command = connection_test_command(profile)
        self.remote_preview.setPlainText(command)
        try:
            args = ssh_arguments(profile, command)
        except RemoteProfileError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._start_external_process(ssh, args, "remote-test")

    def _prepare_remote_build_plan(self) -> Optional[Dict[str, Any]]:
        profile = self._remote_profile_from_widgets()
        errors = validate_profile(profile)
        if errors:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errors))
            return None
        ssh = shutil.which("ssh")
        scp = shutil.which("scp")
        if not ssh or not scp:
            QMessageBox.warning(self, APP_TITLE, "Both ssh and scp must be available in PATH.")
            return None
        if not self._save_remote_profile():
            return None

        cfg = self._collect_config()
        repo = remote_repo_path(profile)
        remote_cfg = dict(cfg)
        remote_cfg["build_script"] = str(PurePosixPath(repo) / "build.py")
        remote_cfg["work_dir"] = profile["remote_work"]
        remote_cfg["jorek_root"] = profile.get("remote_jorek_root", "")
        remote_cfg["fetch_source"] = not bool(remote_cfg["jorek_root"])
        # Local absolute paths and compiler locations must never leak into a
        # native build on another machine. Remote-specific values are explicit.
        for key in ("cc", "cxx", "fc", "mpi_prefix", "toolchain_prefix",
                    "source_dir", "imas_root", "catalyst_root", "solver_prefix"):
            remote_cfg[key] = ""
        remote_cfg["dep_prefixes"] = []
        remote_cfg["sources"] = ""
        try:
            remote_cfg.update(parse_remote_overrides(profile.get("remote_overrides", "")))
        except RemoteProfileError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return None
        remote_cfg["output"] = ""
        remote_cfg["suite_path"] = ""
        remote_cfg["stdin_file"] = ""
        remote_cfg["run_args"] = ""

        selected_action = self.command_combo.currentData()
        if selected_action == "build":
            QMessageBox.information(
                self, APP_TITLE,
                "Remote build-and-download creates a single-file suite. "
                "Choose 'Build and package' or 'Full solver suite' first.")
            return None
        action = "full" if selected_action == "full" else "dist"
        try:
            build_args = build_arguments(remote_cfg, action)
            verify_args = build_arguments(remote_cfg, "verify")
            remote_command = build_remote_command(profile, build_args, verify_args)
            remote_artifact = remote_artifact_path(profile)
            local_dir = Path(profile["artifact_dir"]).expanduser()
            safe_name = re.sub(
                r"[^A-Za-z0-9_.-]+", "-", profile["name"]).strip("-") or "remote"
            local_artifact = local_dir / (
                "jorek-suite-%s-%s" % (safe_name, profile["commit"][:8]))
            ssh_args = ssh_arguments(profile, remote_command)
            download_args = scp_arguments(
                profile, remote_artifact, str(local_artifact))
            cleanup_args = ssh_arguments(
                profile, "rm -f " + shlex.quote(remote_artifact))
        except (OSError, ValueError, RemoteProfileError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return None

        return {
            "profile": profile,
            "remote_config": remote_cfg,
            "build_args": build_args,
            "verify_args": verify_args,
            "remote_command": remote_command,
            "remote_artifact": remote_artifact,
            "local_artifact": local_artifact,
            "local_dir": local_dir,
            "ssh": ssh,
            "ssh_args": ssh_args,
            "download_args": download_args,
            "cleanup_args": cleanup_args,
        }

    def _show_remote_build_preview(
        self, plan: Dict[str, Any], confirm_build: bool,
    ) -> bool:
        profile = plan["profile"]
        summary = {
            "remote_target": profile["destination"],
            "ssh_port": profile["port"],
            "remote_repository": profile["repo_url"],
            "repository_branch": profile["branch"],
            "pinned_commit": profile["commit"],
            "remote_repository_path": remote_repo_path(profile),
            "remote_work_directory": profile["remote_work"],
            "remote_jorek_source": profile.get("remote_jorek_root", "") or
                "(fetch JOREK source automatically)",
            "remote_python": profile.get("python", "python3"),
            "remote_artifact_staging_path": plan["remote_artifact"],
            "local_artifact_path": str(plan["local_artifact"]),
            "remote_overrides": profile.get("remote_overrides", ""),
            "effective_build_configuration": plan["remote_config"],
            "build_arguments": plan["build_args"],
            "verification_arguments": plan["verify_args"],
        }

        dialog = QDialog(self)
        dialog.setWindowTitle("Remote build configuration preview")
        dialog.resize(920, 700)
        outer = QVBoxLayout(dialog)
        note = QLabel(
            "Review the final configuration after local paths have been removed "
            "and remote overrides have been applied. No remote command has run yet.")
        note.setWordWrap(True)
        outer.addWidget(note)

        tabs = QTabWidget(dialog)
        settings_view = QPlainTextEdit()
        settings_view.setReadOnly(True)
        settings_view.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
        tabs.addTab(settings_view, "Effective configuration")

        command_view = QPlainTextEdit()
        command_view.setReadOnly(True)
        command_view.setPlainText(plan["remote_command"])
        tabs.addTab(command_view, "Remote command")

        build_view = QPlainTextEdit()
        build_view.setReadOnly(True)
        build_view.setPlainText(
            shlex.join([profile.get("python", "python3"),
                        remote_repo_path(profile) + "/build.py"] + plan["build_args"]))
        tabs.addTab(build_view, "Build arguments")
        outer.addWidget(tabs, 1)

        if confirm_build:
            buttons = QDialogButtonBox(dialog)
            buttons.addButton("Build now", QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            outer.addWidget(buttons)
        else:
            close_button = QPushButton("Close")
            close_button.clicked.connect(dialog.accept)
            outer.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def preview_remote_build(self) -> None:
        plan = self._prepare_remote_build_plan()
        if plan is None:
            return
        self.remote_preview.setPlainText(plan["remote_command"])
        self._show_remote_build_preview(plan, confirm_build=False)

    def _build_remote(self) -> None:
        plan = self._prepare_remote_build_plan()
        if plan is None:
            return
        self.remote_preview.setPlainText(plan["remote_command"])
        if not self._show_remote_build_preview(plan, confirm_build=True):
            return
        try:
            plan["local_dir"].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_TITLE, "Cannot create artifact directory:\n%s" % exc)
            return
        self.remote_artifact_path = plan["remote_artifact"]
        self.remote_ssh_executable = plan["ssh"]
        self.remote_cleanup_args = plan["cleanup_args"]
        self.remote_download_args = plan["download_args"]
        self.remote_local_artifact = plan["local_artifact"]
        self._start_external_process(
            plan["ssh"], plan["ssh_args"], "remote-build")


    def _build_dependencies_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        controls = QGroupBox("Dependency operation")
        form = QFormLayout(controls)
        self.dep_action_combo = QComboBox()
        self.dep_action_combo.addItems(["list", "graph", "fetch", "build", "verify"])
        self.dep_only_edit = QLineEdit()
        self.dep_only_edit.setPlaceholderText("Optional comma-separated dependency names")
        self.bootstrap_deps_check = QCheckBox("Build the full core dependency profile during bootstrap")
        self.bootstrap_deps_check.setChecked(True)
        form.addRow("Action", self.dep_action_combo)
        form.addRow("Only these dependencies", self.dep_only_edit)
        row = QHBoxLayout()
        row.addWidget(_button("Run dependency action", self.run_dependency_action, page))
        row.addWidget(_button("Refresh dependency catalogue", self.refresh_catalog, page))
        row.addStretch(1)
        outer.addWidget(controls)
        outer.addWidget(self.bootstrap_deps_check)
        row.addWidget(_button("Bootstrap toolchain", self.bootstrap_toolchain, page))
        outer.addLayout(row)
        self.dependency_table = QTableWidget(0, 5)
        self.dependency_table.setHorizontalHeaderLabels(
            ["Name", "Version", "License", "Builder", "Platforms"]
        )
        self.dependency_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.dependency_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self.dependency_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dependency_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        outer.addWidget(self.dependency_table, 1)
        self.tabs.addTab(page, "Dependencies")

    def _build_build_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        actions = QHBoxLayout()
        actions.addWidget(_button("Preview matrix", self.preview_matrix, page))
        actions.addWidget(_button("Start build", self.start_build, page))
        actions.addWidget(_button("Verify suite", self.verify_suite, page))
        actions.addWidget(_button("Run suite", self.run_suite, page))
        actions.addWidget(_button("Cancel", self.cancel_process, page))
        actions.addStretch(1)
        self.task_label = QLabel("Idle")
        actions.addWidget(self.task_label)
        outer.addLayout(actions)

        progress_row = QHBoxLayout()
        self.progress_label = QLabel("No active build phase")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        progress_row.addWidget(self.progress_label)
        progress_row.addWidget(self.progress_bar, 1)
        outer.addLayout(progress_row)

        self.build_views = QTabWidget()
        overview_page = QWidget()
        overview_layout = QVBoxLayout(overview_page)

        self.matrix_table = QTableWidget(0, 5)
        self.matrix_table.setHorizontalHeaderLabels(
            ["Variant", "Model", "Solver", "Features", "Parameters"])
        self.matrix_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.matrix_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        self.matrix_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.matrix_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        overview_layout.addWidget(QLabel("Planned variants"))
        overview_layout.addWidget(self.matrix_table, 1)

        overview_layout.addWidget(QLabel("Build output"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(12000)
        self.log_view.setPlaceholderText(
            "Build overview and diagnostics will appear here. "
            "Compiler and dependency output is available in Detailed logs.")
        overview_layout.addWidget(self.log_view, 2)
        self.build_views.addTab(overview_page, "Overview")

        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        detail_actions = QHBoxLayout()
        detail_actions.addWidget(QLabel(
            "Each dependency, JOREK variant and packaging stage has its own log. "
            "Double-click a row to follow or reopen it."))
        detail_actions.addStretch(1)
        detail_actions.addWidget(_button(
            "Open selected log", self._open_selected_stage_log, details_page))
        details_layout.addLayout(detail_actions)

        self.stage_table = QTableWidget(0, 5)
        self.stage_table.setHorizontalHeaderLabels(
            ["Build part", "Category", "Status", "Elapsed", "Detailed log"])
        self.stage_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.stage_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.stage_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.stage_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.stage_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self.stage_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stage_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stage_table.cellDoubleClicked.connect(self._open_stage_log)
        details_layout.addWidget(self.stage_table, 1)
        self.build_views.addTab(details_page, "Detailed logs")

        outer.addWidget(self.build_views, 1)
        self.tabs.addTab(page, "Build")

    def _build_history_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(_button("Refresh history", self.refresh_history, page))
        row.addWidget(_button("View selected stage logs", self._open_selected_history_stage_logs, page))
        row.addStretch(1)
        outer.addLayout(row)
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(
            ["Started (UTC)", "Task", "Result", "Exit code", "Log file", "Stage logs"])
        self.history_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.cellDoubleClicked.connect(self._open_history_log)
        outer.addWidget(self.history_table, 1)
        self.tabs.addTab(page, "History")

    def refresh_history(self) -> None:
        path = self.config_path.parent / "history.jsonl"
        entries = []
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            entries.append(item)
                    except json.JSONDecodeError:
                        continue
            except OSError as exc:
                self.statusBar().showMessage("Cannot read task history: %s" % exc, 5000)
        entries = entries[-500:][::-1]
        self.history_table.setRowCount(len(entries))
        for row, item in enumerate(entries):
            values = [
                item.get("started_at", ""),
                item.get("task", ""),
                "Success" if item.get("success") else "Failed",
                str(item.get("exit_code", "")),
                item.get("log", ""),
                str(len(item.get("stage_logs", []))),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 4:
                    cell.setData(Qt.ItemDataRole.UserRole, item.get("log", ""))
                elif column == 5:
                    cell.setData(Qt.ItemDataRole.UserRole, item.get("stage_logs", []))
                self.history_table.setItem(row, column, cell)

    def _record_task(self, exit_code: int, normal_exit: bool) -> None:
        finished = datetime.now(timezone.utc)
        started = self.task_started_at or finished
        stage_logs = []
        if self.task_kind not in ("remote-download", "remote-cleanup", "remote-test"):
            for record in self.stage_records.values():
                stage_logs.append({
                    "id": record.get("id", ""),
                    "label": record.get("label", ""),
                    "category": record.get("category", ""),
                    "status": record.get("status", ""),
                    "log": record.get("log", ""),
                    "remote_log_path": record.get("remote_log_path", ""),
                    "elapsed_seconds": record.get("elapsed_seconds", 0.0),
                })
        item = {
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": max(0.0, (finished - started).total_seconds()),
            "task": self.task_kind,
            "success": bool(normal_exit and exit_code == 0),
            "exit_code": int(exit_code),
            "command": self.current_command,
            "log": str(self.task_log_path or ""),
            "stage_logs": stage_logs,
        }
        path = self.config_path.parent / "history.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError as exc:
            self.log_view.appendPlainText("Could not write task history: %s" % exc)
        self.refresh_history()

    def _open_history_log(self, row: int, column: int) -> None:
        if column == 5:
            self._open_history_stage_logs(row)
            return
        cell = self.history_table.item(row, 4)
        path = cell.data(Qt.ItemDataRole.UserRole) if cell else ""
        if path and Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _open_selected_history_stage_logs(self) -> None:
        row = self.history_table.currentRow()
        if row >= 0:
            self._open_history_stage_logs(row)

    def _open_history_stage_logs(self, row: int) -> None:
        cell = self.history_table.item(row, 5)
        records = cell.data(Qt.ItemDataRole.UserRole) if cell else []
        if not records:
            QMessageBox.information(self, APP_TITLE, "This task has no per-stage logs.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Build stages — historical task")
        dialog.resize(820, 480)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Build part", "Category", "Status", "Log file"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setRowCount(len(records))
        for index, record in enumerate(records):
            values = [
                record.get("label", ""),
                record.get("category", ""),
                record.get("status", ""),
                record.get("log", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 3:
                    item.setData(Qt.ItemDataRole.UserRole, record.get("log", ""))
                table.setItem(index, column, item)
        table.cellDoubleClicked.connect(
            lambda selected_row, _column, items=records:
                self._show_stage_log_dialog(
                    items[selected_row].get("label", "Build stage"),
                    items[selected_row].get("log", ""), running=False))
        layout.addWidget(table, 1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.close)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.history_stage_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, target=dialog:
                self.history_stage_dialogs.remove(target)
                if target in self.history_stage_dialogs else None)
        dialog.show()


    def _build_help_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        row = QHBoxLayout()
        self.help_command_combo = QComboBox()
        self.help_command_combo.addItems([
            "General", "doctor", "deps", "bootstrap", "matrix", "build",
            "dist", "full", "verify", "run", "env", "clean", "version",
        ])
        row.addWidget(QLabel("Help topic"))
        row.addWidget(self.help_command_combo)
        row.addWidget(_button("Load help", self.load_help, page))
        row.addStretch(1)
        outer.addLayout(row)
        self.help_text = QPlainTextEdit()
        self.help_text.setReadOnly(True)
        self.help_text.setPlaceholderText("The selected build.py help text will appear here.")
        outer.addWidget(self.help_text, 1)
        self.tabs.addTab(page, "Help")

    def _build_status_bar(self) -> None:
        self.statusBar().showMessage("Ready")
        self.log_path_label = QLabel("No task log")
        self.statusBar().addPermanentWidget(self.log_path_label)

    # ------------------------------------------------------------------
    # Configuration and catalog
    # ------------------------------------------------------------------
    def _load_config_into_widgets(self) -> None:
        cfg = self.config
        self.build_script_edit.setText(cfg.get("build_script", ""))
        self.root_edit.setText(cfg.get("jorek_root", ""))
        self.work_edit.setText(cfg.get("work_dir", ""))
        self._set_combo_data(self.command_combo, cfg.get("command", "dist"))
        self._set_combo_data(self.matrix_combo, cfg.get("matrix", "one"))
        self._set_combo_text(self.model_combo, cfg.get("model", "303"))
        self._set_combo_text(self.programs_combo, cfg.get("programs", "main"))
        self._set_combo_text(self.solver_combo, cfg.get("solver", "mumps"))
        self._set_combo_text(self.deps_combo, cfg.get("deps_policy", "auto"))
        self._set_combo_text(self.profile_combo, cfg.get("profile", "full"))
        self.jobs_spin.setValue(int(cfg.get("jobs", 1)))
        self.compression_spin.setValue(int(cfg.get("compression", 9)))
        self.output_edit.setText(cfg.get("output", ""))
        self.cc_edit.setText(cfg.get("cc", ""))
        self.cxx_edit.setText(cfg.get("cxx", ""))
        self.fc_edit.setText(cfg.get("fc", ""))
        self.mpi_prefix_edit.setText(cfg.get("mpi_prefix", ""))
        self.dep_prefixes_edit.setPlainText("\n".join(cfg.get("dep_prefixes", [])))
        self.param_grid_edit.setPlainText(cfg.get("param_grid", ""))
        self.param_max_edit.setPlainText(cfg.get("param_max", ""))
        self.set_params_edit.setPlainText(cfg.get("set_params", ""))
        self.dep_action_combo.setCurrentText(cfg.get("dep_action", "list"))
        self.dep_only_edit.setText(cfg.get("dep_only", ""))
        self.bootstrap_deps_check.setChecked(bool(cfg.get("bootstrap_with_deps", True)))
        self.jorek_url_edit.setText(cfg.get("jorek_url", ""))
        self.toolchain_prefix_edit.setText(cfg.get("toolchain_prefix", ""))
        self.source_dir_edit.setText(cfg.get("source_dir", ""))
        self.sources_edit.setPlainText(cfg.get("sources", ""))
        self.urls_edit.setPlainText(cfg.get("urls", ""))
        self.mirrors_edit.setPlainText(cfg.get("mirrors", ""))
        self.imas_root_edit.setText(cfg.get("imas_root", ""))
        self.catalyst_root_edit.setText(cfg.get("catalyst_root", ""))
        self.solver_prefix_edit.setText(cfg.get("solver_prefix", ""))
        self.suite_path_edit.setText(cfg.get("suite_path", ""))
        self.stdin_file_edit.setText(cfg.get("stdin_file", ""))
        self.run_model_edit.setText(cfg.get("run_model", ""))
        self.run_variant_edit.setText(cfg.get("run_variant", ""))
        self.run_program_edit.setText(cfg.get("run_program", ""))
        self.run_args_edit.setText(cfg.get("run_args", ""))
        for key, check in self.extra_checks.items():
            check.setChecked(bool(cfg.get(key, False)))
        for key, check in self.option_checks.items():
            check.setChecked(bool(cfg.get(key, False)))

    def _collect_config(self) -> Dict[str, Any]:
        cfg = dict(self.config)
        cfg.update({
            "build_script": self.build_script_edit.text().strip(),
            "jorek_root": self.root_edit.text().strip(),
            "work_dir": self.work_edit.text().strip(),
            "command": self.command_combo.currentData(),
            "matrix": self.matrix_combo.currentData(),
            "model": self.model_combo.currentData() or self.model_combo.currentText(),
            "programs": self.programs_combo.currentText(),
            "solver": self.solver_combo.currentText(),
            "deps_policy": self.deps_combo.currentText(),
            "profile": self.profile_combo.currentText(),
            "jobs": self.jobs_spin.value(),
            "compression": self.compression_spin.value(),
            "output": self.output_edit.text().strip(),
            "cc": self.cc_edit.text().strip(),
            "cxx": self.cxx_edit.text().strip(),
            "fc": self.fc_edit.text().strip(),
            "mpi_prefix": self.mpi_prefix_edit.text().strip(),
            "dep_prefixes": [line.strip() for line in self.dep_prefixes_edit.toPlainText().splitlines()
                             if line.strip()],
            "param_grid": self.param_grid_edit.toPlainText(),
            "param_max": self.param_max_edit.toPlainText(),
            "set_params": self.set_params_edit.toPlainText(),
            "dep_action": self.dep_action_combo.currentText(),
            "dep_only": self.dep_only_edit.text().strip(),
            "bootstrap_with_deps": self.bootstrap_deps_check.isChecked(),
            "jorek_url": self.jorek_url_edit.text().strip(),
            "toolchain_prefix": self.toolchain_prefix_edit.text().strip(),
            "source_dir": self.source_dir_edit.text().strip(),
            "sources": self.sources_edit.toPlainText(),
            "urls": self.urls_edit.toPlainText(),
            "mirrors": self.mirrors_edit.toPlainText(),
            "imas_root": self.imas_root_edit.text().strip(),
            "catalyst_root": self.catalyst_root_edit.text().strip(),
            "solver_prefix": self.solver_prefix_edit.text().strip(),
            "suite_path": self.suite_path_edit.text().strip(),
            "stdin_file": self.stdin_file_edit.text().strip(),
            "run_model": self.run_model_edit.text().strip(),
            "run_variant": self.run_variant_edit.text().strip(),
            "run_program": self.run_program_edit.text().strip(),
            "run_args": self.run_args_edit.text().strip(),
            "feature_states": {name: check.isChecked()
                               for name, check in self.feature_checks.items()},
            "feature_defaults": self.catalog.get("feature_defaults", {}),
            "family_toggle_states": {name: check.isChecked() for name, check in self.family_checks.items()},
            "family_toggle_defaults": self.catalog.get("model_toggle_defaults", {}).get(
                str(self.model_combo.currentData() or ""), {}),
        })
        for key, check in self.option_checks.items():
            cfg[key] = check.isChecked()
        for key, check in self.extra_checks.items():
            cfg[key] = check.isChecked()
        return normalize_config(cfg)

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: str) -> None:
        index = combo.findText(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _browse_build_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select build.py", self.build_script_edit.text(),
            "Python files (build.py);;All files (*)")
        if path:
            self.build_script_edit.setText(path)
            self.refresh_catalog(show_message=False)

    def _browse_jorek_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select JOREK source tree", self.root_edit.text())
        if path:
            self.root_edit.setText(path)
            self.refresh_catalog(show_message=False)

    def _browse_work_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select build work directory", self.work_edit.text())
        if path:
            self.work_edit.setText(path)

    def refresh_catalog(self, show_message: bool = True) -> None:
        script = self.build_script_edit.text().strip()
        if not script:
            return
        try:
            catalog = inspect_checkout(script, self.root_edit.text().strip())
        except (BackendError, OSError, SyntaxError) as exc:
            if show_message:
                QMessageBox.warning(self, APP_TITLE, str(exc))
            self.overview_text.setPlainText("Checkout inspection failed:\n%s" % exc)
            return
        self.catalog = catalog
        current_model = (self.model_combo.currentData() or self.model_combo.currentText()
                        or self.config.get("model", "303"))
        self.model_combo.clear()
        self.model_combo.addItem("All models", "all")
        for model in catalog["models"]:
            self.model_combo.addItem("Model " + model, model)
        self._set_combo_data(self.model_combo, current_model)

        for index in reversed(range(self.feature_container.count())):
            item = self.feature_container.itemAt(index)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            self.feature_container.removeItem(item)
        self.feature_checks.clear()
        defaults = catalog.get("feature_defaults", {})
        saved_states = self.config.get("feature_states", {})
        for index, name in enumerate(catalog.get("features", [])):
            check = QCheckBox(name)
            check.setChecked(bool(saved_states.get(name, defaults.get(name, False))))
            check.setToolTip(
                "Default: %s. Unchecking/enabling this option creates an explicit "
                "override relative to the JOREK source defaults." %
                ("enabled" if defaults.get(name, False) else "disabled")
            )
            self.feature_checks[name] = check
            self.feature_container.addWidget(check, index // 3, index % 3)
        self._populate_dependency_table(catalog.get("dependencies", []))
        self._update_model_toggle_controls()
        report = {
            "build_script": catalog["build_script"],
            "system": catalog["platform"]["system"],
            "release": catalog["platform"]["release"],
            "machine": catalog["platform"]["machine"],
            "python": catalog["platform"]["python"],
            "model_count": len(catalog["models"]),
            "feature_count": len(catalog["features"]),
            "dependency_count": len(catalog["dependencies"]),
        }
        self.overview_text.setPlainText(
            "Build driver: {build_script}\n"
            "Host: {system} {release} ({machine})\n"
            "Python: {python}\n"
            "Models discovered: {model_count}\n"
            "Compile-time feature switches: {feature_count}\n"
            "Dependency recipes: {dependency_count}\n\n"
            "The model and dependency catalogues are read from the selected "
            "checkout. No build has been started.".format(**report)
        )
        self.statusBar().showMessage(
            "Inspected %d models and %d dependency recipes" %
            (len(catalog["models"]), len(catalog["dependencies"])), 5000)
        if show_message:
            QMessageBox.information(self, APP_TITLE, "Checkout metadata refreshed.")

    def _update_model_toggle_controls(self, *_args) -> None:
        for name, check in self.family_checks.items():
            self.config.setdefault("family_toggle_states", {})[name] = check.isChecked()
        self.family_checks.clear()
        while self.family_layout.count():
            item = self.family_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        model = str(self.model_combo.currentData() or "all")
        if model == "all":
            self.family_hint = QLabel(
                "Choose one model to edit its extensions. For all models, use "
                "the Maximal or Complete matrix.")
            self.family_layout.addWidget(self.family_hint, 0, 0, 1, 3)
            return
        if not self.root_edit.text().strip() or not Path(self.root_edit.text().strip()).is_dir():
            self.family_hint = QLabel(
                "Select or clone the JOREK source tree to read this model's true defaults.")
            self.family_layout.addWidget(self.family_hint, 0, 0, 1, 3)
            return
        toggles = self.catalog.get("model_toggles", {}).get(model, [])
        defaults = self.catalog.get("model_toggle_defaults", {}).get(model, {})
        if not toggles:
            self.family_hint = QLabel("This model declares no family extensions.")
            self.family_layout.addWidget(self.family_hint, 0, 0, 1, 3)
            return
        self.family_hint = QLabel(
            "Values below override the selected model's source defaults.")
        self.family_layout.addWidget(self.family_hint, 0, 0, 1, 3)
        saved = self.config.get("family_toggle_states", {})
        for index, name in enumerate(toggles):
            check = QCheckBox(name)
            check.setChecked(bool(saved.get(name, defaults.get(name, False))))
            check.setToolTip("Source default: %s" %
                             ("enabled" if defaults.get(name, False) else "disabled"))
            self.family_checks[name] = check
            self.family_layout.addWidget(check, 1 + index // 3, index % 3)

    def _populate_dependency_table(self, dependencies: List[Dict[str, Any]]) -> None:
        self.dependency_table.setRowCount(len(dependencies))
        for row, item in enumerate(dependencies):
            values = [
                item["name"], item["version"], item["license"], item["builder"],
                ", ".join(item.get("platforms", [])) or "All declared platforms",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0 and item.get("note"):
                    cell.setToolTip(item["note"])
                self.dependency_table.setItem(row, column, cell)

    def save_current_config(self) -> None:
        self.config = self._collect_config()
        try:
            path = save_config(self.config, self.config_path)
        except OSError as exc:
            QMessageBox.critical(self, APP_TITLE, "Could not save configuration:\n%s" % exc)
            return
        self.statusBar().showMessage("Configuration saved: %s" % path, 5000)

    # ------------------------------------------------------------------
    # Diagnostics and legacy import
    # ------------------------------------------------------------------
    def run_doctor(self) -> None:
        cfg = self._collect_config()
        if not cfg["build_script"]:
            QMessageBox.warning(self, APP_TITLE, "Select build.py first.")
            return
        self._start_process(doctor_arguments(cfg), "doctor")

    def import_legacy(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import legacy build.sh", "", "Shell scripts (build.sh *.sh);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, APP_TITLE, "Cannot read file:\n%s" % exc)
            return
        mapped, ignored = import_legacy_text(text)
        if not mapped.keys() - {"feature_states"}:
            summary = "No directly mappable literal assignments were found."
        else:
            summary = "Recognized settings:\n" + "\n".join(
                "%s = %s" % (key, value) for key, value in sorted(mapped.items())
                if key != "feature_states")
        if mapped.get("feature_states"):
            summary += "\nFeature overrides: " + ", ".join(
                "%s=%s" % (key, value) for key, value in sorted(mapped["feature_states"].items()))
        details = "\n".join(ignored) if ignored else "No unrecognized lines."
        answer = QMessageBox(self)
        answer.setWindowTitle("Legacy build.sh import")
        answer.setIcon(QMessageBox.Icon.Information)
        answer.setText(summary + "\n\nThe script was read as text only; it was not executed.")
        answer.setDetailedText(details)
        answer.setStandardButtons(QMessageBox.StandardButton.Apply |
                                  QMessageBox.StandardButton.Cancel)
        if answer.exec() != QMessageBox.StandardButton.Apply:
            return
        self.config.update(mapped)
        self._load_config_into_widgets()
        self.refresh_catalog(show_message=False)
        self.statusBar().showMessage("Imported literal settings from %s" % path, 5000)

    # ------------------------------------------------------------------
    # Build process control
    # ------------------------------------------------------------------
    def _validate_for_action(self, command: str) -> Optional[Dict[str, Any]]:
        cfg = self._collect_config()
        errors = validate_config(cfg, require_source=command in ("build", "dist", "full", "matrix"))
        if errors:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errors))
            return None
        if command in ("build", "dist", "full", "matrix") and not cfg["jorek_root"] and not cfg.get("fetch_source"):
            QMessageBox.warning(
                self, APP_TITLE,
                "Select the JOREK source directory on the Overview tab. "
                "The build driver can auto-detect it, but an explicit path is safer.")
            return None
        return cfg

    def preview_matrix(self) -> None:
        cfg = self._validate_for_action("matrix")
        if cfg is None:
            return
        try:
            args = build_arguments(cfg, "matrix")
        except ValueError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.matrix_table.setRowCount(0)
        self._start_process(args, "matrix-preview")

    def start_build(self) -> None:
        command = self.command_combo.currentData()
        cfg = self._validate_for_action(command)
        if cfg is None:
            return
        if command == "full":
            warning = QMessageBox.question(
                self, "Full solver build",
                "The full action builds several solver stacks and can take a long time "
                "and use substantial disk space. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if warning != QMessageBox.StandardButton.Yes:
                return
        try:
            args = build_arguments(cfg, command)
        except ValueError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._start_process(args, "build")

    def run_suite(self) -> None:
        cfg = self._collect_config()
        if not cfg["build_script"] or not Path(cfg["build_script"]).is_file():
            QMessageBox.warning(self, APP_TITLE, "Select a valid build.py first.")
            return
        if cfg.get("stdin_file") and not Path(cfg["stdin_file"]).is_file():
            QMessageBox.warning(self, APP_TITLE, "The selected standard-input file does not exist.")
            return
        try:
            args = build_arguments(cfg, "run")
        except ValueError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._start_process(args, "run-suite")

    def bootstrap_toolchain(self) -> None:
        cfg = self._collect_config()
        if not cfg["build_script"] or not Path(cfg["build_script"]).is_file():
            QMessageBox.warning(self, APP_TITLE, "Select a valid build.py first.")
            return
        if not cfg.get("toolchain_prefix"):
            answer = QMessageBox.question(
                self, APP_TITLE,
                "No private toolchain prefix is set. The bootstrap will use the "
                "work directory's default dependency prefix. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_process(build_arguments(cfg, "bootstrap"), "bootstrap")

    def run_dependency_action(self) -> None:
        cfg = self._collect_config()
        if not cfg["build_script"] or not Path(cfg["build_script"]).is_file():
            QMessageBox.warning(self, APP_TITLE, "Select a valid build.py first.")
            return
        try:
            args = build_arguments(cfg, "deps")
        except ValueError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._start_process(args, "dependencies")

    def verify_suite(self) -> None:
        cfg = self._collect_config()
        if not cfg["build_script"] or not Path(cfg["build_script"]).is_file():
            QMessageBox.warning(self, APP_TITLE, "Select a valid build.py first.")
            return
        self._start_process(build_arguments(cfg, "verify"), "verify")

    def _start_process(self, args: List[str], kind: str) -> None:
        try:
            script = resolve_build_script(self.build_script_edit.text())
        except BackendError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._start_external_process(
            sys.executable, [str(script)] + list(args), kind,
            working_directory=str(script.parent))

    def _start_external_process(
        self, program: str, args: List[str], kind: str,
        working_directory: Optional[str] = None,
    ) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.warning(self, APP_TITLE, "A task is already running.")
            return
        self.config = self._collect_config()
        try:
            save_config(self.config, self.config_path)
        except OSError as exc:
            QMessageBox.warning(self, APP_TITLE, "Could not save configuration:\n%s" % exc)
            return

        log_dir = self.config_path.parent / "logs"
        preserve_stages = kind in ("remote-download", "remote-cleanup")
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.task_log_path = log_dir / ("%s-%s.log" % (stamp, kind))
            self.task_log = self.task_log_path.open("w", encoding="utf-8", errors="replace")
            if not preserve_stages:
                self._close_stage_log_handles()
                self.stage_records = {}
                self.stage_stack = []
                self.stage_table.setRowCount(0)
                self.task_stage_log_dir = log_dir / ("%s-%s-stages" % (stamp, kind))
                self.task_stage_log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, APP_TITLE, "Cannot create task log:\n%s" % exc)
            return

        self.task_output = ""
        self.task_kind = kind
        self.task_started_at = datetime.now(timezone.utc)
        self.current_command = [str(program)] + list(args)
        self.process_output_buffer = ""
        self._cancel_requested = False
        if not preserve_stages:
            self._stage_view_auto_selected = False
        self.log_view.clear()
        self.log_view.appendPlainText("$ " + " ".join(
            shlex.quote(part) for part in self.current_command))
        self.task_label.setText("Running: " + kind)
        self.log_path_label.setText(str(self.task_log_path))
        self.statusBar().showMessage("Running %s…" % kind)
        if not preserve_stages:
            self.progress_label.setText("Waiting for build progress")
            self.progress_bar.setRange(0, 0)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        if kind in ("build", "dependencies", "bootstrap"):
            environment.insert("JOREK_BUILD_STUDIO_EVENTS", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(working_directory or os.getcwd())
        if os.name == "nt":
            self.process.setProgram(str(program))
            self.process.setArguments(list(args))
        else:
            runner = external_exec_runner()
            self.process.setProgram(sys.executable)
            self.process.setArguments(["-c", runner, str(program)] + list(args))
        self.process.start()

    def _append_overview_output(self, text: str) -> None:
        if not text:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)
        self.log_view.insertPlainText(text)

    def _write_stage_output(self, text: str) -> None:
        if not text or not self.stage_stack:
            return
        stage_id = self.stage_stack[-1]
        handle = self.stage_log_handles.get(stage_id)
        if handle:
            try:
                handle.write(text)
                handle.flush()
            except OSError as exc:
                self._append_overview_output(
                    "\n[Could not write detailed stage log %s: %s]\n" % (stage_id, exc))

    def _consume_process_line(self, line: str) -> None:
        if self.task_log:
            self.task_log.write(line)
            self.task_log.flush()

        stripped = line.rstrip("\r\n")
        prefix = "@@JOREK_BUILD_EVENT@@"
        if stripped.startswith(prefix):
            try:
                event = json.loads(stripped[len(prefix):])
            except json.JSONDecodeError:
                self._append_overview_output(line)
            else:
                self._handle_build_event(event)
            return

        if self.task_kind in ("matrix-preview", "doctor"):
            self.task_output += line
        else:
            self.task_output = (self.task_output + line)[-1000000:]

        if self.stage_stack:
            self._write_stage_output(line)
        else:
            self._append_overview_output(line)

    def _read_process_output(self) -> None:
        data = bytes(self.process.readAllStandardOutput())
        if not data:
            return
        self.process_output_buffer += data.decode("utf-8", "replace")
        while "\n" in self.process_output_buffer:
            line, self.process_output_buffer = self.process_output_buffer.split("\n", 1)
            self._consume_process_line(line + "\n")

    def _flush_process_output_buffer(self) -> None:
        if self.process_output_buffer:
            self._consume_process_line(self.process_output_buffer)
            self.process_output_buffer = ""

    def _handle_build_event(self, event: Dict[str, Any]) -> None:
        event_type = str(event.get("event", ""))
        if event_type == "phase_start":
            phase = str(event.get("phase", ""))
            label = str(event.get("label") or phase or "Build phase")
            total = max(0, int(event.get("total") or 0))
            self.current_phase = phase
            self.progress_label.setText("%s — 0 / %d" % (label, total))
            if total:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(0)
            else:
                self.progress_bar.setRange(0, 0)
            return

        if event_type == "phase_progress":
            phase = str(event.get("phase", self.current_phase))
            completed = max(0, int(event.get("completed") or 0))
            total = max(0, int(event.get("total") or 0))
            if total:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(min(completed, total))
                self.progress_label.setText("%s — %d / %d" % (
                    phase.replace("_", " ").title(), min(completed, total), total))
            return

        if event_type == "stage_start":
            stage_id = str(event.get("id") or "stage-%d" % (len(self.stage_records) + 1))
            label = str(event.get("label") or stage_id)
            category = str(event.get("category") or "Build")
            if self.task_stage_log_dir is None:
                self.task_stage_log_dir = self.config_path.parent / "logs" / "stages"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", stage_id).strip("-")[:72] or "stage"
            token = hashlib.sha1(stage_id.encode("utf-8")).hexdigest()[:10]
            log_path = self.task_stage_log_dir / (safe + "-" + token + ".log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            old_handle = self.stage_log_handles.pop(stage_id, None)
            if old_handle:
                old_handle.close()
            handle = log_path.open("w", encoding="utf-8", errors="replace")
            handle.write("JOREK Build Studio — %s\nCategory: %s\nStage ID: %s\n\n" % (
                label, category, stage_id))
            handle.flush()
            started = datetime.now(timezone.utc)
            record = {
                "id": stage_id,
                "label": label,
                "category": category,
                "status": "running",
                "log": str(log_path),
                "remote_log_path": str(event.get("log_path") or ""),
                "started_at": started,
                "started_monotonic": time.monotonic(),
                "elapsed_seconds": 0.0,
                "error": "",
            }
            self.stage_records[stage_id] = record
            self.stage_log_handles[stage_id] = handle
            self.stage_stack.append(stage_id)
            self._refresh_stage_table_row(stage_id)
            if not self.stage_elapsed_timer.isActive():
                self.stage_elapsed_timer.start()
            self._append_overview_output(
                "\n[%s] %s — started\n" % (category, label))
            self.task_label.setText("Running: " + label)
            if not self._stage_view_auto_selected:
                self.build_views.setCurrentIndex(1)
                self._stage_view_auto_selected = True
            return

        if event_type == "stage_finish":
            stage_id = str(event.get("id") or "")
            record = self.stage_records.get(stage_id)
            if not record:
                return
            status = str(event.get("status") or "completed")
            record["status"] = status
            record["error"] = str(event.get("error") or "")
            record["finished_at"] = datetime.now(timezone.utc)
            record["elapsed_seconds"] = max(
                0.0, time.monotonic() -
                float(record.get("started_monotonic", time.monotonic())))
            handle = self.stage_log_handles.pop(stage_id, None)
            if handle:
                suffix = "\n\n[Stage %s after %.1f seconds]\n" % (
                    status, record["elapsed_seconds"])
                if record["error"]:
                    suffix += "Error: " + record["error"] + "\n"
                handle.write(suffix)
                handle.flush()
                handle.close()
            if stage_id in self.stage_stack:
                self.stage_stack = [
                    item for item in self.stage_stack if item != stage_id]
            dialog = self.stage_dialogs.get(record.get("log", ""))
            if dialog:
                dialog.set_running(False)
            self._refresh_stage_table_row(stage_id)
            if not any(
                item.get("status") == "running"
                for item in self.stage_records.values()
            ):
                self.stage_elapsed_timer.stop()
            self._append_overview_output(
                "[%s] %s — %s (%.1f s)\n" % (
                    record["category"], record["label"], status,
                    record["elapsed_seconds"]))
            return

    def _refresh_stage_table_row(self, stage_id: str) -> None:
        record = self.stage_records[stage_id]
        row = record.get("_row")
        if row is None:
            row = self.stage_table.rowCount()
            self.stage_table.insertRow(row)
            record["_row"] = row
        status = str(record.get("status", ""))
        if status == "running":
            elapsed = max(
                0.0, time.monotonic() -
                float(record.get("started_monotonic", time.monotonic())))
        else:
            elapsed = float(record.get("elapsed_seconds", 0.0))
        values = [
            record.get("label", ""),
            record.get("category", ""),
            status,
            "%.1f s" % elapsed,
            Path(record.get("log", "")).name,
        ]
        for column, value in enumerate(values):
            item = self.stage_table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.stage_table.setItem(row, column, item)
            item.setText(str(value))
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, stage_id)
            elif column == 4:
                item.setData(Qt.ItemDataRole.UserRole, record.get("log", ""))

    def _refresh_running_stage_rows(self) -> None:
        for stage_id, record in self.stage_records.items():
            if record.get("status") == "running":
                self._refresh_stage_table_row(stage_id)

    def _open_stage_log(self, row: int, _column: int) -> None:
        item = self.stage_table.item(row, 0)
        stage_id = item.data(Qt.ItemDataRole.UserRole) if item else ""
        record = self.stage_records.get(str(stage_id))
        if record:
            self._show_stage_log_dialog(
                record.get("label", "Build stage"),
                record.get("log", ""),
                running=record.get("status") == "running")

    def _open_selected_stage_log(self) -> None:
        row = self.stage_table.currentRow()
        if row >= 0:
            self._open_stage_log(row, 0)

    def _show_stage_log_dialog(self, title: str, path: str, running: bool = False) -> None:
        if not path:
            return
        existing = self.stage_dialogs.get(path)
        if existing is not None:
            existing.set_running(running)
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        dialog = StageLogDialog(title, path, running=running, parent=self)
        self.stage_dialogs[path] = dialog
        dialog.destroyed.connect(
            lambda _obj=None, key=path: self.stage_dialogs.pop(key, None))
        dialog.show()

    def _close_stage_log_handles(self) -> None:
        for handle in list(self.stage_log_handles.values()):
            try:
                handle.flush()
                handle.close()
            except OSError:
                pass
        self.stage_log_handles.clear()

    def _finalize_open_stages(self, status: str) -> None:
        for stage_id in list(reversed(self.stage_stack)):
            record = self.stage_records.get(stage_id)
            if not record:
                continue
            record["status"] = status
            record["finished_at"] = datetime.now(timezone.utc)
            record["elapsed_seconds"] = max(
                0.0, time.monotonic() -
                float(record.get("started_monotonic", time.monotonic())))
            handle = self.stage_log_handles.pop(stage_id, None)
            if handle:
                handle.write("\n\n[Stage %s after %.1f seconds]\n" % (
                    status, record["elapsed_seconds"]))
                handle.flush()
                handle.close()
            dialog = self.stage_dialogs.get(record.get("log", ""))
            if dialog:
                dialog.set_running(False)
            self._refresh_stage_table_row(stage_id)
        self.stage_stack = []
        self.stage_elapsed_timer.stop()
        self._close_stage_log_handles()

    def _process_error(self, error) -> None:
        self._read_process_output()
        self._flush_process_output_buffer()
        message = "Process error: %s" % self.process.errorString()
        self._append_overview_output(message + "\n")
        if self.task_log:
            self.task_log.write("\n" + message + "\n")
            self.task_log.flush()
        if error == QProcess.ProcessError.FailedToStart:
            self._finalize_open_stages("failed")
            if self.task_log:
                self.task_log.flush()
                self.task_log.close()
                self.task_log = None
            self.task_label.setText("Failed to start")
            self.statusBar().showMessage(message, 10000)
            self._record_task(-1, False)
            if self.task_kind == "remote-cleanup":
                self._finish_remote_download(
                    bool(getattr(self, "remote_download_succeeded", False)),
                    cleanup_succeeded=False)

    def _process_finished(self, exit_code: int, exit_status) -> None:
        self._read_process_output()
        self._flush_process_output_buffer()
        completed_kind = self.task_kind
        succeeded = exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit
        terminal_stage_status = (
            "completed" if succeeded else
            "cancelled" if self._cancel_requested else "failed")
        self._finalize_open_stages(terminal_stage_status)
        if self.task_log:
            self.task_log.flush()
            self.task_log.close()
            self.task_log = None
        if succeeded:
            self.task_label.setText("Completed")
            self.statusBar().showMessage("Task completed successfully", 8000)
        else:
            self.task_label.setText(
                "Cancelled" if self._cancel_requested else "Failed (exit %d)" % exit_code)
            self.statusBar().showMessage("Task failed; inspect the complete log", 10000)
        self._record_task(exit_code, exit_status == QProcess.ExitStatus.NormalExit)

        if completed_kind == "remote-build":
            if succeeded:
                scp = shutil.which("scp")
                if scp and getattr(self, "remote_download_args", None):
                    self._start_external_process(
                        scp, self.remote_download_args, "remote-download",
                        working_directory=os.getcwd())
                else:
                    self.statusBar().showMessage(
                        "Remote build verified, but scp is unavailable for download", 12000)
            return
        if completed_kind == "remote-download":
            self.remote_download_succeeded = succeeded
            self.remote_download_log_path = self.task_log_path
            cleanup_args = getattr(self, "remote_cleanup_args", None)
            ssh = getattr(self, "remote_ssh_executable", None)
            if cleanup_args and ssh:
                self._start_external_process(ssh, cleanup_args, "remote-cleanup")
            else:
                self._finish_remote_download(succeeded, cleanup_succeeded=None)
            return
        if completed_kind == "remote-cleanup":
            self._finish_remote_download(
                bool(getattr(self, "remote_download_succeeded", False)),
                cleanup_succeeded=succeeded)
            return
        if completed_kind == "remote-test":
            if succeeded:
                self.statusBar().showMessage(
                    "SSH connection succeeded; inspect the log for remote OS and architecture", 12000)
            return

        if not self.root_edit.text().strip():
            fetched_root = Path(self.work_edit.text()).expanduser() / "src" / "JOREK"
            if (fetched_root / "jorek2_main.f90").is_file():
                self.root_edit.setText(str(fetched_root.resolve()))
                self.refresh_catalog(show_message=False)
        if self.task_kind == "matrix-preview" and exit_code == 0:
            self._show_matrix_result()
        elif self.task_kind == "doctor" and exit_code == 0:
            self._show_doctor_result()
        elif self.task_kind == "verify":
            self._show_verify_result()
        if self.task_kind == "dependencies" and exit_code == 0:
            self.refresh_catalog(show_message=False)

    def _finish_remote_download(
        self, downloaded: bool, cleanup_succeeded: Optional[bool],
    ) -> None:
        if downloaded:
            message = (
                "Remote suite downloaded successfully:\n%s\n\n"
                "The remote builder completed its portability verification before transfer."
                % getattr(self, "remote_local_artifact", ""))
            self.statusBar().showMessage("Remote artifact downloaded", 10000)
        else:
            message = (
                "Remote artifact download failed. Inspect the remote-download log at:\n%s"
                % getattr(self, 'remote_download_log_path', self.task_log_path))
            self.statusBar().showMessage("Remote artifact download failed", 10000)
        if cleanup_succeeded is False:
            message += (
                "\n\nWarning: the temporary remote staging file could not be removed. "
                "It is located at:\n%s" % getattr(self, "remote_artifact_path", ""))
        self.overview_text.setPlainText(message)

    def _show_matrix_result(self) -> None:
        try:
            data = json.loads(self.task_output)
            if not isinstance(data, list):
                raise ValueError("Expected a JSON array.")
        except (json.JSONDecodeError, ValueError) as exc:
            self.log_view.appendPlainText("\nCould not parse matrix JSON: %s" % exc)
            return
        self.matrix_table.setRowCount(len(data))
        for row, item in enumerate(data):
            values = [
                item.get("name", ""), item.get("model", ""), item.get("solver", ""),
                ", ".join(item.get("features", [])), ", ".join(item.get("params", [])),
            ]
            for column, value in enumerate(values):
                self.matrix_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.statusBar().showMessage("Matrix preview: %d variants" % len(data), 8000)

    def _show_verify_result(self) -> None:
        try:
            report = json.loads(self.task_output)
        except json.JSONDecodeError as exc:
            self.overview_text.setPlainText(
                "Portability report could not be parsed:\n%s\n\n%s" % (exc, self.task_output[-4000:]))
            return
        checks = [
            ("Artifact", report.get("artifact", {}).get("path", "")),
            ("Format", report.get("binary", {}).get("format", "unknown")),
            ("Architecture", ", ".join(report.get("binary", {}).get("architectures", []))),
            ("Host compatibility", "PASS" if report.get("host_compatibility", {}).get("compatible") else "FAIL"),
            ("Static dependencies", report.get("static_dependency_check", {}).get("status", "unknown").upper()),
            ("Code signature", report.get("code_signature", {}).get("status", "unknown")),
            ("Suite info smoke test", report.get("suite_info_smoke_test", {}).get("status", "unknown")),
            ("Portable on build host", "PASS" if report.get("portable_on_build_host") else "FAIL"),
            ("Minimum GLIBC", report.get("binary", {}).get("minimum_glibc_version") or "not detected"),
            ("Minimum OS", report.get("binary", {}).get("minimum_os_version") or "not detected"),
        ]
        summary = "\n".join("%s: %s" % pair for pair in checks)
        details = report.get("static_dependency_check", {}).get(
            "non_system_dependencies_or_errors", [])
        if details:
            summary += "\n\nStatic dependency findings:\n" + "\n".join(
                "  - " + str(item) for item in details)
        limitations = report.get("limitations", [])
        if limitations:
            summary += "\n\nLimitations:\n" + "\n".join(
                "  - " + str(item) for item in limitations)
        self.overview_text.setPlainText(summary)
        if report.get("portable_on_build_host"):
            self.statusBar().showMessage("Portability checks passed on this build host", 10000)
        else:
            self.statusBar().showMessage("Portability checks did not pass; inspect the report", 10000)

    def _show_doctor_result(self) -> None:
        try:
            data = parse_doctor_output(self.task_output)
        except BackendError as exc:
            self.overview_text.setPlainText("Doctor report could not be parsed:\n%s" % exc)
            return
        self.overview_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

    def cancel_process(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._cancel_requested = True
        self.task_label.setText("Stopping…")
        self.statusBar().showMessage("Sending terminate signal…")
        self._terminate_process_tree(force=False)
        QTimer.singleShot(3000, self._kill_if_still_running)

    def _terminate_process_tree(self, force: bool = False) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        pid = int(self.process.processId())
        if os.name != "nt" and pid > 0:
            try:
                os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            except OSError:
                if force:
                    self.process.kill()
                else:
                    self.process.terminate()
        elif os.name == "nt" and force and pid > 0:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            except OSError:
                self.process.kill()
        elif force:
            self.process.kill()
        else:
            self.process.terminate()

    def _kill_if_still_running(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._terminate_process_tree(force=True)

    # ------------------------------------------------------------------
    # Help and lifecycle
    # ------------------------------------------------------------------
    def load_help(self) -> None:
        script = self.build_script_edit.text().strip()
        if not script:
            QMessageBox.warning(self, APP_TITLE, "Select build.py first.")
            return
        command = self.help_command_combo.currentText()
        try:
            text = cli_help(script, "" if command == "General" else command)
        except (BackendError, OSError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.help_text.setPlainText(text)

    def closeEvent(self, event) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            answer = QMessageBox.question(
                self, APP_TITLE,
                "A build task is still running. Terminate it and close the application?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._terminate_process_tree(force=False)
            self.process.waitForFinished(2500)
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self._terminate_process_tree(force=True)
                self.process.waitForFinished(1500)
        try:
            self.config = self._collect_config()
            save_config(self.config, self.config_path)
        except Exception:
            pass
        if self.task_log:
            self.task_log.close()
            self.task_log = None
        event.accept()
