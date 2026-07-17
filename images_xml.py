from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import statistics
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE: Path | None = None
MAX_LOG_LINES = 2000
LOG_MUTEX_NAME = "Global\\ImagesXmlSharedLog"
HEARTBEAT_SECONDS = 5 * 60
UTF8_BOM = b"\xef\xbb\xbf"
NTP_EPOCH_DELTA = 2_208_988_800
NTP_SERVERS = (
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
)
KYIV_TIMEZONE_NAME = "Europe/Kyiv"
WATCH_IGNORED_SUFFIXES = {
    ".log",
    ".xml",
    ".tmp",
}
EVENT_TYPE_LABELS = {
    "created": "створено",
    "modified": "змінено",
    "deleted": "видалено",
    "moved": "переміщено",
}


def configure_console_encoding() -> None:
    """Use UTF-8 even when Windows/GitHub selects a legacy console encoding."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (AttributeError, OSError, ValueError):
                pass


configure_console_encoding()


class ReliableClock:
    """World-clock time anchored to monotonic time instead of Windows time."""

    def __init__(self, utc_timestamp: float, timezone: ZoneInfo):
        self._utc_timestamp = utc_timestamp
        self._monotonic_anchor = time.monotonic()
        self._timezone = timezone

    def now(self) -> datetime:
        elapsed = time.monotonic() - self._monotonic_anchor
        utc_now = datetime.fromtimestamp(
            self._utc_timestamp + elapsed,
            tz=UTC,
        )
        return utc_now.astimezone(self._timezone)


RELIABLE_CLOCK: ReliableClock | None = None
CLOCK_SOURCE = "system"
CLOCK_WARNING: str | None = None


def query_ntp_time(server: str, timeout: float = 3.0) -> float:
    """Return a UTC Unix timestamp from one NTP server."""
    request = bytearray(48)
    request[0] = 0x23

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        started = time.monotonic()
        client.sendto(request, (server, 123))
        response, _ = client.recvfrom(512)
        finished = time.monotonic()

    if len(response) < 48:
        raise OSError(f"Коротка NTP-відповідь від {server}.")

    seconds, fraction = struct.unpack("!II", response[40:48])
    if seconds == 0:
        raise OSError(f"NTP-сервер {server} не повернув час.")

    server_timestamp = (
        seconds - NTP_EPOCH_DELTA + fraction / 2**32
    )
    return server_timestamp + (finished - started) / 2


def synchronize_reliable_clock() -> ReliableClock:
    """Synchronize against multiple NTP servers and use Kyiv DST rules."""
    samples: list[float] = []
    errors: list[str] = []

    for server in NTP_SERVERS:
        try:
            samples.append(query_ntp_time(server))
        except (OSError, TimeoutError, socket.gaierror) as error:
            errors.append(f"{server}: {error}")

    if not samples:
        raise RuntimeError(
            "Не вдалося отримати світовий час через NTP. "
            + " | ".join(errors)
        )

    try:
        timezone = ZoneInfo(KYIV_TIMEZONE_NAME)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            "Не знайдено часовий пояс Europe/Kyiv. "
            "Перевстановіть залежності з requirements.txt."
        ) from error

    return ReliableClock(statistics.median(samples), timezone)


def initialize_reliable_clock() -> datetime:
    global RELIABLE_CLOCK, CLOCK_SOURCE, CLOCK_WARNING
    try:
        RELIABLE_CLOCK = synchronize_reliable_clock()
        CLOCK_SOURCE = "ntp"
        CLOCK_WARNING = None
        return RELIABLE_CLOCK.now()
    except RuntimeError as error:
        RELIABLE_CLOCK = None
        CLOCK_SOURCE = "system"
        CLOCK_WARNING = str(error)
        return datetime.now().astimezone()


def now_kyiv() -> datetime:
    if RELIABLE_CLOCK is None:
        # Library/test calls may build XML without running main().
        return datetime.now().astimezone()
    return RELIABLE_CLOCK.now()


@contextmanager
def shared_log_lock():
    """Synchronize log writes between Python and the PowerShell supervisor."""
    if os.name != "nt":
        yield
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.CreateMutexW(None, False, LOG_MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Не вдалося створити mutex журналу.")

    acquired = False
    try:
        result = kernel32.WaitForSingleObject(handle, 30_000)
        if result not in (0x00000000, 0x00000080):
            raise TimeoutError("Не вдалося отримати блокування журналу за 30 секунд.")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def log(message: str) -> None:
    """Write a timestamped message to the console and bounded log file."""
    timestamp = now_kyiv().isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)

    if LOG_FILE is None:
        return

    try:
        with shared_log_lock():
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            ensure_utf8_bom(LOG_FILE)
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(f"{line}\n")
            trim_log_file()
    except (OSError, TimeoutError) as error:
        print(f"[{timestamp}] Не вдалося записати журнал {LOG_FILE}: {error}", flush=True)


def ensure_utf8_bom(log_file: Path) -> None:
    """Add a UTF-8 BOM so browsers detect Cyrillic text correctly."""
    if not log_file.exists() or log_file.stat().st_size == 0:
        with log_file.open("wb") as file:
            file.write(UTF8_BOM)
        return

    with log_file.open("rb") as file:
        prefix = file.read(len(UTF8_BOM))
    if prefix == UTF8_BOM:
        return

    temp_file = log_file.with_suffix(f"{log_file.suffix}.bom.tmp")
    with log_file.open("rb") as source, temp_file.open("wb") as destination:
        destination.write(UTF8_BOM)
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
    os.replace(temp_file, log_file)


def trim_log_file() -> None:
    """Keep only the newest configured number of log records."""
    if LOG_FILE is None or MAX_LOG_LINES < 1 or not LOG_FILE.exists():
        return

    with LOG_FILE.open("r", encoding="utf-8-sig") as file:
        lines = file.readlines()

    if len(lines) <= MAX_LOG_LINES:
        return

    temp_file = LOG_FILE.with_suffix(f"{LOG_FILE.suffix}.tmp")
    with temp_file.open("w", encoding="utf-8-sig", newline="\n") as file:
        file.writelines(lines[-MAX_LOG_LINES:])
    os.replace(temp_file, LOG_FILE)


def configure_logging(config: dict[str, Any]) -> None:
    global LOG_FILE, MAX_LOG_LINES
    LOG_FILE = config["log_file"]
    MAX_LOG_LINES = config["max_log_lines"]


def python_heartbeat(stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        log("Python-спостерігач працює та відстежує зміни зображень.")


def load_config(config_file: Path = DEFAULT_CONFIG_FILE) -> dict[str, Any]:
    if not config_file.exists():
        raise FileNotFoundError(
            f"Не знайдено файл налаштувань: {config_file}. "
            "Створіть його з config.example.json."
        )

    with config_file.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    required = ("images_dir",)
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(
            "У config.json не заповнено обов'язкові параметри: "
            + ", ".join(missing)
        )

    max_log_lines = int(raw.get("max_log_lines", 2000))
    if max_log_lines < 1:
        raise ValueError("max_log_lines має бути більше нуля.")

    configured_output_dir = raw.get("output_dir")
    configured_output_xml = raw.get("output_xml")

    if configured_output_dir:
        output_dir = Path(configured_output_dir).expanduser()
        xml_filename = str(raw.get("xml_filename", "images_export.xml"))
        log_filename = str(raw.get("log_filename", "images_export.log"))

        if Path(xml_filename).name != xml_filename:
            raise ValueError("xml_filename має містити лише ім'я файлу.")
        if Path(log_filename).name != log_filename:
            raise ValueError("log_filename має містити лише ім'я файлу.")
        if Path(xml_filename).suffix.lower() != ".xml":
            raise ValueError("xml_filename повинен мати розширення .xml.")
        if Path(log_filename).suffix.lower() != ".log":
            raise ValueError("log_filename повинен мати розширення .log.")

        output_xml = output_dir / xml_filename
        log_file = output_dir / log_filename
    elif configured_output_xml:
        # Backward compatibility with configs created before output_dir.
        output_xml = Path(configured_output_xml).expanduser()
        configured_log_file = raw.get("log_file")
        log_file = (
            Path(configured_log_file).expanduser()
            if configured_log_file
            else output_xml.with_suffix(".log")
        )
    else:
        raise ValueError(
            "Вкажіть output_dir або старий параметр output_xml."
        )

    images_base_url = raw.get("images_base_url", raw.get("base_url"))
    if not images_base_url:
        raise ValueError(
            "Не заповнено images_base_url (або старий параметр base_url)."
        )

    extensions = {
        str(extension).lower()
        if str(extension).startswith(".")
        else f".{str(extension).lower()}"
        for extension in raw.get(
            "allowed_extensions", [".jpg", ".jpeg", ".png", ".webp"]
        )
    }
    if not extensions:
        raise ValueError("allowed_extensions не повинен бути порожнім.")

    return {
        "images_dir": Path(raw["images_dir"]).expanduser(),
        "output_dir": output_xml.parent,
        "output_xml": output_xml,
        "images_base_url": str(images_base_url).rstrip("/"),
        # Internal alias retained while build_xml callers migrate.
        "base_url": str(images_base_url).rstrip("/"),
        "allowed_extensions": extensions,
        "log_file": log_file,
        "max_log_lines": max_log_lines,
    }


def parse_filename(file_path: Path) -> tuple[str, tuple[int, int | str]]:
    """
    Return the product article and image sort key.

    X36B.jpg     -> article=X36B, sort=(0, 0)
    X36B_1.jpg   -> article=X36B, sort=(1, 1)
    X36B_10.jpg  -> article=X36B, sort=(1, 10)
    X36B_side.jpg -> article=X36B, sort=(2, "side")
    """
    stem = file_path.stem

    if "_" not in stem:
        return stem, (0, 0)

    article, suffix = stem.split("_", 1)
    if suffix.isdigit():
        return article, (1, int(suffix))

    return article, (2, suffix.casefold())


def build_xml(config: dict[str, Any]) -> tuple[int, int]:
    images_dir: Path = config["images_dir"]
    output_xml: Path = config["output_xml"]
    images_base_url: str = (
        config.get("images_base_url") or config["base_url"]
    )
    allowed_extensions: set[str] = config["allowed_extensions"]

    if not images_dir.is_dir():
        raise FileNotFoundError(
            f"Папку із зображеннями не знайдено: {images_dir}"
        )

    products: dict[
        tuple[str, str], list[tuple[tuple[int, int | str], str]]
    ] = defaultdict(list)

    for file_path in images_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in allowed_extensions:
            continue

        article, sort_key = parse_filename(file_path)
        if not article:
            log(f"Пропущено файл без артикулу: {file_path}")
            continue

        relative_path = file_path.relative_to(images_dir).as_posix()
        brand = file_path.relative_to(images_dir).parent.as_posix()
        if brand == ".":
            brand = ""
        image_url = f"{images_base_url}/{quote(relative_path, safe='/')}"
        products[(article, brand)].append((sort_key, image_url))

    valid_products: dict[
        tuple[str, str], list[tuple[tuple[int, int | str], str]]
    ] = {}
    for (article, brand), images in products.items():
        if not any(sort_key == (0, 0) for sort_key, _ in images):
            location = brand or "/"
            log(
                "Пропущено групу фото без ключового зображення: "
                f"артикул={article}, папка={location}"
            )
            continue
        valid_products[(article, brand)] = images

    product_count = len(valid_products)
    image_count = sum(len(images) for images in valid_products.values())
    generated_at = now_kyiv()
    generated_at_text = generated_at.isoformat(timespec="seconds")

    root = ET.Element(
        "products",
        generated_at=generated_at_text,
        timezone=str(generated_at.tzinfo),
        products_count=str(product_count),
        images_count=str(image_count),
    )
    root.append(
        ET.Comment(
            f" Актуальність XML: {generated_at_text} | "
            f"артикулів: {product_count} | фото: {image_count} "
        )
    )

    for article, brand in sorted(
        valid_products,
        key=lambda item: (item[0].casefold(), item[1].casefold(), item[0], item[1]),
    ):
        product_element = ET.SubElement(
            root,
            "product",
            article=article,
            brand=brand,
        )
        for _, image_url in sorted(
            valid_products[(article, brand)], key=lambda item: item[0]
        ):
            ET.SubElement(product_element, "image").text = image_url

    output_xml.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")

    temp_file = output_xml.with_suffix(f"{output_xml.suffix}.tmp")
    tree.write(temp_file, encoding="utf-8", xml_declaration=True)
    os.replace(temp_file, output_xml)

    log(
        f"XML оновлено: {output_xml} | "
        f"артикулів: {product_count} | фото: {image_count}"
    )
    return product_count, image_count


class ImagesChangeHandler(FileSystemEventHandler):
    """Rebuild XML when a supported image is changed."""

    def __init__(self, config: dict[str, Any]):
        super().__init__()
        self.config = config
        self.allowed_extensions: set[str] = config["allowed_extensions"]
        self._build_lock = threading.Lock()
        output_xml: Path = config["output_xml"]
        log_file: Path = config.get(
            "log_file",
            output_xml.with_suffix(".log"),
        )
        self._ignored_files = {
            self._normalize_path(output_xml),
            self._normalize_path(log_file),
            self._normalize_path(
                output_xml.with_suffix(f"{output_xml.suffix}.tmp")
            ),
            self._normalize_path(
                log_file.with_suffix(f"{log_file.suffix}.tmp")
            ),
        }

    @staticmethod
    def _normalize_path(path: str | Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _is_ignored_path(self, path: str) -> bool:
        file_path = Path(path)
        suffixes = {suffix.lower() for suffix in file_path.suffixes}
        return (
            self._normalize_path(path) in self._ignored_files
            or bool(suffixes & WATCH_IGNORED_SUFFIXES)
        )

    def _is_image_path(self, path: str) -> bool:
        return (
            not self._is_ignored_path(path)
            and Path(path).suffix.lower() in self.allowed_extensions
        )

    def _rebuild_for_event(self, event: FileSystemEvent) -> None:
        paths = [event.src_path]
        destination = getattr(event, "dest_path", "")
        if destination:
            paths.append(destination)

        if all(self._is_ignored_path(path) for path in paths):
            return

        if (
            not event.is_directory
            and not any(self._is_image_path(path) for path in paths)
        ):
            return

        if not self._build_lock.acquire(blocking=False):
            return

        try:
            event_type = EVENT_TYPE_LABELS.get(
                event.event_type,
                event.event_type,
            )
            log(
                f"Зміна файлів: {event_type} | "
                + " -> ".join(paths)
            )
            build_xml(self.config)
        except Exception as error:
            log(f"Помилка оновлення XML: {error}")
        finally:
            self._build_lock.release()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._rebuild_for_event(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._rebuild_for_event(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._rebuild_for_event(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._rebuild_for_event(event)


def watch_images(config: dict[str, Any]) -> None:
    """Build XML once, then wait for filesystem events."""
    images_dir: Path = config["images_dir"]
    try:
        images_dir_exists = images_dir.is_dir()
    except PermissionError as error:
        raise PermissionError(
            f"Відмовлено в доступі до images_dir: {images_dir}. "
            "Ймовірна причина: обліковий запис, який запускає Python, "
            "не має прав на мережеву папку."
        ) from error

    if not images_dir_exists:
        raise FileNotFoundError(
            f"Папку із зображеннями не знайдено або вона недоступна: {images_dir}. "
            "Якщо це мережева папка, перевірте права облікового запису завдання."
        )

    build_xml(config)

    observer = Observer()
    handler = ImagesChangeHandler(config)
    observer.schedule(handler, str(images_dir), recursive=True)
    observer.start()
    log(f"Відстеження змін запущено: {images_dir}")
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=python_heartbeat,
        args=(heartbeat_stop,),
        name="python-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    try:
        observer.join()
    except KeyboardInterrupt:
        log("Отримано команду зупинки.")
        observer.stop()
        observer.join()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Створення XML із посиланнями на зображення товарів."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Створити XML один раз і завершити роботу.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="Шлях до config.json.",
    )
    parser.add_argument(
        "--print-utc-epoch",
        action="store_true",
        help="Отримати світовий UTC-час через NTP і завершити роботу.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.print_utc_epoch:
        try:
            clock = synchronize_reliable_clock()
            print(f"{clock.now().astimezone(UTC).timestamp():.6f}")
            return 0
        except RuntimeError as error:
            system_timestamp = datetime.now(tz=UTC).timestamp()
            print(
                f"Попередження: NTP недоступний, використовується системний "
                f"UTC-час: {error}",
                file=sys.stderr,
            )
            print(f"{system_timestamp:.6f}")
            return 0

    try:
        config = load_config(args.config.resolve())
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        log(f"Помилка конфігурації: {error}")
        return 1

    synchronized_time = initialize_reliable_clock()

    configure_logging(config)
    if CLOCK_SOURCE == "ntp":
        log(
            "Світовий час синхронізовано через NTP: "
            f"{synchronized_time.isoformat(timespec='seconds')} "
            f"({KYIV_TIMEZONE_NAME})"
        )
    else:
        log(
            "ПОПЕРЕДЖЕННЯ: точний NTP-час недоступний; використовується "
            f"системний час: {synchronized_time.isoformat(timespec='seconds')}. "
            f"Причина: {CLOCK_WARNING}"
        )
    log(
        f"Файл консольного журналу: {config['log_file']} | "
        f"максимум рядків: {config['max_log_lines']}"
    )
    if args.once:
        try:
            build_xml(config)
        except (OSError, ValueError) as error:
            log(f"Помилка: {error}")
            return 1
        return 0

    try:
        watch_images(config)
    except Exception as error:
        log(f"Помилка спостереження за папкою: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
