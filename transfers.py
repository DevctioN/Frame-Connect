"""Remote folder browsing and cancellable SFTP uploads."""

import base64
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from threading import Event
from time import monotonic
from uuid import uuid4

from headset import HeadsetError, ssh_command, ssh_error, ssh_options, validate_address


class TransferError(HeadsetError):
    def __init__(self, message: str, completed: int = 0):
        super().__init__(message)
        self.completed = completed


class TransferCancelled(TransferError):
    pass


def validate_destination(path: str) -> str:
    if not isinstance(path, str):
        raise HeadsetError("The destination must be a SteamOS folder path.")
    path = path.strip()
    if not (path.startswith("/") or path == "~" or path.startswith("~/")):
        raise HeadsetError("Use a SteamOS folder path such as ~/Downloads or /home/steamos/Documents.")
    if any(c in path for c in "\0\r\n"):
        raise HeadsetError("The destination contains an invalid character.")
    return path


def file_request(host: str, user: str, key_file: str, *, timeout=20, **request) -> dict:
    request["path"] = validate_destination(request["path"])
    encoded = base64.urlsafe_b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    command = [*ssh_command(host, user, key_file), "python3", "-", encoded]
    try:
        result = subprocess.run(
            command, input=Path(__file__).with_name("remote_files.py").read_text(encoding="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise HeadsetError("The headset did not respond. Wake it and check your network.") from error
    except OSError as error:
        raise HeadsetError(f"Could not access the headset folder: {error}") from error
    if result.returncode:
        raise ssh_error(result.stderr.strip())
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("Not an object")
        if "error" in data:
            raise HeadsetError(str(data["error"]))
        if request["action"] != "cleanup":
            if not isinstance(data.get("path"), str) or not data["path"].startswith("/"):
                raise ValueError("Invalid folder path")
            validate_destination(data["path"])
        if request["action"] == "list":
            validate_destination(data["parent"])
            if not isinstance(data["folders"], list):
                raise ValueError("Invalid folder list")
            for folder in data["folders"]:
                if not isinstance(folder["name"], str):
                    raise ValueError("Invalid folder name")
                validate_destination(folder["path"])
        if request["action"] == "progress":
            if type(data.get("bytes")) is not int or data["bytes"] < 0:
                raise ValueError("Invalid upload byte count")
        return data
    except (ValueError, TypeError, KeyError) as error:
        raise HeadsetError("The headset returned an unreadable folder response.") from error


def list_folders(host: str, user: str, key_file: str, path: str) -> dict:
    return file_request(host, user, key_file, action="list", path=path)


def sftp_quote(path: str) -> str:
    if any(c in path for c in "\0\r\n"):
        raise HeadsetError("A file path contains an unsupported character.")
    # Quoted glob characters are already escaped by OpenSSH's batch parser.
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send_file(command: list[str], batch: str, cancel: Event, *, sample: Callable[[], None] | None = None) -> None:
    try:
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process:
            first = True
            last_sample = monotonic()
            while True:
                if cancel.is_set():
                    process.kill()
                    process.communicate()
                    raise TransferCancelled("Transfer cancelled")
                try:
                    _output, error = process.communicate(input=batch if first else None, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    now = monotonic()
                    if sample is not None and now - last_sample >= 1:
                        last_sample = now
                        sample()
            if process.returncode:
                raise ssh_error(error.strip())
    except OSError as error:
        raise HeadsetError(f"Could not transfer the file: {error}") from error


def upload_files(
    host: str, user: str, key_file: str, paths: Sequence[str], destination: str,
    cancel: Event, progress: Callable[[str, int, int, int, int], None],
    *, on_verifying: Callable[[str, int, int], None] | None = None,
) -> tuple[str, ...]:
    files = [Path(path).absolute() for path in paths]
    if not files:
        raise TransferError("Choose at least one file.")
    for file in files:
        if not file.is_file():
            raise TransferError(f"Drop files only. This is a folder or unavailable file: {file.name}")
        sftp_quote(file.as_posix())
    if len({file.name for file in files}) != len(files):
        raise TransferError("Some dropped files have the same name. Rename them or send them separately.")
    destination = validate_destination(destination)
    sizes = [file.stat().st_size for file in files]
    total_bytes = sum(sizes)
    completed_bytes = 0
    host, user = host.strip(), user.strip()
    validate_address(host, user)
    sftp = shutil.which("sftp")
    if not sftp:
        raise TransferError("SFTP is missing. Enable OpenSSH Client in Windows Optional Features.")
    options = ssh_options(key_file)
    target = f"[{host}]" if ":" in host else host
    command = [sftp, "-b", "-", *options, "-o", f"User={user}", "--", target]
    completed = []
    temporary = None
    folder = destination
    try:
        if cancel.is_set():
            raise TransferCancelled("Transfer cancelled")
        folder = file_request(host, user, key_file, action="prepare", path=destination,
                              names=[file.name for file in files])["path"]
        for index, file in enumerate(files):
            if cancel.is_set():
                raise TransferCancelled("Transfer cancelled")
            size = sizes[index]
            temporary = f".frame-connect-{uuid4().hex}.part"
            remote = str(PurePosixPath(folder) / temporary)
            observed = 0

            def sample():
                nonlocal observed
                try:
                    reading = file_request(host, user, key_file, timeout=2, action="progress",
                                           path=folder, temporary=temporary)
                except HeadsetError:
                    # A missing progress reading must not interrupt the upload.
                    return
                observed = max(observed, min(size, reading["bytes"]))
                progress(file.name, index, len(files), completed_bytes + observed, total_bytes)

            progress(file.name, index, len(files), completed_bytes, total_bytes)
            send_file(command, f"put {sftp_quote(file.as_posix())} {sftp_quote(remote)}\n", cancel, sample=sample)
            if cancel.is_set():
                raise TransferCancelled("Transfer cancelled")
            progress(file.name, index, len(files), completed_bytes + size, total_bytes)
            if on_verifying is not None:
                on_verifying(file.name, index, len(files))
            saved = file_request(host, user, key_file, timeout=None, action="finish", path=folder,
                                 temporary=temporary, name=file.name, size=size)
            completed.append(saved["path"])
            completed_bytes += size
            temporary = None
            progress(file.name, index + 1, len(files), completed_bytes, total_bytes)
        return tuple(completed)
    except (HeadsetError, OSError) as error:
        cleanup_error = ""
        if temporary:
            try:
                file_request(host, user, key_file, action="cleanup", path=folder, temporary=temporary)
            except HeadsetError:
                cleanup_error = f" A partial file may remain at {PurePosixPath(folder) / temporary}."
        kind = TransferCancelled if isinstance(error, TransferCancelled) else TransferError
        raise kind(f"{error}{cleanup_error}", len(completed)) from error
