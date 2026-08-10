from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_schema import AuditRecord, SourceReference, normalize_text

MAX_METADATA_FILE_BYTES = 512 * 1024 * 1024


def normalize_metadata_value(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_metadata_datetime(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = normalize_metadata_value(value)
        if not text:
            return ""
        if text.startswith("D:"):
            text = text[2:]
            match = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", text)
            if not match:
                return ""
            year, month, day, hour, minute, second = match.groups()
            parsed = datetime(
                int(year), int(month), int(day),
                int(hour or 0), int(minute or 0), int(second or 0),
            )
        else:
            candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                return ""
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_document_path(input_root: Path, record: AuditRecord) -> Path | None:
    if not record.file_path:
        return None
    root = input_root.resolve()
    relative = Path(record.file_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidates: list[Path] = [root / relative]
    for ref in record.source_refs:
        source_parent = Path(ref.source_path.replace("\\", "/")).parent
        candidates.append(root / source_parent / relative)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _read_docx_metadata(path: Path) -> dict[str, str]:
    from docx import Document

    props = Document(path).core_properties
    return {
        "author": normalize_metadata_value(props.author),
        "file_creator": normalize_metadata_value(props.last_modified_by),
        "created_at": parse_metadata_datetime(props.created),
        "modified_at": parse_metadata_datetime(props.modified),
    }


def _read_pdf_metadata(path: Path) -> dict[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    meta = reader.metadata or {}
    return {
        "author": normalize_metadata_value(meta.get("/Author")),
        "file_creator": normalize_metadata_value(meta.get("/Creator")),
        "pdf_producer": normalize_metadata_value(meta.get("/Producer")),
        "created_at": parse_metadata_datetime(meta.get("/CreationDate")),
        "modified_at": parse_metadata_datetime(meta.get("/ModDate")),
    }


def extract_file_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_METADATA_FILE_BYTES:
        return {}
    suffix = path.suffix.casefold()
    values: dict[str, Any] = {"file_sha256": hash_file(path)}
    try:
        if suffix == ".docx":
            values.update(_read_docx_metadata(path))
        elif suffix == ".pdf":
            values.update(_read_pdf_metadata(path))
    except Exception:
        # 文件损坏、加密或元数据解析失败时只保留可计算的 SHA-256，不阻断主链路。
        pass
    return {key: value for key, value in values.items() if value not in {None, ""}}


def enrich_document_record(record: AuditRecord, input_root: Path) -> bool:
    if record.record_type not in {"document", "file_metadata"}:
        return False
    path = resolve_document_path(input_root, record)
    if path is None:
        return False
    extracted = extract_file_metadata(path)
    changed = False
    for field_name in ("file_sha256", "author", "file_creator", "pdf_producer", "created_at", "modified_at"):
        if not getattr(record, field_name) and extracted.get(field_name):
            setattr(record, field_name, extracted[field_name])
            changed = True
    file_sha256 = extracted.get("file_sha256")
    if file_sha256:
        relative = path.resolve().relative_to(input_root.resolve()).as_posix()
        source_format = path.suffix.casefold().lstrip(".") or "file"
        if not any(
            ref.source_path == relative and ref.source_format == source_format
            for ref in record.source_refs
        ):
            record.source_refs.append(SourceReference(
                source_path=relative,
                source_format=source_format,
                row_number=0,
                source_sha256=file_sha256,
            ))
            changed = True
    return changed
