from __future__ import annotations

import hashlib


def finding_id(row: dict) -> str:
    raw = "\x1f".join(
        str(row.get(key) or "")
        for key in ("project_id", "document_id_a", "document_id_b")
    )
    return f"finding:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"
