from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from zhaocai_zhishen.datasets.organize_internal_archives import ArchiveEntry, iter_zip_entries, promote_generic_bid_entries
from zhaocai_zhishen.job_manager import cancel_job
from zhaocai_zhishen.llm_analysis import validate_llm_base_url


class SecurityBoundaryTests(unittest.TestCase):
    def test_zip_member_size_is_bounded_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "sample.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("附件1.pdf", b"0123456789")
            with patch("zhaocai_zhishen.datasets.organize_internal_archives.MAX_MEMBER_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "单文件"):
                    iter_zip_entries(archive_path)

    def test_llm_url_rejects_untrusted_hosts_and_plain_http(self) -> None:
        self.assertEqual(validate_llm_base_url("https://api.deepseek.com"), "https://api.deepseek.com")
        with self.assertRaisesRegex(ValueError, "白名单"):
            validate_llm_base_url("https://127.0.0.1")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_llm_base_url("http://api.deepseek.com")

    def test_local_llm_http_requires_explicit_opt_in(self) -> None:
        with patch.dict(
            os.environ,
            {"DEEPSEEK_ALLOWED_HOSTS": "localhost", "BID_AUDIT_ALLOW_LOCAL_LLM_HTTP": "1"},
            clear=False,
        ):
            self.assertEqual(validate_llm_base_url("http://localhost:8000"), "http://localhost:8000")

    def test_generic_attachment_is_promoted_only_in_bid_context(self) -> None:
        rows = [
            ArchiveEntry("a", "a.zip", "p", "上海公司/投标文件.pdf", "投标文件.pdf", ".pdf", 1, "bid_document", "上海公司", "p/1.pdf"),
            ArchiveEntry("a", "a.zip", "p", "上海公司/附件1.pdf", "附件1.pdf", ".pdf", 1, "reference_material", "", "p/2.pdf"),
            ArchiveEntry("a", "a.zip", "p", "招标文件/附件2.pdf", "附件2.pdf", ".pdf", 1, "reference_material", "", "p/3.pdf"),
        ]
        promoted = promote_generic_bid_entries(rows)
        self.assertEqual(promoted[1].role, "bid_document")
        self.assertEqual(promoted[2].role, "reference_material")

    def test_running_job_can_be_cancelled_without_rewriting_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs_root = Path(temp)
            job_dir = jobs_root / "0123456789abcdef"
            job_dir.mkdir()
            (job_dir / "status.json").write_text(
                '{"job_id":"0123456789abcdef","state":"running","phase":"GPU OCR"}',
                encoding="utf-8",
            )
            status = cancel_job(jobs_root, job_dir.name)
            self.assertTrue(status["cancel_requested"])
            self.assertEqual(status["state"], "running")


if __name__ == "__main__":
    unittest.main()
