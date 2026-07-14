from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from openpyxl import load_workbook
from starlette.datastructures import UploadFile as StarletteUploadFile
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

import images_xml
from horoshop_sync import (
    CatalogIndex,
    HoroshopClient,
    HoroshopError,
    HoroshopSettings,
    build_clear_product,
    build_import_product,
    chunked,
    force_append_upload,
    current_epoch,
    import_article_succeeded,
    import_log_by_article,
    load_horoshop_settings,
    load_xml_products,
    normalize_article,
    read_raw_config,
    with_runtime_credentials,
)
from images_xml import (
    DEFAULT_CONFIG_FILE,
    EVENT_TYPE_LABELS,
    WATCH_IGNORED_SUFFIXES,
    build_xml,
    configure_console_encoding,
    configure_logging,
    initialize_reliable_clock,
    load_config,
    log,
    parse_filename,
)


STATE_SCHEMA_VERSION = 1
STATE_HISTORY_LIMIT = 300
JOB_TTL_SECONDS = 60 * 60
CATALOG_CACHE_MAX_ITEMS = 200_000

CONFIG_FILE = DEFAULT_CONFIG_FILE
RAW_CONFIG: dict[str, Any] = {}
XML_CONFIG: dict[str, Any] = {}
HOROSHOP_SETTINGS: HoroshopSettings | None = None
SERVER_SETTINGS: dict[str, Any] = {}
STATE: "SyncState | None" = None
STATE_LOCK = threading.Lock()
SYNC_LOCK = threading.Lock()
OBSERVER: Observer | None = None
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def load_server_settings(raw: dict[str, Any], xml_config: dict[str, Any]) -> dict[str, Any]:
    server = raw.get("server") or {}
    if not isinstance(server, dict):
        raise ValueError("Секція server у config.json повинна бути об'єктом.")

    state_file = server.get("state_file")
    if state_file:
        resolved_state_file = Path(str(state_file)).expanduser()
    else:
        resolved_state_file = xml_config["output_dir"] / "horoshop_sync_state.json"

    return {
        "enabled": bool(server.get("enabled", False)),
        "host": str(server.get("host", "0.0.0.0")),
        "port": int(server.get("port", 8092)),
        "state_file": resolved_state_file,
    }


def timestamp() -> str:
    return images_xml.now_kyiv().isoformat(timespec="seconds")


class SyncState:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.dirty: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.catalog_products: list[dict[str, Any]] = []
        self.catalog_updated_at = ""
        self.load()

    def load(self) -> None:
        if not self.state_file.exists():
            return
        with self.state_file.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return
        dirty = data.get("dirty", {})
        history = data.get("history", [])
        catalog = data.get("catalog", {})
        if isinstance(dirty, dict):
            self.dirty = {
                normalize_article(article): item
                for article, item in dirty.items()
                if normalize_article(article) and isinstance(item, dict)
            }
        if isinstance(history, list):
            self.history = [item for item in history if isinstance(item, dict)][
                -STATE_HISTORY_LIMIT:
            ]
        if isinstance(catalog, dict):
            products = catalog.get("products", [])
            if isinstance(products, list):
                self.catalog_products = [
                    item for item in products if isinstance(item, dict)
                ][:CATALOG_CACHE_MAX_ITEMS]
            self.catalog_updated_at = normalize_article(catalog.get("updated_at"))

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "updated_at": timestamp(),
            "dirty": self.dirty,
            "history": self.history[-STATE_HISTORY_LIMIT:],
            "catalog": {
                "updated_at": self.catalog_updated_at,
                "products_count": len(self.catalog_products),
                "products": self.catalog_products,
            },
        }
        temp_file = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_file, self.state_file)

    def mark_dirty(self, article: str, event_type: str, image_count: int | None) -> None:
        article = normalize_article(article)
        if not article:
            return
        now = timestamp()
        existing = self.dirty.get(article, {})
        self.dirty[article] = {
            "article": article,
            "first_seen_at": existing.get("first_seen_at") or now,
            "updated_at": now,
            "event_type": event_type,
            "events": int(existing.get("events", 0)) + 1,
            "status": "dirty",
            "message": "Очікує оновлення в Хорошопі.",
            "image_count": image_count,
        }

    def mark_result(
        self,
        *,
        article: str,
        status: str,
        message: str,
        remote_article: str = "",
        image_count: int | None = None,
        clear_dirty: bool = False,
    ) -> None:
        article = normalize_article(article)
        if not article:
            return
        item = self.dirty.get(article, {"article": article})
        item.update(
            {
                "updated_at": timestamp(),
                "status": status,
                "message": message,
                "remote_article": remote_article,
                "image_count": image_count,
            }
        )
        self.history.append(dict(item))
        self.history = self.history[-STATE_HISTORY_LIMIT:]
        if clear_dirty:
            self.dirty.pop(article, None)
        else:
            self.dirty[article] = item

    def clear_dirty(self) -> None:
        self.dirty.clear()

    def set_catalog_products(self, products: list[dict[str, Any]]) -> None:
        self.catalog_products = [
            item for item in products if isinstance(item, dict)
        ][:CATALOG_CACHE_MAX_ITEMS]
        self.catalog_updated_at = timestamp()

    def catalog_meta(self) -> dict[str, Any]:
        return {
            "updated_at": self.catalog_updated_at,
            "products_count": len(self.catalog_products),
            "has_cache": bool(self.catalog_products),
        }


def get_state() -> SyncState:
    if STATE is None:
        raise RuntimeError("Стан синхронізації ще не ініціалізовано.")
    return STATE


def cleanup_jobs() -> None:
    cutoff = current_epoch() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        expired = [
            job_id
            for job_id, item in JOBS.items()
            if float(item.get("updated_epoch", 0)) < cutoff
        ]
        for job_id in expired:
            JOBS.pop(job_id, None)


def set_job(job_id: str, **updates: Any) -> None:
    cleanup_jobs()
    with JOBS_LOCK:
        item = JOBS.get(job_id, {})
        item.update(updates)
        item["updated_epoch"] = current_epoch()
        JOBS[job_id] = item


def get_job(job_id: str) -> dict[str, Any]:
    cleanup_jobs()
    with JOBS_LOCK:
        item = dict(JOBS.get(job_id, {}))
    if not item:
        return {"status": "unknown", "percent": 0, "message": "Завдання не знайдено."}
    return item


def protected_json(_: Request) -> None:
    return


def image_articles_from_event(event: FileSystemEvent, allowed_extensions: set[str]) -> set[str]:
    paths = [event.src_path]
    destination = getattr(event, "dest_path", "")
    if destination:
        paths.append(destination)

    articles: set[str] = set()
    for path in paths:
        file_path = Path(path)
        suffixes = {suffix.lower() for suffix in file_path.suffixes}
        if suffixes & WATCH_IGNORED_SUFFIXES:
            continue
        if file_path.suffix.lower() not in allowed_extensions:
            continue
        article, _ = parse_filename(file_path)
        if article:
            articles.add(article)
    return articles


def image_counts(xml_products: dict[str, list[str]]) -> dict[str, int]:
    return {article: len(urls) for article, urls in xml_products.items()}


def credentials_from_form(form: Any) -> dict[str, str]:
    return {
        "login": str(form.get("login", "")).strip(),
        "password": str(form.get("password", "")),
        "token": str(form.get("token", "")).strip(),
    }


def fresh_catalog_from_form(form: Any) -> bool:
    return str(form.get("fresh_catalog", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def unique_articles(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        article = normalize_article(value)
        if not article:
            continue
        key = article.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(article)
    return result


def parse_excel_articles(data: bytes) -> list[str]:
    workbook = load_workbook(
        io.BytesIO(data),
        read_only=True,
        data_only=True,
    )
    try:
        worksheet = workbook.worksheets[0]
        values: list[str] = []
        for row in worksheet.iter_rows(values_only=True):
            if not row:
                continue
            value = row[0]
            if value is None:
                continue
            text = normalize_article(value)
            if not text:
                continue
            if text.casefold() in {
                "article",
                "артикул",
                "артикул для відображення",
                "артикул для отображения",
            }:
                continue
            values.append(text)
        return unique_articles(values)
    finally:
        workbook.close()


def load_or_refresh_catalog(
    *,
    client: HoroshopClient,
    force_refresh: bool,
    progress: Any | None = None,
) -> tuple[CatalogIndex, dict[str, Any]]:
    with STATE_LOCK:
        state = get_state()
        cached_products = list(state.catalog_products)
        cached_meta = state.catalog_meta()

    if cached_products and not force_refresh:
        return CatalogIndex.from_raw(cached_products), {
            **cached_meta,
            "source": "cache",
        }

    if progress:
        progress(0, "Завантаження свіжого каталогу Хорошопа...")
    products = client.export_catalog(progress=progress)
    with STATE_LOCK:
        state = get_state()
        state.set_catalog_products(products)
        state.save()
        meta = state.catalog_meta()
    return CatalogIndex.from_raw(products), {**meta, "source": "fresh"}


class SyncChangeHandler(FileSystemEventHandler):
    def __init__(self, config: dict[str, Any]):
        super().__init__()
        self.config = config
        self.allowed_extensions: set[str] = config["allowed_extensions"]
        self._build_lock = threading.Lock()

    def _handle(self, event: FileSystemEvent) -> None:
        articles = image_articles_from_event(event, self.allowed_extensions)
        if not articles and not event.is_directory:
            return
        if not self._build_lock.acquire(blocking=False):
            return

        try:
            event_type = EVENT_TYPE_LABELS.get(event.event_type, event.event_type)
            paths = [event.src_path]
            destination = getattr(event, "dest_path", "")
            if destination:
                paths.append(destination)
            log(f"Зміна файлів: {event_type} | " + " -> ".join(paths))
            build_xml(self.config)
            xml_products = load_xml_products(self.config["output_xml"])
            counts = image_counts(xml_products)

            if articles:
                with STATE_LOCK:
                    state = get_state()
                    for article in articles:
                        state.mark_dirty(article, event_type, counts.get(article, 0))
                    state.save()
                log(
                    "Зафіксовано зміни для артикулів: "
                    + ", ".join(sorted(articles, key=str.casefold))
                )
        except Exception as error:
            log(f"Помилка оновлення XML/черги: {error}")
        finally:
            self._build_lock.release()

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._handle(event)


def start_observer() -> Observer:
    build_xml(XML_CONFIG)
    observer = Observer()
    observer.schedule(SyncChangeHandler(XML_CONFIG), str(XML_CONFIG["images_dir"]), recursive=True)
    observer.start()
    log(f"Веб-сервер і відстеження змін запущено: {XML_CONFIG['images_dir']}")
    return observer


def sync_job(
    job_id: str,
    mode: str,
    credentials: dict[str, Any],
    requested_articles: list[str] | None = None,
    fresh_catalog: bool = False,
) -> None:
    base_settings = HOROSHOP_SETTINGS
    if base_settings is None:
        set_job(
            job_id,
            status="error",
            percent=100,
            message="Налаштування Хорошопа не ініціалізовано.",
        )
        return

    if not SYNC_LOCK.acquire(blocking=False):
        set_job(
            job_id,
            status="error",
            percent=100,
            message="Інша синхронізація вже виконується.",
        )
        return

    try:
        settings = with_runtime_credentials(base_settings, credentials)
        set_job(job_id, status="running", percent=2, message="Оновлення XML...")
        build_xml(XML_CONFIG)
        xml_products = load_xml_products(XML_CONFIG["output_xml"])
        counts = image_counts(xml_products)

        with STATE_LOCK:
            dirty_articles = sorted(get_state().dirty, key=str.casefold)

        if mode == "dirty":
            target_articles = requested_articles or dirty_articles
        elif mode in {"article", "excel"}:
            target_articles = requested_articles or []
        elif mode == "all":
            target_articles = sorted(xml_products, key=str.casefold)
        else:
            raise ValueError("Невідомий режим синхронізації.")
        target_articles = unique_articles(target_articles)

        if not target_articles:
            set_job(job_id, status="done", percent=100, message="Немає артикулів для оновлення.")
            return

        client = HoroshopClient(settings)

        def export_progress(count: int, message: str) -> None:
            set_job(job_id, status="running", percent=10, message=message, exported=count)

        set_job(
            job_id,
            status="running",
            percent=8,
            message=(
                "Завантаження каталогу Хорошопа..."
                if fresh_catalog
                else "Підготовка каталогу Хорошопа..."
            ),
        )
        catalog, catalog_meta = load_or_refresh_catalog(
            client=client,
            force_refresh=fresh_catalog,
            progress=export_progress,
        )
        set_job(
            job_id,
            status="running",
            percent=25,
            message="Зіставлення артикулів...",
            catalog_products=len(catalog.products),
            catalog_source=catalog_meta.get("source"),
            catalog_updated_at=catalog_meta.get("updated_at"),
        )

        import_items: list[dict[str, Any]] = []
        local_by_remote: dict[str, str] = {}
        prepared_items: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        should_update_state = mode in {"dirty", "article", "excel"}
        for local_article in target_articles:
            match = catalog.match(local_article)
            item, reason = build_import_product(
                match=match,
                image_urls=xml_products.get(local_article, []),
                settings=settings,
            )
            if item is None:
                skipped.append({"article": local_article, "message": reason})
                if should_update_state:
                    with STATE_LOCK:
                        state = get_state()
                        state.mark_result(
                            article=local_article,
                            status="skipped",
                            message=reason,
                            image_count=counts.get(local_article, 0),
                        )
                        state.save()
                continue
            import_items.append(item)
            local_by_remote[item["article"]] = local_article
            prepared_items.append(
                {
                    "local_article": local_article,
                    "remote_article": item["article"],
                    "item": item,
                    "has_images": bool(
                        item.get(settings.image_field, {}).get("links")
                    ),
                }
            )

        if not import_items:
            set_job(
                job_id,
                status="done",
                percent=100,
                message="Немає підготовлених товарів для імпорту.",
                skipped=skipped,
                imported=0,
            )
            return

        imported = 0
        failed: list[dict[str, str]] = []
        batches = chunked(prepared_items, settings.batch_size)
        for index, batch in enumerate(batches, start=1):
            percent = 25 + ((index - 1) / len(batches)) * 70
            set_job(
                job_id,
                status="running",
                percent=round(percent, 1),
                message=f"Імпорт у Хорошоп: пакет {index} із {len(batches)}...",
                imported=imported,
                total=len(import_items),
            )
            if settings.override and settings.two_phase_replace:
                clear_items = [
                    build_clear_product(prepared["remote_article"], settings)
                    for prepared in batch
                ]
                clear_response = client.import_products(clear_items)
                clear_status = str(clear_response.get("status", "OK")).upper()
                clear_logs = import_log_by_article(clear_response)
                upload_items = [
                    force_append_upload(prepared["item"], settings)
                    for prepared in batch
                    if prepared["has_images"]
                ]
                response = (
                    client.import_products(upload_items)
                    if upload_items
                    else clear_response
                )
                response_status = str(response.get("status", "OK")).upper()
                logs = import_log_by_article(response)
            else:
                raw_batch = [prepared["item"] for prepared in batch]
                clear_status = ""
                clear_logs = {}
                response = client.import_products(raw_batch)
                response_status = str(response.get("status", "OK")).upper()
                logs = import_log_by_article(response)

            for prepared in batch:
                remote_article = prepared["remote_article"]
                local_article = prepared["local_article"]
                article_log = logs.get(remote_article, [])
                clear_success = (
                    True
                    if not clear_status
                    else import_article_succeeded(
                        clear_status,
                        clear_logs.get(remote_article, []),
                    )
                )
                if settings.override and settings.two_phase_replace and not prepared["has_images"]:
                    success = clear_success
                    article_log = clear_logs.get(remote_article, [])
                else:
                    success = clear_success and import_article_succeeded(
                        response_status,
                        article_log,
                    )
                if success:
                    imported += 1
                    if should_update_state:
                        with STATE_LOCK:
                            state = get_state()
                            state.mark_result(
                                article=local_article,
                                status="synced",
                                message="Зображення оновлено в Хорошопі.",
                                remote_article=remote_article,
                                image_count=counts.get(local_article, 0),
                                clear_dirty=True,
                            )
                            state.save()
                else:
                    message = "; ".join(
                        str(entry.get("message", entry)) for entry in article_log
                    ) or "; ".join(
                        str(entry.get("message", entry))
                        for entry in clear_logs.get(remote_article, [])
                    ) or f"Статус імпорту: {response_status}"
                    failed.append({"article": local_article, "message": message})
                    if should_update_state:
                        with STATE_LOCK:
                            state = get_state()
                            state.mark_result(
                                article=local_article,
                                status="error",
                                message=message,
                                remote_article=remote_article,
                                image_count=counts.get(local_article, 0),
                            )
                            state.save()

        set_job(
            job_id,
            status="done" if not failed else "warning",
            percent=100,
            message=(
                f"Готово. Оновлено: {imported}; пропущено: {len(skipped)}; "
                f"помилок: {len(failed)}."
            ),
            imported=imported,
            skipped=skipped,
            failed=failed,
            total=len(import_items),
            catalog_source=catalog_meta.get("source"),
            catalog_updated_at=catalog_meta.get("updated_at"),
        )
        log(
            f"Синхронізація Хорошоп завершена: mode={mode}, "
            f"updated={imported}, skipped={len(skipped)}, failed={len(failed)}"
        )
    except (HoroshopError, OSError, ValueError) as error:
        log(f"Помилка синхронізації Хорошоп: {error}")
        set_job(job_id, status="error", percent=100, message=str(error))
    finally:
        SYNC_LOCK.release()


def start_sync(
    mode: str,
    credentials: dict[str, Any],
    articles: list[str] | None = None,
    fresh_catalog: bool = False,
) -> dict[str, str]:
    job_id = secrets.token_hex(12)
    set_job(job_id, status="queued", percent=0, message="Очікування запуску...")
    thread = threading.Thread(
        target=sync_job,
        args=(job_id, mode, credentials, articles, fresh_catalog),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


def refresh_catalog_job(job_id: str, credentials: dict[str, Any]) -> None:
    base_settings = HOROSHOP_SETTINGS
    if base_settings is None:
        set_job(
            job_id,
            status="error",
            percent=100,
            message="Налаштування Хорошопа не ініціалізовано.",
        )
        return

    if not SYNC_LOCK.acquire(blocking=False):
        set_job(
            job_id,
            status="error",
            percent=100,
            message="Інша операція вже виконується.",
        )
        return

    try:
        settings = with_runtime_credentials(base_settings, credentials)
        client = HoroshopClient(settings)

        def export_progress(count: int, message: str) -> None:
            set_job(job_id, status="running", percent=50, message=message, exported=count)

        set_job(job_id, status="running", percent=5, message="Старт експорту каталогу Хорошопа...")
        catalog, meta = load_or_refresh_catalog(
            client=client,
            force_refresh=True,
            progress=export_progress,
        )
        set_job(
            job_id,
            status="done",
            percent=100,
            message=f"Каталог оновлено: товарів {len(catalog.products)}.",
            catalog_products=len(catalog.products),
            catalog_updated_at=meta.get("updated_at"),
        )
        log(f"Кеш каталогу Хорошопа оновлено: товарів {len(catalog.products)}")
    except (HoroshopError, OSError, ValueError) as error:
        log(f"Помилка оновлення кешу каталогу Хорошопа: {error}")
        set_job(job_id, status="error", percent=100, message=str(error))
    finally:
        SYNC_LOCK.release()


def start_catalog_refresh(credentials: dict[str, Any]) -> dict[str, str]:
    job_id = secrets.token_hex(12)
    set_job(job_id, status="queued", percent=0, message="Очікування запуску...")
    thread = threading.Thread(
        target=refresh_catalog_job,
        args=(job_id, credentials),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


def state_snapshot() -> dict[str, Any]:
    xml_meta: dict[str, Any] = {}
    output_xml = XML_CONFIG.get("output_xml")
    if isinstance(output_xml, Path) and output_xml.exists():
        xml_meta = {
            "path": str(output_xml),
            "updated_at": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(output_xml.stat().st_mtime),
            ),
            "bytes": output_xml.stat().st_size,
        }
    with STATE_LOCK:
        state = get_state()
        dirty = sorted(state.dirty.values(), key=lambda item: str(item.get("article", "")).casefold())
        history = list(reversed(state.history[-50:]))
        catalog_meta = state.catalog_meta()
    return {
        "busy": SYNC_LOCK.locked(),
        "dirty_count": len(dirty),
        "dirty": dirty,
        "history": history,
        "catalog": catalog_meta,
        "xml": xml_meta,
        "image_field": HOROSHOP_SETTINGS.image_field if HOROSHOP_SETTINGS else "",
        "import_mode": "replace" if HOROSHOP_SETTINGS and HOROSHOP_SETTINGS.override else "append",
        "remove_all_when_no_local_images": (
            HOROSHOP_SETTINGS.remove_all_when_no_local_images
            if HOROSHOP_SETTINGS
            else False
        ),
    }


def render_page() -> str:
    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Python Scan - Horoshop Sync</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --ink: #17202a;
      --muted: #637083;
      --line: #cbd3df;
      --accent: #166534;
      --accent-strong: #14532d;
      --warn: #a16207;
      --bad: #b91c1c;
      --panel: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }}
    h1 {{ margin: 0; font-size: 22px; letter-spacing: 0; }}
    main {{ padding: 20px 24px 36px; display: grid; gap: 18px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .panel {{
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .fields {{ display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 10px; }}
    label {{ display: grid; gap: 6px; font-weight: 700; color: #334155; }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-strong); }}
    button.secondary {{ background: #334155; }}
    button.danger {{ background: var(--bad); }}
    button.small {{ padding: 7px 10px; font-size: 13px; }}
    button:disabled {{ background: #94a3b8; cursor: wait; }}
    .status {{
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      white-space: pre-wrap;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 19px; }}
    .table-wrap {{ overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 860px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; }}
    th {{ background: #eef2f7; font-size: 13px; color: #334155; }}
    tr:last-child td {{ border-bottom: 0; }}
    .muted {{ color: var(--muted); }}
    .status-dirty {{ color: var(--warn); font-weight: 700; }}
    .status-error {{ color: var(--bad); font-weight: 700; }}
    .status-skipped {{ color: #475569; font-weight: 700; }}
    .empty {{ padding: 18px; color: var(--muted); }}
    @media (max-width: 760px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      main {{ padding: 16px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .fields {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Horoshop Sync</h1>
      <div class="muted">Локальна панель без окремого пароля</div>
    </div>
    <div class="toolbar">
      <button id="refreshButton" class="secondary" type="button">Оновити статус</button>
      <button id="rebuildButton" class="secondary" type="button">Перебудувати XML</button>
      <button id="syncDirtyButton" type="button">Оновити змінені артикули</button>
      <button id="syncAllButton" class="danger" type="button">Повне оновлення</button>
    </div>
  </header>
  <main>
    <section class="panel">
      <div class="fields">
        <label>Логін Хорошоп
          <input id="shopLogin" autocomplete="username" placeholder="api@example.com">
        </label>
        <label>Пароль Хорошоп
          <input id="shopPassword" type="password" autocomplete="current-password">
        </label>
        <label>Excel зі списком артикулів
          <input id="excelFile" type="file" accept=".xlsx,.xlsm">
        </label>
      </div>
      <div class="toolbar">
        <label style="display:flex;align-items:center;gap:8px;font-weight:700;">
          <input id="freshCatalog" type="checkbox" style="width:auto;">
          Зробити свіжий експорт каталогу перед запуском
        </label>
        <button id="refreshCatalogButton" class="secondary" type="button">Згенерувати імпорт сайту</button>
        <button id="syncExcelButton" type="button">Оновити артикули з Excel</button>
      </div>
      <div id="catalogStatus" class="muted">Імпорт сайту ще не завантажено.</div>
      <div class="muted">В Excel читається перший стовпець першого аркуша: артикул для відображення на сайті.</div>
    </section>
    <div class="grid">
      <div class="metric"><span>Змінені артикули</span><strong id="dirtyCount">0</strong></div>
      <div class="metric"><span>Стан синхронізації</span><strong id="busyState">-</strong></div>
      <div class="metric"><span>Галерея</span><strong id="imageField">-</strong></div>
      <div class="metric"><span>Режим</span><strong id="importMode">-</strong></div>
    </div>
    <div id="status" class="status">Завантаження...</div>
    <section>
      <h2>Черга змін</h2>
      <div id="dirtyTable" class="table-wrap"><div class="empty">Завантаження...</div></div>
    </section>
    <section>
      <h2>Останні події</h2>
      <div id="historyTable" class="table-wrap"><div class="empty">Завантаження...</div></div>
    </section>
  </main>
  <script>
    const statusBox = document.getElementById('status');
    const dirtyCount = document.getElementById('dirtyCount');
    const busyState = document.getElementById('busyState');
    const imageField = document.getElementById('imageField');
    const importMode = document.getElementById('importMode');
    const dirtyTable = document.getElementById('dirtyTable');
    const historyTable = document.getElementById('historyTable');
    const shopLogin = document.getElementById('shopLogin');
    const shopPassword = document.getElementById('shopPassword');
    const excelFile = document.getElementById('excelFile');
    const freshCatalog = document.getElementById('freshCatalog');
    const catalogStatus = document.getElementById('catalogStatus');
    const buttons = [
      'refreshButton', 'rebuildButton', 'syncDirtyButton', 'syncAllButton',
      'refreshCatalogButton', 'syncExcelButton'
    ].map((id) => document.getElementById(id));
    let activeJob = '';

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function setBusy(isBusy) {{
      buttons.forEach((button) => {{
        if (button.id !== 'refreshButton') button.disabled = isBusy;
      }});
    }}

    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{
        cache: 'no-store',
        headers: {{ 'Accept': 'application/json' }},
        ...options
      }});
      if (!response.ok) {{
        let message = response.statusText;
        try {{
          const payload = await response.json();
          message = payload.detail || message;
        }} catch (_) {{}}
        throw new Error(message);
      }}
      return response.json();
    }}

    function credentialFormData() {{
      const login = shopLogin.value.trim();
      const password = shopPassword.value;
      if (!login || !password) {{
        throw new Error('Введіть логін і пароль Хорошопа.');
      }}
      const data = new FormData();
      data.append('login', login);
      data.append('password', password);
      data.append('fresh_catalog', freshCatalog.checked ? '1' : '0');
      return data;
    }}

    function requireFullUpdateCredentials() {{
      const login = shopLogin.value.trim();
      const password = shopPassword.value;
      if (!login || !password) {{
        throw new Error('Введіть логін і пароль Хорошопа.');
      }}
      const repeatedLogin = window.prompt('Повторно введіть логін Хорошопа для повного оновлення:') || '';
      if (repeatedLogin.trim() !== login) {{
        throw new Error('Повторний логін не збігається. Повне оновлення скасовано.');
      }}
      const repeatedPassword = window.prompt('Повторно введіть пароль Хорошопа для повного оновлення:') || '';
      if (repeatedPassword !== password) {{
        throw new Error('Повторний пароль не збігається. Повне оновлення скасовано.');
      }}
    }}

    function renderTable(target, rows) {{
      if (!rows.length) {{
        target.innerHTML = '<div class="empty">Немає записів.</div>';
        return;
      }}
      let markup = '<table><thead><tr>' +
        '<th>Артикул</th><th>Статус</th><th>Фото</th><th>Оновлено</th><th>Повідомлення</th><th>Дія</th>' +
        '</tr></thead><tbody>';
      for (const row of rows) {{
        const status = escapeHtml(row.status || '');
        const article = escapeHtml(row.article || '');
        markup += '<tr>' +
          '<td><strong>' + article + '</strong>' +
          (row.remote_article ? '<div class="muted">H: ' + escapeHtml(row.remote_article) + '</div>' : '') +
          '</td>' +
          '<td class="status-' + status + '">' + status + '</td>' +
          '<td>' + escapeHtml(row.image_count ?? '') + '</td>' +
          '<td>' + escapeHtml(row.updated_at || row.first_seen_at || '') + '</td>' +
          '<td>' + escapeHtml(row.message || '') + '</td>' +
          '<td><button class="small article-sync" type="button" data-article="' + article + '">Оновити</button></td>' +
          '</tr>';
      }}
      markup += '</tbody></table>';
      target.innerHTML = markup;
      target.querySelectorAll('.article-sync').forEach((button) => {{
        button.addEventListener('click', async () => {{
          try {{
            const data = credentialFormData();
            const article = button.getAttribute('data-article') || '';
            const result = await api('/api/sync/article/' + encodeURIComponent(article), {{
              method: 'POST',
              body: data
            }});
            pollJob(result.job_id);
          }} catch (error) {{
            statusBox.textContent = error.message;
          }}
        }});
      }});
    }}

    async function refreshState() {{
      const state = await api('/api/state');
      dirtyCount.textContent = state.dirty_count;
      busyState.textContent = state.busy ? 'Працює' : 'Вільно';
      imageField.textContent = state.image_field || '-';
      importMode.textContent = state.import_mode || '-';
      renderTable(dirtyTable, state.dirty || []);
      renderTable(historyTable, state.history || []);
      const xml = state.xml || {{}};
      const catalog = state.catalog || {{}};
      catalogStatus.textContent = catalog.has_cache
        ? 'Імпорт сайту: ' + (catalog.products_count || 0) +
          ' товарів, оновлено ' + (catalog.updated_at || '-')
        : 'Імпорт сайту ще не створено. Перед першим оновленням буде виконано свіжий експорт каталогу.';
      statusBox.textContent =
        'XML: ' + (xml.path || '-') + '\\n' +
        'Оновлено: ' + (xml.updated_at || '-') + '\\n' +
        'Розмір: ' + (xml.bytes || 0) + ' байт';
      setBusy(Boolean(state.busy || activeJob));
    }}

    async function pollJob(jobId) {{
      activeJob = jobId;
      setBusy(true);
      while (activeJob === jobId) {{
        const job = await api('/api/progress/' + encodeURIComponent(jobId));
        statusBox.textContent = Math.round(job.percent || 0) + '% - ' + (job.message || '');
        if (job.status === 'done' || job.status === 'warning' || job.status === 'error') {{
          activeJob = '';
          await refreshState();
          return;
        }}
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }}
    }}

    document.getElementById('refreshButton').addEventListener('click', refreshState);
    document.getElementById('rebuildButton').addEventListener('click', async () => {{
      setBusy(true);
      try {{
        const result = await api('/api/rebuild', {{ method: 'POST' }});
        statusBox.textContent = result.message;
        await refreshState();
      }} catch (error) {{
        statusBox.textContent = error.message;
      }} finally {{
        setBusy(false);
      }}
    }});
    document.getElementById('syncDirtyButton').addEventListener('click', async () => {{
      try {{
        const result = await api('/api/sync/dirty', {{ method: 'POST', body: credentialFormData() }});
        pollJob(result.job_id);
      }} catch (error) {{
        statusBox.textContent = error.message;
      }}
    }});
    document.getElementById('syncAllButton').addEventListener('click', async () => {{
      const warning = [
        'Повне оновлення пройде по всіх артикулах із XML.',
        'У режимі replace галереї товарів будуть очищені й завантажені заново.',
        'Якщо в XML неповний набір фото, на сайті залишиться саме неповний набір.',
        'Для контрольованого оновлення краще використати Excel-файл зі списком потрібних артикулів.'
      ].join('\\n');
      if (!window.confirm(warning)) return;
      const typed = window.prompt('Для запуску введіть: ПОВНЕ ОНОВЛЕННЯ');
      if (typed !== 'ПОВНЕ ОНОВЛЕННЯ') {{
        statusBox.textContent = 'Повне оновлення скасовано.';
        return;
      }}
      try {{
        requireFullUpdateCredentials();
        const result = await api('/api/sync/all', {{ method: 'POST', body: credentialFormData() }});
        pollJob(result.job_id);
      }} catch (error) {{
        statusBox.textContent = error.message;
      }}
    }});
    document.getElementById('syncExcelButton').addEventListener('click', async () => {{
      try {{
        if (!excelFile.files.length) throw new Error('Виберіть Excel-файл зі списком артикулів.');
        const data = credentialFormData();
        data.append('file', excelFile.files[0], excelFile.files[0].name);
        const result = await api('/api/sync/excel', {{
          method: 'POST',
          body: data
        }});
        pollJob(result.job_id);
      }} catch (error) {{
        statusBox.textContent = error.message;
      }}
    }});
    document.getElementById('refreshCatalogButton').addEventListener('click', async () => {{
      try {{
        const data = credentialFormData();
        const result = await api('/api/catalog/refresh', {{
          method: 'POST',
          body: data
        }});
        pollJob(result.job_id);
      }} catch (error) {{
        statusBox.textContent = error.message;
      }}
    }});

    refreshState().catch((error) => {{
      statusBox.textContent = error.message;
    }});
    window.setInterval(() => {{
      if (!activeJob) refreshState().catch(() => {{}});
    }}, 10000);
  </script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    global OBSERVER
    OBSERVER = start_observer()
    try:
        yield
    finally:
        if OBSERVER is not None:
            OBSERVER.stop()
            OBSERVER.join()
            OBSERVER = None


app = FastAPI(title="Python Scan Horoshop Sync", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    return render_page()


@app.get("/status")
def public_status() -> dict[str, Any]:
    return {
        "ok": True,
        "busy": SYNC_LOCK.locked(),
        "dirty_count": len(get_state().dirty) if STATE is not None else 0,
        "server": {
            "port": SERVER_SETTINGS.get("port"),
            "mode": "horoshop_sync",
        },
    }


@app.get("/api/state")
def api_state(request: Request) -> dict[str, Any]:
    protected_json(request)
    return state_snapshot()


@app.post("/api/rebuild")
def api_rebuild(request: Request) -> dict[str, Any]:
    protected_json(request)
    product_count, image_count = build_xml(XML_CONFIG)
    return {
        "ok": True,
        "message": f"XML перебудовано: артикулів {product_count}, фото {image_count}.",
    }


@app.post("/api/sync/dirty")
async def api_sync_dirty(request: Request) -> dict[str, str]:
    protected_json(request)
    form = await request.form()
    return start_sync(
        "dirty",
        credentials_from_form(form),
        fresh_catalog=fresh_catalog_from_form(form),
    )


@app.post("/api/sync/all")
async def api_sync_all(request: Request) -> dict[str, str]:
    protected_json(request)
    form = await request.form()
    return start_sync(
        "all",
        credentials_from_form(form),
        fresh_catalog=fresh_catalog_from_form(form),
    )


@app.post("/api/sync/article/{article}")
async def api_sync_article(article: str, request: Request) -> dict[str, str]:
    protected_json(request)
    form = await request.form()
    return start_sync(
        "article",
        credentials_from_form(form),
        [article],
        fresh_catalog=fresh_catalog_from_form(form),
    )


@app.post("/api/sync/excel")
async def api_sync_excel(request: Request) -> dict[str, Any]:
    protected_json(request)
    form = await request.form()
    uploaded = form.get("file")
    if not isinstance(uploaded, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="Excel-файл не передано.")
    filename = uploaded.filename or ""
    if Path(filename).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Підтримуються лише .xlsx та .xlsm.")
    data = await uploaded.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Excel-файл більший за 20 МБ.")
    try:
        articles = parse_excel_articles(data)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Не вдалося прочитати Excel: {error}") from error
    if not articles:
        raise HTTPException(status_code=400, detail="В Excel не знайдено артикулів у першому стовпці.")
    result = start_sync(
        "excel",
        credentials_from_form(form),
        articles,
        fresh_catalog=fresh_catalog_from_form(form),
    )
    result["articles_count"] = len(articles)
    return result


@app.post("/api/catalog/refresh")
async def api_catalog_refresh(request: Request) -> dict[str, str]:
    protected_json(request)
    form = await request.form()
    return start_catalog_refresh(credentials_from_form(form))


@app.get("/api/progress/{job_id}")
def api_progress(job_id: str, request: Request) -> dict[str, Any]:
    protected_json(request)
    return get_job(job_id)


@app.post("/api/dirty/{article}")
def api_mark_dirty(article: str, request: Request) -> dict[str, Any]:
    protected_json(request)
    with STATE_LOCK:
        state = get_state()
        try:
            xml_products = load_xml_products(XML_CONFIG["output_xml"])
        except OSError:
            xml_products = {}
        state.mark_dirty(article, "manual", len(xml_products.get(article, [])))
        state.save()
    return {"ok": True}


@app.post("/api/clear-dirty")
def api_clear_dirty(request: Request) -> dict[str, Any]:
    protected_json(request)
    with STATE_LOCK:
        state = get_state()
        state.clear_dirty()
        state.save()
    return {"ok": True}


def configure_runtime(config_file: Path) -> None:
    global CONFIG_FILE, RAW_CONFIG, XML_CONFIG, HOROSHOP_SETTINGS, SERVER_SETTINGS, STATE
    CONFIG_FILE = config_file
    configure_console_encoding()
    RAW_CONFIG = read_raw_config(config_file)
    XML_CONFIG = load_config(config_file)
    HOROSHOP_SETTINGS = load_horoshop_settings(RAW_CONFIG)
    SERVER_SETTINGS = load_server_settings(RAW_CONFIG, XML_CONFIG)
    initialize_reliable_clock()
    configure_logging(XML_CONFIG)
    with STATE_LOCK:
        STATE = SyncState(SERVER_SETTINGS["state_file"])
    log(
        "Horoshop Sync ініціалізовано: "
        f"порт {SERVER_SETTINGS['port']}, галерея {HOROSHOP_SETTINGS.image_field}."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Локальна веб-панель синхронізації зображень із Хорошопом."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="Шлях до config.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        configure_runtime(args.config.resolve())
    except Exception as error:
        print(f"Помилка конфігурації: {error}", flush=True)
        return 1

    uvicorn.run(
        app,
        host=SERVER_SETTINGS["host"],
        port=SERVER_SETTINGS["port"],
        log_level="info",
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
