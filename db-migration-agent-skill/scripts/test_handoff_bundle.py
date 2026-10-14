#!/usr/bin/env python3
"""Local-only handoff behavior and adversarial fixtures; no engagement inputs required.

Run: python3 scripts/test_handoff_bundle.py
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared" / "scripts"))
import handoff_bundle as hb  # noqa: E402


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /var and /tmp are aliases: exercise real paths, not those symlinks.
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "engagement"
        self.root.mkdir()
        self.plan = self.root / "migration-plan.md"
        self.plan.write_bytes(b"# Migration plan\nSee validation and external evidence.\n")
        (self.root / "runbooks").mkdir()
        self.runbook = self.root / "runbooks" / "cutover.md"
        self.runbook.write_bytes(b"# Cutover\nReviewed instructions.\n")
        self.config = self.base / "reviewed.json"
        self.output = self.base / "handoff.zip"
        self.value = {
            "version": 1,
            "reviewed_nonsecret": True,
            "files": ["runbooks/cutover.md", "migration-plan.md"],
            "references": [
                {"ref": "migration-plan.md", "status": "included", "path": "migration-plan.md"},
                {"ref": "runbooks/cutover.md §Rollback", "status": "included",
                 "path": "runbooks/cutover.md"},
                {"ref": "validation-evidence", "status": "external",
                 "reason": "Held by the customer; retrieval needs separate access."},
                {"ref": "raw-database-export", "status": "omitted",
                 "reason": "Contains production data; intentionally excluded."},
            ],
        }
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.value), encoding="utf-8")

    def create(self):
        return hb.create_bundle(self.root, self.config, self.output)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hb.main(list(map(str, args)))
        return rc, out.getvalue(), err.getvalue()

    def assert_create_fails(self):
        rc, out, err = self.cli("create", "--root", self.root, "--config", self.config,
                                "--output", self.output)
        self.assertEqual(rc, 1, (out, err))
        self.assertTrue(err)
        self.assertEqual(out, "")
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.base.glob(".handoff-*.tmp")), [])
        return err

    def fixture(self, manifest=None, members=None):
        """Build ZIPs independently of the writer to exercise the verification contract."""
        if manifest is None:
            manifest = {
                "version": 1, "scope": "declared-only", "reviewed_nonsecret": True,
                "files": [{"path": "notes.md", "size": 6,
                           "sha256": hashlib.sha256(b"notes\n").hexdigest()}],
                "references": [],
                "reference_counts": {"included": 0, "external": 0, "omitted": 0},
            }
        if members is None:
            members = [("files/notes.md", b"notes\n"),
                       ("manifest.json", json.dumps(manifest).encode())]
        fixture = self.base / "fixture.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(fixture, "w") as archive:
                for name, data in members:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o400) << 16
                    archive.writestr(info, data)
        return fixture

    def assert_verify_fails(self, path):
        rc, out, err = self.cli("verify", path)
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual(out, "")
        self.assertTrue(err)
        return err

    def test_snapshot_is_exact_private_deterministic_and_self_contained(self):
        (self.root / "credentials.txt").write_text("NEVER INCLUDE")
        (self.root / "dump.sql").write_text("NEVER INCLUDE")
        (self.root / "activity.log").write_text("NEVER INCLUDE")
        unicode_file = self.root / "검토.md"
        unicode_file.write_bytes(b"\x00binary\xff\n")
        self.value["files"].append(unicode_file.name)
        self.save()
        manifest, digest = self.create()
        self.assertEqual(manifest["scope"], "declared-only")
        self.assertEqual(manifest["reference_counts"], {"included": 2, "external": 1, "omitted": 1})
        self.assertEqual(manifest["references"], sorted(self.value["references"], key=lambda r: r["ref"]))
        self.assertEqual(digest, hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o400)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {
                "files/migration-plan.md", "files/runbooks/cutover.md",
                "files/검토.md", "manifest.json"})
            self.assertEqual(json.loads(archive.read("manifest.json")), manifest)
            for entry in manifest["files"]:
                data = (self.root / entry["path"]).read_bytes()
                self.assertEqual(archive.read("files/" + entry["path"]), data)
                self.assertEqual(entry["size"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(all(i.external_attr >> 16 == stat.S_IFREG | 0o400
                                for i in archive.infolist()))
        other = self.base / "handoff-v2.zip"
        self.value["files"].reverse()
        self.value["references"].reverse()
        self.save()
        self.assertEqual(hb.create_bundle(self.root, self.config, other), (manifest, digest))
        self.assertEqual(other.read_bytes(), self.output.read_bytes())
        shutil.rmtree(self.root)
        self.config.unlink()
        self.assertEqual(hb.verify_bundle(self.output), (manifest, digest))

    def test_each_source_content_is_read_only_once(self):
        original = os.read
        seen = {}
        def read(fd, size):
            data = original(fd, size)
            inode = os.fstat(fd).st_ino
            seen[inode] = seen.get(inode, 0) + len(data)
            return data
        with mock.patch.object(hb.os, "read", side_effect=read):
            self.create()
        self.assertEqual(seen, {self.plan.stat().st_ino: self.plan.stat().st_size,
                                self.runbook.stat().st_ino: self.runbook.stat().st_size})

    def test_cli_end_to_end_and_exit_status(self):
        command = [sys.executable, "-B", hb.__file__]
        created = subprocess.run(command + ["create", "--root", str(self.root),
                                 "--config", str(self.config), "--output", str(self.output)],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        verified = subprocess.run(command + ["verify", str(self.output)],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(created.stdout), json.loads(verified.stdout))
        bad_args = subprocess.run(command + ["create"], capture_output=True, text=True)
        self.assertEqual(bad_args.returncode, 2)

    def test_invalid_configs_fail_without_output(self):
        cases = []
        for field, value in (("reviewed_nonsecret", False), ("reviewed_nonsecret", 1),
                             ("version", True), ("files", []), ("files", "all"),
                             ("references", None)):
            item = copy.deepcopy(self.value)
            item[field] = value
            cases.append(item)
        for patch in ({"complete": True}, {"include": ["**/*"]}):
            cases.append(dict(self.value, **patch))
        for item in cases:
            with self.subTest(item=item):
                self.config.write_text(json.dumps(item))
                self.assert_create_fails()

    def test_unsafe_duplicate_and_colliding_allowlist_paths(self):
        for paths in (["../outside"], ["/etc/passwd"], ["runbooks/../migration-plan.md"],
                      ["./migration-plan.md"], ["runbooks//cutover.md"], ["runbooks/"],
                      ["C:notes"], ["bad\\notes"], ["x\0y"], ["NUL.txt"], ["file."],
                      ["x\n"], ["runbooks/*"], ["e\u0301.md"], ["a" * 513],
                      ["migration-plan.md"] * 2,
                      ["migration-plan.md", "Migration-Plan.md"],
                      ["runbooks", "runbooks/cutover.md"]):
            with self.subTest(paths=paths):
                self.value["files"] = paths
                self.save()
                self.assert_create_fails()

    def test_unknown_unresolved_or_contradictory_references(self):
        for ref in ({"ref": "x", "status": "complete"},
                    {"ref": "x", "status": "included", "path": "missing.md"},
                    {"ref": "x", "status": "external", "reason": ""},
                    {"ref": "x", "status": "omitted"},
                    {"ref": "migration-plan.md", "status": "external", "reason": "Elsewhere"},
                    {"ref": "x", "status": "omitted", "reason": "Private", "complete": True}):
            with self.subTest(ref=ref):
                self.value["references"] = [ref]
                self.save()
                self.assert_create_fails()
        self.value["references"] = [
            {"ref": "x", "status": "external", "reason": "Held separately."}] * 2
        self.save()
        self.assert_create_fails()

    def test_duplicate_json_keys_and_invalid_json(self):
        for content in ('{"version":1,"version":1}', 'null', '{', '{"version":NaN}',
                        '[' * 1100 + ']' * 1100):
            with self.subTest(content=content[:50]):
                self.config.write_text(content)
                self.assert_create_fails()

    def test_missing_directory_fifo_and_symlink_sources(self):
        self.plan.unlink()
        self.assert_create_fails()
        self.plan.mkdir()
        self.assert_create_fails()
        self.plan.rmdir()
        os.mkfifo(self.plan)
        self.assert_create_fails()  # Must not hang while opening a FIFO.
        self.plan.unlink()
        outside = self.base / "outside.md"
        outside.write_text("outside")
        self.plan.symlink_to(outside)
        self.assert_create_fails()
        self.plan.unlink()
        self.plan.symlink_to("does-not-exist")
        self.assert_create_fails()

    def test_symlink_in_source_directory_chain_root_config_or_output(self):
        moved = self.root / "original-runbooks"
        self.runbook.parent.rename(moved)
        self.runbook.parent.symlink_to(moved, target_is_directory=True)
        self.assert_create_fails()
        self.runbook.parent.unlink()
        moved.rename(self.runbook.parent)
        alias = self.base / "root-link"
        alias.symlink_to(self.root, target_is_directory=True)
        real_root = self.root
        self.root = alias
        self.assert_create_fails()
        self.root = real_root
        real_config = self.config
        self.config = self.base / "config-link"
        self.config.symlink_to(real_config)
        self.assert_create_fails()
        self.config = real_config
        outside = self.base / "destination"
        outside.mkdir()
        output_link = self.base / "output-link"
        output_link.symlink_to(outside, target_is_directory=True)
        self.output = output_link / "handoff.zip"
        self.assert_create_fails()

    def test_output_in_input_or_traversing_paths(self):
        self.output = self.root / "nested.zip"
        self.assert_create_fails()
        self.output = self.runbook.parent / "nested.zip"
        self.assert_create_fails()
        self.output = self.base / "engagement" / ".." / "escaped.zip"
        self.assert_create_fails()

    def test_existing_output_file_directory_or_dangling_link_never_overwritten(self):
        self.output.write_bytes(b"existing")
        rc, _, _ = self.cli("create", "--root", self.root, "--config", self.config,
                            "--output", self.output)
        self.assertEqual(rc, 1)
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.output.unlink()
        self.output.mkdir()
        rc, _, _ = self.cli("create", "--root", self.root, "--config", self.config,
                            "--output", self.output)
        self.assertEqual(rc, 1)
        self.assertTrue(self.output.is_dir())
        self.output.rmdir()
        self.output.symlink_to("absent-target")
        rc, _, _ = self.cli("create", "--root", self.root, "--config", self.config,
                            "--output", self.output)
        self.assertEqual(rc, 1)
        self.assertTrue(self.output.is_symlink())

    def test_output_created_by_competing_writer_is_preserved(self):
        original = hb._verify_stream
        def competing_writer(stream):
            result = original(stream)
            self.output.write_bytes(b"another writer")
            return result
        with mock.patch.object(hb, "_verify_stream", side_effect=competing_writer):
            rc, _, _ = self.cli("create", "--root", self.root, "--config", self.config,
                                "--output", self.output)
        self.assertEqual(rc, 1)
        self.assertEqual(self.output.read_bytes(), b"another writer")
        self.assertEqual(list(self.base.glob(".handoff-*.tmp")), [])

    def test_leaf_swapped_to_symlink_between_stat_and_open(self):
        outside = self.base / "outside.md"
        outside.write_text("outside contents")
        original = os.open
        triggered = []
        def swap(path, flags, *args, **kwargs):
            if path == "migration-plan.md" and not triggered:
                triggered.append(True)
                self.plan.unlink()
                self.plan.symlink_to(outside)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(hb.os, "open", side_effect=swap):
            # The platform feature test checks function identity; retain its set entry.
            with mock.patch.object(hb.os, "supports_dir_fd", os.supports_dir_fd | {hb.os.open}):
                self.assert_create_fails()
        self.assertTrue(triggered)
        self.assertEqual(outside.read_text(), "outside contents")

    def test_directory_swapped_to_symlink_during_walk(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "cutover.md").write_text("outside")
        original = os.open
        triggered = []
        def swap(path, flags, *args, **kwargs):
            if path == "runbooks" and not triggered:
                triggered.append(True)
                self.runbook.parent.rename(self.root / "old-runbooks")
                self.runbook.parent.symlink_to(outside, target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(hb.os, "open", side_effect=swap):
            with mock.patch.object(hb.os, "supports_dir_fd", os.supports_dir_fd | {hb.os.open}):
                self.assert_create_fails()
        self.assertTrue(triggered)

    def test_leaf_replaced_by_regular_file_between_stat_and_open(self):
        original, triggered = os.open, []
        def swap(path, flags, *args, **kwargs):
            if path == "migration-plan.md" and not triggered:
                triggered.append(True)
                content = self.plan.read_bytes()
                self.plan.rename(self.root / "old-plan.md")
                self.plan.write_bytes(content)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(hb.os, "open", side_effect=swap):
            with mock.patch.object(hb.os, "supports_dir_fd", os.supports_dir_fd | {hb.os.open}):
                self.assertIn("changed while opening", self.assert_create_fails())
        self.assertTrue(triggered)

    def test_root_swapped_to_symlink_after_capture(self):
        original = hb._capture
        triggered = []
        def capture(*args):
            result = original(*args)
            if not triggered:
                triggered.append(True)
                self.root.rename(self.base / "old-root")
                self.root.symlink_to(self.base, target_is_directory=True)
            return result
        with mock.patch.object(hb, "_capture", side_effect=capture):
            self.assert_create_fails()

    def test_same_size_change_with_restored_mtime_during_read(self):
        original, triggered = os.read, []
        before = self.plan.stat()
        def read(fd, size):
            data = original(fd, size)
            if os.fstat(fd).st_ino == before.st_ino and data and not triggered:
                triggered.append(True)
                self.plan.write_bytes(b"X" * before.st_size)
                os.utime(self.plan, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data
        with mock.patch.object(hb.os, "read", side_effect=read), \
                mock.patch.object(hb, "CHUNK_BYTES", 4):
            self.assert_create_fails()
        self.assertTrue(triggered)

    def test_growing_and_shrinking_source_abort(self):
        original = hb._capture
        for data in (b"", b"G" * 1000):
            with self.subTest(size=len(data)):
                self.plan.write_bytes(b"original")
                def capture(fd, before, path, archive):
                    if path == "migration-plan.md":
                        self.plan.write_bytes(data)
                    return original(fd, before, path, archive)
                with mock.patch.object(hb, "_capture", side_effect=capture):
                    self.assert_create_fails()

    def test_earlier_source_changed_while_later_file_is_captured(self):
        original = hb._capture
        def capture(fd, before, path, archive):
            result = original(fd, before, path, archive)
            if path == "runbooks/cutover.md":
                self.plan.write_bytes(b"changed after first capture")
            return result
        with mock.patch.object(hb, "_capture", side_effect=capture):
            self.assert_create_fails()

    def test_same_content_path_replacement_after_capture_aborts(self):
        original, triggered = hb._capture, []
        def capture(*args):
            result = original(*args)
            if not triggered:
                triggered.append(True)
                content = self.plan.read_bytes()
                self.plan.rename(self.root / "previous-plan.md")
                self.plan.write_bytes(content)
            return result
        with mock.patch.object(hb, "_capture", side_effect=capture):
            self.assert_create_fails()

    def test_output_directory_swapped_during_snapshot_cannot_escape(self):
        directory = self.base / "output"
        directory.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.output = directory / "handoff.zip"
        original, triggered = hb._capture, []
        def capture(*args):
            result = original(*args)
            if not triggered:
                triggered.append(True)
                directory.rename(self.base / "moved-output")
                directory.symlink_to(outside, target_is_directory=True)
            return result
        with mock.patch.object(hb, "_capture", side_effect=capture):
            self.assert_create_fails()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.base / "moved-output").iterdir()), [])

    def test_limits_abort_before_output(self):
        for field, limit in (("MAX_FILES", 1), ("MAX_FILE_BYTES", 8),
                             ("MAX_TOTAL_BYTES", 60), ("MAX_JSON_BYTES", 8),
                             ("MAX_REFERENCES", 1)):
            with self.subTest(field=field), mock.patch.object(hb, field, limit):
                self.assert_create_fails()

    def test_private_staging_and_failure_cleanup(self):
        original = hb._verify_stream
        def fail(stream):
            self.assertEqual(stat.S_IMODE(os.fstat(stream.fileno()).st_mode), 0o600)
            original(stream)
            raise OSError("injected verification failure")
        with mock.patch.object(hb, "_verify_stream", side_effect=fail):
            self.assert_create_fails()

    def test_independently_generated_archive_verifies(self):
        fixture = self.fixture()
        manifest, digest = hb.verify_bundle(fixture)
        self.assertEqual(manifest["reference_counts"], {"included": 0, "external": 0, "omitted": 0})
        self.assertEqual(digest, hashlib.sha256(fixture.read_bytes()).hexdigest())

    def test_empty_selected_file_and_empty_reference_inventory(self):
        self.plan.write_bytes(b"")
        self.value["files"] = ["migration-plan.md"]
        self.value["references"] = []
        self.save()
        manifest, digest = self.create()
        self.assertEqual(manifest["files"], [
            {"path": "migration-plan.md", "size": 0,
             "sha256": hashlib.sha256(b"").hexdigest()}])
        self.assertEqual(manifest["reference_counts"], {"included": 0, "external": 0, "omitted": 0})
        self.assertEqual(manifest["scope"], "declared-only")
        self.assertEqual(hb.verify_bundle(self.output), (manifest, digest))

    def test_archive_duplicate_extra_missing_and_unsafe_members(self):
        fixture = self.fixture()
        with zipfile.ZipFile(fixture) as archive:
            entries = [(i.filename, archive.read(i)) for i in archive.infolist()]
        cases = [entries + [entries[0]], entries + [("files/extra.md", b"x")],
                 entries[:1], entries[1:], entries[1:] + [("files/other.md", b"x")]]
        for name in ("files/../escape", "/absolute", "files/a\\b", "files/C:drive",
                     "files/./dot", "files/double//slash", "files/directory/",
                     "files/Notes.md", "manifest.json/child"):
            cases.append(entries + [(name, b"x")])
        for entries in cases:
            with self.subTest(names=[n for n, _ in entries]):
                self.assert_verify_fails(self.fixture(members=entries))

    def test_archive_manifest_hash_size_counts_and_completeness_tampering(self):
        with zipfile.ZipFile(self.fixture()) as archive:
            baseline = json.loads(archive.read("manifest.json"))
        for mutate in (
            lambda m: m["files"][0].update(sha256="0" * 64),
            lambda m: m["files"][0].update(size=99),
            lambda m: m["files"][0].update(size=True),
            lambda m: m["files"].append(dict(m["files"][0])),
            lambda m: m.update(complete=True),
            lambda m: m.update(scope="complete"),
            lambda m: m.update(version=True),
            lambda m: m.update(reviewed_nonsecret=False),
            lambda m: m["reference_counts"].update(external=1),
            lambda m: m["reference_counts"].update(included=False),
            lambda m: m["references"].append({"ref": "unpacked.md", "status": "included",
                                              "path": "unpacked.md"}),
        ):
            manifest = copy.deepcopy(baseline)
            mutate(manifest)
            with self.subTest(manifest=manifest):
                self.assert_verify_fails(self.fixture(manifest=manifest))

    def test_archive_corruption_truncation_and_trailing_data(self):
        fixture = self.fixture()
        original = fixture.read_bytes()
        corrupted = bytearray(original)
        data_offset = 30 + len("files/notes.md")
        corrupted[data_offset] ^= 1  # Keep recorded CRC and SHA unchanged.
        for data in (b"not a zip", original[:-1], original[:40], original + b"extra",
                     b"prefix" + original, bytes(corrupted)):
            fixture.write_bytes(data)
            self.assert_verify_fails(fixture)

    def test_archive_bad_local_headers_and_central_member_types(self):
        fixture = self.fixture()
        original = fixture.read_bytes()
        central = original.index(b"PK\x01\x02")
        for offset, replacement in (
            (4, b"\x00\x00"),  # local extraction version differs
            (6, b"\x01\x00"),  # local encryption flag differs
            (10, b"\x01\x00"),  # local time differs
            (18, struct.pack("<L", 999)),  # local compressed size differs
            (30, b"X"),  # local name differs
            (central + 38, struct.pack("<L", (stat.S_IFLNK | 0o777) << 16)),
            (central + 38, struct.pack("<L", (stat.S_IFREG | 0o644) << 16)),
            (central + 8, b"\x01\x00"),  # central encryption flag
            (central + 10, b"\x08\x00"),  # compression unsupported
            (central + 12, b"\x01\x00"),  # central time differs
        ):
            with self.subTest(offset=offset, replacement=replacement):
                data = bytearray(original)
                data[offset:offset + len(replacement)] = replacement
                fixture.write_bytes(data)
                self.assert_verify_fails(fixture)

    def test_archive_nul_name_not_silently_truncated(self):
        fixture = self.fixture()
        data = fixture.read_bytes().replace(b"files/notes.md", b"files/not\x00s.md")
        fixture.write_bytes(data)
        self.assert_verify_fails(fixture)

    def test_archive_duplicate_json_keys_not_accepted(self):
        fixture = self.fixture(members=[("files/notes.md", b"notes\n"),
                                       ("manifest.json", b'{"version":1,"version":1}')])
        self.assert_verify_fails(fixture)

    def test_archive_growth_during_digest_is_bounded(self):
        data = self.fixture().read_bytes()
        class GrowingArchive(io.BytesIO):
            def __init__(self, initial):
                super().__init__(initial)
                self.total_reads = 0

            def read(self, size=-1):
                self.total_reads += 1
                if self.tell() == len(data) and size == 1:
                    return b"x"
                return super().read(size)
        stream = GrowingArchive(data)
        with self.assertRaisesRegex(hb.BundleError, "grew during verification"):
            hb._verify_stream(stream)
        self.assertLess(stream.total_reads, 50)

    def test_archive_symlink_and_path_change_during_verification_rejected(self):
        fixture = self.fixture()
        alias = self.base / "link.zip"
        alias.symlink_to(fixture)
        self.assert_verify_fails(alias)
        original = hb._verify_stream
        def replace(stream):
            result = original(stream)
            fixture.rename(self.base / "original.zip")
            fixture.write_bytes((self.base / "original.zip").read_bytes())
            return result
        with mock.patch.object(hb, "_verify_stream", side_effect=replace):
            self.assert_verify_fails(fixture)


if __name__ == "__main__":
    unittest.main()
