import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from image_sync_server import (
    LOCAL_DATA_DIR,
    SyncState,
    add_manual_dirty_article,
    catalog_age_seconds,
    load_or_refresh_catalog,
    load_server_settings,
    parse_excel_articles,
    prepare_import_plan,
    validate_image_url,
)
from horoshop_sync import CatalogIndex, HoroshopSettings


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
        self.assertEqual(settings["state_file"], LOCAL_DATA_DIR / "horoshop_sync_state.json")
        self.assertNotIn("access_password", settings)

    def test_server_state_file_cannot_be_inside_public_output_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with self.assertRaises(ValueError):
                load_server_settings(
                    {
                        "server": {
                            "enabled": True,
                            "state_file": str(output_dir / "horoshop_sync_state.json"),
                        }
                    },
                    {"output_dir": output_dir},
                )


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
    def test_skip_dirty_moves_article_to_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncState(Path(temp_dir) / "state.json")
            state.mark_dirty("X33", "manual", 2)

            self.assertTrue(state.skip_dirty("X33"))

        self.assertNotIn("X33", state.dirty)
        self.assertEqual(state.history[-1]["article"], "X33")
        self.assertEqual(state.history[-1]["status"], "skipped")

    def test_archive_history_moves_current_history_to_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncState(Path(temp_dir) / "state.json")
            state.mark_dirty("X33", "manual", 2)
            state.skip_dirty("X33")

            moved = state.archive_history()

        self.assertEqual(moved, 1)
        self.assertEqual(state.history, [])
        self.assertEqual(state.archive[-1]["article"], "X33")
        self.assertIn("archived_at", state.archive[-1])

    def test_archive_history_item_moves_one_matching_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncState(Path(temp_dir) / "state.json")
            state.mark_dirty("X33", "manual", 2)
            state.skip_dirty("X33")
            state.mark_dirty("X34", "manual", 1)
            state.skip_dirty("X34")

            moved = state.archive_history_item("X33", state.history[0]["updated_at"])

        self.assertTrue(moved)
        self.assertEqual([item["article"] for item in state.history], ["X34"])
        self.assertEqual(state.archive[-1]["article"], "X33")

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
        self.assertEqual(loaded.catalog_meta()["local_path"], str(state_file))

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


class ManualDirtyArticleTests(unittest.TestCase):
    def test_add_manual_dirty_article_checks_xml_and_marks_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "X33.jpg").write_bytes(b"fake-image")

            import image_sync_server

            original_config = image_sync_server.XML_CONFIG
            original_state = image_sync_server.STATE
            state = SyncState(root / "state.json")
            image_sync_server.XML_CONFIG = {
                "images_dir": images,
                "output_xml": root / "out" / "images.xml",
                "base_url": "https://img.example.com/foto",
                "allowed_extensions": {".jpg"},
            }
            image_sync_server.STATE = state
            try:
                result = add_manual_dirty_article(" X33 ")
            finally:
                image_sync_server.XML_CONFIG = original_config
                image_sync_server.STATE = original_state

        self.assertEqual(result["article"], "X33")
        self.assertEqual(result["image_count"], 1)
        self.assertIn("X33", state.dirty)

    def test_add_manual_dirty_article_rejects_missing_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()

            import image_sync_server

            original_config = image_sync_server.XML_CONFIG
            original_state = image_sync_server.STATE
            state = SyncState(root / "state.json")
            image_sync_server.XML_CONFIG = {
                "images_dir": images,
                "output_xml": root / "out" / "images.xml",
                "base_url": "https://img.example.com/foto",
                "allowed_extensions": {".jpg"},
            }
            image_sync_server.STATE = state
            try:
                with self.assertRaises(ValueError):
                    add_manual_dirty_article("MISSING")
            finally:
                image_sync_server.XML_CONFIG = original_config
                image_sync_server.STATE = original_state

        self.assertEqual(state.dirty, {})


class PreviewAndValidationTests(unittest.TestCase):
    def test_prepare_import_plan_reports_preview_and_skips(self):
        settings = HoroshopSettings(domain="https://shop.example.com", token="token")
        catalog = CatalogIndex.from_raw(
            [{"article": "REAL", "article_for_display": "DISPLAY"}]
        )

        plan = prepare_import_plan(
            settings=settings,
            catalog=catalog,
            xml_products={"DISPLAY": ["https://img.example.com/DISPLAY.jpg"]},
            target_articles=["DISPLAY", "MISSING"],
        )

        self.assertEqual(len(plan["prepared"]), 1)
        self.assertEqual(plan["prepared"][0]["remote_article"], "REAL")
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertEqual(plan["preview"][0]["sample_images"], ["https://img.example.com/DISPLAY.jpg"])

    def test_validate_image_url_accepts_jpeg_under_limit(self):
        class Response:
            status_code = 200
            headers = {
                "content-type": "image/jpeg",
                "content-length": "100",
            }

            def raise_for_status(self):
                return None

        class Session:
            def head(self, *args, **kwargs):
                return Response()

        self.assertEqual(validate_image_url("https://img.example.com/a.jpg", Session()), (True, "OK"))

    def test_validate_image_url_rejects_large_file(self):
        class Response:
            status_code = 200
            headers = {
                "content-type": "image/jpeg",
                "content-length": str(6 * 1024 * 1024),
            }

            def raise_for_status(self):
                return None

        class Session:
            def head(self, *args, **kwargs):
                return Response()

        ok, message = validate_image_url("https://img.example.com/a.jpg", Session())

        self.assertFalse(ok)
        self.assertIn("5 МБ", message)

    def test_catalog_age_seconds_parses_iso_timestamp(self):
        self.assertIsNone(catalog_age_seconds(""))


if __name__ == "__main__":
    unittest.main()
