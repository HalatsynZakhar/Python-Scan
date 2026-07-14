import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from image_sync_server import load_server_settings, parse_excel_articles


class ServerSettingsTests(unittest.TestCase):
    def test_server_settings_do_not_require_local_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            settings = load_server_settings(
                {
                    "server": {
                        "enabled": True,
                        "host": "0.0.0.0",
                        "port": 8092,
                    }
                },
                {"output_dir": output_dir},
            )

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["port"], 8092)
        self.assertEqual(settings["state_file"], output_dir / "horoshop_sync_state.json")
        self.assertNotIn("access_password", settings)


class ExcelArticleTests(unittest.TestCase):
    def test_reads_first_column_unique_articles(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["Артикул", "Назва"])
        worksheet.append(["X33", "Toy"])
        worksheet.append(["X33", "Duplicate"])
        worksheet.append([" X34 ", "Toy 2"])
        worksheet.append([None, "Ignored"])
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()

        self.assertEqual(parse_excel_articles(buffer.getvalue()), ["X33", "X34"])


if __name__ == "__main__":
    unittest.main()

