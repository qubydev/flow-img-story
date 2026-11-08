"""Flow Image desktop application."""

import json
import importlib.util
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QHeaderView,
)
from flow_client import ASPECTS, MODELS, FlowClient

DATA = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Flow Image Story"
STATE = DATA / "session.json"
PROFILES = DATA / "profiles"
OUTPUT = Path.home() / "Pictures" / "Flow Image Story"
DATA.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA / "app.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def profile_for(name):
    return PROFILES / str(uuid.uuid5(uuid.NAMESPACE_URL, name.casefold()))


class LoginWorker(QThread):
    finished_login = Signal(bool, str, str)
    progress = Signal(str)

    def __init__(self, profile):
        super().__init__()
        self.profile = profile

    def run(self):
        try:
            email = FlowClient(self.profile).sign_in(self.progress.emit)
            self.finished_login.emit(True, "Google sign-in and Flow access verified.", email or "")
        except Exception as exc:
            self.finished_login.emit(False, str(exc), "")


class QueueWorker(QThread):
    changed = Signal(str, str, str)
    ended = Signal()

    def __init__(self, tasks, profile, folder, subfolders):
        super().__init__()
        self.tasks, self.profile, self.folder = tasks, profile, folder
        self.subfolders = subfolders
        self.stopping = False

    def run(self):
        for task in self.tasks:
            if self.stopping:
                break
            self.changed.emit(task["id"], "Running", "")
            try:
                folder = Path(self.folder)
                if self.subfolders:
                    slug = re.sub(r"[^\w .-]", "_", task["prompt"][:48]).strip(" .") or "Task"
                    folder = folder / f"{slug}_{task['id'][:8]}"
                files = FlowClient(self.profile).generate(
                    task["prompt"], task["model"], task["aspect"],
                    task["count"], task["seed"], folder)
                self.changed.emit(task["id"], "Done", "\n".join(files))
            except Exception as exc:
                self.changed.emit(task["id"], "Failed", str(exc))
        self.ended.emit()


class AccountsDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Manage Flow accounts")
        self.resize(900, 480)
        layout = QVBoxLayout(self)
        heading = QLabel("Google Flow accounts")
        heading.setStyleSheet("font-size: 19px; font-weight: 650;")
        layout.addWidget(heading)
        note = QLabel("Sign in with Chrome. The browser closes when Flow sign-in is detected.")
        layout.addWidget(note)
        self.status = QLabel("Click Add account to open a Chrome sign-in window.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        controls.addWidget(main.button("+ Add account", main.add_account, True))
        controls.addWidget(main.button("Sign in / refresh selected", self.refresh_selected))
        controls.addWidget(main.button("Remove selected", self.remove_selected))
        controls.addStretch()
        layout.addLayout(controls)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["On", "Account", "Login", "Action", "Type", "Image", "Credits"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self):
        self.table.setRowCount(len(self.main.accounts))
        for row, name in enumerate(self.main.accounts):
            enabled = QCheckBox()
            enabled.setChecked(self.main.enabled.get(name, True))
            enabled.toggled.connect(lambda value, n=name: self.main.set_account_enabled(n, value))
            holder = QWidget()
            line = QHBoxLayout(holder)
            line.setContentsMargins(8, 0, 0, 0)
            line.addWidget(enabled)
            self.table.setCellWidget(row, 0, holder)
            self.table.setItem(row, 1, QTableWidgetItem(name))
            self.table.setItem(row, 2, QTableWidgetItem(self.main.login_status.get(name, "Saved")))
            actions = QWidget()
            buttons = QHBoxLayout(actions)
            buttons.setContentsMargins(2, 0, 2, 0)
            buttons.addWidget(self.main.button("↻", lambda checked=False, n=name: self.main.add_account(n)))
            buttons.addWidget(self.main.button("✕", lambda checked=False, n=name: self.main.remove_account(n)))
            self.table.setCellWidget(row, 3, actions)
            self.table.setItem(row, 4, QTableWidgetItem("Flow"))
            image = QCheckBox()
            image.setChecked(self.main.image_enabled.get(name, True))
            image.toggled.connect(lambda value, n=name: self.main.set_image_enabled(n, value))
            image_holder = QWidget()
            image_line = QHBoxLayout(image_holder)
            image_line.setContentsMargins(8, 0, 0, 0)
            image_line.addWidget(image)
            self.table.setCellWidget(row, 5, image_holder)
            self.table.setItem(row, 6, QTableWidgetItem("—"))

    def show_status(self, message):
        self.status.setText(message)

    def selected_name(self):
        row = self.table.currentRow()
        return self.main.accounts[row] if 0 <= row < len(self.main.accounts) else None

    def refresh_selected(self):
        name = self.selected_name()
        if name:
            self.main.add_account(name)
        else:
            self.show_status("Select an account row first. Use Add account for a new sign-in.")

    def remove_selected(self):
        name = self.selected_name()
        if name:
            self.main.remove_account(name)
        else:
            self.show_status("There is no account selected to remove.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Flow Image Story")
        self.resize(1300, 820)
        self.tasks, self.accounts = [], []
        self.worker = self.login_worker = None
        self.pending_start = False
        self.pending_account = None
        self.enabled = {}
        self.image_enabled = {}
        self.login_status = {}
        self.account_profiles = {}
        self.accounts_dialog = None
        self.build()
        self.restore()

    def button(self, label, callback, primary=False):
        b = QPushButton(label)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            b.setObjectName("primary")
        b.clicked.connect(callback)
        return b

    def build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(13)
        title = QLabel("Flow Image")
        title.setObjectName("title")
        outer.addWidget(title)
        outer.addWidget(QLabel("Generate images with your own Google Flow account"))

        bar = QHBoxLayout()
        self.counts = QLabel()
        bar.addWidget(self.counts)
        bar.addStretch()
        bar.addWidget(self.button("Manage accounts", self.manage_accounts, True))
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left.setObjectName("panel")
        left.setMinimumWidth(350)
        left.setMaximumWidth(470)
        layout = QVBoxLayout(left)
        layout.addWidget(QLabel("Basic settings"))
        form = QFormLayout()
        self.model = QComboBox()
        self.model.addItems(MODELS)
        form.addRow("Model", self.model)
        self.aspect = QComboBox()
        self.aspect.addItems(ASPECTS)
        self.aspect.setCurrentText("16:9")
        form.addRow("Aspect ratio", self.aspect)
        self.count = QSpinBox()
        self.count.setRange(1, 8)
        form.addRow("Images per prompt", self.count)
        self.seed = QLineEdit()
        self.seed.setPlaceholderText("Random")
        form.addRow("Seed", self.seed)
        layout.addLayout(form)
        layout.addWidget(QLabel("Save folder"))
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(OUTPUT))
        folder_row.addWidget(self.folder)
        folder_row.addWidget(self.button("Browse", self.browse))
        layout.addLayout(folder_row)
        self.subfolders = QCheckBox("Create a subfolder per task")
        self.subfolders.setChecked(True)
        layout.addWidget(self.subfolders)
        layout.addWidget(QLabel("Prompts — separate tasks with a blank line"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Describe the image to create…")
        layout.addWidget(self.prompt, 1)
        row = QHBoxLayout()
        row.addWidget(self.button("Import TXT", self.import_txt))
        self.generate_button = self.button("Start generation", self.toggle_generation, True)
        row.addWidget(self.generate_button)
        layout.addLayout(row)
        split.addWidget(left)

        right = QWidget()
        right.setObjectName("panel")
        layout = QVBoxLayout(right)
        layout.addWidget(QLabel("Task manager"))
        row = QHBoxLayout()
        for label, callback in (
            ("Delete selected", self.delete_selected), ("Clear", self.clear), ("Save session", self.save_session),
            ("Load session", self.load_session)
        ):
            row.addWidget(self.button(label, callback))
        row.addStretch()
        layout.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Prompt", "Model / ratio", "Status", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self.open_result)
        layout.addWidget(self.table, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 2)
        outer.addWidget(split, 1)
        self.message = QLabel("Manage accounts to sign in, then enter a prompt and start generation.")
        outer.addWidget(self.message)
        self.setCentralWidget(root)
        self.setStyleSheet("""
            QWidget { background: #141922; color: #edf1f8; font-size: 13px; }
            QLabel#title { font-size: 28px; font-weight: 700; }
            QWidget#panel { background: #1e2735; border: 1px solid #354157; border-radius: 9px; }
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QTableWidget {
                background: #111824; border: 1px solid #3b4b61; border-radius: 6px;
                padding: 7px; selection-background-color: #405fc0; }
            QHeaderView::section { background: #2b3950; padding: 8px; border: 0; }
            QPushButton { background: #34435a; border: 1px solid #50627a;
                          border-radius: 6px; padding: 8px 11px; }
            QPushButton:hover { background: #475c7a; }
            QPushButton:disabled { background: #263142; color: #8490a2; }
            QPushButton#primary { background: #5274e2; border-color: #5274e2; font-weight: 600; }
        """)

    def state(self):
        return dict(auth_version=2, accounts=self.accounts, enabled=self.enabled, image_enabled=self.image_enabled,
                    login_status=self.login_status, account_profiles=self.account_profiles,
                    tasks=self.tasks, folder=self.folder.text(),
                    model=self.model.currentText(), aspect=self.aspect.currentText(),
                    count=self.count.value(), subfolders=self.subfolders.isChecked())

    def persist(self):
        save_json(STATE, self.state())

    def restore(self):
        if STATE.exists():
            try:
                state = json.loads(STATE.read_text(encoding="utf-8"))
                self.accounts = state.get("accounts", [])
                self.enabled = state.get("enabled", {})
                self.image_enabled = state.get("image_enabled", {})
                self.account_profiles = state.get("account_profiles", {})
                self.login_status = {
                    name: "Saved" if status == "ACTIVE" else status
                    for name, status in state.get("login_status", {}).items()
                }
                if state.get("auth_version", 0) < 2:
                    self.login_status = {name: "Needs sign-in" for name in self.accounts}
                self.tasks = state.get("tasks", [])
                for task in self.tasks:
                    if task["status"] in ("Running", "Queued"):
                        task["status"] = "Stopped"
                self.folder.setText(state.get("folder", str(OUTPUT)))
                self.model.setCurrentText(state.get("model", list(MODELS)[0]))
                self.aspect.setCurrentText(state.get("aspect", "16:9"))
                self.count.setValue(state.get("count", 1))
                self.subfolders.setChecked(state.get("subfolders", True))
            except (OSError, ValueError, KeyError) as exc:
                self.message.setText(f"Session load failed: {exc}")
        self.refresh()

    def refresh(self):
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            values = (str(row + 1), task["prompt"],
                      f"{task['model']} · {task['aspect']} · {task['count']}",
                      task["status"], task.get("result", ""))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        statuses = [t["status"] for t in self.tasks]
        self.counts.setText("   •   ".join(f"{s} {statuses.count(s)}"
                            for s in ("Running", "Queued", "Done", "Failed", "Stopped"))
                            + f"   •   Accounts {len(self.accounts)}")
        self.persist()

    def manage_accounts(self):
        if self.accounts_dialog is None:
            self.accounts_dialog = AccountsDialog(self)
        self.accounts_dialog.refresh()
        self.accounts_dialog.show()
        self.accounts_dialog.raise_()
        self.accounts_dialog.activateWindow()

    def set_account_enabled(self, name, value):
        self.enabled[name] = value
        self.persist()

    def set_image_enabled(self, name, value):
        self.image_enabled[name] = value
        self.persist()

    def add_account(self, existing=None):
        if self.login_worker and self.login_worker.isRunning():
            self.account_status("The account browser is already open. Complete sign-in there.")
            return
        if importlib.util.find_spec("playwright") is None:
            error = "Browser support is missing. Run: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
            logging.error(error)
            self.account_status(error)
            QMessageBox.warning(self.accounts_dialog or self, "Browser support missing", error)
            return
        if existing:
            self.pending_account = existing
        else:
            number = 1
            while f"Flow account {number}" in self.accounts:
                number += 1
            self.pending_account = f"Flow account {number}"
        if self.pending_account not in self.accounts:
            self.accounts.append(self.pending_account)
        self.pending_profile = self.account_profiles.get(
            self.pending_account, str(profile_for(self.pending_account)))
        self.account_profiles[self.pending_account] = self.pending_profile
        self.login_status[self.pending_account] = "Signing in"
        self.persist()
        if self.accounts_dialog:
            self.accounts_dialog.refresh()
        self.account_status("Opening Google sign-in in Chrome. Complete sign-in there.")
        logging.info("Opening Chrome for %s", self.pending_account)
        self.login_worker = LoginWorker(self.pending_profile)
        self.login_worker.progress.connect(self.on_login_progress)
        self.login_worker.finished_login.connect(self.on_login)
        self.login_worker.start()

    def on_login_progress(self, message):
        if self.pending_account:
            self.login_status[self.pending_account] = "Checking..."
            if self.accounts_dialog:
                self.accounts_dialog.refresh()
        self.account_status(message)

    def on_login(self, success, message, email):
        self.account_status(message)
        logging.info("Sign-in result: %s", message) if success else logging.error("Sign-in failed: %s", message)
        if self.pending_account:
            name = self.pending_account
            if success and email and email not in self.accounts:
                index = self.accounts.index(name)
                self.accounts[index] = email
                self.account_profiles[email] = self.account_profiles.pop(name)
                self.enabled[email] = self.enabled.pop(name, True)
                self.image_enabled[email] = self.image_enabled.pop(name, True)
                self.login_status.pop(name, None)
                name = email
            self.login_status[name] = "ACTIVE" if success else "Needs sign-in"
            self.persist()
        if self.accounts_dialog:
            self.accounts_dialog.refresh()
        if not success:
            QMessageBox.warning(self.accounts_dialog or self, "Flow sign-in failed", message)
        self.pending_account = None
        if success and self.pending_start:
            self.pending_start = False

    def account_status(self, message):
        self.message.setText(message)
        if self.accounts_dialog:
            self.accounts_dialog.show_status(message)
            self.start()
        else:
            self.pending_start = False

    def remove_account(self, name):
        if name in self.accounts:
            self.accounts.remove(name)
            self.enabled.pop(name, None)
            self.image_enabled.pop(name, None)
            self.login_status.pop(name, None)
            self.account_profiles.pop(name, None)
            self.persist()
            self.refresh()
            if self.accounts_dialog:
                self.accounts_dialog.refresh()
            self.message.setText("Account removed from the list. Its browser profile remains on this computer.")

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Save images", self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.persist()

    def import_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import prompts", "", "Text files (*.txt)")
        if path:
            self.prompt.setPlainText(Path(path).read_text(encoding="utf-8-sig"))

    def add_tasks(self):
        prompts = [p.strip() for p in re.split(r"\n\s*\n", self.prompt.toPlainText()) if p.strip()]
        if not prompts:
            self.message.setText("Enter a prompt to generate.")
            return []
        try:
            seed = int(self.seed.text()) if self.seed.text().strip() else None
        except ValueError:
            self.message.setText("Seed must be a whole number.")
            return []
        created = []
        for prompt in prompts:
            task = dict(id=str(uuid.uuid4()), prompt=prompt,
                model=self.model.currentText(), aspect=self.aspect.currentText(),
                count=self.count.value(), seed=seed, status="Queued", result="")
            self.tasks.append(task)
            created.append(task)
        self.prompt.clear()
        self.refresh()
        return created

    def retry(self):
        for task in self.tasks:
            if task["status"] == "Failed":
                task["status"], task["result"] = "Queued", ""
        self.refresh()

    def delete_selected(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if self.worker and self.worker.isRunning():
            return
        self.tasks = [t for i, t in enumerate(self.tasks) if i not in rows]
        self.refresh()

    def clear(self):
        if self.worker and self.worker.isRunning():
            return
        self.tasks.clear()
        self.refresh()

    def save_session(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save session", "flow-image-session.json", "JSON (*.json)")
        if path:
            save_json(Path(path), self.state())

    def load_session(self):
        if self.worker and self.worker.isRunning():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Load session", "", "JSON (*.json)")
        if path:
            try:
                self.tasks = json.loads(Path(path).read_text(encoding="utf-8"))["tasks"]
                for task in self.tasks:
                    if task["status"] in ("Running", "Queued"):
                        task["status"] = "Stopped"
                self.refresh()
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "Load failed", str(exc))

    def toggle_generation(self):
        if self.worker and self.worker.isRunning():
            self.stop()
        else:
            self.start()

    def start(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.prompt.toPlainText().strip():
            self.message.setText("Enter a prompt to generate.")
            return
        account = next((name for name in self.accounts
                        if self.enabled.get(name, True) and self.image_enabled.get(name, True)
                        and self.login_status.get(name, "Saved") not in ("Needs sign-in", "Signing in", "Checking...")), None)
        if not account:
            self.pending_start = True
            self.message.setText("Add a Google Flow account in Manage accounts to start generation.")
            self.manage_accounts()
            return
        queued = self.add_tasks()
        if not queued:
            return
        self.worker = QueueWorker(
            queued, self.account_profiles.get(account, str(profile_for(account))),
            self.folder.text(), self.subfolders.isChecked())
        self.worker.changed.connect(self.task_changed)
        self.worker.ended.connect(self.generation_ended)
        self.worker.start()
        self.generate_button.setText("Stop generation")
        self.message.setText("Generating with Google Flow…")

    def generation_ended(self):
        if self.worker:
            for task in self.worker.tasks:
                if task["status"] == "Queued":
                    task["status"] = "Stopped"
            self.refresh()
        self.generate_button.setText("Start generation")
        self.generate_button.setEnabled(True)
        if self.worker and self.worker.stopping:
            self.message.setText("Generation stopped.")
        else:
            self.message.setText("Generation finished. See task results above.")

    def task_changed(self, task_id, status, result):
        for task in self.tasks:
            if task["id"] == task_id:
                task["status"], task["result"] = status, result
                break
        self.refresh()
        if status == "Failed":
            self.message.setText(result)

    def stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stopping = True
            self.generate_button.setEnabled(False)
            self.message.setText("Stopping after the current image request…")

    def open_result(self, index):
        task = self.tasks[index.row()]
        if task["status"] == "Done" and task["result"]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(task["result"].splitlines()[0]).parent)))

    def closeEvent(self, event):
        if (self.worker and self.worker.isRunning()) or (self.login_worker and self.login_worker.isRunning()):
            QMessageBox.information(self, "Busy", "Finish sign-in or the current generation before closing.")
            event.ignore()
        else:
            self.persist()
            event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
