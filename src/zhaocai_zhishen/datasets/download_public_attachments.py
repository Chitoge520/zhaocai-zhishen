from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ALLOWED_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".zip", ".rar"}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def safe_name(value: str, max_length: int = 90) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value or "")
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:max_length] or "attachment"


def suffix_from_url_or_name(url: str, name: str) -> str:
    for candidate in (Path(urlparse(url).path).suffix, Path(name).suffix):
        suffix = candidate.lower()
        if suffix in ALLOWED_SUFFIXES:
            return suffix
    return ""


def is_tender_like(name: str, url: str) -> bool:
    text = f"{name} {url}".lower()
    if any(word in name for word in ("招标文件", "采购文件", "资格声明", "投标格式", "采购需求")):
        return True
    return any(ext in text for ext in (".doc", ".docx", ".zip", ".rar"))


def download(url: str, output_path: Path, timeout: int = 60) -> int:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 zhaocai-zhishen research collector",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return len(data)


def run(input_path: Path, output_dir: Path, delay: float, max_files: int | None, tender_only: bool) -> list[dict]:
    notices = read_jsonl(input_path)
    manifest: list[dict] = []
    downloaded = 0
    for notice_index, notice in enumerate(notices, 1):
        notice_title = notice.get("title") or f"notice_{notice_index:04d}"
        notice_dir = output_dir / f"{notice_index:04d}_{safe_name(notice_title, 60)}"
        attachments = notice.get("attachments") or []
        for attachment_index, attachment in enumerate(attachments, 1):
            name = attachment.get("name") or f"attachment_{attachment_index}"
            url = attachment.get("url") or ""
            if not url:
                continue
            if tender_only and not is_tender_like(name, url):
                continue
            suffix = suffix_from_url_or_name(url, name)
            if not suffix:
                continue
            filename = f"{attachment_index:02d}_{safe_name(name)}"
            if not filename.lower().endswith(suffix):
                filename += suffix
            output_path = notice_dir / filename
            status = "downloaded"
            error = ""
            size = 0
            try:
                size = download(url, output_path)
                downloaded += 1
            except Exception as exc:
                status = "failed"
                error = str(exc)
            manifest.append(
                {
                    "notice_title": notice_title,
                    "notice_url": notice.get("url", ""),
                    "attachment_name": name,
                    "attachment_url": url,
                    "local_path": str(output_path.relative_to(output_dir)) if status == "downloaded" else "",
                    "size_bytes": size,
                    "status": status,
                    "error": error,
                }
            )
            if max_files and downloaded >= max_files:
                return manifest
            time.sleep(delay)
    return manifest


def write_manifest(output_dir: Path, manifest: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "attachment_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["notice_title,attachment_name,status,size_bytes,local_path,attachment_url,error"]
    for item in manifest:
        values = [
            item["notice_title"],
            item["attachment_name"],
            item["status"],
            str(item["size_bytes"]),
            item["local_path"],
            item["attachment_url"],
            item["error"],
        ]
        escaped = ['"' + value.replace('"', '""') + '"' for value in values]
        lines.append(",".join(escaped))
    (output_dir / "attachment_manifest.csv").write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download public tender-related attachments from collected notices.")
    parser.add_argument("--input", required=True, help="Path to notices.jsonl.")
    parser.add_argument("--output", required=True, help="Output directory.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between downloads.")
    parser.add_argument("--max-files", type=int, default=10, help="Maximum successfully downloaded files.")
    parser.add_argument("--all", action="store_true", help="Download all supported attachment types, not only tender-like files.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    manifest = run(
        input_path=Path(args.input),
        output_dir=output_dir,
        delay=args.delay,
        max_files=args.max_files,
        tender_only=not args.all,
    )
    write_manifest(output_dir, manifest)
    ok = sum(1 for item in manifest if item["status"] == "downloaded")
    failed = sum(1 for item in manifest if item["status"] == "failed")
    print(f"Downloaded {ok} files, failed {failed}, manifest entries {len(manifest)} into {output_dir}")


if __name__ == "__main__":
    main()

