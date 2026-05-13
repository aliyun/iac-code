from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ACPVersionSpec:
    protocol_version: int
    sdk_version: str


CURRENT_VERSION = ACPVersionSpec(protocol_version=1, sdk_version="0.9.0")
SUPPORTED_VERSIONS: dict[int, ACPVersionSpec] = {1: CURRENT_VERSION}
MIN_PROTOCOL_VERSION = 1


def negotiate_version(client_protocol_version: int) -> ACPVersionSpec:
    if client_protocol_version < MIN_PROTOCOL_VERSION:
        return CURRENT_VERSION

    best: ACPVersionSpec | None = None
    for version, spec in SUPPORTED_VERSIONS.items():
        if version <= client_protocol_version and (best is None or version > best.protocol_version):
            best = spec

    return best if best is not None else CURRENT_VERSION
