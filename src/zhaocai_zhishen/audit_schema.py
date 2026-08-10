from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

AUDIT_INGESTION_SCHEMA_VERSION = "bid-audit-ingestion/v1"
AUDIT_RECORD_SCHEMA_VERSION = "bid-audit-record/v1"

RECORD_TYPES = {
    "project",
    "bidder",
    "bid",
    "quote",
    "network_event",
    "file_metadata",
    "historical_relation",
    "document",
}

RECORD_TYPE_ALIASES = {
    "project": "project",
    "projects": "project",
    "项目": "project",
    "bidder": "bidder",
    "bidders": "bidder",
    "投标人": "bidder",
    "企业": "bidder",
    "bid": "bid",
    "bids": "bid",
    "投标": "bid",
    "quote": "quote",
    "quotes": "quote",
    "报价": "quote",
    "network_event": "network_event",
    "network": "network_event",
    "ip": "network_event",
    "网络日志": "network_event",
    "file_metadata": "file_metadata",
    "file": "file_metadata",
    "文件元数据": "file_metadata",
    "historical_relation": "historical_relation",
    "history": "historical_relation",
    "历史关系": "historical_relation",
    "document": "document",
    "documents": "document",
    "文档": "document",
}

FIELD_ALIASES = {
    "record_type": {"record_type", "type", "数据类型", "记录类型"},
    "record_id": {"record_id", "记录编号", "sample_id", "样本编号"},
    "project_id": {"project_id", "项目编号", "项目id", "项目_id"},
    "project_name": {"project_name", "项目名称", "项目"},
    "tender_no": {"tender_no", "tender_number", "招标编号", "采购编号"},
    "bid_id": {"bid_id", "投标编号", "投标id"},
    "bidder_id": {"bidder_id", "投标人编号", "企业编号"},
    "bidder_name": {"bidder_name", "bidder", "company_name", "bidder_candidate", "投标人名称", "投标人", "企业名称", "供应商名称"},
    "bidder_aliases": {"bidder_aliases", "aliases", "企业别名", "投标人别名"},
    "credit_code": {"credit_code", "uscc", "统一社会信用代码", "信用代码"},
    "amount": {"amount", "quote_amount", "bid_amount", "报价金额", "投标报价", "总价", "金额"},
    "currency": {"currency", "币种", "货币"},
    "event_time": {"event_time", "timestamp", "time", "事件时间", "操作时间", "时间"},
    "ip_address": {"ip_address", "ip", "ip地址", "网络地址"},
    "device_id": {"device_id", "device", "设备编号", "设备标识", "终端标识"},
    "file_name": {"file_name", "filename", "文件名", "文档名称"},
    "file_path": {"file_path", "path", "standard_path", "source_file", "文件路径", "文档路径"},
    "file_sha256": {"file_sha256", "sha256", "file_hash", "文件哈希", "文件摘要"},
    "author": {"author", "作者", "文档作者"},
    "created_at": {"created_at", "file_created_at", "创建时间"},
    "modified_at": {"modified_at", "file_modified_at", "修改时间"},
    "relation_type": {"relation_type", "关系类型"},
    "related_bidder_name": {"related_bidder_name", "关联投标人", "关联企业名称"},
    "related_credit_code": {"related_credit_code", "关联信用代码"},
    "source_system": {"source_system", "来源系统"},
}

_SENSITIVE_FIELDS = {
    "bidder_name",
    "bidder_aliases",
    "credit_code",
    "related_bidder_name",
    "related_credit_code",
    "ip_address",
    "device_id",
    "file_path",
    "author",
}

_CREDIT_CODE_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_CREDIT_CODE_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def normalize_field_key(value: object) -> str:
    text = normalize_text(value).casefold()
    text = re.sub(r"[\s\-./]+", "_", text)
    return text.strip("_")


_ALIAS_LOOKUP = {
    normalize_field_key(alias): canonical
    for canonical, aliases in FIELD_ALIASES.items()
    for alias in aliases
}


def canonicalize_input_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for original_key, value in row.items():
        key = normalize_field_key(original_key)
        target = _ALIAS_LOOKUP.get(key)
        if target:
            if target not in canonical or not normalize_text(canonical[target]):
                canonical[target] = value
        elif key:
            extra[key] = value
    return canonical, extra


def normalize_record_type(value: object) -> str:
    key = normalize_field_key(value)
    return RECORD_TYPE_ALIASES.get(key, key if key in RECORD_TYPES else "")


def normalize_company_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("有限责任公司", "有限公司").replace("股份有限责任公司", "股份有限公司")
    return "".join(char for char in text if char.isalnum())


def normalize_credit_code(value: object) -> str:
    return re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", str(value or "")).upper())


def is_valid_credit_code(value: object) -> bool:
    code = normalize_credit_code(value)
    if len(code) != 18 or any(char not in _CREDIT_CODE_CHARS for char in code):
        return False
    total = sum(_CREDIT_CODE_CHARS.index(char) * weight for char, weight in zip(code[:17], _CREDIT_CODE_WEIGHTS))
    expected = _CREDIT_CODE_CHARS[(31 - total % 31) % 31]
    return code[-1] == expected


def split_aliases(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_values: Iterable[object] = value
    else:
        raw_values = re.split(r"[;,，；|\n]+", str(value or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in raw_values:
        text = normalize_text(item)
        key = normalize_company_name(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def stable_identifier(prefix: str, value: object, length: int = 20) -> str:
    digest = hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def stable_bidder_id(name: object = "", credit_code: object = "") -> str:
    code = normalize_credit_code(credit_code)
    if is_valid_credit_code(code):
        return stable_identifier("bidder", f"credit:{code}")
    normalized_name = normalize_company_name(name)
    return stable_identifier("bidder", f"name:{normalized_name}") if normalized_name else ""


def stable_project_id(project_name: object) -> str:
    normalized = normalize_company_name(project_name)
    return stable_identifier("project", normalized) if normalized else ""


def parse_amount(value: object) -> tuple[str | None, bool]:
    if value is None or normalize_text(value) == "":
        return None, True
    text = unicodedata.normalize("NFKC", str(value)).replace(",", "")
    text = re.sub(r"(?:人民币|RMB|CNY|元|￥|¥)", "", text, flags=re.IGNORECASE).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None, False
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, False
    if not number.is_finite() or number <= 0:
        return None, False
    normalized = format(number.normalize(), "f")
    return normalized, True


def parse_timestamp(value: object) -> tuple[str, bool]:
    text = normalize_text(value)
    if not text:
        return "", True
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed.isoformat(timespec="seconds"), True
    except ValueError:
        pass
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M:%S",
        "%Y年%m月%d日",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.isoformat(timespec="seconds") if "%H" in pattern else parsed.date().isoformat(), True
        except ValueError:
            continue
    return "", False


def normalize_ip_address(value: object) -> tuple[str, bool]:
    text = normalize_text(value)
    if not text:
        return "", True
    try:
        return str(ipaddress.ip_address(text)), True
    except ValueError:
        return "", False


def normalize_sha256(value: object) -> tuple[str, bool]:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    if not text:
        return "", True
    return (text, True) if re.fullmatch(r"[0-9a-f]{64}", text) else ("", False)


def redacted_value_hint(field_name: str, value: object) -> str:
    text = normalize_text(value)
    if not text:
        return "<empty>"
    if field_name in _SENSITIVE_FIELDS:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest};len={len(text)}"
    clipped = re.sub(r"[\r\n\t]", " ", text)[:80]
    return clipped


@dataclass(frozen=True)
class SourceReference:
    source_path: str
    source_format: str
    row_number: int
    source_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str
    field: str
    source_path: str
    row_number: int
    value_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditRecord:
    record_id: str
    record_type: str
    project_id: str = ""
    project_name: str = ""
    tender_no: str = ""
    bid_id: str = ""
    bidder_id: str = ""
    bidder_name: str = ""
    credit_code: str = ""
    bidder_aliases: list[str] = field(default_factory=list)
    amount: str | None = None
    currency: str = "CNY"
    event_time: str = ""
    ip_address: str = ""
    device_id: str = ""
    file_name: str = ""
    file_path: str = ""
    file_sha256: str = ""
    author: str = ""
    created_at: str = ""
    modified_at: str = ""
    relation_type: str = ""
    related_bidder_id: str = ""
    related_bidder_name: str = ""
    related_credit_code: str = ""
    source_system: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    source_refs: list[SourceReference] = field(default_factory=list)
    schema_version: str = AUDIT_RECORD_SCHEMA_VERSION

    def fingerprint_payload(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("record_id", None)
        value.pop("source_refs", None)
        value.pop("schema_version", None)
        return value

    def fingerprint(self) -> str:
        payload = json.dumps(self.fingerprint_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_refs"] = [ref.to_dict() for ref in self.source_refs]
        return value


def make_source_reference(source_path: str, source_format: str, row_number: int, raw_row: dict[str, Any]) -> SourceReference:
    encoded = json.dumps(raw_row, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return SourceReference(
        source_path=source_path,
        source_format=source_format,
        row_number=row_number,
        source_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def validate_and_normalize_fields(
    row: dict[str, Any],
    source: SourceReference,
    inferred_record_type: str = "",
) -> tuple[dict[str, Any] | None, list[ValidationIssue]]:
    canonical, extra = canonicalize_input_row(row)
    issues: list[ValidationIssue] = []
    record_type = normalize_record_type(canonical.get("record_type") or inferred_record_type)
    if not record_type:
        issues.append(ValidationIssue(
            code="unknown_record_type",
            message="记录类型缺失或不受支持",
            severity="error",
            field="record_type",
            source_path=source.source_path,
            row_number=source.row_number,
            value_hint=redacted_value_hint("record_type", canonical.get("record_type")),
        ))
        return None, issues

    normalized = {key: normalize_text(value) for key, value in canonical.items() if key not in {"amount", "bidder_aliases"}}
    normalized["record_type"] = record_type
    normalized["bidder_aliases"] = split_aliases(canonical.get("bidder_aliases"))
    normalized["extra"] = {key: value for key, value in extra.items() if normalize_text(value)}

    amount, amount_ok = parse_amount(canonical.get("amount"))
    normalized["amount"] = amount
    if not amount_ok:
        issues.append(ValidationIssue(
            code="invalid_amount",
            message="金额不是有效的正数，已按缺失值降级处理",
            severity="warning",
            field="amount",
            source_path=source.source_path,
            row_number=source.row_number,
            value_hint=redacted_value_hint("amount", canonical.get("amount")),
        ))

    for time_field in ("event_time", "created_at", "modified_at"):
        parsed_time, time_ok = parse_timestamp(canonical.get(time_field))
        normalized[time_field] = parsed_time
        if not time_ok:
            issues.append(ValidationIssue(
                code="invalid_time",
                message="时间格式无法识别，已按缺失值降级处理",
                severity="warning",
                field=time_field,
                source_path=source.source_path,
                row_number=source.row_number,
                value_hint=redacted_value_hint(time_field, canonical.get(time_field)),
            ))

    ip_address, ip_ok = normalize_ip_address(canonical.get("ip_address"))
    normalized["ip_address"] = ip_address
    if not ip_ok:
        issues.append(ValidationIssue(
            code="invalid_ip",
            message="IP 地址格式无效，已按缺失值降级处理",
            severity="warning",
            field="ip_address",
            source_path=source.source_path,
            row_number=source.row_number,
            value_hint=redacted_value_hint("ip_address", canonical.get("ip_address")),
        ))

    file_sha256, sha_ok = normalize_sha256(canonical.get("file_sha256"))
    normalized["file_sha256"] = file_sha256
    if not sha_ok:
        issues.append(ValidationIssue(
            code="invalid_file_sha256",
            message="文件摘要不是 64 位 SHA-256，已按缺失值降级处理",
            severity="warning",
            field="file_sha256",
            source_path=source.source_path,
            row_number=source.row_number,
            value_hint=redacted_value_hint("file_sha256", canonical.get("file_sha256")),
        ))

    for field_name in ("credit_code", "related_credit_code"):
        raw_code = canonical.get(field_name)
        code = normalize_credit_code(raw_code)
        if code and not is_valid_credit_code(code):
            issues.append(ValidationIssue(
                code="invalid_credit_code",
                message="统一社会信用代码校验失败，企业映射将回退到规范化名称",
                severity="warning",
                field=field_name,
                source_path=source.source_path,
                row_number=source.row_number,
                value_hint=redacted_value_hint(field_name, raw_code),
            ))
            code = ""
        normalized[field_name] = code

    normalized["currency"] = normalize_text(canonical.get("currency") or "CNY").upper()
    return normalized, issues
