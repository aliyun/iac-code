"""Type definitions for telemetry payloads."""

from __future__ import annotations

from typing import Union

# Primitive values allowed directly in event/metric metadata.
# Strings are NOT in this list by design — callers must sanitize text explicitly.
AllowedMetadataValue = Union[bool, int, float, None, list, str]
# Note: `str` is included because sanitize_*() returns str. Callers must only pass
# strings that came from a sanitize_*() function or from a well-known enum constant.

EventMetadata = dict[str, AllowedMetadataValue]
