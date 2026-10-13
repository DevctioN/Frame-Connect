import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from headset import HeadsetError, power_off_headset, remote_script_source
from remote_power import request_power_off


class PowerTests(unittest.TestCase):
    @patch("remote_power.query_steam")
    def test_only_explicit_steam_acknowledgement_counts_as_requested(self, query):
        query.return_value = {"requested": True}
        self.assertEqual(request_power_off(), {"requested": True})
        query.return_value = {"requested": False, "error": "Shutdown is unavailable"}
        self.assertEqual(request_power_off()["error"], "Shutdown is unavailable")
        for value in (None, True, 1, "ok", {"requested": 1}):
            query.return_value = value
            with self.subTest(value=value):
                self.assertFalse(request_power_off()["requested"])
                self.assertIn("Check the headset", request_power_off()["error"])

    @patch("headset.shutil.which", return_value="ssh")
    @patch("headset.subprocess.run")
    def test_power_request_uses_existing_verified_ssh_and_sends_helpers_in_memory(self, run, _which):
        run.return_value = subprocess.CompletedProcess([], 0, '{"requested":true}', "")
        power_off_headset("frame", "steamos")
        command = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(command[-6:], ["-l", "steamos", "--", "frame", "python3", "-"])
        self.assertIn("ShutdownPC", run.call_args.kwargs["input"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @patch("headset.shutil.which", return_value="ssh")
    @patch("headset.subprocess.run")
    def test_errors_and_disconnects_do_not_retry_or_claim_success(self, run, _which):
        for output in ('{"requested":false,"error":"Not supported"}', 'not JSON', '{"requested":1}'):
            run.return_value = subprocess.CompletedProcess([], 0, output, "")
            with self.subTest(output=output), self.assertRaises(HeadsetError):
                power_off_headset("frame")
        run.reset_mock()
        run.return_value = subprocess.CompletedProcess([], 255, "", "Connection closed")
        with self.assertRaisesRegex(HeadsetError, "Check the headset"):
            power_off_headset("frame")
        run.assert_called_once()
        run.side_effect = subprocess.TimeoutExpired("ssh", 20)
        with self.assertRaisesRegex(HeadsetError, "timed out"):
            power_off_headset("frame")

    def test_status_payload_stays_read_only_and_runs_without_installed_helper(self):
        source = remote_script_source("probe.py")
        self.assertNotIn("ShutdownPC", source)
        result = subprocess.run([sys.executable, "-I", "-"], input=source, text=True,
                                capture_output=True, timeout=5, check=True)
        self.assertGreater(json.loads(result.stdout)["storage"]["total"], 0)
