#!/usr/bin/env python3
"""Require a recorded customer choice before starting a provisioning command.

This validates the record, not who authored it. The agent must record the actual
customer response; it must never manufacture confirmation or infer a default.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path


def validate_decision(value, account, region, prefix, mode=None):
    if not isinstance(value, dict):
        raise ValueError("Customer decision must be a JSON object")
    if not re.fullmatch(r"\d{12}", account):
        raise ValueError("Target account must be a 12-digit AWS account ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
        raise ValueError("Invalid target region")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", prefix):
        raise ValueError("Prefix must use lowercase letters, digits or hyphens")
    for key, expected in [("account_id", account), ("region", region), ("prefix", prefix)]:
        if value.get(key) != expected:
            raise ValueError(f"Customer decision does not match target {key}")
    if type(value.get("use_smus")) is not bool:
        raise ValueError("Ask the customer to choose SMUS or standalone; no default is allowed")
    confirmation = value.get("customer_decision", {})
    if (not isinstance(confirmation, dict)
            or confirmation.get("source") != "customer"
            or confirmation.get("confirmed") is not True
            or not isinstance(confirmation.get("answer"), str)
            or not confirmation["answer"].strip()):
        raise ValueError("An explicit customer response is required before provisioning")
    selected = "smus" if value["use_smus"] else "standalone"
    if mode is not None and mode != selected:
        raise ValueError(f"Requested {mode} conflicts with customer's {selected} choice")
    return selected


def load_decision(path, account, region, prefix, mode=None):
    return validate_decision(json.loads(Path(path).read_text()), account, region, prefix, mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--mode", choices=["smus", "standalone"], required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        mode = load_decision(args.decision, args.account, args.region, args.prefix, args.mode)
    except (ValueError, OSError) as error:
        parser.exit(2, f"PROVISIONING BLOCKED: {error}\n")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    print(f"Customer platform choice validated: {mode}", flush=True)
    if command:
        # No shell interpolation; no subprocess at all before the gate passes.
        return subprocess.run(command, check=False).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
