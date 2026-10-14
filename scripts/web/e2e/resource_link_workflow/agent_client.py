"""Standalone, standard-library-only worker client; never owns cloud credentials."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def save_image(result: dict, directory: Path) -> dict:
    image = result.get("image_url")
    if image is None:
        return result
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", image)
    if not match:
        raise ValueError("invalid original image payload")
    pixels = base64.b64decode(match[2], validate=True)
    sha = hashlib.sha256(pixels).hexdigest()
    if result.get("sha256") != sha:
        raise ValueError("original image SHA mismatch; do not accept")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / (sha + "." + match[1])
    try:
        with target.open("xb") as handle:
            os.chmod(target, 0o600)
            handle.write(pixels)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != pixels:
            raise ValueError("original image copy conflict") from None
    return {key: value for key, value in result.items() if key != "image_url"} | {
        "image_path": str(target.resolve()), "inspect_with": "view_image", "original_pixels": True,
    }


def invoke(request: dict, descriptor: dict) -> dict:
    if (not isinstance(request, dict) or set(request) != {"operation", "arguments"}
            or not isinstance(request["operation"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", request["operation"])
            or not isinstance(request["arguments"], dict)):
        raise ValueError("use {operation: public operation, arguments: object}; no transport operations")
    origin = descriptor["origin"]
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", origin):
        raise ValueError("job loopback bridge required")
    http_request = urllib.request.Request(
        origin + "/invoke", data=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + descriptor["token"]},
        method="POST",
    )
    # No proxy and no automatic retry: a lost response to a write is UNKNOWN.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(http_request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return json.load(error)
        except (ValueError, OSError):
            raise ValueError("bridge response unavailable; reconcile original job, do not retry") from None
    except (urllib.error.URLError, OSError):
        raise ValueError("bridge outcome unknown; reconcile original job, do not retry") from None


def main(argv: list[str] | None = None, *, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", help="e.g. context, snapshot, request, inspect")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--json", help="complete operation/arguments JSON")
    inputs.add_argument("--arguments", help="arguments JSON for the named operation")
    inputs.add_argument("--arguments-file", type=Path, help="arguments JSON file for the named operation")
    options = parser.parse_args(argv)
    directory = home or Path(__file__).resolve().parent
    phase = "arguments"
    try:
        if options.json:
            if options.operation:
                raise ValueError("use either operation or --json")
            request = json.loads(options.json)
        else:
            arguments = json.loads(options.arguments_file.read_text(encoding="utf-8")) if (
                options.arguments_file
            ) else json.loads(options.arguments or "{}")
            request = {"operation": options.operation, "arguments": arguments}
        phase = "descriptor"
        descriptor = json.loads((directory / ".workflow-client.json").read_text(encoding="utf-8"))
        phase = "bridge"
        envelope = invoke(request, descriptor)
        if envelope.get("ok") is not True:
            print(json.dumps(envelope, ensure_ascii=False))
            return 1
        phase = "result"
        result = save_image(envelope["result"], directory / ".workflow-images")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        # Never echo config, credentials, HTTP headers, private unlock URLs or traceback.
        print(json.dumps({"error": "client failed; preserve original job and reconcile before retry", "ok": False,
                          "phase": phase, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
