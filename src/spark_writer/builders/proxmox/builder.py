#!/usr/bin/python3
"""Proxmox auto-install OCI builder entrypoint."""

import hashlib
import json
import subprocess
from pathlib import Path


def main() -> None:
    request = json.loads(Path("/inputs/request.json").read_text(encoding="utf-8"))
    if request.get("requestVersion") != "1" or request.get("builder") != "proxmox-auto-install":
        raise RuntimeError("Unsupported builder request")

    inputs = request.get("inputs", {})
    answer = inputs.get("answer-file")
    if not answer:
        raise RuntimeError("answer-file input is required")

    output = Path("/artifacts/proxmox-auto.iso")
    command = [
        "proxmox-auto-install-assistant",
        "prepare-iso",
        "--fetch-from",
        "iso",
        "--answer-file",
        answer,
        "--tmp",
        "/tmp",
    ]
    first_boot = inputs.get("first-boot")
    if first_boot:
        command.extend(["--on-first-boot", first_boot])
    command.extend(["--output", str(output), request["source"]])
    subprocess.run(command, check=True)

    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    version_result = subprocess.run(
        ["proxmox-auto-install-assistant", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    version = (version_result.stdout or version_result.stderr).strip()
    Path("/artifacts/result.json").write_text(
        json.dumps(
            {
                "resultVersion": "1",
                "artifact": "/artifacts/proxmox-auto.iso",
                "sha256": digest.hexdigest(),
                "mediaType": "application/x-iso9660-image",
                "builder": "proxmox-auto-install",
                "builderVersion": version,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
