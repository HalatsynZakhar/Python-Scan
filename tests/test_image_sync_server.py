import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from image_sync_server import (
    SyncState,
    load_or_refresh_catalog,
    load_server_settings,
    parse_excel_articles,
)


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


class CatalogCacheTests(unittest.TestCase):
    def test_state_persists_catalog_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            state = SyncState(state_file)
            state.set_catalog_products(
                [{"article": "REAL", "article_for_display": "DISPLAY"}]
            )
            state.save()

            loaded = SyncState(state_file)

        self.assertEqual(loaded.catalog_meta()["products_count"], 1)
        self.assertTrue(loaded.catalog_meta()["has_cache"])
        self.assertEqual(loaded.catalog_products[0]["article"], "REAL")

    def test_load_or_refresh_catalog_uses_cache_when_allowed(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def export_catalog(self, progress=None):
                self.calls += 1
                return [{"article": "REMOTE"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncState(Path(temp_dir) / "state.json")
            state.set_catalog_products([{"article": "CACHED"}])

            import image_sync_server

            original_state = image_sync_server.STATE
            image_sync_server.STATE = state
            try:
                client = FakeClient()
                catalog, meta = load_or_refresh_catalog(
                    client=client,
                    force_refresh=False,
                )
            finally:
                image_sync_server.STATE = original_state

        self.assertEqual(client.calls, 0)
        self.assertEqual(meta["source"], "cache")
        self.assertIn("CACHED", catalog.by_article)

    def test_load_or_refresh_catalog_refreshes_and_saves(self):
        class FakeClient:
            def export_catalog(self, progress=None):
                return [{"article": "REMOTE"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncState(Path(temp_dir) / "state.json")

            import image_sync_server

            original_state = image_sync_server.STATE
            image_sync_server.STATE = state
            try:
                catalog, meta = load_or_refresh_catalog(
                    client=FakeClient(),
                    force_refresh=True,
                )
            finally:
                image_sync_server.STATE = original_state

        self.assertEqual(meta["source"], "fresh")
        self.assertIn("REMOTE", catalog.by_article)
        self.assertEqual(state.catalog_meta()["products_count"], 1)


if __name__ == "__main__":
    unittest.main()
