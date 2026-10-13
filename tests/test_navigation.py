import json
from threading import Event
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QMessageBox

from qt_helpers import APPLICATION, dispose, wait_until
from app import FrameConnectApp
from headset import HeadsetError, parse_status
from test_headset import SAMPLE


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.app = FrameConnectApp()
        self.app.show()
        APPLICATION.processEvents()

    def tearDown(self):
        dispose(self.app)

    def connect(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))

    def test_navigation_keeps_home_state_and_footer_position(self):
        self.connect()
        self.app.destination.setText("/home/steamos/Videos")
        self.app.refresh_actions[30000].trigger()
        size, footer = self.app.size(), self.app.status_footer.geometry()
        for _ in range(4):
            self.app.settings_tab.click()
            APPLICATION.processEvents()
            self.assertIs(self.app.pages.currentWidget(), self.app.settings_page)
            self.assertTrue(self.app.settings_tab.isChecked())
            self.assertFalse(self.app.drop_area.isVisible())
            self.assertTrue(self.app.power_button.isVisible())
            self.assertTrue(self.app.status_footer.isVisible())
            self.assertEqual(self.app.status_footer.geometry(), footer)
            self.app.home_tab.click()
            APPLICATION.processEvents()
            self.assertIs(self.app.pages.currentWidget(), self.app.home_page)
            self.assertTrue(self.app.home_tab.isChecked())
            self.assertEqual(self.app.battery.text(), "84%")
            self.assertEqual(self.app.destination.text(), "/home/steamos/Videos")
            self.assertEqual(self.app._auto_timer.interval(), 30000)
            self.assertEqual(self.app.size(), size)
            self.assertEqual(self.app.status_footer.geometry(), footer)
        self.assertLessEqual(self.app.navigation.y() + self.app.navigation.height(), self.app.pages.y())

    def test_power_is_disabled_offline_and_during_transfer_on_either_page(self):
        self.assertFalse(self.app.power_button.isEnabled())
        with patch("app.power_off_headset") as power, patch.object(self.app, "_confirm_power_off") as confirm:
            self.app.request_power_off()
            confirm.assert_not_called()
            self.connect()
            self.assertTrue(self.app.power_button.isEnabled())
            with patch("app.Thread"):
                self.app.start_upload(("C:/clip.mp4",))
            self.app.settings_tab.click()
            self.assertFalse(self.app.power_button.isEnabled())
            self.app.request_power_off()
            confirm.assert_not_called()
            power.assert_not_called()
            self.app._handle_transfer_result("uploaded", ("/home/steamos/Downloads/clip.mp4",))
            self.assertTrue(self.app.power_button.isEnabled())

    def test_confirmation_defaults_to_cancel_and_cancel_never_sends_power_command(self):
        self.connect()
        observed = []
        def cancel_dialog():
            dialog = APPLICATION.activeModalWidget()
            observed.append(isinstance(dialog, QMessageBox))
            observed.append(dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel))
            dialog.button(QMessageBox.StandardButton.Cancel).click()
        QTimer.singleShot(0, cancel_dialog)
        with patch("app.power_off_headset") as power:
            self.app.request_power_off()
            power.assert_not_called()
        self.assertEqual(observed, [True, True])
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app._auto_timer.isActive())

    def test_connection_changed_during_confirmation_cannot_power_off_a_different_headset(self):
        self.connect()
        def confirm(_host):
            self.app.host.setText("different-frame")
            return True
        with patch.object(self.app, "_confirm_power_off", side_effect=confirm), patch("app.power_off_headset") as power:
            self.app.request_power_off()
            power.assert_not_called()
        self.assertIsNone(self.app._power_request)

    def test_power_request_ignores_late_monitor_results_and_disconnects_after_acknowledgement(self):
        self.connect()
        started, release = Event(), Event()
        def power(*_args):
            started.set()
            release.wait(3)
        with patch("app.Thread"):
            self.app._auto_timer.timeout.emit()
        old_status_request = self.app._status_request
        with patch.object(self.app, "_confirm_power_off", return_value=True), patch("app.power_off_headset", side_effect=power) as request:
            try:
                self.app.request_power_off()
                self.assertTrue(started.wait(1))
                self.assertTrue(self.app.busy)
                self.assertFalse(self.app._auto_timer.isActive())
                self.assertFalse(self.app.power_button.isEnabled())
                self.app.request_power_off()
                self.app._handle_status_result(old_status_request, parse_status(json.dumps(SAMPLE)))
                self.assertTrue(self.app.busy)
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: self.app._power_request is None))
            request.assert_called_once_with("frame", "steamos", "")
        self.assertIsNone(self.app.connected_to)
        self.assertEqual(self.app.connect.text(), "Connect")
        self.assertFalse(self.app._auto_timer.isActive())
        self.assertFalse(self.app._poll_timer.isActive())
        self.assertEqual(self.app.footer_status.text(), "Power-off requested")
        self.app._handle_status_result(old_status_request, parse_status(json.dumps(SAMPLE)))
        self.assertIsNone(self.app.connected_to)

    def test_power_error_is_visible_and_monitoring_can_resume(self):
        self.connect()
        with patch.object(self.app, "_confirm_power_off", return_value=True), patch("app.power_off_headset", side_effect=HeadsetError("Steam control interface is unavailable")):
            self.app.request_power_off()
            self.assertTrue(wait_until(lambda: self.app._power_request is None))
        self.assertIn("unavailable", self.app.power_status.text())
        self.assertEqual(self.app.power_status.property("state"), "error")
        self.assertTrue(self.app._auto_timer.isActive())
        self.assertTrue(self.app.power_button.isEnabled())
        self.app.show_status(parse_status(json.dumps(SAMPLE)), automatic=True)
        self.assertIn("unavailable", self.app.power_status.text())

    def test_settings_connection_button_uses_existing_dialog(self):
        self.app.settings_tab.click()
        self.app.settings_connection_button.click()
        self.assertTrue(self.app.settings_dialog.isVisible())
        self.app.settings_dialog.reject()
        for size in ((680, 680), (800, 1100)):
            self.app.resize(*size)
            APPLICATION.processEvents()
            self.assertTrue(self.app.power_button.isVisible())
            self.assertLess(self.app.power_button.mapTo(self.app, QPoint()).y(), self.app.status_footer.y())

    def test_close_during_power_request_waits_for_result_without_retrying(self):
        self.connect()
        started, release = Event(), Event()
        def power(*_args):
            started.set()
            release.wait(3)
        with patch.object(self.app, "_confirm_power_off", return_value=True), patch("app.power_off_headset", side_effect=power) as request:
            try:
                self.app.request_power_off()
                self.assertTrue(started.wait(1))
                self.app.close()
                self.assertTrue(self.app.isVisible())
                self.assertFalse(self.app._auto_timer.isActive())
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: not self.app.isVisible()))
            request.assert_called_once()
