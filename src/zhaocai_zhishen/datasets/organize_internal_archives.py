from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path


DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
}

BID_KEYWORDS = ("投标", "响应", "应答", "报价", "商务标", "技术标", "投标书", "技术部分", "商务部分")
EVALUATION_KEYWORDS = ("评标", "评审", "回标分析", "开标", "中标", "汇标")
TENDER_KEYWORDS = ("招标文件", "采购文件", "比价文件", "技术规格书", "工程量清单", "清单", "最高投标限价", "限价")
REFERENCE_KEYWORDS = ("澄清", "立项", "申请报告", "标准文件")
GENERIC_BID_FILE_PATTERN = re.compile(
    r"^(附件|附表|格式|承诺书|声明函|授权委托书|法定代表人|资格证明|报价函|投标函|技术方案|商务响应|技术响应)"
)

# Uploads are untrusted input. These limits bound both metadata expansion and
# decompression work before OCR or model inference starts.
MAX_ARCHIVE_ENTRIES = 5000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_NESTED_ARCHIVE_DEPTH = 2


@dataclass(frozen=True)
class ArchiveEntry:
    archive_id: str
    archive_name: str
    project_name: str
    inner_path: str
    file_name: str
    extension: str
    size_bytes: int
    role: str
    bidder_candidate: str
    extracted_path: str


@dataclass
class ExtractionBudget:
    file_count: int = 0
    total_bytes: int = 0

    def consume(self, info: zipfile.ZipInfo) -> None:
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"压缩包内单文件超过 {MAX_MEMBER_BYTES // (1024 * 1024)} MB 限制：{info.filename}")
        self.file_count += 1
        self.total_bytes += info.file_size
        if self.file_count > MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"压缩包文件数量超过 {MAX_ARCHIVE_ENTRIES} 个限制")
        if self.total_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("压缩包预计解压总大小超过 2 GB 限制")


def row_to_dict(row: ArchiveEntry) -> dict:
    return {
        "archive_id": row.archive_id,
        "archive_name": row.archive_name,
        "project_name": row.project_name,
        "inner_path": row.inner_path,
        "file_name": row.file_name,
        "extension": row.extension,
        "size_bytes": row.size_bytes,
        "role": row.role,
        "bidder_candidate": row.bidder_candidate,
        "extracted_path": row.extracted_path,
    }


def safe_name(value: str, max_length: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return (text or "unnamed")[:max_length].strip()


def project_name_from_archive(path: Path) -> str:
    name = path.stem
    match = re.match(r"C04-项目比价审批-[^-]+-(\d{4}-\d{2}-\d{2})_(\d+)", name)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    return safe_name(name)


def archive_id(path: Path) -> str:
    digest = hashlib.sha1(path.name.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{project_name_from_archive(path)}_{digest}"


def detect_role(path: str, extension: str) -> str:
    if extension.lower() == ".zip":
        return "nested_archive"
    if any(word in path for word in EVALUATION_KEYWORDS):
        return "evaluation_material"
    if any(word in path for word in TENDER_KEYWORDS):
        return "tender_material"
    if any(word in path for word in REFERENCE_KEYWORDS):
        return "reference_material"
    if any(word in path for word in BID_KEYWORDS):
        return "bid_document"
    if extension.lower() in DOCUMENT_EXTENSIONS:
        return "reference_material"
    return "other"


def guess_bidder(path: str, role: str) -> str:
    if role != "bid_document":
        return ""
    name = Path(path).stem
    for keyword in BID_KEYWORDS:
        name = name.replace(keyword, "")
    name = re.sub(r"[-_：:（）()\[\]【】]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80]


def promote_generic_bid_entries(rows: list[ArchiveEntry]) -> list[ArchiveEntry]:
    """Use project context to recover generic names such as 附件1.pdf.

    A generic attachment is promoted only when the same archive already has a
    clearly named bid document and the attachment path is not under tender,
    evaluation, or reference material directories.
    """
    if not any(row.role == "bid_document" for row in rows):
        return rows
    promoted: list[ArchiveEntry] = []
    for row in rows:
        normalized = row.inner_path.replace("\\", "/")
        stem = Path(row.file_name).stem.strip()
        blocked = any(keyword in normalized for keyword in (*EVALUATION_KEYWORDS, *TENDER_KEYWORDS, *REFERENCE_KEYWORDS))
        if row.role == "reference_material" and not blocked and GENERIC_BID_FILE_PATTERN.match(stem):
            promoted.append(ArchiveEntry(**{**row.__dict__, "role": "bid_document", "bidder_candidate": ""}))
        else:
            promoted.append(row)
    return promoted


def display_role(role: str) -> str:
    return {
        "bid_document": "投标文件",
        "evaluation_material": "评标评审材料",
        "tender_material": "招标采购材料",
        "reference_material": "参考材料",
        "nested_archive": "嵌套压缩包",
        "other": "其他材料",
    }.get(role, role)


def iter_zip_entries(archive_path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(archive_path) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        budget = ExtractionBudget()
        for entry in entries:
            budget.consume(entry)
        return entries


def unique_target(project_dir: Path, inner_path: str) -> Path:
    target = project_dir / safe_name(inner_path.replace("\\", "/").replace("::", "__"))
    counter = 1
    while target.exists():
        target = project_dir / f"{target.stem}_{counter}{target.suffix}"
        counter += 1
    return target


def extract_zip_entries(
    archive: zipfile.ZipFile,
    project_dir: Path,
    output_root: Path,
    project_id: str,
    archive_name: str,
    project_name: str,
    prefix: str = "",
    depth: int = 0,
    budget: ExtractionBudget | None = None,
) -> list[ArchiveEntry]:
    budget = budget or ExtractionBudget()
    rows: list[ArchiveEntry] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        budget.consume(info)
        inner_path = f"{prefix}{info.filename}"
        file_name = Path(info.filename).name
        extension = Path(file_name).suffix.lower()
        role = detect_role(inner_path, extension)
        target = unique_target(project_dir, inner_path)
        with archive.open(info) as source, target.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        rows.append(
            ArchiveEntry(
                archive_id=project_id,
                archive_name=archive_name,
                project_name=project_name,
                inner_path=inner_path,
                file_name=file_name,
                extension=extension,
                size_bytes=info.file_size,
                role=role,
                bidder_candidate=guess_bidder(inner_path, role),
                extracted_path=str(target.relative_to(output_root)),
            )
        )
        if extension == ".zip" and depth < MAX_NESTED_ARCHIVE_DEPTH:
            try:
                with archive.open(info) as nested_source:
                    nested_data = nested_source.read()
                with zipfile.ZipFile(io.BytesIO(nested_data)) as nested_archive:
                    rows.extend(
                        extract_zip_entries(
                            nested_archive,
                            project_dir,
                            output_root,
                            project_id,
                            archive_name,
                            project_name,
                            prefix=f"{inner_path}::",
                            depth=depth + 1,
                            budget=budget,
                        )
                    )
            except zipfile.BadZipFile:
                continue
    return rows


def extract_archive(archive_path: Path, output_root: Path, project_id: str) -> list[ArchiveEntry]:
    project_dir = output_root / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        return extract_zip_entries(
            archive,
            project_dir,
            output_root,
            project_id,
            archive_path.name,
            project_name_from_archive(archive_path),
            budget=ExtractionBudget(),
        )


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def copy_unique(source: Path, target_dir: Path, preferred_name: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name(preferred_name, max_length=120)
    counter = 1
    while target.exists():
        target = target_dir / f"{target.stem}_{counter}{target.suffix}"
        counter += 1
    shutil.copy2(source, target)
    return target


def build_standard_dataset(output_dir: Path, file_rows: list[ArchiveEntry]) -> list[dict]:
    dataset_root = output_dir / "standard_dataset"
    extracted_root = output_dir / "projects"
    if dataset_root.exists():
        shutil.rmtree(dataset_root)
    dataset_root.mkdir(parents=True, exist_ok=True)

    sample_rows: list[dict] = []
    file_manifest: list[dict] = []
    counters: dict[str, int] = {}
    for row in file_rows:
        if not row.extracted_path:
            continue
        source = extracted_root / row.extracted_path
        if not source.exists():
            continue

        project_dir = dataset_root / safe_name(row.project_name, max_length=100)
        if row.role == "bid_document":
            counters[row.archive_id] = counters.get(row.archive_id, 0) + 1
            bidder = safe_name(row.bidder_candidate or Path(row.file_name).stem, max_length=60)
            preferred = f"{counters[row.archive_id]:03d}_{bidder}{row.extension}"
            target = copy_unique(source, project_dir / "投标文件", preferred)
            sample = {
                "sample_id": f"{row.archive_id}__{counters[row.archive_id]:03d}",
                "archive_id": row.archive_id,
                "project_name": row.project_name,
                "bidder_candidate": row.bidder_candidate,
                "bid_file": str(target.relative_to(dataset_root)),
                "source_inner_path": row.inner_path,
                "label": "",
                "label_name": "",
                "label_basis": "",
                "split": "",
            }
            sample_rows.append(sample)
        else:
            role_dir = display_role(row.role)
            preferred = f"{role_dir}_{row.file_name}"
            target = copy_unique(source, project_dir / "其他文件" / role_dir, preferred)

        file_manifest.append(
            {
                "archive_id": row.archive_id,
                "project_name": row.project_name,
                "role": row.role,
                "role_name": display_role(row.role),
                "standard_path": str(target.relative_to(dataset_root)),
                "source_inner_path": row.inner_path,
                "source_extracted_path": row.extracted_path,
                "file_name": row.file_name,
                "extension": row.extension,
                "size_bytes": row.size_bytes,
            }
        )

    write_csv(
        dataset_root / "samples.csv",
        sample_rows,
        ["sample_id", "archive_id", "project_name", "bidder_candidate", "bid_file", "source_inner_path", "label", "label_name", "label_basis", "split"],
    )
    write_csv(
        dataset_root / "file_manifest.csv",
        file_manifest,
        ["archive_id", "project_name", "role", "role_name", "standard_path", "source_inner_path", "source_extracted_path", "file_name", "extension", "size_bytes"],
    )
    return sample_rows


def organize(raw_dir: Path, output_dir: Path, extract: bool) -> dict:
    archives = sorted(raw_dir.glob("*.zip"))
    file_rows: list[ArchiveEntry] = []
    archive_rows = []
    extracted_root = output_dir / "projects"
    for archive_path in archives:
        project_id = archive_id(archive_path)
        entries = iter_zip_entries(archive_path)
        archive_rows.append(
            {
                "archive_id": project_id,
                "archive_name": archive_path.name,
                "project_name": project_name_from_archive(archive_path),
                "archive_size_bytes": archive_path.stat().st_size,
                "entry_count": len(entries),
            }
        )
        if extract:
            file_rows.extend(promote_generic_bid_entries(extract_archive(archive_path, extracted_root, project_id)))
        else:
            indexed_rows = []
            for info in entries:
                extension = Path(info.filename).suffix.lower()
                role = detect_role(info.filename, extension)
                indexed_rows.append(
                    ArchiveEntry(
                        archive_id=project_id,
                        archive_name=archive_path.name,
                        project_name=project_name_from_archive(archive_path),
                        inner_path=info.filename,
                        file_name=Path(info.filename).name,
                        extension=extension,
                        size_bytes=info.file_size,
                        role=role,
                        bidder_candidate=guess_bidder(info.filename, role),
                        extracted_path="",
                    )
                )
            file_rows.extend(promote_generic_bid_entries(indexed_rows))

    standard_samples = build_standard_dataset(output_dir, file_rows) if extract else []
    file_dicts = [row_to_dict(row) for row in file_rows]
    samples = [
        {
            "sample_id": f"{row.archive_id}__{index:04d}",
            "archive_id": row.archive_id,
            "project_name": row.project_name,
            "bidder_candidate": row.bidder_candidate,
            "source_file": row.extracted_path or row.inner_path,
            "label": "",
            "label_name": "",
            "label_basis": "",
            "split": "",
        }
        for index, row in enumerate(file_rows, 1)
        if row.role == "bid_document"
    ]

    summary = {
        "archive_count": len(archive_rows),
        "file_count": len(file_rows),
        "sample_count": len(samples),
        "by_role": {},
        "by_extension": {},
        "extracted": extract,
        "standard_dataset_sample_count": len(standard_samples),
    }
    for row in file_rows:
        summary["by_role"][row.role] = summary["by_role"].get(row.role, 0) + 1
        summary["by_extension"][row.extension or "(none)"] = summary["by_extension"].get(row.extension or "(none)", 0) + 1

    write_csv(output_dir / "archives.csv", archive_rows, list(archive_rows[0].keys()) if archive_rows else [])
    write_csv(output_dir / "files.csv", file_dicts, list(file_dicts[0].keys()) if file_dicts else [])
    write_csv(
        output_dir / "samples_to_label.csv",
        samples,
        ["sample_id", "archive_id", "project_name", "bidder_candidate", "source_file", "label", "label_name", "label_basis", "split"],
    )
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Organize internal bid archives into a local training dataset workspace.")
    parser.add_argument("--raw-dir", default="data/raw_internal", help="Directory containing internal zip archives.")
    parser.add_argument("--output-dir", default="data/training_internal", help="Output directory for manifests and extracted files.")
    parser.add_argument("--no-extract", action="store_true", help="Only index zip contents, do not extract files.")
    args = parser.parse_args()
    summary = organize(Path(args.raw_dir), Path(args.output_dir), extract=not args.no_extract)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
