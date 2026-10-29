#!/usr/bin/env python3
"""Value-safe comparison of a live application config with its staged replacement, run ON the
application host (cutover-procedures.md §Credential delivery — the only exception to hard
constraint 4). FAIL-CLOSED: it prints a value only when it can prove the value is a harmless
connection component; everything else is reported as changed / unchanged / added / removed.

WHY (reviews 11 and 12): masking leaked API_KEY/PRIVATE_KEY lines, JSON/YAML passwords,
multi-line values and second URLs; a plain allow-list of KEY NAMES still leaked
`DATABASE=host=old password=…`, `ENDPOINT=h?password=…`, YAML anchors and escaped quotes. So
every printed value must pass BOTH gates:

1. Key gate: the key's last name segment is a known connection field AND the key contains no
   secret word (PASS, PWD, SECRET, TOKEN, KEY, CREDENTIAL, AUTH, PRIVATE, CERT, SIGNATURE, SALT,
   COOKIE, SESSION — a secret word always wins, also over --allow-type).
2. Value gate, by field type (strict patterns, no partial printing):
     host     HOST HOSTNAME ENDPOINT ADDR ADDRESS SERVER — a hostname or IPv4, or a bracketed IPv6;
              no spaces, '=', '?', '@', '/', ':' (except inside the IPv6 brackets)
     port     PORT — an integer 1..65535
     tls      TLS SSL USE_TLS TLS_ENABLED — a boolean token (true/false/yes/no/on/off/1/0)
     sslmode  SSLMODE SSL_MODE — disable|allow|prefer|require|verify-ca|verify-full (+ MySQL forms)
     index    DB INDEX — an integer 0..999
     dbname   DATABASE DBNAME — an identifier [A-Za-z_][A-Za-z0-9_$-]{0,63}
     user     USER USERNAME — [A-Za-z0-9_.@-]{1,64}
     url      URL URI — shown only as scheme://host[:port] (userinfo, path, query and fragment are
              never shown — paths can carry credentials); a value that is not exactly one URL is not shown
   A value failing its pattern is reported only as changed / unchanged.

Formats (--format, default auto by file name):
  env    strict KEY=VALUE: optional `export `; an unquoted value without spaces, quotes or
         backslashes; or one complete single- or double-quoted value on the same line (double
         quotes are escape-aware). ANY other construct — multi-line or continued values,
         unterminated quotes, text after a closing quote, duplicate keys, an unparseable line —
         makes the WHOLE FILE opaque.
  json   strict JSON object, compared as a whole: only TOP-LEVEL keys whose name exactly matches a
         connection field (host, port, tls, sslmode, db, database, user, url, …) with a scalar value
         passing its pattern are named and shown; everything else is one line, `other content:
         changed (N other entries changed) | unchanged`, from a type- and container-preserving
         canonical form (false vs "false", null vs "null", {} vs [] all count as changes). No other
         JSON key name is ever printed. A duplicate key or a non-object top level -> opaque
  opaque the whole file is one value: `<file>: changed | unchanged` (YAML is ALWAYS opaque)
  auto   *.json -> json; *.env or names containing "env" -> env; everything else -> opaque
Env key names are printed only when they match ^[A-Za-z_][A-Za-z0-9_]{0,63}$ (else <key #n>).
Errors never echo file content.

Usage (on the app host; the files never leave it):
  python3 -B config_diff.py /opt/app/app.env /opt/app/app.env.target
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

SECRET_WORDS = ("PASS", "PWD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "AUTH", "PRIVATE", "CERT",
                "SIGNATURE", "SALT", "COOKIE", "SESSION")
FIELD_TYPES = {
    "HOST": "host", "HOSTNAME": "host", "ENDPOINT": "host", "ADDR": "host", "ADDRESS": "host",
    "SERVER": "host", "PORT": "port", "TLS": "tls", "SSL": "tls", "SSLMODE": "sslmode",
    "DB": "index", "INDEX": "index", "DATABASE": "dbname", "DBNAME": "dbname",
    "USER": "user", "USERNAME": "user", "URL": "url", "URI": "url",
}
COMPOUND = {"SSL_MODE": "sslmode", "USE_TLS": "tls", "TLS_ENABLED": "tls", "SSL_ENABLED": "tls"}
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.?$")
_BOOL = {"true", "false", "yes", "no", "on", "off", "1", "0"}
_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full",
             "disabled", "preferred", "required", "verify_ca", "verify_identity"}
_DBNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,63}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ENV_KEY = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_UNQUOTED = re.compile(r"^[^\s\"'\\`]*$")


class Opaque(Exception):
    """The file cannot be parsed unambiguously: compare it as one opaque value."""


def field_type(key: str) -> Optional[str]:
    up = key.upper()
    if any(w in up for w in SECRET_WORDS):
        return None
    for compound, t in COMPOUND.items():
        if up == compound or up.endswith("_" + compound):
            return t
    last = re.split(r"[._\-]", up)[-1] if up else ""
    return FIELD_TYPES.get(last)


def _valid_host(v: str) -> bool:
    if v.startswith("[") and v.endswith("]"):
        try:
            return ipaddress.ip_address(v[1:-1]).version == 6
        except ValueError:
            return False
    try:
        ipaddress.IPv4Address(v)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(v)) and not re.match(r"^[0-9.]+$", v)


def _valid_port(v: str) -> bool:
    return v.isdigit() and len(v) <= 5 and 1 <= int(v) <= 65535


def safe_display(ftype: Optional[str], value: str) -> Optional[str]:
    """The value as it may be printed, or None (report changed/unchanged only)."""
    if ftype is None or not isinstance(value, str):
        return None
    v = value
    if ftype == "host":
        return v if _valid_host(v) else None
    if ftype == "port":
        return v if _valid_port(v) else None
    if ftype == "tls":
        return v if v.lower() in _BOOL else None
    if ftype == "sslmode":
        return v if v.lower() in _SSLMODES else None
    if ftype == "index":
        return v if v.isdigit() and len(v) <= 3 else None
    if ftype == "dbname":
        return v if _DBNAME.match(v) else None
    if ftype == "user":
        return v if _USER.match(v) else None
    if ftype == "url":
        if any(c.isspace() for c in v) or "," in v or v.count("://") != 1:
            return None
        try:
            u = urlsplit(v)
            port = u.port
        except ValueError:
            return None
        if not re.match(r"^[a-z][a-z0-9+.-]{0,15}$", u.scheme or ""):
            return None
        host = u.hostname or ""
        host_disp = f"[{host}]" if ":" in host else host
        if not host or not _valid_host(host_disp):
            return None
        # scheme://host[:port] ONLY — paths can carry credentials (webhook tokens), never shown
        return f"{u.scheme}://{host_disp}" + (f":{port}" if port is not None else "")
    return None


def parse_env(text: str) -> Dict[Tuple[str, ...], str]:
    out: Dict[Tuple[str, ...], str] = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_KEY.match(stripped)
        if not m:
            raise Opaque("unparseable line")
        key, rest = m.group(1), m.group(2)
        if rest[:1] == '"':
            val, i, closed = [], 1, False
            while i < len(rest):
                c = rest[i]
                if c == "\\":
                    if i + 1 >= len(rest):
                        raise Opaque("trailing backslash")          # continuation: unsupported
                    nxt = rest[i + 1]
                    if nxt not in '"\\$`n':
                        raise Opaque("unsupported escape")
                    val.append("\n" if nxt == "n" else nxt)
                    i += 2
                    continue
                if c == '"':
                    closed = True
                    i += 1
                    break
                val.append(c)
                i += 1
            if not closed or rest[i:].strip():
                raise Opaque("unterminated quote or trailing text")  # multi-line values: opaque
            value = "".join(val)
        elif rest[:1] == "'":
            end = rest.find("'", 1)
            if end < 0 or rest[end + 1:].strip():
                raise Opaque("unterminated quote or trailing text")
            value = rest[1:end]
        else:
            if not _UNQUOTED.match(rest):
                raise Opaque("ambiguous unquoted value")
            value = rest
        if (key,) in out:
            raise Opaque("duplicate key")
        out[(key,)] = value
    return out


def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Opaque("duplicate JSON key")
    return dict(pairs)


def _canon(node) -> str:
    """Type- and container-preserving canonical form (false != "false", {} != [])."""
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _top_level_field(key: str) -> Optional[str]:
    """A JSON top-level key qualifies only by EXACT name (case-insensitive) in the allow-list."""
    up = key.upper()
    return FIELD_TYPES.get(up) or COMPOUND.get(up)


def _scalar_text(v) -> Optional[str]:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    return None


def parse(path: Path, fmt: str):
    """(effective format, parsed) — env: {(key,): value}; json: the decoded top-level object;
    None = opaque."""
    raw = path.read_bytes()
    if fmt == "auto":
        name = path.name.lower()
        fmt = "json" if name.endswith(".json") else "env" if (name.endswith(".env") or "env" in name) else "opaque"
    if fmt in ("opaque", "yaml"):
        return "opaque", None
    try:
        text = raw.decode("utf-8")
        if fmt == "env":
            return "env", parse_env(text)
        doc = json.loads(text, object_pairs_hook=_no_dupes)
        if not isinstance(doc, dict):
            return "opaque (JSON top level is not an object)", None
        return "json", doc
    except (Opaque, ValueError, UnicodeDecodeError, RecursionError, TypeError):
        return f"opaque (not parseable as strict {fmt})", None


def _h(value) -> str:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogateescape")
    return hashlib.sha256(data).hexdigest()


def _state(a_has, b_has, a, b) -> str:
    if not b_has:
        return "removed"
    if not a_has:
        return "added"
    return "unchanged" if _h(a) == _h(b) else "changed"


def _compare_json(lv: dict, sv: dict) -> List[str]:
    """Only exact top-level connection keys with valid scalar values are named and shown; every
    other entry is folded into one line — no other key name is ever printed."""
    lines, shown = [], set()
    for key in sorted(k for k in set(lv) | set(sv) if _top_level_field(k)):
        ftype = _top_level_field(key)
        a, b = lv.get(key), sv.get(key)
        a_txt = _scalar_text(a) if key in lv else "-"
        b_txt = _scalar_text(b) if key in sv else "-"
        if a_txt is None or b_txt is None:
            continue                                   # non-scalar: part of "other content"
        old = safe_display(ftype, a_txt) if key in lv else "-"
        new = safe_display(ftype, b_txt) if key in sv else "-"
        if old is None or new is None:
            continue
        state = _state(key in lv, key in sv, _canon(a), _canon(b))
        shown.add(key)
        lines.append(f"{key}: unchanged  {new}" if state == "unchanged" else f"{key}: {state}  {old} -> {new}")
    rest = sorted((set(lv) | set(sv)) - shown)
    differ = sum(1 for k in rest if (k in lv) != (k in sv) or _canon(lv[k]) != _canon(sv[k]))
    lines.append(f"other content: {'changed (' + str(differ) + ' other entries changed)' if differ else 'unchanged'}")
    return lines


def compare(live: Path, staged: Path, fmt: str = "auto") -> List[str]:
    lf, lv = parse(live, fmt)
    sf, sv = parse(staged, fmt)
    lines = [f"# config_diff (format: {lf} / {sf}); values shown only for validated connection fields"]
    if lv is None or sv is None or lf != sf:
        same = _h(live.read_bytes()) == _h(staged.read_bytes())
        lines.append(f"{staged.name}: {'unchanged' if same else 'changed'} (opaque — no keys compared)")
        return lines
    if lf == "json":
        return lines + _compare_json(lv, sv)
    for n, path in enumerate(sorted(set(lv) | set(sv)), 1):
        key = path[0]
        state = _state(path in lv, path in sv, lv.get(path), sv.get(path))
        if not _ENV_NAME.match(key):
            lines.append(f"<key #{n}>: {state}")
            continue
        ftype = field_type(key)
        old = safe_display(ftype, lv[path]) if path in lv else "-"
        new = safe_display(ftype, sv[path]) if path in sv else "-"
        if old is None or new is None:
            lines.append(f"{key}: {state}")
        elif state == "unchanged":
            lines.append(f"{key}: unchanged  {new}")
        else:
            lines.append(f"{key}: {state}  {old} -> {new}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Value-safe live vs staged app-config comparison (run on the app host).")
    ap.add_argument("live", type=Path)
    ap.add_argument("staged", type=Path)
    ap.add_argument("--format", choices=("auto", "env", "json", "yaml", "opaque"), default="auto")
    args = ap.parse_args(argv)
    try:
        for f in (args.live, args.staged):
            if not f.is_file():
                print("config_diff: an input path is not a readable file", file=sys.stderr)
                return 2
        print("\n".join(compare(args.live, args.staged, args.format)))
        return 0
    except Exception:  # noqa: BLE001 - never echo content through a traceback
        print("config_diff: internal error while comparing (details suppressed to avoid printing config content)",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
