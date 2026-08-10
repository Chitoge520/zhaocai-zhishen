from __future__ import annotations

import csv
import json
import math
import re
import zlib
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .document_pipeline import normalize_text

HASH_DIMENSION = 1 << 16
MAX_TEXT_CHARS = 120_000
ENTITY_SCAN_CHARS = 16_000
SIMILARITY_ONLY_THRESHOLD = 0.75
REPEATED_SEGMENT_LENGTH = 48
REPEATED_SEGMENT_STRIDE = 16
MAX_REPEATED_SEGMENTS = 8
COMPANY_PATTERN = re.compile(
    r"(?:投标人(?:名称)?|供应商(?:名称)?|单位名称)[：:\s]*"
    r"([^\n，,；;]{4,60}?(?:有限责任公司|股份有限公司|有限公司|公司))"
)
COMPANY_VALUE_PATTERN = re.compile(r"[^\n，,；;/\\]{2,60}?(?:有限责任公司|股份有限公司|有限公司|公司)")
ENGLISH_COMPANY_PATTERN = re.compile(
    r"(?:投标人(?:名称)?|供应商(?:名称)?|单位名称)[：:\s]*"
    r"([A-Za-z][A-Za-z0-9 .,&()'’-]{1,80}?(?:Co\.?\s*,?\s*Ltd\.?|Limited|Corporation|Inc\.?))",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8})(?!\d)")
EMAIL_PATTERN = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
CREDIT_CODE_PATTERN = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])")
CONTACT_PATTERN = re.compile(r"联系人[：:\s]*([\u4e00-\u9fff·]{2,8}?)(?=\s|[，,；;]|联系电话|电话|手机|$)")
CONTACT_STOPWORDS = {"及电话", "和电话", "及手机", "和手机", "联系电话", "联系方式", "联系人", "姓名"}
ADDRESS_PATTERN = re.compile(
    r"(?:注册地址|联系地址|公司地址|地址)[：:\s]*"
    r"([^\n；;]{6,100}?)"
    r"(?=\s*(?:传真|电话|联系电话|手机|邮箱|电子邮箱|Email|E-mail|邮编|邮政编码|联系人|"
    r"统一社会信用代码|开户银行|银行账号)[：:\s]|\n|；|;|$)",
    re.IGNORECASE,
)
TENDER_NO_PATTERN = re.compile(r"(?:招标编号|项目编号|采购编号)[：:\s]*([A-Za-z0-9_./－—-]{4,60})")
CREDIT_CODE_CHARACTERS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
CREDIT_CODE_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
THREE_DIGIT_AREA_CODES = {"010", "020", "021", "022", "023", "024", "025", "027", "028", "029"}
FOUR_DIGIT_AREA_CODES = {
    "0335", "0349", "0421", "0427", "0429", "0482", "0483", "0523", "0527", "0543", "0546",
    "0580", "0631", "0632", "0633", "0634", "0635", "0660", "0662", "0663", "0668", "0691", "0692",
    "0701", "0722", "0724", "0728", "0743", "0744", "0745", "0746", "0760", "0762", "0763", "0766",
    "0768", "0769", "0812", "0813", "0816", "0817", "0818", "0825", "0826", "0827", "0883", "0886",
    "0887", "0888", "0941", "0943",
}
for prefix, start, end in (
    ("03", 10, 19), ("03", 50, 59), ("03", 70, 79), ("03", 91, 98),
    ("04", 11, 18), ("04", 31, 39), ("04", 51, 59), ("04", 64, 69), ("04", 70, 79),
    ("05", 10, 19), ("05", 30, 39), ("05", 50, 59), ("05", 61, 66), ("05", 70, 79), ("05", 91, 99),
    ("07", 10, 19), ("07", 30, 39), ("07", 50, 59), ("07", 70, 79), ("07", 90, 99),
    ("08", 30, 39), ("08", 51, 59), ("08", 70, 79), ("08", 91, 98),
    ("09", 1, 3), ("09", 6, 6), ("09", 8, 9), ("09", 11, 19), ("09", 30, 39),
    ("09", 51, 55), ("09", 70, 79), ("09", 90, 99),
):
    FOUR_DIGIT_AREA_CODES.update(f"{prefix}{value:02d}" for value in range(start, end + 1))
ADDRESS_MARKERS = ("省", "市", "区", "县", "镇", "乡", "街道", "路", "街", "巷", "弄", "号", "楼", "室", "座", "园区", "大厦")
ADDRESS_STOPWORDS = ("部件名称", "文件正本", "副本", "如有的话", "资格声明", "出口销售", "签字", "盖章")


def is_valid_credit_code(value: str) -> bool:
    value = value.upper()
    if len(value) != 18 or value.isdigit() or any(char not in CREDIT_CODE_CHARACTERS for char in value):
        return False
    total = sum(CREDIT_CODE_CHARACTERS.index(char) * weight for char, weight in zip(value[:17], CREDIT_CODE_WEIGHTS))
    expected = CREDIT_CODE_CHARACTERS[(31 - total % 31) % 31]
    return value[-1] == expected


def is_valid_phone(value: str) -> bool:
    if re.fullmatch(r"1[3-9]\d{9}", value):
        return True
    if not value.startswith("0"):
        return False
    if value[:3] in THREE_DIGIT_AREA_CODES:
        return len(value) in {10, 11}
    if value[:4] in FOUR_DIGIT_AREA_CODES:
        return len(value) in {11, 12}
    return False


def is_valid_address(value: str) -> bool:
    if any(word in value for word in ADDRESS_STOPWORDS):
        return False
    return len(value) >= 8 and any(marker in value for marker in ADDRESS_MARKERS)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {line_number} 行不是有效 JSON") from exc
    return rows


def unique_sorted(values: Iterable[str]) -> list[str]:
    return sorted({value.strip() for value in values if value and value.strip()})


def extract_contacts(text: str) -> list[str]:
    values = []
    for value in CONTACT_PATTERN.findall(text):
        value = value.strip()
        if value in CONTACT_STOPWORDS or any(word in value for word in ("电话", "手机", "方式", "姓名")):
            continue
        if 2 <= len(value.replace("·", "")) <= 4:
            values.append(value)
    return unique_sorted(values)


def extract_company_name(value: str) -> str:
    match = COMPANY_VALUE_PATTERN.search(normalize_text(value))
    if not match:
        return ""
    company = re.sub(r"^\s*\d+[_ .、-]*", "", match.group(0))
    company = re.sub(r"^(?:文件|投标文件|扫描件|盖章版)[\s_：:.-]*", "", company)
    company = re.sub(r"[（(].*?(?:盖章|单位章|公章).*?[）)]", "", company)
    return company.strip(" _-—：:（()）")


def normalize_bidder_name(document: dict, text: str) -> str:
    metadata_company = extract_company_name(document.get("bidder_name", ""))
    if metadata_company:
        return metadata_company
    match = COMPANY_PATTERN.search(text[:12_000])
    if match:
        return extract_company_name(match.group(1)) or normalize_text(match.group(1)).strip()
    english_match = ENGLISH_COMPANY_PATTERN.search(text[:ENTITY_SCAN_CHARS])
    if english_match:
        company = re.sub(r"\s+", " ", english_match.group(1)).strip(" .,:：")
        company = re.sub(r"(?i)\s*Co\.?\s*,?\s*Ltd\.?$", " Co., Ltd.", company)
        return company
    return normalize_text(document.get("bidder_name", "")) or Path(document.get("file_path", "")).stem


def canonicalize_bidder_names(entities: list[dict]) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entity in entities:
        grouped[str(entity.get("project_id", ""))].append(entity)
    suffix_pattern = re.compile(r"(?:有限责任公司|股份有限公司|有限公司|公司)$")
    for rows in grouped.values():
        companies = unique_sorted(row["bidder"] for row in rows if suffix_pattern.search(row.get("bidder", "")))
        for row in rows:
            raw = row.get("bidder", "")
            row["bidder_raw"] = raw
            if not raw or suffix_pattern.search(raw) or not companies:
                continue
            compact_raw = re.sub(r"\s+", "", raw)
            matches: list[tuple[int, str, str]] = []
            for company in companies:
                company_stem = suffix_pattern.sub("", company)
                match = SequenceMatcher(None, compact_raw, company_stem).find_longest_match()
                fragment = compact_raw[match.a : match.a + match.size]
                if match.size >= 4 and fragment not in {"有限公司", "责任公司", "股份有限", "科技有限", "集团有限"}:
                    matches.append((match.size, company, fragment))
            matches.sort(reverse=True)
            if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]):
                row["bidder"] = matches[0][1]


def extract_entities(document: dict) -> dict:
    text = normalize_text(document.get("text", ""))
    entity_text = text[:ENTITY_SCAN_CHARS]
    phones = []
    for match in PHONE_PATTERN.finditer(entity_text):
        value = re.sub(r"\D", "", match.group(1))
        if is_valid_phone(value):
            phones.append(value)
    addresses = [re.sub(r"\s+", "", match.group(1)).strip("：:，,。 ") for match in ADDRESS_PATTERN.finditer(entity_text)]
    addresses = [value for value in addresses if len(value) <= 100 and is_valid_address(value)]
    return {
        "document_id": document["document_id"],
        "project_id": document.get("project_id", ""),
        "file_path": document.get("file_path", ""),
        "bidder": normalize_bidder_name(document, text),
        "phones": unique_sorted(phones),
        "emails": unique_sorted(value.lower() for value in EMAIL_PATTERN.findall(entity_text)),
        "credit_codes": unique_sorted(value for value in CREDIT_CODE_PATTERN.findall(entity_text.upper()) if is_valid_credit_code(value)),
        "contacts": extract_contacts(entity_text),
        "addresses": unique_sorted(addresses)[:20],
        "tender_numbers": unique_sorted(TENDER_NO_PATTERN.findall(entity_text))[:20],
    }


def hashed_ngram_counts(text: str, ngram_size: int = 3) -> Counter[int]:
    compact = re.sub(r"\s+", "", normalize_text(text))[:MAX_TEXT_CHARS]
    counts: Counter[int] = Counter()
    if len(compact) < ngram_size:
        return counts
    encoded_cache: dict[str, int] = {}
    for index in range(len(compact) - ngram_size + 1):
        ngram = compact[index : index + ngram_size]
        feature = encoded_cache.get(ngram)
        if feature is None:
            feature = zlib.crc32(ngram.encode("utf-8")) % HASH_DIMENSION
            encoded_cache[ngram] = feature
        counts[feature] += 1
    return counts


def build_tfidf_vectors(documents: list[dict]) -> list[dict[int, float]]:
    counts = [hashed_ngram_counts(document.get("text", "")) for document in documents]
    document_frequency: Counter[int] = Counter()
    for row in counts:
        document_frequency.update(row.keys())
    total = len(documents)
    vectors: list[dict[int, float]] = []
    for row in counts:
        weighted = {
            feature: (1.0 + math.log(frequency)) * (math.log((1.0 + total) / (1.0 + document_frequency[feature])) + 1.0)
            for feature, frequency in row.items()
        }
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        vectors.append({feature: value / norm for feature, value in weighted.items()} if norm else {})
    return vectors


def cosine_similarity(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(feature, 0.0) for feature, value in left.items())


def _segment_windows(text: str) -> dict[str, int]:
    """生成可定位的固定长度文本窗口，避免对长文档做 O(n²) 序列匹配。"""
    compact = re.sub(r"\s+", "", normalize_text(text))[:MAX_TEXT_CHARS]
    if len(compact) < REPEATED_SEGMENT_LENGTH:
        return {}
    windows: dict[str, int] = {}
    for start in range(0, len(compact) - REPEATED_SEGMENT_LENGTH + 1, REPEATED_SEGMENT_STRIDE):
        segment = compact[start : start + REPEATED_SEGMENT_LENGTH]
        # 低信息量窗口通常是目录、分隔线或 OCR 噪声，不作为复制证据。
        if len(set(segment)) < 12 or not re.search(r"[\u4e00-\u9fffA-Za-z]", segment):
            continue
        windows.setdefault(segment, start)
    return windows


def build_repeated_segment_index(documents: list[dict]) -> tuple[list[dict[str, int]], dict[str, int]]:
    """返回每份文档的窗口索引，以及在项目中至少出现三次的公共模板窗口。"""
    indexes = [_segment_windows(document.get("text", "")) for document in documents]
    document_frequency: Counter[str] = Counter()
    for index in indexes:
        document_frequency.update(index.keys())
    common = {segment: count for segment, count in document_frequency.items() if count >= 3}
    return indexes, common


def repeated_segment_evidence(
    left_text: str,
    right_text: str,
    *,
    common_segments: set[str] | None = None,
    left_index: dict[str, int] | None = None,
    right_index: dict[str, int] | None = None,
) -> dict:
    """提取两份文档之间的非公共模板重复片段，并返回可解释统计。"""
    left_index = left_index if left_index is not None else _segment_windows(left_text)
    right_index = right_index if right_index is not None else _segment_windows(right_text)
    common_segments = common_segments or set()
    common_prefixes = {segment[:24] for segment in common_segments if len(segment) >= 24}
    shared = [
        segment
        for segment in left_index.keys() & right_index.keys()
        if segment not in common_segments and not any(prefix in segment for prefix in common_prefixes)
    ]
    if not shared:
        return {"count": 0, "chars": 0, "segments": []}

    shared.sort(key=lambda segment: (left_index[segment], -len(segment)))
    selected: list[str] = []
    covered: list[tuple[int, int]] = []
    for segment in shared:
        start = left_index[segment]
        end = start + len(segment)
        if any(start < current_end and end > current_start for current_start, current_end in covered):
            continue
        selected.append(segment)
        covered.append((start, end))
        if len(selected) >= MAX_REPEATED_SEGMENTS:
            break

    # 窗口之间可能重叠，只统计左文档上的并集长度，避免夸大证据规模。
    merged: list[list[int]] = []
    for start, end in sorted(covered):
        if merged and start <= merged[-1][1] + REPEATED_SEGMENT_STRIDE:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    chars = sum(end - start for start, end in merged)
    return {"count": len(selected), "chars": chars, "segments": selected}


def intersection(left: dict, right: dict, key: str) -> list[str]:
    return sorted(set(left.get(key, [])) & set(right.get(key, [])))


def find_project_common_entities(entities: list[dict]) -> dict[str, dict[str, set[str]]]:
    keys = ("phones", "emails", "credit_codes", "contacts", "addresses")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entity in entities:
        grouped[str(entity.get("project_id", ""))].append(entity)
    result: dict[str, dict[str, set[str]]] = {}
    for project_id, rows in grouped.items():
        common: dict[str, set[str]] = {}
        for key in keys:
            counts: Counter[str] = Counter()
            for row in rows:
                counts.update(set(row.get(key, [])))
            common[key] = {value for value, count in counts.items() if count >= 3}
        result[project_id] = common
    return result


def page_index(pages_path: Path) -> dict[str, list[dict]]:
    if not pages_path.exists():
        return {}
    result: dict[str, list[dict]] = defaultdict(list)
    for page in load_jsonl(pages_path):
        result[page.get("document_id", "")].append(page)
    return result


def locate_evidence(pages: dict[str, list[dict]], document_id: str, values: list[str]) -> list[int]:
    if not values:
        return []
    normalized_values = [re.sub(r"\s+", "", value) for value in values]
    found: list[int] = []
    for page in pages.get(document_id, []):
        text = re.sub(r"\s+", "", str(page.get("text", "")))
        if any(value in text for value in normalized_values):
            found.append(int(page.get("page_number", 0)))
        if len(found) >= 5:
            break
    return found


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_unsupervised_analysis(processed_dir: Path, output_dir: Path) -> dict:
    processed_dir = processed_dir.resolve()
    documents_path = processed_dir / "documents.jsonl"
    if not documents_path.exists():
        raise FileNotFoundError(f"未找到统一文档数据: {documents_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    documents = load_jsonl(documents_path)
    entities = [extract_entities(document) for document in documents]
    canonicalize_bidder_names(entities)
    vectors = build_tfidf_vectors(documents)
    repeated_indexes, common_segments = build_repeated_segment_index(documents)
    pages = page_index(processed_dir / "pages.jsonl")
    project_common = find_project_common_entities(entities)
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, document in enumerate(documents):
        grouped[str(document.get("project_id", ""))].append(index)

    pairs: list[dict] = []
    anomalies: list[dict] = []
    for project_id, indexes in grouped.items():
        for offset, left_index in enumerate(indexes):
            for right_index in indexes[offset + 1 :]:
                left = entities[left_index]
                right = entities[right_index]
                similarity = cosine_similarity(vectors[left_index], vectors[right_index])
                repeated = repeated_segment_evidence(
                    documents[left_index].get("text", ""),
                    documents[right_index].get("text", ""),
                    common_segments=set(common_segments),
                    left_index=repeated_indexes[left_index],
                    right_index=repeated_indexes[right_index],
                )
                shared = {
                    key: [
                        value for value in intersection(left, right, key)
                        if value not in project_common.get(project_id, {}).get(key, set())
                    ]
                    for key in ("phones", "emails", "credit_codes", "contacts", "addresses")
                }
                same_bidder = left["bidder"] == right["bidder"]
                score = similarity * 70
                score += 20 if shared["phones"] else 0
                score += 20 if shared["emails"] else 0
                score += 25 if shared["credit_codes"] else 0
                score += 10 if shared["contacts"] else 0
                score += 8 if shared["addresses"] else 0
                pair = {
                    "project_id": project_id,
                    "document_id_a": left["document_id"],
                    "document_id_b": right["document_id"],
                    "bidder_a": left["bidder"],
                    "bidder_b": right["bidder"],
                    "same_bidder": same_bidder,
                    "similarity": round(similarity, 6),
                    "shared_phones": json.dumps(shared["phones"], ensure_ascii=False),
                    "shared_emails": json.dumps(shared["emails"], ensure_ascii=False),
                    "shared_credit_codes": json.dumps(shared["credit_codes"], ensure_ascii=False),
                    "shared_contacts": json.dumps(shared["contacts"], ensure_ascii=False),
                    "shared_addresses": json.dumps(shared["addresses"], ensure_ascii=False),
                    "repeated_segment_count": repeated["count"],
                    "repeated_segment_chars": repeated["chars"],
                    "repeated_segments": json.dumps(repeated["segments"], ensure_ascii=False),
                    "anomaly_score": score,
                }
                pairs.append(pair)
                strong_entity = bool(shared["phones"] or shared["emails"] or shared["credit_codes"])
                corroborated_entities = sum(bool(values) for values in shared.values()) >= 2
                similarity_only = similarity >= SIMILARITY_ONLY_THRESHOLD
                if same_bidder or not (strong_entity or corroborated_entities or similarity_only):
                    continue
                # 重复片段只作为已有线索的佐证，不能单独触发异常，避免把正常合同模板当成风险。
                score += min(4, repeated["chars"] / 1200 * 4)
                score = round(min(score, 100), 2)
                pair["anomaly_score"] = score
                evidence_values = [value for values in shared.values() for value in values]
                evidence = [f"文档字符级 TF-IDF 相似度为 {similarity:.2%}"]
                labels = {
                    "phones": "共同电话",
                    "emails": "共同邮箱",
                    "credit_codes": "共同统一社会信用代码",
                    "contacts": "共同联系人",
                    "addresses": "共同地址",
                }
                for key, values in shared.items():
                    if values:
                        evidence.append(f"{labels[key]}：{'、'.join(values[:5])}")
                if repeated["segments"]:
                    evidence.append(f"非公共模板重复片段：{repeated['count']} 段，覆盖约 {repeated['chars']} 个字符")
                    evidence.extend(f"重复片段：{segment[:96]}" for segment in repeated["segments"][:3])
                anomalies.append({
                    "project_id": project_id,
                    "document_id_a": left["document_id"],
                    "document_id_b": right["document_id"],
                    "bidder_a": left["bidder"],
                    "bidder_b": right["bidder"],
                    "anomaly_score": score,
                    "risk_level": "高" if score >= 70 else "中" if score >= 45 else "低",
                    "similarity": round(similarity, 6),
                    "evidence": json.dumps(evidence, ensure_ascii=False),
                    "evidence_pages_a": json.dumps(locate_evidence(pages, left["document_id"], evidence_values + repeated["segments"])),
                    "evidence_pages_b": json.dumps(locate_evidence(pages, right["document_id"], evidence_values + repeated["segments"])),
                    "repeated_segment_count": repeated["count"],
                    "repeated_segment_chars": repeated["chars"],
                    "repeated_segments": json.dumps(repeated["segments"], ensure_ascii=False),
                    "review_status": "待复核",
                })

    pairs.sort(key=lambda row: row["anomaly_score"], reverse=True)
    anomalies.sort(key=lambda row: row["anomaly_score"], reverse=True)
    write_jsonl(output_dir / "document_entities.jsonl", entities)
    write_csv(output_dir / "pairwise_similarity.csv", pairs, list(pairs[0].keys()) if pairs else [
        "project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "same_bidder",
        "similarity", "shared_phones", "shared_emails", "shared_credit_codes", "shared_contacts",
        "shared_addresses", "anomaly_score",
        "repeated_segment_count", "repeated_segment_chars", "repeated_segments",
    ])
    write_csv(output_dir / "anomaly_results.csv", anomalies, list(anomalies[0].keys()) if anomalies else [
        "project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "anomaly_score",
        "risk_level", "similarity", "evidence", "evidence_pages_a", "evidence_pages_b", "review_status",
        "repeated_segment_count", "repeated_segment_chars", "repeated_segments",
    ])
    summary = {
        "processed_dir": str(processed_dir),
        "output_dir": str(output_dir.resolve()),
        "document_count": len(documents),
        "project_count": len(grouped),
        "pair_count": len(pairs),
        "anomaly_count": len(anomalies),
        "high_risk_count": sum(row["risk_level"] == "高" for row in anomalies),
        "medium_risk_count": sum(row["risk_level"] == "中" for row in anomalies),
        "repeated_segment_pair_count": sum(int(row.get("repeated_segment_count", 0)) > 0 for row in pairs),
        "repeated_segment_evidence_count": sum(int(row.get("repeated_segment_count", 0)) > 0 for row in anomalies),
        "common_template_segment_count": len(common_segments),
        "hash_dimension": HASH_DIMENSION,
        "max_text_chars_per_document": MAX_TEXT_CHARS,
        "entity_scan_chars_per_document": ENTITY_SCAN_CHARS,
        "warning": "结果仅为异常线索，必须结合原始文件和页码进行人工复核。",
    }
    (output_dir / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
