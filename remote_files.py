"""Small filesystem helper sent over SSH; not installed on the headset."""

import base64
import errno
import json
import os
from pathlib import Path
import re
import shutil
import sys


def filename(value: str) -> str:
    if not value or value in (".", "..") or any(c in value for c in "/\\\0\r\n"):
        raise ValueError("Invalid filename")
    return value


def temporary_path(folder: Path, temporary: str) -> Path:
    if not isinstance(temporary, str) or not re.fullmatch(r"\.frame-connect-[0-9a-f]{32}\.part", temporary):
        raise ValueError("Invalid temporary filename")
    return folder / temporary


def finish_upload(folder: Path, temporary: str, name: str, size: int) -> str:
    source, target = temporary_path(folder, temporary), folder / filename(name)
    if source.is_symlink() or source.stat().st_size != size:
        raise ValueError("Uploaded file size does not match the original")
    try:
        # Publish atomically without replacing an existing file, even in a race.
        os.link(source, target)
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        # Some removable filesystems do not support hard links. Exclusive
        # creation still protects existing files there.
        with source.open("rb") as reader, target.open("xb") as writer:
            try:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            except BaseException:
                writer.close()
                target.unlink(missing_ok=True)
                raise
    source.unlink()
    return str(target)


def handle(request: dict) -> dict:
    folder = Path(request["path"]).expanduser().resolve()
    action = request["action"]
    if action == "list":
        directories = sorted(
            (child for child in folder.iterdir() if child.is_dir()),
            key=lambda child: child.name.casefold(),
        )
        return {"path": str(folder), "parent": str(folder.parent),
                "folders": [{"name": child.name, "path": str(child)} for child in directories]}
    if action == "prepare":
        names = [filename(name) for name in request["names"]]
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            if os.path.lexists(folder / name):
                raise FileExistsError(f"File already exists: {name}. Choose another folder or rename the PC file.")
        return {"path": str(folder)}
    if action == "finish":
        return {"path": finish_upload(folder, request["temporary"], request["name"], request["size"])}
    if action == "progress":
        source = temporary_path(folder, request["temporary"])
        if source.is_symlink():
            raise ValueError("Invalid temporary file")
        try:
            size = source.stat().st_size
        except FileNotFoundError:
            size = 0
        return {"path": str(folder), "bytes": size}
    if action == "cleanup":
        temporary_path(folder, request["temporary"]).unlink(missing_ok=True)
        return {"cleaned": True}
    raise ValueError("Unknown file operation")


if __name__ == "__main__":
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        print(json.dumps(handle(request)))
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
