"""Transfer destinations survive relaunches without touching real AppData."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from qt_helpers import dispose, wait_until
from app import FrameConnectApp
from headset import parse_status
from preferences import DEFAULT_DESTINATION, DestinationPreferences
from test_headset import SAMPLE, fixture_directory


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(fixture_directory())
        self.path = self.directory / "settings.json"

    def window(self):
        window = FrameConnectApp(preferences=DestinationPreferences(self.path))
        self.addCleanup(dispose, window)
        return window

    def status(self, downloads="/home/steamos/Downloads"):
        return parse_status(json.dumps(dict(SAMPLE, downloads=downloads)))

    def saved(self, host="frame", user="steamos"):
        return DestinationPreferences(self.path).destination(host, user)

    def test_first_launch_and_unselected_downloads_keep_following_the_headset_default(self):
        first = self.window()
        self.assertEqual(first.destination.text(), DEFAULT_DESTINATION)
        first.show_status(self.status("/home/steamos/Téléchargements"))
        self.assertEqual(first.destination.text(), "/home/steamos/Téléchargements")
        first.close()
        self.assertEqual(self.saved(), DEFAULT_DESTINATION)
        second = self.window()
        second.show_status(self.status("/home/steamos/New Downloads"))
        self.assertEqual(second.destination.text(), "/home/steamos/New Downloads")

    def test_choosing_a_folder_saves_immediately_and_survives_relaunch_and_refresh(self):
        path = "/home/steamos/Videos/VR – test"
        first = self.window()
        first.show_status(self.status())
        with patch("app.FolderPicker") as picker:
            first.choose_folder()
        _parent, _connection, selected, closed = picker.call_args.args
        selected(path)
        closed()
        self.assertEqual(self.saved(), path)
        first.close()
        second = self.window()
        self.assertEqual(second.destination.text(), path)
        second.show_status(self.status())
        second.show_status(self.status(), automatic=True)
        self.assertEqual(second.destination.text(), path)
        self.assertIn("VR – test", self.path.read_text(encoding="utf-8"))

    def test_typed_folder_is_saved_on_edit_completion(self):
        first = self.window()
        first.show_status(self.status())
        first.destination.setText("~/Videos")
        first.destination.editingFinished.emit()
        self.assertEqual(self.saved(), "~/Videos")
        self.assertEqual(self.window().destination.text(), "~/Videos")

    def test_closing_saves_an_edit_even_without_focus_changing(self):
        first = self.window()
        first.show_status(self.status())
        first.destination.setText("/home/steamos/Movies")
        first.close()
        self.assertEqual(self.window().destination.text(), "/home/steamos/Movies")

    def test_empty_or_incomplete_edits_do_not_replace_the_last_valid_folder(self):
        first = self.window()
        first.show_status(self.status())
        first._select_destination("~/Videos")
        for invalid in ("", "Videos", "C:/Windows", "/home/steamos/Bad\nPath"):
            first.destination.setText(invalid)
            first.destination.editingFinished.emit()
            self.assertEqual(self.saved(), "~/Videos")
        first.close()
        self.assertEqual(self.window().destination.text(), "~/Videos")

    def test_folders_are_separate_by_headset_and_user_but_not_by_ssh_key(self):
        first = self.window()
        first.show_status(self.status())
        first._select_destination("/home/steamos/Videos")
        first.host.setText("other-frame")
        self.assertEqual(first.destination.text(), DEFAULT_DESTINATION)
        first._select_destination("~/Documents")
        first.user.setText("otheruser")
        self.assertEqual(first.destination.text(), DEFAULT_DESTINATION)
        first.user.setText("steamos")
        self.assertEqual(first.destination.text(), "~/Documents")
        first.host.setText("FRAME")
        self.assertEqual(first.destination.text(), "/home/steamos/Videos")
        first.key_file.setText("C:/another-key")
        self.assertEqual(first.destination.text(), "/home/steamos/Videos")
        self.assertEqual(self.saved("other-frame"), "~/Documents")

    def test_selecting_downloads_again_restores_the_dynamic_default(self):
        first = self.window()
        first.show_status(self.status())
        first._select_destination("~/Videos")
        first._select_destination("/home/steamos/Downloads")
        self.assertEqual(self.saved(), DEFAULT_DESTINATION)
        first.close()
        second = self.window()
        second.show_status(self.status("/home/steamos/Custom Downloads"))
        self.assertEqual(second.destination.text(), "/home/steamos/Custom Downloads")

    def test_reopened_app_uploads_to_the_remembered_folder_and_saves_the_resolved_path(self):
        first = self.window()
        first._select_destination("~/Videos")
        first.close()
        second = self.window()
        second.show_status(self.status())
        with patch("app.upload_files", return_value=("/home/steamos/Videos/clip.mp4",)) as upload:
            self.assertTrue(second.start_upload(("C:/clip.mp4",)))
            self.assertTrue(wait_until(lambda: not second.uploading))
        self.assertEqual(upload.call_args.args[4], "~/Videos")
        self.assertEqual(self.saved(), "/home/steamos/Videos")
        self.assertEqual(self.window().destination.text(), "/home/steamos/Videos")

    def test_damaged_settings_or_invalid_saved_paths_fall_back_to_downloads(self):
        for content in ("{broken", "[]", '{"destinations": []}', '{"destinations": {"steamos@frame": 42}}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(self.saved(), DEFAULT_DESTINATION)

    def test_a_failed_save_keeps_the_previous_file_and_allows_retry(self):
        first = self.window()
        first.show_status(self.status())
        first._select_destination("~/Videos")
        before = self.path.read_bytes()
        with patch("preferences.Path.replace", side_effect=PermissionError("test write failure")):
            first._select_destination("~/Movies")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob("*.tmp")))
        self.assertIn("Could not save", first.destination.toolTip())
        self.assertTrue(first._can_drop())
        first.destination.editingFinished.emit()
        self.assertEqual(self.saved(), "~/Movies")
        self.assertEqual(first.destination.toolTip(), "")
