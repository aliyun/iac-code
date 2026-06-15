from __future__ import annotations

import time
import uuid
from typing import Any, TypeAlias

Record: TypeAlias = dict[str, Any]


def new_record(kind: str, **fields: Any) -> Record:
    data: Record = {
        "id": f"record_{uuid.uuid4().hex}",
        "kind": kind,
        "received_at": time.time(),
        "resource": {},
        "scope": {},
        "name": "",
        "timestamp_unix_nano": 0,
        "attributes": {},
        "trace_id": "",
        "span_id": "",
        "parent_span_id": "",
        "value": None,
        "raw": {},
    }
    data.update(fields)
    return data
