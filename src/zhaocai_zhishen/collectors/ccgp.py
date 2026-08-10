from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


CCGP_LOCAL_OPEN_TENDER = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/"
CCGP_CENTRAL_OPEN_TENDER = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/"


@dataclass
class NoticeItem:
    title: str
    url: str
    published_at: str
    region: str
    purchaser: str
    source_list: str


@dataclass
class NoticeDetail:
    title: str
    url: str
    published_at: str
    region: str
    purchaser: str
    source_list: str
    html_path: str
    text: str
    attachments: list[dict[str, str]]


def fetch_text(url: str, timeout: int = 20) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 zhaocai-zhishen research collector",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def list_url(base_url: str, page_index: int) -> str:
    if page_index == 0:
        return urljoin(base_url, "index.htm")
    return urljoin(base_url, f"index_{page_index}.htm")


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_list_page(html: str, base_url: str, source_list: str) -> list[NoticeItem]:
    items: list[NoticeItem] = []
    list_match = re.search(r'(?is)<ul[^>]+class="[^"]*c_list_bid[^"]*"[^>]*>(?P<body>.*?)</ul>', html)
    if list_match:
        html = list_match.group("body")
    for li in re.findall(r"(?is)<li[^>]*>(.*?)</li>", html):
        link = re.search(r'(?is)<a(?P<attrs>[^>]+)>(?P<title>.*?)</a>', li)
        if not link:
            continue
        attrs = link.group("attrs")
        href_match = re.search(r'href="([^"]+)"', attrs)
        if not href_match:
            continue
        title_attr = re.search(r'title="([^"]+)"', attrs)
        title = strip_tags(title_attr.group(1) if title_attr else link.group("title"))
        href = unescape(href_match.group(1))
        if not title or "公告" not in title:
            continue
        text = strip_tags(li)
        date = ""
        region = ""
        purchaser = ""
        date_match = re.search(r"发布时间：\s*([0-9\-: ]+)", text)
        region_match = re.search(r"地域：\s*(.*?)\s+采购人：", text)
        purchaser_match = re.search(r"采购人：\s*(.*)$", text)
        if date_match:
            date = date_match.group(1).strip()
        if region_match:
            region = region_match.group(1).strip()
        if purchaser_match:
            purchaser = purchaser_match.group(1).strip()
        items.append(
            NoticeItem(
                title=title,
                url=urljoin(base_url, href),
                published_at=date,
                region=region,
                purchaser=purchaser,
                source_list=source_list,
            )
        )
    return items


def find_attachments(html: str, detail_url: str) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S):
        label_text = strip_tags(label)
        href = unescape(href)
        lower = href.lower()
        if any(ext in lower for ext in (".doc", ".docx", ".xls", ".xlsx", ".pdf", ".zip", ".rar")):
            attachments.append({"name": label_text or Path(urlparse(href).path).name, "url": urljoin(detail_url, href)})
    return attachments


def safe_name(value: str, max_length: int = 80) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:max_length] or "notice"


def collect(
    output_dir: Path,
    pages: int,
    delay: float,
    sources: list[tuple[str, str]],
    max_notices: int | None = None,
) -> list[NoticeDetail]:
    raw_dir = output_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    details: list[NoticeDetail] = []
    seen_urls: set[str] = set()

    for source_name, base_url in sources:
        for page_index in range(pages):
            url = list_url(base_url, page_index)
            html = fetch_text(url)
            items = parse_list_page(html, base_url, source_name)
            for item in items:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                detail_html = fetch_text(item.url)
                html_name = f"{len(details)+1:04d}_{safe_name(item.title)}.html"
                html_path = raw_dir / html_name
                html_path.write_text(detail_html, encoding="utf-8")
                details.append(
                    NoticeDetail(
                        **asdict(item),
                        html_path=str(html_path.relative_to(output_dir)),
                        text=strip_tags(detail_html),
                        attachments=find_attachments(detail_html, item.url),
                    )
                )
                if max_notices and len(details) >= max_notices:
                    return details
                time.sleep(delay)
            time.sleep(delay)
    return details


def write_outputs(output_dir: Path, details: list[NoticeDetail]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "notices.jsonl"
    with json_path.open("w", encoding="utf-8") as f:
        for detail in details:
            f.write(json.dumps(asdict(detail), ensure_ascii=False) + "\n")

    csv_path = output_dir / "notices.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "title",
                "url",
                "published_at",
                "region",
                "purchaser",
                "source_list",
                "html_path",
                "attachment_count",
            ],
        )
        writer.writeheader()
        for detail in details:
            writer.writerow(
                {
                    "title": detail.title,
                    "url": detail.url,
                    "published_at": detail.published_at,
                    "region": detail.region,
                    "purchaser": detail.purchaser,
                    "source_list": detail.source_list,
                    "html_path": detail.html_path,
                    "attachment_count": len(detail.attachments),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect public notices from China Government Procurement Network.")
    parser.add_argument("--output", default="data/public_training/ccgp", help="Output directory.")
    parser.add_argument("--pages", type=int, default=1, help="List pages per source.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between HTTP requests.")
    parser.add_argument("--max-notices", type=int, default=30, help="Maximum notices to collect.")
    parser.add_argument("--central", action="store_true", help="Include central government notices.")
    parser.add_argument("--local", action="store_true", help="Include local government notices.")
    args = parser.parse_args()

    sources = []
    if args.local or not args.central:
        sources.append(("ccgp_local_open_tender", CCGP_LOCAL_OPEN_TENDER))
    if args.central:
        sources.append(("ccgp_central_open_tender", CCGP_CENTRAL_OPEN_TENDER))

    output_dir = Path(args.output)
    details = collect(output_dir=output_dir, pages=args.pages, delay=args.delay, sources=sources, max_notices=args.max_notices)
    write_outputs(output_dir, details)
    print(f"Collected {len(details)} notices into {output_dir}")


if __name__ == "__main__":
    main()
