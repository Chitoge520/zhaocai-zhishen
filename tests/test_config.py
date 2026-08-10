from pathlib import Path
import unittest

from zhaocai_zhishen.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_project_root_contains_pyproject(self) -> None:
        settings = load_settings()
        self.assertTrue((settings.project_root / "pyproject.toml").exists())
        self.assertEqual(settings.project_root, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()
