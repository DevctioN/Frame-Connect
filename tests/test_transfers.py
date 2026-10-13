import base64
import errno
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

from headset import HeadsetError
from probe import downloads_directory
from remote_files import finish_upload, handle
from test_headset import fixture_directory
from transfers import TransferCancelled, TransferError, file_request, send_file, upload_files


TEMPORARY = ".frame-connect-" + "a" * 32 + ".part"


class RemoteFilesTests(unittest.TestCase):
    def test_progress_reads_only_valid_staging_files_and_tracks_growth(self):
        with fixture_directory() as root:
            request = {"action": "progress", "path": str(root), "temporary": TEMPORARY}
            self.assertEqual(handle(request)["bytes"], 0)
            source = root / TEMPORARY
            source.write_bytes(b"first")
            self.assertEqual(handle(request)["bytes"], 5)
            with source.open("ab") as writer:
                writer.write(b" second")
            self.assertEqual(handle(request)["bytes"], 12)
            for name in ("existing.txt", "../escape.txt"):
                with self.assertRaises(ValueError):
                    handle(dict(request, temporary=name))

    def test_browse_includes_hidden_folders_and_prepares_destination(self):
        with fixture_directory() as root:
            (root / "Documents").mkdir()
            (root / ".local").mkdir()
            (root / "file.txt").write_text("not a folder", encoding="utf-8")
            listing = handle({"action": "list", "path": str(root)})
            self.assertEqual([item["name"] for item in listing["folders"]], [".local", "Documents"])
            destination = root / "Downloads" / "VR videos"
            handle({"action": "prepare", "path": str(destination), "names": ["clip.mp4"]})
            self.assertTrue(destination.is_dir())

    def test_publish_keeps_existing_files_and_checks_size(self):
        with fixture_directory() as root:
            source = root / TEMPORARY
            source.write_bytes(b"new data")
            target = root / "clip [1].mp4"
            target.write_bytes(b"existing data")
            with self.assertRaises(FileExistsError):
                finish_upload(root, TEMPORARY, target.name, 8)
            self.assertEqual(target.read_bytes(), b"existing data")
            with self.assertRaises(ValueError):
                finish_upload(root, TEMPORARY, "fresh.mp4", 999)
            saved = finish_upload(root, TEMPORARY, "fresh.mp4", 8)
            self.assertEqual(Path(saved).read_bytes(), b"new data")
            self.assertFalse(source.exists())

    def test_removable_filesystem_fallback_also_refuses_overwrites(self):
        with fixture_directory() as root:
            source = root / TEMPORARY
            source.write_bytes(b"hello")
            with patch("remote_files.os.link", side_effect=OSError(errno.ENOTSUP, "No hard links")):
                saved = finish_upload(root, TEMPORARY, "new.txt", 5)
                self.assertEqual(Path(saved).read_bytes(), b"hello")
                source.write_bytes(b"other")
                with self.assertRaises(FileExistsError):
                    finish_upload(root, TEMPORARY, "new.txt", 5)
            self.assertEqual(Path(saved).read_bytes(), b"hello")

    def test_collision_and_cleanup_cannot_remove_user_files(self):
        with fixture_directory() as root:
            (root / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                handle({"action": "prepare", "path": str(root), "names": ["existing.txt"]})
            for action in ("cleanup", "finish"):
                with self.assertRaises(ValueError):
                    handle({"action": action, "path": str(root), "temporary": "existing.txt",
                            "name": "new.txt", "size": 4})
            self.assertEqual((root / "existing.txt").read_text(encoding="utf-8"), "keep")
            with self.assertRaises(ValueError):
                handle({"action": "prepare", "path": str(root), "names": ["../escape.txt"]})

    def test_remote_helper_runs_over_stdin_with_encoded_paths(self):
        with fixture_directory() as root:
            request = {"action": "list", "path": str(root)}
            encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
            source = (Path(__file__).resolve().parents[1] / "remote_files.py").read_text(encoding="utf-8")
            result = subprocess.run([sys.executable, "-", encoded], input=source, text=True,
                                    capture_output=True, check=True, timeout=5)
            self.assertEqual(json.loads(result.stdout)["path"], str(root))

    @patch("probe.subprocess.run")
    def test_downloads_default_uses_headset_user_directory(self, run):
        localized = str(Path.home() / "Lataukset")
        run.return_value = subprocess.CompletedProcess([], 0, localized + "\n", "")
        self.assertEqual(downloads_directory(), localized)
        run.side_effect = FileNotFoundError()
        self.assertEqual(downloads_directory(), str(Path.home() / "Downloads"))


class TransferTests(unittest.TestCase):
    @patch("transfers.shutil.which", return_value="sftp")
    def test_verification_callback_follows_sending_and_precedes_publication(self, _which):
        with fixture_directory() as root:
            files = [root / "empty.txt", root / "clip.mp4"]
            files[0].touch()
            files[1].write_bytes(b"data")
            verifying, published = [], []
            progress = Mock()
            def on_verifying(name, completed, total):
                self.assertEqual(send.call_count, completed + 1)
                self.assertNotIn(name, published)
                self.assertEqual(progress.call_args.args[1:], (completed, total, 0 if completed == 0 else 4, 4))
                verifying.append((name, completed, total))
            def request(*_connection, **data):
                if data["action"] == "finish":
                    self.assertEqual(verifying[-1][0], data["name"])
                    published.append(data["name"])
                return {"path": "/home/steamos/Downloads" + ("/" + data["name"] if data["action"] == "finish" else "")}
            with patch("transfers.file_request", side_effect=request), patch("transfers.send_file") as send:
                saved = upload_files("frame", "steamos", "", [str(f) for f in files], "~/Downloads", Event(), progress, on_verifying=on_verifying)
            self.assertEqual(verifying, [("empty.txt", 0, 2), ("clip.mp4", 1, 2)])
            self.assertEqual(published, [file.name for file in files])
            self.assertEqual(len(saved), 2)

    @patch("transfers.shutil.which", return_value="sftp")
    def test_multiple_files_are_staged_then_published_using_same_identity(self, _which):
        with fixture_directory() as root:
            files = [root / "clip [1].mp4", root / "Ää file.txt"]
            for file in files:
                file.write_bytes(b"abc")
            requests, batches = [], []

            def request(*connection, **data):
                requests.append((connection, data))
                return {"path": "/home/steamos/Downloads" + ("/" + data["name"] if data["action"] == "finish" else "")}

            with patch("transfers.file_request", side_effect=request), patch("transfers.send_file") as send:
                progress = Mock()
                saved = upload_files("frame", "steamos", "", [str(f) for f in files], "~/Downloads", Event(), progress)
                batches = send.call_args_list
            self.assertEqual(len(saved), 2)
            self.assertEqual([data["action"] for _, data in requests], ["prepare", "finish", "finish"])
            self.assertTrue(all(connection == ("frame", "steamos", "") for connection, _ in requests))
            command, batch, _cancel = batches[0].args
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("User=steamos", command)
            self.assertIn('clip [1].mp4"', batch)
            self.assertIn(".part\"", batch)
            self.assertEqual(batch.count("\n"), 1)
            self.assertEqual(progress.call_args.args[1:], (2, 2, 6, 6))
            self.assertTrue(all(file.exists() for file in files))

    @patch("transfers.shutil.which", return_value="sftp")
    def test_cancel_or_failure_cleans_only_staging_file_and_reports_completed_count(self, _which):
        with fixture_directory() as root:
            files = [root / "one.txt", root / "two.txt"]
            for file in files:
                file.write_bytes(b"abc")

            def request(*_connection, **data):
                return {"path": "/home/steamos/Downloads/" + data.get("name", "")}

            for failure in (HeadsetError("Network lost"), TransferCancelled("Transfer cancelled")):
                with self.subTest(failure=failure), patch("transfers.file_request", side_effect=request) as remote:
                    with patch("transfers.send_file", side_effect=[None, failure]):
                        with self.assertRaises(TransferError) as caught:
                            upload_files("frame", "steamos", "", [str(f) for f in files], "~/Downloads", Event(), Mock())
                    self.assertEqual(caught.exception.completed, 1)
                    self.assertEqual(remote.call_args.kwargs["action"], "cleanup")
                    self.assertTrue(remote.call_args.kwargs["temporary"].startswith(".frame-connect-"))
                    self.assertEqual(isinstance(caught.exception, TransferCancelled), isinstance(failure, TransferCancelled))

    @patch("transfers.file_request")
    def test_folders_and_duplicate_names_are_rejected_before_contacting_headset(self, remote):
        with fixture_directory() as root:
            file = root / "file.txt"
            file.write_bytes(b"abc")
            for paths in ([str(root)], [str(file), str(file)]):
                with self.assertRaises(TransferError):
                    upload_files("frame", "steamos", "", paths, "~/Downloads", Event(), Mock())
            remote.assert_not_called()

    @patch("transfers.subprocess.Popen")
    @patch("transfers.monotonic", side_effect=[0, 0.2, 1.2])
    def test_sftp_samples_progress_while_waiting_and_sends_batch_once(self, _clock, popen):
        process = popen.return_value.__enter__.return_value
        process.returncode = 0
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("sftp", 0.2),
            subprocess.TimeoutExpired("sftp", 0.2),
            ("", ""),
        ]
        sample = Mock()
        send_file(["sftp"], "put fixture\n", Event(), sample=sample)
        sample.assert_called_once_with()
        self.assertEqual([call.kwargs["input"] for call in process.communicate.call_args_list], ["put fixture\n", None, None])

    @patch("transfers.shutil.which", return_value="sftp")
    def test_byte_progress_weights_files_and_survives_missing_or_regressing_samples(self, _which):
        with fixture_directory() as root:
            files = [root / "small.txt", root / "large.mp4"]
            files[0].write_bytes(b"1234")
            files[1].write_bytes(b"012345678901")
            readings = iter([2, 1, HeadsetError("Sample timed out"), 6, 99, 3])
            requests = []
            def request(*_connection, **data):
                requests.append(data)
                if data["action"] == "progress":
                    reading = next(readings)
                    if isinstance(reading, HeadsetError):
                        raise reading
                    self.assertEqual(data["timeout"], 2)
                    return {"path": "/home/steamos/Downloads", "bytes": reading}
                return {"path": "/home/steamos/Downloads" + ("/" + data["name"] if data["action"] == "finish" else "")}
            def send(_command, _batch, _cancel, *, sample):
                for _ in range(3):
                    sample()
            with patch("transfers.file_request", side_effect=request), patch("transfers.send_file", side_effect=send):
                progress = Mock()
                saved = upload_files("frame", "steamos", "", [str(file) for file in files], "~/Downloads", Event(), progress)
            values = [call.args[3] for call in progress.call_args_list]
            self.assertEqual(values, sorted(values))
            self.assertIn(2, values)
            self.assertIn(10, values)
            self.assertEqual(progress.call_args.args[1:], (2, 2, 16, 16))
            self.assertTrue(all(call.args[4] == 16 for call in progress.call_args_list))
            self.assertEqual(len(saved), 2)
            self.assertNotIn("cleanup", [request["action"] for request in requests])

    @patch("transfers.shutil.which", return_value="sftp")
    def test_zero_byte_file_reports_completion_without_division(self, _which):
        with fixture_directory() as root:
            file = root / "empty.txt"
            file.touch()
            def request(*_connection, **data):
                return {"path": "/home/steamos/Downloads" + ("/" + data["name"] if data["action"] == "finish" else "")}
            with patch("transfers.file_request", side_effect=request), patch("transfers.send_file"):
                progress = Mock()
                upload_files("frame", "steamos", "", [str(file)], "~/Downloads", Event(), progress)
            self.assertEqual(progress.call_args.args[1:], (1, 1, 0, 0))

    @patch("transfers.ssh_command", return_value=["ssh", "frame"])
    @patch("transfers.subprocess.run")
    def test_progress_responses_require_nonnegative_integer_bytes(self, run, _ssh):
        for value in (-1, True, "12", None):
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"path": "/home/steamos/Downloads", "bytes": value}), "")
            with self.subTest(value=value), self.assertRaises(HeadsetError):
                file_request("frame", "steamos", "", action="progress", path="~/Downloads", temporary=TEMPORARY)

    @patch("transfers.subprocess.Popen")
    def test_cancel_terminates_sftp_process(self, popen):
        process = popen.return_value.__enter__.return_value
        cancel = Event()
        cancel.set()
        with self.assertRaises(TransferCancelled):
            send_file(["sftp"], "put fixture\n", cancel)
        process.kill.assert_called_once()
        process.communicate.assert_called_once()

    @patch("transfers.ssh_command", return_value=["ssh", "frame"])
    @patch("transfers.subprocess.run")
    def test_folder_paths_are_data_rather_than_remote_shell_commands(self, run, _ssh):
        run.return_value = subprocess.CompletedProcess([], 0, '{"path":"/home/steamos/odd; name"}', "")
        file_request("frame", "steamos", "", action="prepare", path="~/odd; name", names=["a.txt"])
        command = run.call_args.args[0]
        request = json.loads(base64.urlsafe_b64decode(command[-1]))
        self.assertEqual(request["path"], "~/odd; name")
        self.assertNotIn("~/odd; name", command)


if __name__ == "__main__":
    unittest.main()
