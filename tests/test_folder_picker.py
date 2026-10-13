from threading import Event
import unittest
from unittest.mock import Mock, patch

from qt_helpers import APPLICATION, dispose, wait_until
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from folder_picker import FolderPicker
from headset import HeadsetError
from theme import apply_dark_theme


class FolderPickerTests(unittest.TestCase):
    def setUp(self):
        apply_dark_theme(APPLICATION)
        self.parent = QWidget()
        self.parent.show()
        self.picker = None

    def tearDown(self):
        if self.picker is not None:
            dispose(self.picker)
        dispose(self.parent)

    def finish_load(self):
        self.assertTrue(wait_until(lambda: not self.picker.loading))

    def test_browse_opens_remote_folder_and_returns_selected_path(self):
        selected, closed = Mock(), Mock()
        def folders(host, user, key, path):
            return {"path": "/home/steamos" if path == "~" else path, "parent": "/home",
                    "folders": [{"name": "Downloads", "path": "/home/steamos/Downloads"}]}
        with patch("folder_picker.list_folders", side_effect=folders) as listing:
            self.picker = FolderPicker(self.parent, ("frame", "steamos", ""), selected, closed)
            self.picker.open()
            self.finish_load()
            self.picker.listbox.setCurrentRow(0)
            self.picker.open_selected()
            self.finish_load()
            self.picker.choose_folder()
        selected.assert_called_once_with("/home/steamos/Downloads")
        closed.assert_called_once()
        self.assertEqual(listing.call_args.args, ("frame", "steamos", "", "/home/steamos/Downloads"))

    def test_permission_error_allows_navigation_but_not_selection(self):
        with patch("folder_picker.list_folders", side_effect=HeadsetError("Permission denied")):
            self.picker = FolderPicker(self.parent, ("frame", "steamos", ""), Mock(), Mock())
            self.finish_load()
        self.assertEqual(self.picker.message.text(), "Permission denied")
        self.assertFalse(self.picker.choose.isEnabled())
        self.assertTrue(self.picker.home.isEnabled())

    def test_single_click_chooses_highlighted_folder_instead_of_home(self):
        selected, closed = Mock(), Mock()
        response = {"path": "/home/steamos", "parent": "/home",
                    "folders": [{"name": "Videos", "path": "/home/steamos/Videos"}]}
        with patch("folder_picker.list_folders", return_value=response):
            self.picker = FolderPicker(self.parent, ("frame", "steamos", ""), selected, closed)
            self.picker.open()
            self.finish_load()
            item = self.picker.listbox.item(0)
            QTest.mouseClick(self.picker.listbox.viewport(), Qt.MouseButton.LeftButton,
                             pos=self.picker.listbox.visualItemRect(item).center())
            self.picker.choose.click()
        selected.assert_called_once_with("/home/steamos/Videos")
        closed.assert_called_once()

    def test_no_highlight_chooses_current_folder(self):
        selected = Mock()
        response = {"path": "/home/steamos", "parent": "/home",
                    "folders": [{"name": "Videos", "path": "/home/steamos/Videos"}]}
        with patch("folder_picker.list_folders", return_value=response):
            self.picker = FolderPicker(self.parent, ("frame", "steamos", ""), selected, Mock())
            self.picker.open()
            self.finish_load()
            self.assertFalse(self.picker.listbox.selectedItems())
            self.picker.choose.click()
        selected.assert_called_once_with("/home/steamos")

    def test_close_while_loading_stops_polling_and_releases_parent(self):
        release = Event()
        def slow(*_args):
            release.wait(2)
            return {"path": "/home/steamos", "parent": "/home", "folders": []}
        closed = Mock()
        with patch("folder_picker.list_folders", side_effect=slow):
            self.picker = FolderPicker(self.parent, ("frame", "steamos", ""), Mock(), closed)
            self.picker.open()
            try:
                self.picker.reject()
                self.assertFalse(self.picker._poll_timer.isActive())
                closed.assert_called_once()
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
