"""The public download includes notices/sources and excludes private files."""

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tarfile
import unittest
from unittest.mock import patch
import zipfile

from test_headset import fixture_directory
from prepare_release import copy_qt_notices, get_sources, package_release
import build_exe


class ReleaseTests(unittest.TestCase):
    def test_source_build_can_use_the_public_logo_without_private_ico_files(self):
        with fixture_directory() as root:
            logo = root / "docs" / "assets" / "frame-connect.png"
            logo.parent.mkdir(parents=True)
            logo.write_bytes(Path(__file__).resolve().parents[1].joinpath("docs/assets/frame-connect.png").read_bytes())
            for name in ("LICENSE", "THIRD_PARTY_NOTICES.txt", "SOURCES.md"):
                (root / name).write_text("test notice", encoding="utf-8")
            (root / "licenses").mkdir()
            with patch.dict(os.environ), patch.object(build_exe, "__file__", str(root / "build_exe.py")), patch("build_exe.PyInstaller.__main__.run") as build, patch("build_exe.refresh_windows_icons"):
                build_exe.main()
            options = build.call_args.args[0]
            self.assertEqual(options[options.index("--name") + 1], "FrameConnect")
            icon = Path(options[options.index("--icon") + 1])
            self.assertTrue(icon.is_file())
            self.assertEqual(icon.parent, root / "build")
            self.assertIn(f"{root / 'licenses'}:licenses", options)

    def test_release_uses_a_whitelist_and_keeps_corresponding_sources_separate(self):
        with fixture_directory() as root:
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.5"\n', encoding="utf-8")
            (root / "FrameConnect.exe").write_bytes(b"test executable")
            for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", "SOURCES.md"):
                (root / name).write_text("test notice", encoding="utf-8")
            (root / "licenses").mkdir()
            (root / "licenses" / "source-manifest.json").write_text("[]", encoding="utf-8")
            (root / "licenses" / "LGPL-3.0.txt").write_text("test library license", encoding="utf-8")
            source_folder = root / "release" / "third-party-sources"
            source_folder.mkdir(parents=True)
            (source_folder / "library.tar.xz").write_bytes(b"test corresponding source")
            for private_name in (".env", "id_ed25519", "crash-log.txt"):
                (root / private_name).write_text("must not ship", encoding="utf-8")
            windows, sources, sums = package_release(root, [{"filename": "library.tar.xz"}])
            with zipfile.ZipFile(windows) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {
                    "FrameConnect.exe", "LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", "SOURCES.md",
                    "licenses/source-manifest.json", "licenses/LGPL-3.0.txt",
                })
            with zipfile.ZipFile(sources) as archive:
                self.assertEqual(archive.read("library.tar.xz"), b"test corresponding source")
                self.assertNotIn("FrameConnect.exe", archive.namelist())
            for path in (windows, sources):
                self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest(), sums.read_text())

    def test_a_wrong_source_download_never_replaces_the_verified_source_path(self):
        with fixture_directory() as root:
            expected = hashlib.sha256(b"authentic source").hexdigest()
            with patch("prepare_release.SOURCE_ARCHIVES", (("library.tar.xz", expected),)), patch("prepare_release.urlopen", return_value=BytesIO(b"wrong source")):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    get_sources(root)
            self.assertFalse((root / "release" / "third-party-sources" / "library.tar.xz").exists())

    def test_upstream_license_reference_cannot_escape_its_source_archive(self):
        with fixture_directory() as root:
            source = root / "source.tar"
            record = {"Id": "test", "QDocModule": "qtcore", "QtParts": ["libs"], "LicenseFile": "../../../outside.txt"}
            data = json.dumps(record).encode()
            with tarfile.open(source, "w") as archive:
                info = tarfile.TarInfo("qtbase/src/test/qt_attribution.json")
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
            with self.assertRaisesRegex(RuntimeError, "Missing upstream license"):
                copy_qt_notices(source, root / "licenses")
            self.assertFalse((root / "outside.txt").exists())
