from __future__ import annotations

import argparse
import csv
import json
import zipfile
from pathlib import Path


SUPPORTED_EXTENSIONS = {".doc", ".docx", ".pdf", ".xls", ".xlsx", ".zip", ".rar"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".xls", ".xlsx"}


def read_manifest(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_extract_zip(zip_path: Path, output_dir: Path) -> list[Path]:
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            member_name = member.filename.replace("\\", "/")
            target = (output_dir / member_name).resolve()
            if not str(target).startswith(str(output_dir.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("wb") as dst:
                dst.write(src.read())
            extracted.append(target)
    return extracted


def index_file(path: Path, root: Path, source_notice: dict, extracted_from: str = "") -> dict:
    suffix = path.suffix.lower()
    return {
        "file_path": str(path.relative_to(root)),
        "file_name": path.name,
        "extension": suffix,
        "size_bytes": path.stat().st_size,
        "is_document": suffix in DOCUMENT_EXTENSIONS,
        "is_archive": suffix in {".zip", ".rar"},
        "extracted_from": extracted_from,
        "notice_title": source_notice.get("notice_title", ""),
        "notice_url": source_notice.get("notice_url", ""),
        "attachment_name": source_notice.get("attachment_name", ""),
        "attachment_url": source_notice.get("attachment_url", ""),
    }


def build_source_lookup(manifest: list[dict], root: Path) -> dict[str, dict]:
    lookup = {}
    for item in manifest:
        local_path = item.get("local_path") or ""
        if local_path:
            lookup[str((root / local_path).resolve())] = item
    return lookup


def index_attachments(input_dir: Path, manifest_path: Path, extract_dir: Path) -> list[dict]:
    input_dir = input_dir.resolve()
    manifest_path = manifest_path.resolve()
    extract_dir = extract_dir.resolve()
    manifest = read_manifest(manifest_path)
    source_lookup = build_source_lookup(manifest, input_dir)
    rows: list[dict] = []

    downloaded_files = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.name not in {"attachment_manifest.csv", "attachment_manifest.json"}
    ]

    for path in downloaded_files:
        source = source_lookup.get(str(path.resolve()), {})
        rows.append(index_file(path, input_dir, source))
        if path.suffix.lower() == ".zip":
            target_dir = extract_dir / path.relative_to(input_dir).with_suffix("")
            try:
                extracted = safe_extract_zip(path, target_dir)
            except zipfile.BadZipFile:
                continue
            for extracted_file in extracted:
                if extracted_file.suffix.lower() in SUPPORTED_EXTENSIONS:
                    rows.append(index_file(extracted_file, extract_dir, source, extracted_from=str(path.relative_to(input_dir))))
    return rows


def write_outputs(output_dir: Path, rows: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "file_index.json"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = output_dir / "file_index.csv"
    fieldnames = [
        "file_path",
        "file_name",
        "extension",
        "size_bytes",
        "is_document",
        "is_archive",
        "extracted_from",
        "notice_title",
        "notice_url",
        "attachment_name",
        "attachment_url",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "file_count": len(rows),
        "document_count": sum(1 for row in rows if row["is_document"]),
        "archive_count": sum(1 for row in rows if row["is_archive"]),
        "extensions": {},
    }
    for row in rows:
        summary["extensions"][row["extension"]] = summary["extensions"].get(row["extension"], 0) + 1
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract and index downloaded public procurement attachments.")
    parser.add_argument("--input-dir", required=True, help="Directory containing downloaded attachments.")
    parser.add_argument("--manifest", required=True, help="Attachment manifest JSON or CSV.")
    parser.add_argument("--output", required=True, help="Output directory for file index.")
    parser.add_argument("--extract-dir", default="", help="Directory for extracted archives. Defaults to output/extracted.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output).resolve()
    extract_dir = Path(args.extract_dir).resolve() if args.extract_dir else output_dir / "extracted"
    rows = index_attachments(input_dir=input_dir, manifest_path=Path(args.manifest), extract_dir=extract_dir)
    write_outputs(output_dir, rows)
    print(f"Indexed {len(rows)} files into {output_dir}")


if __name__ == "__main__":
    main()
