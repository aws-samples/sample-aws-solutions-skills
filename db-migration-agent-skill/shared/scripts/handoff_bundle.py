#!/usr/bin/env python3
"""Local, stdlib-only, reviewed-allowlist handoff ZIP creation and verification.

See shared/reference/handoff-packaging.md for the schema and guarantee boundaries.
No extraction, recursive discovery, secret scanning, or network operations.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
import unicodedata
import zipfile

MAX_FILES = 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_REFERENCES = 4096
MAX_PATH_BYTES = 512
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + MAX_JSON_BYTES + 4 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MANIFEST = "manifest.json"
SCOPE = "declared-only"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BundleError(ValueError):
    """Invalid input, changed source, or invalid bundle."""


def _require(condition, message):
    if not condition:
        raise BundleError(message)


def _keys(value, keys, label):
    _require(isinstance(value, dict) and set(value) == set(keys),
             f"{label}: expected exactly {', '.join(sorted(keys))}")


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 4096
             and not any(ord(c) < 32 or ord(c) == 127 for c in value),
             f"{label}: expected nonempty text without control characters")
    return value


def _path(value):
    _text(value, "path")
    _require(len(value.encode("utf-8")) <= MAX_PATH_BYTES
             and unicodedata.normalize("NFC", value) == value,
             f"path too long or not NFC normalized: {value!r}")
    for part in value.split("/"):
        _require(part not in ("", ".", "..") and not part.endswith((" ", "."))
                 and not any(c in part for c in '\\:<>"|?*'),
                 f"unsafe relative path: {value!r}")
        _require(not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])",
                                  part.split(".")[0]),
                 f"reserved path: {value!r}")
    return value


def _paths(values):
    _require(isinstance(values, list) and 1 <= len(values) <= MAX_FILES,
             f"files: require 1..{MAX_FILES} explicit paths")
    paths = [_path(p) for p in values]
    folded = {p.casefold() for p in paths}
    _require(len(folded) == len(paths), "duplicate or case-colliding paths")
    for p in folded:
        parts = p.split("/")
        _require(not any("/".join(parts[:i]) in folded for i in range(1, len(parts))),
                 "file/directory path collision")
    return sorted(paths)


def _references(values, paths):
    _require(isinstance(values, list) and len(values) <= MAX_REFERENCES,
             f"references: require an explicit list of at most {MAX_REFERENCES}")
    refs, seen = [], set()
    for value in values:
        _require(isinstance(value, dict), "reference must be an object")
        disposition = value.get("status")
        _require(disposition in ("included", "external", "omitted"),
                 "reference status must be included, external, or omitted")
        _keys(value, ("ref", "status", "path" if disposition == "included" else "reason"),
              "reference")
        ref = _text(value["ref"], "reference ref")
        _require(ref not in seen, f"duplicate reference: {ref!r}")
        seen.add(ref)
        if disposition == "included":
            _require(_path(value["path"]) in paths,
                     f"included reference is not allowlisted: {ref!r}")
        else:
            _text(value["reason"], "reference reason")
            _require(ref not in paths, f"allowlisted file labeled {disposition}: {ref!r}")
        refs.append(dict(value))
    return sorted(refs, key=lambda r: r["ref"])


def _counts(refs):
    return {s: sum(r["status"] == s for r in refs)
            for s in ("included", "external", "omitted")}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def _json_load(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise BundleError(f"invalid JSON constant: {value}")

    _require(len(data) <= MAX_JSON_BYTES, "JSON exceeds size limit")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BundleError(f"invalid JSON: {exc}") from exc


def _config(value):
    _keys(value, ("version", "reviewed_nonsecret", "files", "references"), "config")
    _require(type(value["version"]) is int and value["version"] == 1,
             "config version must be 1")
    _require(value["reviewed_nonsecret"] is True,
             "reviewed_nonsecret must be true after reviewing the selected files and config")
    paths = _paths(value["files"])
    return paths, _references(value["references"], paths)


def _fingerprint(st):
    # ctime detects same-size edits even when a writer restores the old mtime.
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _identity(st):
    return st.st_dev, st.st_ino


def _absolute(value):
    value = os.fspath(value)
    _require("\0" not in value and ".." not in value.split("/"),
             "filesystem paths must not contain NUL or '..'")
    return Path(os.path.abspath(value))


def _platform():
    _require(os.name == "posix" and hasattr(os, "O_NOFOLLOW")
             and hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
             and os.stat in os.supports_dir_fd and os.link in os.supports_dir_fd,
             "requires POSIX descriptor-relative operations and O_NOFOLLOW")


@contextlib.contextmanager
def _directory(path):
    """Walk every component without following symlinks; pin the final directory."""
    path = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    identities = {_identity(os.fstat(fd))}
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
            identities.add(_identity(os.fstat(fd)))
        yield fd, identities
    finally:
        os.close(fd)


@contextlib.contextmanager
def _source(root_fd, path):
    """Never resolve a selected name through an unpinned directory or a symlink."""
    parts = path.split("/")
    parent = os.dup(root_fd)
    fd = None
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent)
            os.close(parent)
            parent = next_fd
        before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        _require(stat.S_ISREG(before.st_mode), f"not a regular file: {path!r}")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        _require(_fingerprint(before) == _fingerprint(os.fstat(fd)),
                 f"source changed while opening: {path!r}")
        yield fd, before
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _unchanged(fd, before, path):
    _require(_fingerprint(os.fstat(fd)) == _fingerprint(before),
             f"source changed during snapshot: {path!r}")


def _read_config(path):
    path = _absolute(path)
    with _directory(path.parent) as (parent, _):
        with _source(parent, path.name) as (fd, before):
            _require(before.st_size <= MAX_JSON_BYTES, "config exceeds size limit")
            with os.fdopen(os.dup(fd), "rb") as src:
                data = src.read(MAX_JSON_BYTES + 1)
            _unchanged(fd, before, path.name)
            return _config(_json_load(data))


def _info(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o400) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _capture(fd, before, path, archive):
    digest, size = hashlib.sha256(), 0
    with archive.open(_info("files/" + path), "w") as dst:
        while True:
            block = os.read(fd, CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            _require(size <= before.st_size, f"source grew during snapshot: {path!r}")
            digest.update(block)
            dst.write(block)
    _unchanged(fd, before, path)
    _require(size == before.st_size, f"source size changed during snapshot: {path!r}")
    return {"path": path, "size": size, "sha256": digest.hexdigest()}


def _manifest(value):
    _keys(value, ("version", "scope", "reviewed_nonsecret", "files",
                  "references", "reference_counts"), "manifest")
    _require(type(value["version"]) is int and value["version"] == 1
             and value["scope"] == SCOPE and value["reviewed_nonsecret"] is True,
             "unsupported manifest version, scope, or review declaration")
    _require(isinstance(value["files"], list), "manifest files must be a list")
    for entry in value["files"]:
        _keys(entry, ("path", "size", "sha256"), "manifest file")
        _require(type(entry["size"]) is int and 0 <= entry["size"] <= MAX_FILE_BYTES,
                 "invalid manifest file size")
        _require(isinstance(entry["sha256"], str) and SHA256.fullmatch(entry["sha256"]),
                 "invalid SHA-256")
    paths = _paths([f["path"] for f in value["files"]])
    _require(sum(f["size"] for f in value["files"]) <= MAX_TOTAL_BYTES,
             "total payload exceeds size limit")
    refs = _references(value["references"], paths)
    counts = value["reference_counts"]
    _keys(counts, ("included", "external", "omitted"), "reference_counts")
    _require(all(type(n) is int for n in counts.values()) and counts == _counts(refs),
             "reference counts do not match explicit dispositions")
    return value


def _verify_stream(stream):
    """Accept only this bounded, stored, single-disk ZIP format; never extract."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    _require(22 <= length <= MAX_ARCHIVE_BYTES, "invalid archive size")
    stream.seek(-22, os.SEEK_END)
    sig, disk, start_disk, count_disk, count, cd_size, cd_offset, comment = struct.unpack(
        "<4s4H2LH", stream.read(22))
    _require(sig == b"PK\x05\x06" and disk == start_disk == comment == 0
             and count_disk == count and 2 <= count <= MAX_FILES + 1
             and cd_offset + cd_size + 22 == length
             and cd_size <= (MAX_FILES + 1) * (46 + MAX_PATH_BYTES + 6),
             "invalid ZIP directory, member count, trailing data, or ZIP64 archive")
    with zipfile.ZipFile(stream, "r") as archive:
        infos = archive.infolist()
        _require(len(infos) == count and archive.start_dir == cd_offset,
                 "ZIP directory mismatch")
        names, folded, offset, directory_size, total = set(), set(), 0, 0, 0
        for info in infos:
            name = info.orig_filename
            _require(name == info.filename, "truncated ZIP member name")
            if name != MANIFEST:
                _require(name.startswith("files/"), f"unexpected member: {name!r}")
                _path(name[6:])
            _require(name not in names and name.casefold() not in folded,
                     f"duplicate or case-colliding ZIP member: {name!r}")
            names.add(name)
            folded.add(name.casefold())
            _require(info.create_system == 3
                     and info.external_attr == (stat.S_IFREG | 0o400) << 16,
                     f"member must be a private regular file: {name!r}")
            limit = MAX_JSON_BYTES if name == MANIFEST else MAX_FILE_BYTES
            _require(info.compress_type == zipfile.ZIP_STORED
                     and info.flag_bits in (0, 0x800)
                     and info.compress_size == info.file_size <= limit
                     and info.create_version == info.extract_version == 20
                     and info.date_time == (1980, 1, 1, 0, 0, 0)
                     and info.volume == info.internal_attr == 0
                     and not info.extra and not info.comment,
                     f"unsupported member metadata: {name!r}")
            total += info.file_size
            _require(total <= MAX_TOTAL_BYTES + MAX_JSON_BYTES, "archive payload too large")
            # zipfile checks CRC and local names on read. Also reject prefixes, gaps,
            # overlaps, and inconsistent local sizes/flags that many ZIP readers ignore.
            _require(info.header_offset == offset, "noncontiguous ZIP members")
            stream.seek(offset)
            header = stream.read(30)
            _require(len(header) == 30, "truncated local ZIP header")
            fields = struct.unpack("<4s5H3L2H", header)
            encoded = name.encode("utf-8" if info.flag_bits & 0x800 else "ascii")
            _require(fields[0] == b"PK\x03\x04" and fields[1] == info.extract_version
                     and fields[2] == info.flag_bits and fields[3] == info.compress_type
                     and fields[4:6] == (0, 33)  # Fixed DOS time/date: 1980-01-01.
                     and fields[6:9] == (info.CRC, info.compress_size, info.file_size)
                     and fields[9:] == (len(encoded), 0)
                     and stream.read(len(encoded)) == encoded,
                     f"local ZIP header mismatch: {name!r}")
            offset += 30 + len(encoded) + info.file_size
            directory_size += 46 + len(encoded)
        _require(offset == cd_offset and directory_size == cd_size
                 and MANIFEST in names, "missing manifest or ZIP layout mismatch")
        manifest = _manifest(_json_load(archive.read(MANIFEST)))
        expected = {MANIFEST} | {"files/" + f["path"] for f in manifest["files"]}
        _require(names == expected, "extra or missing ZIP members")
        for entry in manifest["files"]:
            digest, size = hashlib.sha256(), 0
            with archive.open("files/" + entry["path"]) as src:
                while True:
                    block = src.read(CHUNK_BYTES)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
            _require(size == entry["size"] and digest.hexdigest() == entry["sha256"],
                     f"size/hash mismatch: {entry['path']!r}")
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        block = stream.read(min(CHUNK_BYTES, remaining))
        _require(bool(block), "archive shrank during verification")
        digest.update(block)
        remaining -= len(block)
    _require(not stream.read(1), "archive grew during verification")
    return manifest, digest.hexdigest()


def verify_bundle(path):
    _platform()
    path = _absolute(path)
    with _directory(path.parent) as (parent, _):
        with _source(parent, path.name) as (fd, before):
            with os.fdopen(os.dup(fd), "rb") as stream:
                result = _verify_stream(stream)
            _unchanged(fd, before, path.name)
            with _source(parent, path.name) as (_, current):
                _require(_fingerprint(before) == _fingerprint(current),
                         "archive path changed during verification")
            return result


def create_bundle(root, config, output):
    _platform()
    root, output = _absolute(root), _absolute(output)
    paths, refs = _read_config(config)
    _require(output != root and root not in output.parents, "output must be outside input root")
    with contextlib.ExitStack() as stack:
        root_fd, _ = stack.enter_context(_directory(root))
        parent_fd, ancestors = stack.enter_context(_directory(output.parent))
        root_identity = _identity(os.fstat(root_fd))
        parent_identity = _identity(os.fstat(parent_fd))
        _require(root_identity not in ancestors, "output must be outside input root")
        try:
            os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BundleError("output already exists; choose a new filename")
        sources, total = [], 0
        for path in paths:
            fd, before = stack.enter_context(_source(root_fd, path))
            _require(before.st_size <= MAX_FILE_BYTES, f"file exceeds size limit: {path!r}")
            total += before.st_size
            _require(total <= MAX_TOTAL_BYTES, "total payload exceeds size limit")
            sources.append((path, fd, before))
        stage = ".handoff-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED,
                                     allowZip64=False) as archive:
                    entries = [_capture(source, before, path, archive)
                               for path, source, before in sources]
                    manifest = {"version": 1, "scope": SCOPE, "reviewed_nonsecret": True,
                                "files": entries, "references": refs,
                                "reference_counts": _counts(refs)}
                    data = _json_bytes(manifest)
                    _require(len(data) <= MAX_JSON_BYTES, "generated manifest exceeds size limit")
                    archive.writestr(_info(MANIFEST), data)
                stream.flush()
                result = _verify_stream(stream)
                # All descriptors were pinned before the first read. Recheck every
                # descriptor AND its current path after the entire archive was verified.
                for path, source, before in sources:
                    _unchanged(source, before, path)
                    with _source(root_fd, path) as (_, current):
                        _require(_fingerprint(before) == _fingerprint(current),
                                 f"source path changed during snapshot: {path!r}")
                with _directory(root) as (current, _):
                    _require(_identity(os.fstat(current)) == root_identity,
                             "input root moved during snapshot")
                with _directory(output.parent) as (current, _):
                    _require(_identity(os.fstat(current)) == parent_identity,
                             "output directory moved during snapshot")
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
                # Hard-link publication is atomic and fails if ANY destination exists.
                # No rename/replace fallback: that would permit accidental overwrite.
                os.link(stage, output.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False)
                os.fsync(parent_fd)
            return result
        finally:
            os.unlink(stage, dir_fd=parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="snapshot a reviewed explicit allowlist")
    create.add_argument("--root", required=True, help="engagement input directory")
    create.add_argument("--config", required=True, help="reviewed JSON allowlist")
    create.add_argument("--output", required=True, help="new ZIP outside input root")
    verify = commands.add_parser("verify", help="verify without extracting or accessing sources")
    verify.add_argument("archive")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest, digest = create_bundle(args.root, args.config, args.output)
        else:
            manifest, digest = verify_bundle(args.archive)
        print(json.dumps({"archive_sha256": digest, "files": len(manifest["files"]),
                          "bytes": sum(f["size"] for f in manifest["files"]),
                          "scope": manifest["scope"],
                          "references": manifest["reference_counts"]}, sort_keys=True))
        return 0
    except (BundleError, OSError, zipfile.BadZipFile, zipfile.LargeZipFile,
            RuntimeError, UnicodeError, struct.error) as exc:
        print(f"handoff bundle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
