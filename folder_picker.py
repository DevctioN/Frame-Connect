"""Asynchronous Qt browser for directories on the headset."""

from queue import Empty, Queue
from threading import Thread

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout

from diagnostics import LOGGER
from headset import HeadsetError
from transfers import list_folders


class FolderPicker(QDialog):
    def __init__(self, parent, connection, selected, closed):
        super().__init__(parent)
        self.connection, self.selected, self.closed = connection, selected, closed
        self.results = Queue()
        self.folders = []
        self.parent_path = "~"
        self.ready = self.loading = False
        self._notified_closed = False
        self.setWindowTitle("Choose a folder on Steam Frame")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(620, 440)
        self.setMinimumSize(520, 380)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._poll)
        self.finished.connect(self._on_finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        bar = QHBoxLayout()
        self.home, self.up, self.go = QPushButton("Home"), QPushButton("Up"), QPushButton("Go")
        self.home.clicked.connect(lambda: self.load("~"))
        self.up.clicked.connect(lambda: self.load(self.parent_path))
        self.path = self.entry = QLineEdit("~")
        self.entry.setAccessibleName("Folder on Steam Frame")
        self.go.clicked.connect(lambda: self.load(self.path.text()))
        self.entry.returnPressed.connect(lambda: self.load(self.path.text()))
        bar.addWidget(self.home)
        bar.addWidget(self.up)
        bar.addWidget(self.entry, 1)
        bar.addWidget(self.go)
        layout.addLayout(bar)
        self.listbox = QListWidget()
        self.listbox.itemDoubleClicked.connect(self.open_selected)
        self.listbox.itemActivated.connect(self.open_selected)
        self.listbox.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.listbox, 1)
        self.message = QLabel("Loading folders…")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setProperty("muted", True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.choose = QPushButton("Use this folder")
        self.choose.clicked.connect(self.choose_folder)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for control in (self.home, self.up, self.go, self.choose, cancel):
            control.setAutoDefault(False)
        buttons.addWidget(self.choose)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.load("~")

    def load(self, path):
        if self.loading:
            return
        self.loading, self.ready = True, False
        for widget in (self.home, self.up, self.go, self.entry, self.choose, self.listbox):
            widget.setEnabled(False)
        self.message.setText("Loading folders…")
        Thread(target=self._fetch, args=(path,), daemon=True).start()
        self._poll_timer.start()

    def _fetch(self, path):
        try:
            self.results.put(list_folders(*self.connection, path))
        except HeadsetError as error:
            LOGGER.warning("Folder listing failed for %s:%s: %s", self.connection[0], path, error)
            self.results.put(error)

    def _poll(self):
        try:
            result = self.results.get_nowait()
        except Empty:
            return
        self._poll_timer.stop()
        self.loading = False
        for widget in (self.home, self.up, self.go, self.entry):
            widget.setEnabled(True)
        if isinstance(result, HeadsetError):
            self.message.setText(str(result))
            return
        self.path.setText(result["path"])
        self.parent_path = result["parent"]
        self.folders = result["folders"]
        self.listbox.clear()
        self.listbox.addItems([folder["name"] for folder in self.folders])
        self.listbox.setEnabled(True)
        self.ready = True
        self._selection_changed()
        self.choose.setEnabled(True)

    def _selected_path(self):
        items = self.listbox.selectedItems()
        if items:
            return self.folders[self.listbox.row(items[0])]["path"]
        return self.path.text()

    def _selection_changed(self):
        if not self.ready:
            return
        selected = bool(self.listbox.selectedItems())
        self.choose.setText("Use selected folder" if selected else "Use this folder")
        self.choose.setToolTip(self._selected_path())
        self.message.setText(
            f"Selected folder: {self._selected_path()}" if selected
            else "Select a folder to use it, or double-click to browse inside."
        )

    def open_selected(self, *_args):
        row = self.listbox.currentRow()
        if self.ready and row >= 0:
            self.load(self.folders[row]["path"])

    def choose_folder(self):
        if self.ready:
            self.selected(self._selected_path())
            self.accept()

    def _on_finished(self, _result):
        self._poll_timer.stop()
        if not self._notified_closed:
            self._notified_closed = True
            self.closed()
