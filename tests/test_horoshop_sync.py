import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from horoshop_sync import (
    CatalogIndex,
    HoroshopSettings,
    build_import_product,
    import_article_succeeded,
    import_log_by_article,
    load_horoshop_settings,
    load_xml_products,
)


class HoroshopSettingsTests(unittest.TestCase):
    def test_loads_login_password_and_panel_safe_defaults(self):
        settings = load_horoshop_settings(
            {
                "horoshop": {
                    "domain": "https://shop.example.com/",
                    "login": "api@example.com",
                    "password": "secret",
                }
            }
        )

        self.assertEqual(settings.domain, "https://shop.example.com")
        self.assertEqual(settings.image_field, "images")
        self.assertTrue(settings.override)
        self.assertFalse(settings.remove_all_when_no_local_images)

    def test_rejects_invalid_image_field(self):
        with self.assertRaisesRegex(ValueError, "image_field"):
            load_horoshop_settings(
                {
                    "horoshop": {
                        "domain": "https://shop.example.com",
                        "token": "token",
                        "image_field": "description",
                    }
                }
            )


class CatalogIndexTests(unittest.TestCase):
    def test_matches_direct_article_first(self):
        index = CatalogIndex.from_raw(
            [
                {
                    "article": "REAL",
                    "article_for_display": "DISPLAY",
                    "parent_article": "REAL",
                },
                {
                    "article": "DISPLAY",
                    "article_for_display": "",
                    "parent_article": "DISPLAY",
                },
            ]
        )

        match = index.match("DISPLAY")

        self.assertEqual(match.status, "matched")
        self.assertEqual(match.product.article, "DISPLAY")

    def test_matches_unique_article_for_display(self):
        index = CatalogIndex.from_raw(
            [
                {
                    "article": "REAL",
                    "article_for_display": "DISPLAY",
                    "parent_article": "REAL",
                }
            ]
        )

        match = index.match("DISPLAY")

        self.assertEqual(match.status, "matched")
        self.assertEqual(match.product.article, "REAL")

    def test_reports_ambiguous_display_article(self):
        index = CatalogIndex.from_raw(
            [
                {"article": "A1", "article_for_display": "DUP"},
                {"article": "A2", "article_for_display": "DUP"},
            ]
        )

        match = index.match("DUP")

        self.assertEqual(match.status, "ambiguous")
        self.assertIn("A1", match.message)
        self.assertIn("A2", match.message)


class ImportPayloadTests(unittest.TestCase):
    def test_builds_replace_images_payload(self):
        settings = HoroshopSettings(
            domain="https://shop.example.com",
            token="token",
            image_field="images",
            override=True,
        )
        match = CatalogIndex.from_raw(
            [{"article": "REAL", "article_for_display": "DISPLAY"}]
        ).match("DISPLAY")

        item, reason = build_import_product(
            match=match,
            image_urls=["https://img.example.com/DISPLAY.jpg"],
            settings=settings,
        )

        self.assertEqual(reason, "")
        self.assertEqual(
            item,
            {
                "article": "REAL",
                "images": {
                    "override": True,
                    "links": ["https://img.example.com/DISPLAY.jpg"],
                },
            },
        )

    def test_empty_local_images_do_not_clear_by_default(self):
        settings = HoroshopSettings(domain="https://shop.example.com", token="token")
        match = CatalogIndex.from_raw([{"article": "REAL"}]).match("REAL")

        item, reason = build_import_product(
            match=match,
            image_urls=[],
            settings=settings,
        )

        self.assertIsNone(item)
        self.assertIn("очищення вимкнено", reason)

    def test_empty_local_images_can_clear_gallery(self):
        settings = HoroshopSettings(
            domain="https://shop.example.com",
            token="token",
            remove_all_when_no_local_images=True,
        )
        match = CatalogIndex.from_raw([{"article": "REAL"}]).match("REAL")

        item, _ = build_import_product(match=match, image_urls=[], settings=settings)

        self.assertEqual(item, {"article": "REAL", "images": {"removeAll": True}})


class ImportResponseTests(unittest.TestCase):
    def test_warning_with_image_error_is_failed(self):
        response = {
            "status": "WARNING",
            "response": {
                "log": [
                    {
                        "article": "REAL",
                        "info": [{"code": 27, "message": "bad MIME"}],
                    }
                ]
            },
        }

        logs = import_log_by_article(response)

        self.assertFalse(import_article_succeeded("WARNING", logs["REAL"]))

    def test_warning_with_gallery_uploaded_is_success(self):
        self.assertTrue(
            import_article_succeeded(
                "WARNING",
                [{"code": 22, "message": "images uploaded"}],
            )
        )


class XmlProductTests(unittest.TestCase):
    def test_loads_current_xml_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            xml_file = Path(temp_dir) / "images.xml"
            root = ET.Element("products")
            product = ET.SubElement(root, "product", article="X36B")
            ET.SubElement(product, "image").text = "https://img.example.com/X36B.jpg"
            ET.ElementTree(root).write(xml_file, encoding="utf-8", xml_declaration=True)

            products = load_xml_products(xml_file)

        self.assertEqual(products, {"X36B": ["https://img.example.com/X36B.jpg"]})


if __name__ == "__main__":
    unittest.main()

