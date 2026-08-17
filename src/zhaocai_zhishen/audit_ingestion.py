from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .audit_schema import (
    AUDIT_INGESTION_SCHEMA_VERSION,
    AuditRecord,
    SourceReference,
    ValidationIssue,
    is_valid_credit_code,
    make_source_reference,
    normalize_company_name,
    normalize_credit_code,
    normalize_field_key,
    normalize_record_type,
    normalize_text,
    redacted_value_hint,
    stable_bidder_id,
    stable_identifier,
    stable_project_id,
    validate_and_normalize_fields,
)
from .metadata_analysis import enrich_document_record

MAX_STRUCTURED_FILE_BYTES = 64 * 1024 * 1024
MAX_STRUCTURED_ROWS = 1_000_000
SUPPORTED_SUFFIXES = {".csv", ".jsonl"}

FILE_TYPE_HINTS = {
    "projects": "project",
    "project": "project",
    "项目": "project",
    "bidders": "bidder",
    "bidder": "bidder",
    "投标人": "bidder",
    "企业": "bidder",
    "bids": "bid",
    "bid": "bid",
    "投标": "bid",
    "quotes": "quote",
    "quote": "quote",
    "报价": "quote",
    "network_events": "network_event",
    "network_event": "network_event",
    "network": "network_event",
    "网络日志": "network_event",
    "ip日志": "network_event",
    "file_metadata": "file_metadata",
    "files_metadata": "file_metadata",
    "文件元数据": "file_metadata",
    "historical_relations": "historical_relation",
    "historical_relation": "historical_relation",
    "history": "historical_relation",
    "历史关系": "historical_relation",
    "documents": "document",
    "document": "document",
    "文档": "document",
    "audit_records": "",
    "审计记录": "",
}


@dataclass(frozen=True)
class StructuredSource:
    path: Path
    relative_path: str
    source_format: str
    inferred_record_type: str
    adapter: str = "canonical"


@dataclass
class LoadedRow:
    raw: dict[str, Any]
    source: SourceReference
    inferred_record_type: str


class AuditIngestionError(ValueError):
    pass


class BidderRegistry:
    def __init__(self) -> None:
        self.name_codes: dict[str, set[str]] = defaultdict(set)
        self.alias_targets: dict[str, set[str]] = defaultdict(set)
        self.display_names: dict[str, str] = {}
        self._finalized = False
        self._issues: list[ValidationIssue] = []

    def observe(self, row: dict[str, Any], source: SourceReference) -> None:
        name = normalize_text(row.get("bidder_name"))
        name_key = normalize_company_name(name)
        code = normalize_credit_code(row.get("credit_code"))
        if name_key:
            self.display_names.setdefault(name_key, name)
            if code and is_valid_credit_code(code):
                self.name_codes[name_key].add(code)
        for alias in row.get("bidder_aliases") or []:
            alias_key = normalize_company_name(alias)
            if alias_key and name_key and alias_key != name_key:
                self.alias_targets[alias_key].add(name_key)
                self.display_names.setdefault(alias_key, normalize_text(alias))

        related_name = normalize_text(row.get("related_bidder_name"))
        related_key = normalize_company_name(related_name)
        related_code = normalize_credit_code(row.get("related_credit_code"))
        if related_key:
            self.display_names.setdefault(related_key, related_name)
            if related_code and is_valid_credit_code(related_code):
                self.name_codes[related_key].add(related_code)

    def finalize(self) -> list[ValidationIssue]:
        if self._finalized:
            return list(self._issues)
        self._finalized = True
        for name_key, codes in sorted(self.name_codes.items()):
            if len(codes) > 1:
                self._issues.append(ValidationIssue(
                    code="credit_code_conflict",
                    message="同一规范化企业名称对应多个统一社会信用代码，未对无代码记录进行强制归并",
                    severity="error",
                    field="credit_code",
                    source_path="<aggregate>",
                    row_number=0,
                    value_hint=f"name_hash:{stable_identifier('name', name_key, 12)};code_count={len(codes)}",
                ))
        for alias_key, targets in sorted(self.alias_targets.items()):
            if len(targets) > 1:
                self._issues.append(ValidationIssue(
                    code="alias_conflict",
                    message="同一企业别名指向多个规范化名称，别名归并已停用",
                    severity="error",
                    field="bidder_aliases",
                    source_path="<aggregate>",
                    row_number=0,
                    value_hint=f"alias_hash:{stable_identifier('alias', alias_key, 12)};target_count={len(targets)}",
                ))
        return list(self._issues)

    def _canonical_name_key(self, name: object) -> str:
        key = normalize_company_name(name)
        targets = self.alias_targets.get(key, set())
        return next(iter(targets)) if len(targets) == 1 else key

    def resolve(self, name: object = "", credit_code: object = "") -> str:
        code = normalize_credit_code(credit_code)
        if code and is_valid_credit_code(code):
            return stable_bidder_id(credit_code=code)
        name_key = self._canonical_name_key(name)
        codes = self.name_codes.get(name_key, set())
        if len(codes) == 1:
            return stable_bidder_id(credit_code=next(iter(codes)))
        return stable_bidder_id(name=name_key)

    def index_rows(self) -> list[dict[str, Any]]:
        keys = set(self.display_names) | set(self.name_codes) | set(self.alias_targets)
        rows = []
        for key in sorted(keys):
            targets = self.alias_targets.get(key, set())
            canonical_key = next(iter(targets)) if len(targets) == 1 else key
            codes = sorted(self.name_codes.get(canonical_key, set()))
            bidder_id = self.resolve(self.display_names.get(key, key), codes[0] if len(codes) == 1 else "")
            rows.append({
                "bidder_id": bidder_id,
                "normalized_name_hash": stable_identifier("name", canonical_key, 16),
                "display_name": self.display_names.get(canonical_key, self.display_names.get(key, "")),
                "credit_code": codes[0] if len(codes) == 1 else "",
                "credit_code_conflict": len(codes) > 1,
                "is_alias": canonical_key != key,
            })
        return rows


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _normalized_stem(path: Path) -> str:
    return normalize_field_key(path.stem)


def _preview_csv_headers(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader, [])


def _preview_jsonl(path: Path) -> dict[str, Any] | None:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _is_standard_samples(headers: Iterable[str]) -> bool:
    keys = {normalize_field_key(value) for value in headers}
    identity_fields = {"sample_id", "project_name", "bidder_candidate"}
    path_fields = {"standard_path", "bid_file"}
    return identity_fields.issubset(keys) and bool(keys & path_fields)


def _has_record_type_field(keys: Iterable[str]) -> bool:
    normalized = {normalize_field_key(value) for value in keys}
    return bool(normalized & {"record_type", "type", "数据类型", "记录类型"})


def discover_structured_sources(input_dir: Path) -> tuple[list[StructuredSource], dict[str, int]]:
    root = input_dir.resolve()
    if not root.exists():
        return [], {"discovered": 0, "recognized": 0, "ignored": 0, "oversized": 0}
    if not root.is_dir():
        raise AuditIngestionError(f"结构化数据输入必须是目录: {input_dir}")

    sources: list[StructuredSource] = []
    discovered = ignored = oversized = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        discovered += 1
        if path.stat().st_size > MAX_STRUCTURED_FILE_BYTES:
            oversized += 1
            continue
        inferred = FILE_TYPE_HINTS.get(_normalized_stem(path))
        adapter = "canonical"
        recognized = inferred is not None
        try:
            if path.suffix.lower() == ".csv":
                headers = _preview_csv_headers(path)
                if _is_standard_samples(headers):
                    inferred = "document"
                    adapter = "standard_samples"
                    recognized = True
                elif _has_record_type_field(headers):
                    inferred = inferred or ""
                    recognized = True
            else:
                preview = _preview_jsonl(path)
                if preview and _has_record_type_field(preview.keys()):
                    inferred = inferred or ""
                    recognized = True
        except (OSError, UnicodeError, csv.Error):
            recognized = True
        if recognized:
            sources.append(StructuredSource(
                path=path,
                relative_path=_relative_path(path, root),
                source_format=path.suffix.lower().lstrip("."),
                inferred_record_type=inferred or "",
                adapter=adapter,
            ))
        else:
            ignored += 1
    return sources, {
        "discovered": discovered,
        "recognized": len(sources),
        "ignored": ignored,
        "oversized": oversized,
    }


def _adapt_standard_sample(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {normalize_field_key(key): value for key, value in row.items()}
    standard_path = normalize_text(
        normalized.get("standard_path") or normalized.get("bid_file") or normalized.get("source_file")
    )
    return {
        "record_type": "document",
        "record_id": normalized.get("sample_id", ""),
        "project_name": normalized.get("project_name", ""),
        "bidder_name": normalized.get("bidder_candidate", ""),
        "file_path": standard_path,
        "file_name": Path(standard_path).name if standard_path else "",
        "source_inner_path": normalized.get("source_inner_path", ""),
        "archive_id": normalized.get("archive_id", ""),
    }


def _load_csv(source: StructuredSource) -> tuple[list[LoadedRow], list[ValidationIssue], bool]:
    rows: list[LoadedRow] = []
    issues: list[ValidationIssue] = []
    try:
        with source.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return rows, issues, True
            for line_number, raw in enumerate(reader, 2):
                if len(rows) >= MAX_STRUCTURED_ROWS:
                    raise AuditIngestionError(f"结构化数据行数超过上限 {MAX_STRUCTURED_ROWS}")
                if not any(normalize_text(value) for value in raw.values()):
                    continue
                adapted = _adapt_standard_sample(raw) if source.adapter == "standard_samples" else dict(raw)
                ref = make_source_reference(source.relative_path, source.source_format, line_number, dict(raw))
                rows.append(LoadedRow(adapted, ref, source.inferred_record_type))
    except (OSError, UnicodeError, csv.Error) as exc:
        issues.append(ValidationIssue(
            code="source_read_error",
            message=f"CSV 文件无法读取: {type(exc).__name__}",
            severity="error",
            field="",
            source_path=source.relative_path,
            row_number=0,
        ))
    return rows, issues, not rows


def _load_jsonl(source: StructuredSource) -> tuple[list[LoadedRow], list[ValidationIssue], bool]:
    rows: list[LoadedRow] = []
    issues: list[ValidationIssue] = []
    try:
        with source.path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                if len(rows) >= MAX_STRUCTURED_ROWS:
                    raise AuditIngestionError(f"结构化数据行数超过上限 {MAX_STRUCTURED_ROWS}")
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.append(ValidationIssue(
                        code="invalid_jsonl",
                        message=f"JSONL 行无法解析: {exc.msg}",
                        severity="error",
                        field="",
                        source_path=source.relative_path,
                        row_number=line_number,
                    ))
                    continue
                if not isinstance(raw, dict):
                    issues.append(ValidationIssue(
                        code="invalid_jsonl_row",
                        message="JSONL 每行必须是对象",
                        severity="error",
                        field="",
                        source_path=source.relative_path,
                        row_number=line_number,
                        value_hint=redacted_value_hint("", type(raw).__name__),
                    ))
                    continue
                ref = make_source_reference(source.relative_path, source.source_format, line_number, raw)
                rows.append(LoadedRow(raw, ref, source.inferred_record_type))
    except (OSError, UnicodeError) as exc:
        issues.append(ValidationIssue(
            code="source_read_error",
            message=f"JSONL 文件无法读取: {type(exc).__name__}",
            severity="error",
            field="",
            source_path=source.relative_path,
            row_number=0,
        ))
    return rows, issues, not rows


def _issue(
    code: str,
    message: str,
    field: str,
    source: SourceReference,
    value: object = "",
    severity: str = "warning",
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        severity=severity,
        field=field,
        source_path=source.source_path,
        row_number=source.row_number,
        value_hint=redacted_value_hint(field, value),
    )


def _build_record(row: dict[str, Any], source: SourceReference, registry: BidderRegistry) -> tuple[AuditRecord, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    project_name = normalize_text(row.get("project_name"))
    project_id = normalize_text(row.get("project_id")) or stable_project_id(project_name)
    bidder_name = normalize_text(row.get("bidder_name"))
    credit_code = normalize_credit_code(row.get("credit_code"))
    bidder_id = normalize_text(row.get("bidder_id")) or registry.resolve(bidder_name, credit_code)
    related_name = normalize_text(row.get("related_bidder_name"))
    related_code = normalize_credit_code(row.get("related_credit_code"))
    related_bidder_id = registry.resolve(related_name, related_code)
    record_type = row["record_type"]

    project_scoped = {"bid", "quote", "network_event", "file_metadata", "historical_relation", "document"}
    bidder_scoped = {"bid", "quote", "network_event", "file_metadata", "historical_relation", "document", "bidder"}
    if record_type in project_scoped and not project_id:
        issues.append(_issue("missing_project", "缺少项目编号或项目名称，记录仍保留但无法进行项目级聚合", "project_id", source))
    if record_type in bidder_scoped and not bidder_id:
        issues.append(_issue("missing_bidder", "缺少企业名称或有效信用代码，记录仍保留但无法进行企业级关联", "bidder_id", source))
    is_item_quote = bool(row.get("item_code") or row.get("item_name") or row.get("unit_price"))
    if record_type == "quote" and row.get("amount") is None and not (is_item_quote and row.get("unit_price") is not None):
        issues.append(_issue("missing_amount", "报价记录缺少有效金额，文本和其他数据仍可继续分析", "amount", source))
    if record_type == "network_event" and not row.get("ip_address") and not normalize_text(row.get("device_id")):
        issues.append(_issue("missing_network_identity", "网络日志缺少 IP 和设备标识，已降级保留时间及来源信息", "ip_address", source))
    if record_type in {"file_metadata", "document"} and not any(normalize_text(row.get(field)) for field in ("file_name", "file_path", "file_sha256")):
        issues.append(_issue("missing_file_identity", "文件记录缺少文件名、路径和摘要", "file_name", source))
    if record_type == "historical_relation" and not related_bidder_id:
        issues.append(_issue("missing_related_bidder", "历史关系缺少关联企业", "related_bidder_name", source))

    record = AuditRecord(
        record_id="",
        record_type=record_type,
        project_id=project_id,
        project_name=project_name,
        tender_no=normalize_text(row.get("tender_no")),
        bid_id=normalize_text(row.get("bid_id")),
        bidder_id=bidder_id,
        bidder_name=bidder_name,
        credit_code=credit_code,
        bidder_aliases=list(row.get("bidder_aliases") or []),
        amount=row.get("amount"),
        control_amount=row.get("control_amount"),
        amount_unit=normalize_text(row.get("amount_unit")),
        quote_scope=normalize_text(row.get("quote_scope")).lower(),
        item_code=normalize_text(row.get("item_code")),
        item_name=normalize_text(row.get("item_name")),
        quantity=row.get("quantity"),
        unit_price=row.get("unit_price"),
        currency=normalize_text(row.get("currency") or "CNY").upper(),
        event_time=normalize_text(row.get("event_time")),
        ip_address=normalize_text(row.get("ip_address")),
        device_id=normalize_text(row.get("device_id")),
        account_id=normalize_text(row.get("account_id")),
        file_name=normalize_text(row.get("file_name")),
        file_path=normalize_text(row.get("file_path")),
        file_sha256=normalize_text(row.get("file_sha256")),
        author=normalize_text(row.get("author")),
        file_creator=normalize_text(row.get("file_creator")),
        pdf_producer=normalize_text(row.get("pdf_producer")),
        created_at=normalize_text(row.get("created_at")),
        modified_at=normalize_text(row.get("modified_at")),
        uploaded_at=normalize_text(row.get("uploaded_at")),
        network_role=normalize_text(row.get("network_role")),
        is_public_exit=row.get("is_public_exit"),
        relation_type=normalize_text(row.get("relation_type")),
        related_bidder_id=related_bidder_id,
        related_bidder_name=related_name,
        related_credit_code=related_code,
        source_system=normalize_text(row.get("source_system")),
        agency_id=normalize_text(row.get("agency_id")),
        is_winner=row.get("is_winner"),
        rank=row.get("rank"),
        extra=dict(row.get("extra") or {}),
        source_refs=[source],
    )
    supplied_record_id = normalize_text(row.get("record_id"))
    record.record_id = supplied_record_id or stable_identifier("record", record.fingerprint())
    return record, issues


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


def _coverage_summary(
    records: list[AuditRecord],
    issues: list[ValidationIssue],
    source_stats: dict[str, int],
    input_rows: int,
    duplicate_count: int,
    empty_source_count: int,
    registry: BidderRegistry,
) -> dict[str, Any]:
    by_type = Counter(record.record_type for record in records)
    projects = {record.project_id for record in records if record.project_id}
    bidders = {record.bidder_id for record in records if record.bidder_id}
    missing_fields = Counter()
    for record in records:
        if not record.project_id and record.record_type not in {"project", "bidder"}:
            missing_fields["project_id"] += 1
        if not record.bidder_id and record.record_type not in {"project"}:
            missing_fields["bidder_id"] += 1
        is_item_quote = bool(record.item_code or record.item_name or record.unit_price)
        if record.record_type == "quote" and record.amount is None and not (is_item_quote and record.unit_price is not None):
            missing_fields["amount"] += 1
        if record.record_type == "network_event" and not record.ip_address:
            missing_fields["ip_address"] += 1

    def category(record_types: set[str], usable) -> dict[str, Any]:
        selected = [record for record in records if record.record_type in record_types]
        usable_records = [record for record in selected if usable(record)]
        category_projects = {record.project_id for record in selected if record.project_id}
        return {
            "record_count": len(selected),
            "usable_record_count": len(usable_records),
            "project_count": len(category_projects),
            "project_coverage_rate": round(len(category_projects) / len(projects), 4) if projects else 0.0,
            "available": bool(selected),
        }

    issue_codes = Counter(issue.code for issue in issues)
    severity_counts = Counter(issue.severity for issue in issues)
    return {
        "schema_version": AUDIT_INGESTION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": {
            **source_stats,
            "empty": empty_source_count,
        },
        "records": {
            "input_row_count": input_rows,
            "accepted_record_count": len(records),
            "duplicate_record_count": duplicate_count,
            "project_count": len(projects),
            "bidder_count": len(bidders),
            "by_type": dict(sorted(by_type.items())),
        },
        "categories": {
            "files": category({"document", "file_metadata"}, lambda row: bool(row.file_name or row.file_path or row.file_sha256)),
            "quotes": category({"quote"}, lambda row: row.amount is not None or (bool(row.item_code or row.item_name or row.unit_price) and row.unit_price is not None)),
            "ip": category({"network_event"}, lambda row: bool(row.ip_address)),
            "history": category({"historical_relation"}, lambda row: bool(row.related_bidder_id)),
        },
        "data_quality": {
            "issue_count": len(issues),
            "severity_counts": dict(sorted(severity_counts.items())),
            "issue_codes": dict(sorted(issue_codes.items())),
            "missing_fields": dict(sorted(missing_fields.items())),
            "credit_code_conflict_count": issue_codes.get("credit_code_conflict", 0),
            "alias_conflict_count": issue_codes.get("alias_conflict", 0),
        },
        "degradation": {
            "text_analysis_allowed_without_quotes": True,
            "text_analysis_allowed_without_ip": True,
            "partial_input_supported": True,
        },
        "privacy": {
            "aggregate_only": True,
            "contains_bidder_names": False,
            "contains_credit_codes": False,
            "contains_ip_addresses": False,
            "contains_source_paths": False,
        },
    }


def empty_coverage_summary() -> dict[str, Any]:
    registry = BidderRegistry()
    return _coverage_summary([], [], {"discovered": 0, "recognized": 0, "ignored": 0, "oversized": 0}, 0, 0, 0, registry)


def load_coverage_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_coverage_summary()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty_coverage_summary()
    return value if isinstance(value, dict) else empty_coverage_summary()


def ingest_audit_data(input_dir: Path, output_dir: Path, *, strict: bool = False) -> dict[str, Any]:
    sources, source_stats = discover_structured_sources(input_dir)
    loaded_rows: list[LoadedRow] = []
    issues: list[ValidationIssue] = []
    empty_source_count = 0
    for source in sources:
        rows, source_issues, is_empty = _load_csv(source) if source.source_format == "csv" else _load_jsonl(source)
        loaded_rows.extend(rows)
        issues.extend(source_issues)
        empty_source_count += int(is_empty)

    normalized_rows: list[tuple[dict[str, Any], SourceReference]] = []
    registry = BidderRegistry()
    for loaded in loaded_rows:
        normalized, row_issues = validate_and_normalize_fields(loaded.raw, loaded.source, loaded.inferred_record_type)
        issues.extend(row_issues)
        if normalized is None:
            continue
        normalized_rows.append((normalized, loaded.source))
        registry.observe(normalized, loaded.source)
    issues.extend(registry.finalize())

    records_by_fingerprint: dict[str, AuditRecord] = {}
    duplicate_count = 0
    for normalized, source in normalized_rows:
        record, record_issues = _build_record(normalized, source, registry)
        issues.extend(record_issues)
        enrich_document_record(record, input_dir)
        fingerprint = record.fingerprint()
        existing = records_by_fingerprint.get(fingerprint)
        if existing is not None:
            duplicate_count += 1
            if source not in existing.source_refs:
                existing.source_refs.append(source)
            continue
        records_by_fingerprint[fingerprint] = record

    records = sorted(records_by_fingerprint.values(), key=lambda row: (row.record_type, row.project_id, row.bidder_id, row.record_id))
    coverage = _coverage_summary(records, issues, source_stats, len(loaded_rows), duplicate_count, empty_source_count, registry)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "audit_records.jsonl", (record.to_dict() for record in records))
    _write_jsonl(output_dir / "validation_issues.jsonl", (issue.to_dict() for issue in issues))
    _write_json(output_dir / "bidder_index.json", {
        "schema_version": AUDIT_INGESTION_SCHEMA_VERSION,
        "bidders": registry.index_rows(),
    })
    _write_json(output_dir / "coverage_summary.json", coverage)

    if strict and any(issue.severity == "error" for issue in issues):
        raise AuditIngestionError(f"结构化数据校验失败，共 {coverage['data_quality']['severity_counts'].get('error', 0)} 项错误")
    return {
        "schema_version": AUDIT_INGESTION_SCHEMA_VERSION,
        "record_count": len(records),
        "issue_count": len(issues),
        "coverage": coverage,
        "outputs": {
            "records": str(output_dir / "audit_records.jsonl"),
            "issues": str(output_dir / "validation_issues.jsonl"),
            "bidder_index": str(output_dir / "bidder_index.json"),
            "coverage": str(output_dir / "coverage_summary.json"),
        },
    }
