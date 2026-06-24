import io
import json
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import images_xml
from images_xml import (
    ImagesChangeHandler,
    ReliableClock,
    build_xml,
    configure_logging,
    configure_console_encoding,
    ensure_utf8_bom,
    load_config,
    log,
    parse_filename,
    python_heartbeat,
    synchronize_reliable_clock,
    watch_images,
)
from watchdog.events import (
    DirDeletedEvent,
    FileCreatedEvent,
    FileOpenedEvent,
    FileMovedEvent,
)
from watchdog.observers import Observer


class ParseFilenameTests(unittest.TestCase):
    def test_main_image_is_first(self):
        self.assertEqual(parse_filename(Path("X36B.jpg")), ("X36B", (0, 0)))

    def test_numeric_suffix_uses_numeric_sorting(self):
        self.assertEqual(parse_filename(Path("X36B_10.jpg")), ("X36B", (1, 10)))

    def test_text_suffix_is_supported(self):
        self.assertEqual(
            parse_filename(Path("X36B_side.jpg")), ("X36B", (2, "side"))
        )


class BuildXmlTests(unittest.TestCase):
    def test_builds_encoded_sorted_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            output = root / "out" / "products.xml"
            nested = images / "Каталог"
            nested.mkdir(parents=True)

            for name in ("X36B_10.jpg", "X36B.jpg", "X36B_2.jpg"):
                (nested / name).touch()
            (nested / "ignore.txt").touch()

            products, image_count = build_xml(
                {
                    "images_dir": images,
                    "output_xml": output,
                    "base_url": "https://img.example.com",
                    "allowed_extensions": {".jpg"},
                }
            )

            self.assertEqual((products, image_count), (1, 3))
            root_element = ET.parse(output).getroot()
            self.assertIn("generated_at", root_element.attrib)
            self.assertIn("+", root_element.attrib["generated_at"])
            self.assertEqual(root_element.attrib["products_count"], "1")
            self.assertEqual(root_element.attrib["images_count"], "3")

            xml_text = output.read_text(encoding="utf-8")
            self.assertIn("Актуальність XML:", xml_text)
            self.assertLess(
                xml_text.index("Актуальність XML:"),
                xml_text.index("<product "),
            )

            product = root_element.find("product")
            self.assertIsNotNone(product)
            self.assertEqual(product.attrib["article"], "X36B")
            urls = [element.text for element in product.findall("image")]
            self.assertTrue(urls[0].endswith("/X36B.jpg"))
            self.assertTrue(urls[1].endswith("/X36B_2.jpg"))
            self.assertTrue(urls[2].endswith("/X36B_10.jpg"))
            self.assertIn("%D0%9A", urls[0])


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        images_xml.LOG_FILE = None
        images_xml.MAX_LOG_LINES = 2000
        images_xml.RELIABLE_CLOCK = None
        images_xml.CLOCK_SOURCE = "system"
        images_xml.CLOCK_WARNING = None

    def test_log_keeps_only_newest_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "console.log"
            configure_logging({"log_file": log_file, "max_log_lines": 3})

            for number in range(5):
                log(f"record-{number}")

            lines = log_file.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("record-2", lines[0])
            self.assertIn("record-4", lines[2])

    def test_log_contains_utf8_bom_for_browser_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "console.log"
            configure_logging({"log_file": log_file, "max_log_lines": 10})

            log("Кирилиця відображається правильно")

            self.assertTrue(
                log_file.read_bytes().startswith(images_xml.UTF8_BOM)
            )
            self.assertIn(
                "Кирилиця відображається правильно",
                log_file.read_text(encoding="utf-8-sig"),
            )

    def test_bom_is_added_to_existing_log_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "existing.log"
            original = "Наявний запис\n"
            log_file.write_text(original, encoding="utf-8")

            ensure_utf8_bom(log_file)

            self.assertTrue(
                log_file.read_bytes().startswith(images_xml.UTF8_BOM)
            )
            self.assertEqual(
                log_file.read_text(encoding="utf-8-sig"),
                original,
            )

    def test_console_is_reconfigured_to_utf8(self):
        class ReconfigurableStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.settings = None

            def reconfigure(self, **kwargs):
                self.settings = kwargs

        stdout = ReconfigurableStream()
        stderr = ReconfigurableStream()

        with patch.object(sys, "stdout", stdout), patch.object(
            sys,
            "stderr",
            stderr,
        ):
            configure_console_encoding()

        self.assertEqual(stdout.settings["encoding"], "utf-8")
        self.assertEqual(stderr.settings["encoding"], "utf-8")

    def test_default_log_is_created_next_to_xml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.json"
            config_file.write_text(
                """
                {
                  "images_dir": "D:\\\\images",
                  "output_xml": "D:\\\\images\\\\export.xml",
                  "base_url": "https://img.example.com"
                }
                """,
                encoding="utf-8",
            )

            config = load_config(config_file)
            self.assertEqual(config["log_file"], Path("D:\\images\\export.log"))
            self.assertEqual(config["max_log_lines"], 2000)

    def test_output_directory_and_filenames_are_combined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.json"
            output_dir = root / "export"
            config_file.write_text(
                json.dumps(
                    {
                        "images_dir": str(root / "images"),
                        "output_dir": str(output_dir),
                        "xml_filename": "catalog.xml",
                        "log_filename": "catalog.log",
                        "images_base_url": "https://img.example.com/foto",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_file)

            self.assertEqual(config["output_dir"], output_dir)
            self.assertEqual(
                config["output_xml"],
                output_dir / "catalog.xml",
            )
            self.assertEqual(
                config["log_file"],
                output_dir / "catalog.log",
            )
            self.assertEqual(
                config["images_base_url"],
                "https://img.example.com/foto",
            )

    def test_output_filename_rejects_nested_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "images_dir": str(root / "images"),
                        "output_dir": str(root / "export"),
                        "xml_filename": "nested/catalog.xml",
                        "images_base_url": "https://img.example.com",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "лише ім'я файлу"):
                load_config(config_file)

    def test_python_heartbeat_writes_health_message(self):
        stop_event = unittest.mock.Mock()
        stop_event.wait.side_effect = [False, True]

        with patch("images_xml.log") as mocked_log:
            python_heartbeat(stop_event)

        mocked_log.assert_called_once_with(
            "Python-спостерігач працює та відстежує зміни зображень."
        )

    def test_python_heartbeat_interval_is_five_minutes(self):
        self.assertEqual(images_xml.HEARTBEAT_SECONDS, 300)

    def test_reliable_clock_uses_monotonic_elapsed_time(self):
        timezone = ZoneInfo("Europe/Kyiv")

        with patch("images_xml.time.monotonic", side_effect=[100.0, 160.0]):
            clock = ReliableClock(1_735_689_600.0, timezone)
            current = clock.now()

        expected_utc = datetime.fromtimestamp(
            1_735_689_660.0,
            tz=UTC,
        )
        self.assertEqual(current, expected_utc.astimezone(timezone))

    def test_ntp_synchronization_uses_median_and_kyiv_timezone(self):
        with patch(
            "images_xml.query_ntp_time",
            side_effect=[
                1_735_689_600.0,
                1_735_689_602.0,
                1_735_689_601.0,
            ],
        ), patch("images_xml.time.monotonic", return_value=50.0):
            clock = synchronize_reliable_clock()
            current = clock.now()

        expected = datetime.fromtimestamp(
            1_735_689_601.0,
            tz=UTC,
        ).astimezone(ZoneInfo("Europe/Kyiv"))
        self.assertEqual(current, expected)

    def test_clock_falls_back_to_system_time_when_ntp_fails(self):
        expected = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)

        with patch(
            "images_xml.synchronize_reliable_clock",
            side_effect=RuntimeError("NTP blocked"),
        ), patch("images_xml.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = expected
            current = images_xml.initialize_reliable_clock()

        self.assertEqual(current, expected)
        self.assertEqual(images_xml.CLOCK_SOURCE, "system")
        self.assertEqual(images_xml.CLOCK_WARNING, "NTP blocked")

    def test_kyiv_timezone_applies_winter_and_summer_offsets(self):
        timezone = ZoneInfo("Europe/Kyiv")
        winter = datetime(2026, 1, 15, tzinfo=UTC).astimezone(timezone)
        summer = datetime(2026, 7, 15, tzinfo=UTC).astimezone(timezone)

        self.assertEqual(winter.utcoffset().total_seconds(), 2 * 3600)
        self.assertEqual(summer.utcoffset().total_seconds(), 3 * 3600)


class ChangeHandlerTests(unittest.TestCase):
    @staticmethod
    def make_handler(
        allowed_extensions: set[str] | None = None,
    ) -> ImagesChangeHandler:
        return ImagesChangeHandler(
            {
                "allowed_extensions": allowed_extensions or {".jpg"},
                "output_xml": Path(r"D:\images\images_export.xml"),
                "log_file": Path(r"D:\images\images_export.log"),
            }
        )

    def test_image_event_rebuilds_xml(self):
        handler = self.make_handler()

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(FileCreatedEvent(r"D:\images\X36B.jpg"))

        mocked_build.assert_called_once_with(handler.config)

    def test_unrelated_file_event_is_ignored(self):
        handler = self.make_handler()

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(FileCreatedEvent(r"D:\images\readme.txt"))

        mocked_build.assert_not_called()

    def test_deleted_directory_rebuilds_xml(self):
        handler = self.make_handler()

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(DirDeletedEvent(r"D:\images\catalog"))

        mocked_build.assert_called_once_with(handler.config)

    def test_opening_image_does_not_rebuild_xml(self):
        handler = self.make_handler()

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(FileOpenedEvent(r"D:\images\X36B.jpg"))

        mocked_build.assert_not_called()

    def test_real_observer_rebuilds_after_file_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            output = root / "products.xml"
            images.mkdir()
            config = {
                "images_dir": images,
                "output_xml": output,
                "base_url": "https://img.example.com",
                "allowed_extensions": {".jpg"},
            }

            observer = Observer()
            observer.schedule(
                ImagesChangeHandler(config), str(images), recursive=True
            )
            observer.start()
            try:
                (images / "X36B.jpg").touch()
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                observer.stop()
                observer.join()

            self.assertTrue(output.exists())
            product = ET.parse(output).getroot().find("product")
            self.assertIsNotNone(product)
            self.assertEqual(product.attrib["article"], "X36B")

    def test_xml_and_log_events_are_always_ignored(self):
        handler = self.make_handler({".jpg", ".xml", ".log"})

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(
                FileCreatedEvent(r"D:\images\images_export.xml")
            )
            handler.dispatch(
                FileCreatedEvent(r"D:\images\images_export.log")
            )

        mocked_build.assert_not_called()

    def test_atomic_xml_temp_move_is_ignored(self):
        handler = self.make_handler({".jpg", ".xml", ".tmp"})

        with patch("images_xml.build_xml") as mocked_build:
            handler.dispatch(
                FileMovedEvent(
                    r"D:\images\images_export.xml.tmp",
                    r"D:\images\images_export.xml",
                )
            )

        mocked_build.assert_not_called()

    def test_access_denied_has_specific_explanation(self):
        config = {"images_dir": Path(r"\\server\share")}

        with patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(
                PermissionError,
                "не має прав на мережеву папку",
            ):
                watch_images(config)


if __name__ == "__main__":
    unittest.main()
