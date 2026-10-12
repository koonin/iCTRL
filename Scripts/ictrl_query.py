#!/usr/bin/env python3
"""JSON-only iCTRL adapter for an OpenMinis session.

Usage remains: python3 ictrl_query.py METHOD '{"parameter":"value"}'
Uses the paired static client in ~/.ictrl/bin; never pairs or retries actions.
ictrl-last.json is the last successful page_source, not the last RPC response.
Use client.snapshot_file for an immutable response from a particular call.
"""

import argparse
import base64
import binascii
import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


PAGE_METHODS = {"page_source", "view_tree", "ui_tree"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


def decode(text):
    return json.loads(text, parse_constant=reject_constant)


def atomic_write(path, data):
    """Readers see either the previous complete file or the new complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def failure(message, code=-32098, **details):
    return {"jsonrpc": "2.0", "id": None,
            "error": {"code": code, "message": message, "data": details}}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument("method")
    parser.add_argument("params", nargs="?", default="{}")
    parser.add_argument("--host", default="127.0.0.1:8200")
    parser.add_argument("--client", type=Path, default=Path.home() / ".ictrl/bin/ictrl.sh")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/minis/workspace"))
    parser.add_argument("--attachments-dir", type=Path, default=Path("/var/minis/attachments"))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.method):
        raise ValueError("Invalid RPC method name")
    params = decode(args.params)
    if not isinstance(params, dict):
        raise ValueError("RPC parameters must be a JSON object")
    if not math.isfinite(args.timeout) or args.timeout < 30:
        raise ValueError("--timeout must be a finite number of at least 30 seconds")
    args.params = encode(params)
    return args


def query(args):
    command = ["sh", str(args.client), "--host", args.host,
               "call", args.method, args.params]
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return failure("iCTRL client timed out; the action may still be running.",
                       completion="unknown", retry_hint="Check iCTRL logs before repeating an action."), 1
    except OSError as error:
        return failure("Could not start iCTRL client: " + str(error), completion="not_started"), 1

    # Client diagnostics are never mixed into the machine-readable stdout.
    if process.stderr:
        print(process.stderr, file=sys.stderr, end="" if process.stderr.endswith("\n") else "\n")
    exit_code = process.returncode if 0 < process.returncode < 126 else int(process.returncode != 0)
    try:
        response = decode(process.stdout)
        if not isinstance(response, dict):
            raise ValueError("Expected a JSON object")
        has_result = "result" in response
        has_error = response.get("error") is not None
        if has_result == has_error:
            raise ValueError("Expected exactly one of result or error")
        if has_error and not isinstance(response["error"], dict):
            raise ValueError("Invalid RPC error")
    except (ValueError, TypeError) as error:
        return failure("Invalid iCTRL response: " + str(error), completion="unknown",
                       client_exit_code=process.returncode,
                       retry_hint="Check iCTRL logs before repeating an action."), exit_code or 1

    # The static CLI may return 0 for a decrypted JSON-RPC error.
    if has_error:
        exit_code = exit_code or 1
    result = response.get("result")
    succeeded = exit_code == 0 and not has_error
    stamp = datetime.datetime.now(datetime.timezone.utc)
    call_id = stamp.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    snapshot = args.state_dir / "ictrl-snapshots" / (call_id + "-" + args.method + ".json")
    metadata = {"method": args.method, "captured_at": stamp.isoformat(),
                "rpc_succeeded": succeeded, "client_exit_code": process.returncode,
                "snapshot_file": str(snapshot)}
    response["client"] = metadata

    # Keep screenshot payloads out of model context. Preserve all geometry and
    # save the actual returned format, rather than writing every image as .jpg.
    if succeeded and args.method == "screenshot" and isinstance(result, dict) and "data" in result:
        try:
            image_format = result.get("format", "jpeg").lower()
            extension = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}[image_format]
            image_bytes = base64.b64decode(result["data"], validate=True)
            if not image_bytes:
                raise ValueError("Empty screenshot")
            attachment = args.attachments_dir / ("ictrl-" + call_id + "." + extension)
            atomic_write(attachment, image_bytes)
            result["saved_image"] = str(attachment)
            del result["data"]
        except (KeyError, AttributeError, TypeError, ValueError, binascii.Error, OSError) as error:
            metadata["attachment_error"] = str(error)
            exit_code = 1

    # Never replace a page snapshot with a find/tap result, or with a failed read.
    # Per-call files are immutable, so multi-step scripts can retain an exact read.
    targets = [snapshot, args.state_dir / "ictrl-last-response.json"]
    if succeeded:
        targets.append(args.state_dir / ("ictrl-last-" + args.method + ".json"))
        if args.method in PAGE_METHODS and isinstance(result, dict) and isinstance(result.get("source"), str):
            targets.extend([args.state_dir / "ictrl-last-page.json", args.state_dir / "ictrl-last.json"])
            metadata["last_page_file"] = str(args.state_dir / "ictrl-last.json")
    payload = encode(response).encode("utf-8")
    for target in targets:
        try:
            atomic_write(target, payload)
        except OSError as error:
            # A local storage failure must not turn a successful phone action
            # into an apparently failed tap which an agent then repeats.
            metadata.setdefault("storage_warnings", []).append({"path": str(target), "message": str(error)})
    return response, exit_code


def main(argv=None):
    try:
        args = parse_args(argv)
    except (ValueError, TypeError) as error:
        print(encode(failure(str(error), code=-32602, completion="not_started")))
        return 2
    response, exit_code = query(args)
    print(encode(response))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
