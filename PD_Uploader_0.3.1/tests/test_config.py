from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_room_uploader.config import ConfigError, first_enabled_page_url, load_csv


HEADER = "enabled,page_url,tier,department,shift,metric,image_1,image_2,image_3,image_4,image_5,image_6\n"


class ConfigTests(unittest.TestCase):
    def test_loads_enabled_row_and_resolves_relative_jpg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "one.jpg"
            image.write_bytes(b"fake jpg")
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER
                + "yes,https://example.invalid/TierDisplay/TierDisplay?siteid=33,T1,Dept,Day,Metric,one.jpg,,,,,\n",
                encoding="utf-8",
            )

            result = load_csv(csv_path)

            self.assertEqual(len(result.enabled_rows), 1)
            self.assertEqual(result.enabled_rows[0].image_paths[0], image.resolve())
            self.assertEqual(result.warnings, [])

    def test_allows_uppercase_jpg_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "one.JPG"
            image.write_bytes(b"fake jpg")
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER
                + "yes,https://example.invalid/?siteid=33,T1,Dept,Day,Metric,one.JPG,,,,,\n",
                encoding="utf-8",
            )

            result = load_csv(csv_path)

            self.assertEqual(result.enabled_rows[0].image_paths[0], image.resolve())
            self.assertEqual(result.warnings, [])

    def test_rejects_non_jpg_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "one.jpeg"
            image.write_bytes(b"fake jpg")
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER
                + "yes,https://example.invalid/TierDisplay/TierDisplay?siteid=33,T1,Dept,Day,Metric,one.jpeg,,,,,\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_csv(csv_path)

    def test_disabled_rows_do_not_validate_image_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER
                + "no,https://example.invalid/?siteid=33,T1,Dept,Day,Metric,missing.png,,,,,\n",
                encoding="utf-8",
            )

            result = load_csv(csv_path)

            self.assertEqual(len(result.enabled_rows), 0)

    def test_allows_start_page_url_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "one.jpg"
            image.write_bytes(b"fake jpg")
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER + "yes,https://example.invalid/start,T1,Dept,Day,Metric,one.jpg,,,,,\n",
                encoding="utf-8",
            )

            result = load_csv(csv_path)

            self.assertEqual(result.warnings, [])

    def test_first_enabled_page_url_does_not_validate_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "config.csv"
            csv_path.write_text(
                HEADER
                + "yes,https://example.invalid/?siteid=33,T1,Dept,Day,Metric,missing.jpg,,,,,\n",
                encoding="utf-8",
            )

            page_url, warnings = first_enabled_page_url(csv_path)

            self.assertEqual(page_url, "https://example.invalid/?siteid=33")
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
