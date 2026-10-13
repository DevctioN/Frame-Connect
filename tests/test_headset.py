import copy
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from headset import HeadsetError, fetch_status, parse_status, validate_address
from probe import collect_status, read_batteries, read_memory, read_os_version, read_temperature


SAMPLE = {
    "hostname": "frame",
    "batteries": [{"name": "max1720x", "percent": 84, "state": "Discharging"}],
    "storage": {"total": 1000 * 1024**3, "used": 400 * 1024**3, "free": 600 * 1024**3},
}

READINGS = {
    "temperature": {"celsius": 52.5, "sensor": "soc-thermal"},
    "memory": {"total": 16 * 1024**3, "available": 10 * 1024**3},
}


@contextmanager
def fixture_directory():
    # Inherit workspace permissions; Windows' tempfile ACLs can exclude
    # restricted test processes. These fixtures contain no credentials.
    test_root = Path(__file__).resolve().parent
    directory = test_root / f".fixture-{uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent != test_root:
            raise RuntimeError("Fixture directory escaped the test folder")
        shutil.rmtree(directory)


class StatusTests(unittest.TestCase):
    def test_os_version_accepts_reported_text_and_missing_data_but_rejects_invalid_values(self):
        self.assertIsNone(parse_status(json.dumps(SAMPLE)).os_version)
        self.assertIsNone(parse_status(json.dumps(dict(SAMPLE, os_version=None))).os_version)
        self.assertEqual(
            parse_status(json.dumps(dict(SAMPLE, os_version="SteamOS 3.7-test"))).os_version,
            "SteamOS 3.7-test",
        )
        for value in ("", "  ", 3.7, True, [], {}):
            with self.subTest(value=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(dict(SAMPLE, os_version=value)))

    def test_temperature_and_memory_are_optional_and_validated(self):
        old_status = parse_status(json.dumps(SAMPLE))
        self.assertIsNone(old_status.temperature)
        self.assertIsNone(old_status.memory)
        status = parse_status(json.dumps(dict(SAMPLE, **READINGS)))
        self.assertEqual(status.temperature.celsius, 52.5)
        self.assertEqual(status.temperature.sensor, "soc-thermal")
        self.assertEqual(status.memory.total, 16 * 1024**3)
        self.assertEqual(status.memory.used, 6 * 1024**3)
        missing = parse_status(json.dumps(dict(SAMPLE, temperature=None, memory=None)))
        self.assertIsNone(missing.temperature)
        self.assertIsNone(missing.memory)

    def test_invalid_temperature_or_memory_is_rejected(self):
        for key, field, value in (
            ("temperature", "celsius", True), ("temperature", "celsius", "52.5"),
            ("temperature", "celsius", float("nan")), ("temperature", "celsius", float("inf")),
            ("temperature", "celsius", 201), ("temperature", "celsius", -101),
            ("temperature", "sensor", ""), ("temperature", "sensor", None),
            ("memory", "total", 0), ("memory", "total", True),
            ("memory", "available", -1), ("memory", "available", 17 * 1024**3),
            ("memory", "available", "100"),
        ):
            data = copy.deepcopy(dict(SAMPLE, **READINGS))
            data[key][field] = value
            with self.subTest(key=key, field=field, value=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(data))
        for key in READINGS:
            for value in ([], {}, "missing"):
                with self.subTest(key=key, value=value), self.assertRaises(HeadsetError):
                    parse_status(json.dumps(dict(SAMPLE, **{key: value})))

    def test_valid_status_and_missing_battery(self):
        status = parse_status(json.dumps(SAMPLE))
        self.assertEqual(status.batteries[0].percent, 84)
        self.assertEqual(status.free, 600 * 1024**3)
        data = copy.deepcopy(SAMPLE)
        data["batteries"] = []
        self.assertEqual(parse_status(json.dumps(data)).batteries, ())

    def test_invalid_readings_are_not_shown_as_real_data(self):
        invalid = ["not JSON", "null", "[]", "{}"]
        for field, value in [("percent", 101), ("percent", True), ("state", None)]:
            data = copy.deepcopy(SAMPLE)
            data["batteries"][0][field] = value
            invalid.append(json.dumps(data))
        for field, value in [("total", 0), ("free", -1), ("used", "400"), ("free", 1000 * 1024**3)]:
            data = copy.deepcopy(SAMPLE)
            data["storage"][field] = value
            invalid.append(json.dumps(data))
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(HeadsetError):
                parse_status(output)


class CollectorTests(unittest.TestCase):
    @patch("probe.platform.freedesktop_os_release", return_value={
        "NAME": "SteamOS", "VERSION_ID": "3.7-test", "VERSION": "3.7-test (test channel)",
    })
    def test_os_version_uses_distribution_release_and_is_included_in_status(self, release):
        self.assertEqual(read_os_version(), "SteamOS 3.7-test")
        status = parse_status(json.dumps(collect_status()))
        self.assertEqual(status.os_version, "SteamOS 3.7-test")
        self.assertTrue(release.called)

    @patch("probe.platform.freedesktop_os_release")
    def test_os_version_fallbacks_and_missing_release_do_not_break_other_readings(self, release):
        release.return_value = {"NAME": "SteamOS", "VERSION": "3.7-test (preview)"}
        self.assertEqual(read_os_version(), "SteamOS 3.7-test (preview)")
        release.return_value = {"NAME": "SteamOS", "PRETTY_NAME": "SteamOS"}
        self.assertIsNone(read_os_version())
        for error in (FileNotFoundError, PermissionError, UnicodeError):
            release.side_effect = error
            with self.subTest(error=error):
                status = parse_status(json.dumps(collect_status()))
                self.assertIsNone(status.os_version)
                self.assertGreater(status.total, 0)

    def test_temperature_uses_highest_valid_zone_and_converts_millidegrees(self):
        with fixture_directory() as root:
            for name, kind, value in (
                ("thermal_zone0", "battery", "32500"),
                ("thermal_zone7", "soc-thermal", "52500"),
                ("thermal_zone8", "offline", "not available"),
                ("thermal_zone9", "driver-error", "2147483647"),
                ("cooling_device0", "fan", "99000"),
            ):
                zone = root / name
                zone.mkdir()
                (zone / "type").write_text(kind, encoding="utf-8")
                (zone / "temp").write_text(value, encoding="utf-8")
            (root / "thermal_zone10").mkdir()
            self.assertEqual(read_temperature(root), READINGS["temperature"])
            self.assertIsNone(read_temperature(root / "absent"))

    def test_temperature_missing_or_unreadable_is_not_reported_as_zero(self):
        with fixture_directory() as root:
            zone = root / "thermal_zone1"
            zone.mkdir()
            self.assertIsNone(read_temperature(root))
            (zone / "temp").write_text("0", encoding="utf-8")
            self.assertEqual(read_temperature(root), {"celsius": 0.0, "sensor": "thermal_zone1"})
            with patch("probe.Path.read_text", side_effect=PermissionError):
                self.assertIsNone(read_temperature(root))

    def test_memory_uses_available_not_free_memory_and_converts_kib_to_bytes(self):
        with fixture_directory() as root:
            path = root / "meminfo"
            path.write_text("MemTotal: 16777216 kB\nMemFree: 1048576 kB\nMemAvailable: 10485760 kB\n", encoding="utf-8")
            self.assertEqual(read_memory(path), READINGS["memory"])
            path.write_text("MemTotal: 16777216 kB\nMemAvailable: 0 kB\n", encoding="utf-8")
            self.assertEqual(read_memory(path)["available"], 0)

    def test_invalid_or_unavailable_memory_is_omitted(self):
        with fixture_directory() as root:
            path = root / "meminfo"
            self.assertIsNone(read_memory(path))
            for source in (
                "MemTotal: 100 kB\nMemFree: 50 kB\n",
                "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
                "MemTotal: 100 kB\nMemAvailable: -1 kB\n",
                "MemTotal: 100 kB\nMemAvailable: 101 kB\n",
                "MemTotal: 100 MB\nMemAvailable: 50 kB\n",
                "MemTotal: 100 kB\nMemAvailable: unknown kB\n",
                "MemTotal: 100 kB\nMemAvailable: 50 kB\nMemAvailable: 60 kB\n",
            ):
                path.write_text(source, encoding="utf-8")
                with self.subTest(source=source):
                    self.assertIsNone(read_memory(path))
            with patch("probe.Path.read_text", side_effect=PermissionError):
                self.assertIsNone(read_memory(path))

    def test_collector_includes_new_readings_in_the_status_response(self):
        with patch("probe.read_temperature", return_value=READINGS["temperature"]), patch(
            "probe.read_memory", return_value=READINGS["memory"]
        ):
            status = parse_status(json.dumps(collect_status()))
        self.assertEqual(status.temperature.celsius, 52.5)
        self.assertEqual(status.memory.used, 6 * 1024**3)

    def test_system_battery_is_found_without_assuming_bat0(self):
        with fixture_directory() as root:
            supplies = {
                "max1720x": {"type": "Battery", "scope": "System", "capacity": "84", "status": "Discharging"},
                "controller": {"type": "Battery", "scope": "Device", "capacity": "12"},
                "charger": {"type": "USB", "capacity": "100"},
                "removed": {"type": "Battery", "present": "0", "capacity": "50"},
                "invalid": {"type": "Battery", "capacity": "101"},
                "missing": {"type": "Battery"},
            }
            for name, attributes in supplies.items():
                supply = root / name
                supply.mkdir()
                for attribute, value in attributes.items():
                    (supply / attribute).write_text(value, encoding="utf-8")
            self.assertEqual(read_batteries(root), SAMPLE["batteries"])
            self.assertEqual(read_batteries(root / "absent"), [])

    def test_probe_runs_from_stdin_and_returns_parseable_status(self):
        source = (Path(__file__).resolve().parents[1] / "probe.py").read_text(encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-"], input=source, text=True, capture_output=True,
            encoding="utf-8", check=True, timeout=5,
        )
        self.assertGreater(parse_status(result.stdout).total, 0)


class ConnectionTests(unittest.TestCase):
    def test_addresses_cannot_be_ssh_options_or_shell_commands(self):
        for host in ["frame", "frame.local", "192.168.1.100", "fe80::1234"]:
            validate_address(host, "steamos")
        for host in ["", "-oProxyCommand=calc", "frame; reboot", "steamos@frame", "frame:22"]:
            with self.subTest(host=host), self.assertRaises(HeadsetError):
                validate_address(host, "steamos")

    @patch("headset.shutil.which", return_value="ssh")
    @patch("headset.subprocess.run")
    def test_ssh_uses_verified_keys_and_sends_probe_over_stdin(self, run, _which):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(SAMPLE), "")
        self.assertEqual(fetch_status("frame").hostname, "frame")
        arguments, options = run.call_args
        self.assertIn("BatchMode=yes", arguments[0])
        self.assertIn("StrictHostKeyChecking=yes", arguments[0])
        self.assertEqual(arguments[0][-6:], ["-l", "steamos", "--", "frame", "python3", "-"])
        self.assertIn("def collect_status", options["input"])
        self.assertFalse(options.get("shell", False))

    @patch("headset.shutil.which", return_value="ssh")
    @patch("headset.subprocess.run")
    def test_connection_failures_provide_useful_errors(self, run, _which):
        failures = [
            ("Permission denied (publickey,password).", "SSH key login failed"),
            ("Host key verification failed.", "First connect with SSH"),
            ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "identity changed"),
            ("ssh: Could not resolve hostname frame", "Could not resolve hostname"),
            ("sh: python3: command not found", "Python 3 is unavailable"),
        ]
        for stderr, expected in failures:
            run.return_value = subprocess.CompletedProcess([], 255, "", stderr)
            with self.subTest(stderr=stderr), self.assertRaisesRegex(HeadsetError, expected):
                fetch_status("frame")
        run.side_effect = subprocess.TimeoutExpired("ssh", 20)
        with self.assertRaisesRegex(HeadsetError, "timed out"):
            fetch_status("frame")

    @patch("headset.shutil.which", return_value=None)
    def test_missing_ssh_is_explained(self, _which):
        with self.assertRaisesRegex(HeadsetError, "OpenSSH Client"):
            fetch_status("frame")

    @patch("headset.shutil.which", return_value="ssh")
    @patch("headset.subprocess.run")
    def test_selected_key_is_passed_as_one_argument(self, run, _which):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(SAMPLE), "")
        with fixture_directory() as directory:
            key = directory / "private key"
            key.write_text("test fixture; not a real key", encoding="utf-8")
            fetch_status("frame", key_file=str(key))
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-i") + 1], str(key))
            self.assertIn("IdentitiesOnly=yes", command)


if __name__ == "__main__":
    unittest.main()
