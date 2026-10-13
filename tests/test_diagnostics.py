"""Diagnostics persist without real crashes, network calls, or desktop actions."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from qt_helpers import APPLICATION, dispose

import diagnostics
from app import FrameConnectApp
from headset import HeadsetError, parse_status
from test_headset import SAMPLE, fixture_directory
from transfers import TransferCancelled


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(fixture_directory())
        self.addCleanup(diagnostics.shutdown_logging)

    def make_app(self):
        window = FrameConnectApp()
        self.addCleanup(dispose, window)
        return window

    def read_log(self):
        return (self.directory / "frame-connect-log.txt").read_text(encoding="utf-8")

    def test_connection_failure_and_recovery_persist_without_logging_successful_polls(self):
        diagnostics.configure_logging(self.directory)
        window = self.make_app()
        status = parse_status(json.dumps(SAMPLE))
        window.show_status(status)
        connection = window.connected_to

        def finish(result):
            request = (object(), connection, True)
            window._status_request = request
            window._handle_status_result(request, result)

        finish(HeadsetError("ssh: connection to frame timed out"))
        window._set_refresh_interval(10000)
        finish(status)
        finish(status)
        diagnostics.shutdown_logging()
        text = self.read_log()
        self.assertEqual(text.count("connection check failed"), 1)
        self.assertIn("WARNING", text)
        self.assertIn("ssh: connection to frame timed out", text)
        self.assertEqual(text.count("Headset connection established: frame"), 1)
        self.assertIn("Frame Connect process exiting", text)

    def test_transfer_and_power_results_are_logged_without_per_byte_spam(self):
        diagnostics.configure_logging(self.directory)
        window = self.make_app()
        window._handle_transfer_result("progress", ("clip.mp4", 0, 1, 5, 10))
        window._handle_transfer_result("upload_error", HeadsetError("SFTP connection interrupted"))
        window._handle_transfer_result("upload_error", TransferCancelled("Cancelled by user", completed=0))
        window._handle_transfer_result("uploaded", ("/home/steamos/Downloads/clip.mp4",))
        request = (object(), ("frame", "steamos", ""))
        window._power_request = request
        window._handle_power_result(request, HeadsetError("Steam control interface unavailable"))
        text = self.read_log()
        self.assertIn("ERROR", text)
        self.assertIn("SFTP connection interrupted", text)
        self.assertIn("Cancelled; 0 file(s) completed", text)
        self.assertIn("Upload completed: 1 file(s)", text)
        self.assertIn("Power-off request failed for frame", text)
        self.assertNotIn("clip.mp4", text)

    def test_log_rotation_retains_only_two_backups_and_preserves_unicode(self):
        with patch.object(diagnostics, "MAX_LOG_BYTES", 512):
            diagnostics.configure_logging(self.directory)
        for index in range(40):
            diagnostics.LOGGER.warning("Connection failure %d: %s", index, "åäö · " * 12)
        diagnostics.shutdown_logging()
        files = sorted(self.directory.glob("frame-connect-log.txt*"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 512 for path in files))
        self.assertIn("åäö", self.read_log())

    def test_repeated_initialization_appends_and_does_not_duplicate_handlers(self):
        original_hook = sys.excepthook
        diagnostics.configure_logging(self.directory)
        diagnostics.configure_logging(self.directory)
        diagnostics.LOGGER.info("first session marker")
        diagnostics.shutdown_logging()
        self.assertIs(sys.excepthook, original_hook)
        diagnostics.configure_logging(self.directory)
        diagnostics.LOGGER.info("second session marker")
        diagnostics.shutdown_logging()
        text = self.read_log()
        self.assertEqual(text.count("first session marker"), 1)
        self.assertEqual(text.count("second session marker"), 1)

    def test_log_creation_failure_still_allows_interface_to_start(self):
        blocked = self.directory / "not-a-directory"
        blocked.write_text("fixture", encoding="utf-8")
        diagnostics.configure_logging(blocked / "logs")
        window = self.make_app()
        self.assertIsNone(diagnostics.get_log_directory())
        self.assertIn("Could not create diagnostic log", window.log_status.text())
        self.assertFalse(window.logs_button.isEnabled())
        self.assertEqual(window.connect.text(), "Connect")

    def test_native_trace_setup_failure_keeps_regular_logging_available(self):
        with patch("diagnostics.faulthandler.enable", side_effect=OSError("native trace unavailable")):
            diagnostics.configure_logging(self.directory)
        window = self.make_app()
        self.assertTrue(window.logs_button.isEnabled())
        self.assertIn("native trace unavailable", window.log_status.text())
        diagnostics.LOGGER.error("regular logging still works")
        self.assertIn("regular logging still works", self.read_log())

    def test_settings_opens_the_local_log_folder_and_remains_usable_while_busy(self):
        diagnostics.configure_logging(self.directory)
        window = self.make_app()
        window._set_busy(True)
        with patch("app.QDesktopServices.openUrl", return_value=True) as open_url:
            window.logs_button.click()
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(self.directory).replace("\\", "/"))
        self.assertIn(str(self.directory), window.log_status.text())
        with patch("app.QDesktopServices.openUrl", return_value=False):
            window.logs_button.click()
        self.assertIn("Open it manually", window.log_status.text())

    def test_settings_can_open_bundled_license_notices(self):
        window = self.make_app()
        window._set_busy(True)
        with patch("app.QDesktopServices.openUrl", return_value=True) as open_url:
            window.licenses_button.click()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()).name, "THIRD_PARTY_NOTICES.txt")

    def test_background_main_qt_and_native_traces_persist_with_no_console(self):
        # A separate process proves real hook dispatch, including PyInstaller's
        # windowed stderr=None behavior. Dump a stack; do not cause a native fault.
        script = '''
from pathlib import Path
import faulthandler
import sys
import threading
import diagnostics
diagnostics.configure_logging(Path(sys.argv[1]))
sys.stderr = None
def fail_worker():
    raise RuntimeError("diagnostic worker failure")
worker = threading.Thread(target=fail_worker, name="test-worker")
worker.start()
worker.join()
diagnostics.install_qt_message_handler()
from PySide6.QtCore import QCoreApplication, QTimer, qWarning
qWarning("diagnostic Qt warning")
application = QCoreApplication([])
def fail_slot():
    raise RuntimeError("diagnostic Qt slot failure")
QTimer.singleShot(0, fail_slot)
QTimer.singleShot(1, application.quit)
application.exec()
faulthandler.dump_traceback(file=diagnostics._crash_stream, all_threads=True)
raise ValueError("diagnostic main failure")
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.directory)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        text = self.read_log()
        self.assertIn("Unhandled exception in background thread test-worker", text)
        self.assertIn("RuntimeError: diagnostic worker failure", text)
        self.assertIn("Qt: diagnostic Qt warning", text)
        self.assertIn("RuntimeError: diagnostic Qt slot failure", text)
        self.assertIn("Unhandled Python exception", text)
        self.assertIn("ValueError: diagnostic main failure", text)
        self.assertIn("Traceback (most recent call last)", text)
        crash = (self.directory / "frame-connect-crash.txt").read_text(encoding="utf-8")
        self.assertIn("Session", crash)
        self.assertIn('File "<string>"', crash)

    def test_native_crash_log_is_rotated_at_startup(self):
        path = self.directory / "frame-connect-crash.txt"
        path.write_text("previous crash trace\n" * 20, encoding="utf-8")
        previous = path.read_bytes()
        with patch.object(diagnostics, "MAX_LOG_BYTES", 128):
            diagnostics.configure_logging(self.directory)
        self.assertEqual((self.directory / "frame-connect-crash.previous.txt").read_bytes(), previous)
        self.assertIn("Session", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
