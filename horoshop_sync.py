from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests


DEFAULT_LIMIT = 500
DEFAULT_BATCH_SIZE = 50
SUCCESS_IMPORT_CODES = {0, 22, 28}
IMAGE_ERROR_CODES = {23, 24, 25, 26, 27}


class HoroshopError(RuntimeError):
    pass


@dataclass(frozen=True)
class HoroshopSettings:
    domain: str
    login: str = ""
    password: str = ""
    token: str = ""
    auth_endpoint: str = "/api/auth/"
    export_endpoint: str = "/api/catalog/export/"
    import_endpoint: str = "/api/catalog/import/"
    request_timeout_seconds: int = 60
    export_limit: int = DEFAULT_LIMIT
    batch_size: int = DEFAULT_BATCH_SIZE
    image_field: str = "images"
    override: bool = True
    two_phase_replace: bool = True
    remove_all_when_no_local_images: bool = False


@dataclass(frozen=True)
class CatalogProduct:
    article: str
    parent_article: str
    article_for_display: str
    images: tuple[str, ...]
    gallery_common: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class MatchResult:
    status: str
    local_article: str
    product: CatalogProduct | None = None
    message: str = ""


def read_raw_config(config_file: Path) -> dict[str, Any]:
    with config_file.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("config.json повинен містити JSON-об'єкт.")
    return data


def load_horoshop_settings(raw: dict[str, Any]) -> HoroshopSettings:
    horoshop = raw.get("horoshop") or {}
    if not isinstance(horoshop, dict):
        raise ValueError("Секція horoshop у config.json повинна бути об'єктом.")

    domain = str(horoshop.get("domain", "")).strip()
    if not domain:
        raise ValueError("У config.json не заповнено horoshop.domain.")

    token = str(horoshop.get("token", "")).strip()
    login = str(horoshop.get("login", "")).strip()
    password = str(horoshop.get("password", ""))

    image_field = str(horoshop.get("image_field", "images")).strip()
    if image_field not in {"images", "gallery_common", "gallery_360"}:
        raise ValueError(
            "horoshop.image_field повинен бути images, gallery_common або gallery_360."
        )

    mode = str(horoshop.get("import_mode", "replace")).strip().lower()
    if mode not in {"replace", "append"}:
        raise ValueError("horoshop.import_mode повинен бути replace або append.")

    return HoroshopSettings(
        domain=domain.rstrip("/"),
        login=login,
        password=password,
        token=token,
        auth_endpoint=str(horoshop.get("auth_endpoint", "/api/auth/")),
        export_endpoint=str(horoshop.get("export_endpoint", "/api/catalog/export/")),
        import_endpoint=str(horoshop.get("import_endpoint", "/api/catalog/import/")),
        request_timeout_seconds=int(horoshop.get("request_timeout_seconds", 60)),
        export_limit=min(DEFAULT_LIMIT, int(horoshop.get("export_limit", DEFAULT_LIMIT))),
        batch_size=max(1, int(horoshop.get("batch_size", DEFAULT_BATCH_SIZE))),
        image_field=image_field,
        override=(mode == "replace"),
        two_phase_replace=bool(horoshop.get("two_phase_replace", True)),
        remove_all_when_no_local_images=bool(
            horoshop.get("remove_all_when_no_local_images", False)
        ),
    )


def with_runtime_credentials(
    settings: HoroshopSettings,
    credentials: dict[str, Any],
) -> HoroshopSettings:
    token = str(credentials.get("token", "")).strip()
    login = str(credentials.get("login", "")).strip()
    password = str(credentials.get("password", ""))

    resolved = replace(
        settings,
        token=token or settings.token,
        login=login or settings.login,
        password=password or settings.password,
    )
    if not resolved.token and (not resolved.login or not resolved.password):
        raise ValueError("Введіть логін і пароль Хорошопа на сторінці.")
    return resolved


def endpoint_url(domain: str, endpoint: str) -> str:
    return urljoin(f"{domain.rstrip('/')}/", endpoint.lstrip("/"))


class HoroshopClient:
    def __init__(
        self,
        settings: HoroshopSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self._token = settings.token

    def token(self) -> str:
        if self._token:
            return self._token

        response = self._post(
            self.settings.auth_endpoint,
            {
                "login": self.settings.login,
                "password": self.settings.password,
            },
            include_token=False,
        )
        token = (
            response.get("response", {}).get("token")
            if isinstance(response.get("response"), dict)
            else None
        )
        if not token:
            raise HoroshopError("Hорошоп не повернув token після auth.")

        self._token = str(token)
        return self._token

    def export_products(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        included_params: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "token": self.token(),
            "offset": offset,
            "limit": limit or self.settings.export_limit,
        }
        if included_params:
            payload["includedParams"] = included_params

        response = self._post(self.settings.export_endpoint, payload, include_token=False)
        products = response.get("response", {}).get("products")
        if not isinstance(products, list):
            raise HoroshopError("Некоректна відповідь catalog/export: немає products.")
        return products

    def export_catalog(
        self,
        progress: Callable[[int, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        included = [
            "article_for_display",
            "images",
            "gallery_common",
            "gallery_360",
        ]
        limit = self.settings.export_limit
        offset = 0
        products: list[dict[str, Any]] = []

        while True:
            page = self.export_products(
                offset=offset,
                limit=limit,
                included_params=included,
            )
            products.extend(page)
            if progress:
                progress(len(products), f"Експортовано товарів із Хорошопа: {len(products)}")
            if len(page) < limit:
                return products
            offset += limit

    def import_products(self, products: list[dict[str, Any]]) -> dict[str, Any]:
        if not products:
            return {"status": "OK", "response": {"log": []}}

        return self._post(
            self.settings.import_endpoint,
            {"token": self.token(), "products": products},
            include_token=False,
        )

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        include_token: bool,
    ) -> dict[str, Any]:
        if include_token:
            payload = dict(payload)
            payload["token"] = self.token()

        url = endpoint_url(self.settings.domain, endpoint)
        try:
            response = self.session.post(
                url,
                json=payload,
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            raise HoroshopError(f"HTTP-помилка Хорошоп API: {error}") from error
        except ValueError as error:
            raise HoroshopError("Hорошоп повернув не JSON-відповідь.") from error

        if not isinstance(data, dict):
            raise HoroshopError("Hорошоп повернув JSON не у форматі об'єкта.")
        status = str(data.get("status", "")).upper()
        if status in {"ERROR", "EXCEPTION"}:
            raise HoroshopError(f"Hорошоп повернув статус {status}: {data}")
        if status and status not in {"OK", "WARNING"}:
            raise HoroshopError(f"Невідомий статус Хорошоп API: {status}")
        return data


def normalize_article(value: Any) -> str:
    return str(value or "").strip()


def strings_from_gallery(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value if isinstance(item, str) and item)
    return ()


def catalog_product_from_raw(raw: dict[str, Any]) -> CatalogProduct | None:
    article = normalize_article(raw.get("article"))
    if not article:
        return None
    return CatalogProduct(
        article=article,
        parent_article=normalize_article(raw.get("parent_article")),
        article_for_display=normalize_article(raw.get("article_for_display")),
        images=strings_from_gallery(raw.get("images")),
        gallery_common=strings_from_gallery(raw.get("gallery_common")),
        raw=raw,
    )


class CatalogIndex:
    def __init__(self, products: list[CatalogProduct]):
        self.products = products
        self.by_article = {product.article: product for product in products}
        self.by_display: dict[str, list[CatalogProduct]] = {}
        for product in products:
            display = product.article_for_display
            if display:
                self.by_display.setdefault(display, []).append(product)

    @classmethod
    def from_raw(cls, raw_products: list[dict[str, Any]]) -> "CatalogIndex":
        products = [
            product
            for raw in raw_products
            if isinstance(raw, dict)
            for product in [catalog_product_from_raw(raw)]
            if product is not None
        ]
        return cls(products)

    def match(self, local_article: str) -> MatchResult:
        article = normalize_article(local_article)
        if not article:
            return MatchResult("missing", article, message="Порожній артикул.")

        direct = self.by_article.get(article)
        if direct is not None:
            return MatchResult("matched", article, product=direct)

        display_matches = self.by_display.get(article, [])
        if len(display_matches) == 1:
            return MatchResult("matched", article, product=display_matches[0])
        if len(display_matches) > 1:
            return MatchResult(
                "ambiguous",
                article,
                message=(
                    "article_for_display не унікальний: "
                    + ", ".join(product.article for product in display_matches[:10])
                ),
            )
        return MatchResult("missing", article, message="Не знайдено в Хорошопі.")


def load_xml_products(xml_file: Path) -> dict[str, list[str]]:
    if not xml_file.exists():
        raise FileNotFoundError(f"XML не знайдено: {xml_file}")

    root = ET.parse(xml_file).getroot()
    result: dict[str, list[str]] = {}
    for product_element in root.findall("product"):
        article = normalize_article(product_element.attrib.get("article"))
        if not article:
            continue
        urls = [
            normalize_article(image.text)
            for image in product_element.findall("image")
            if normalize_article(image.text)
        ]
        result[article] = urls
    return result


def build_import_product(
    *,
    match: MatchResult,
    image_urls: list[str],
    settings: HoroshopSettings,
) -> tuple[dict[str, Any] | None, str]:
    if match.status != "matched" or match.product is None:
        return None, match.message or "Товар не зіставлено."

    gallery_payload: dict[str, Any]
    if image_urls:
        gallery_payload = {
            "override": settings.override,
            "links": image_urls,
        }
    elif settings.remove_all_when_no_local_images:
        gallery_payload = {"removeAll": True}
    else:
        return None, "У локальному XML немає зображень; очищення вимкнено."

    return (
        {
            "article": match.product.article,
            settings.image_field: gallery_payload,
        },
        "",
    )


def build_clear_product(article: str, settings: HoroshopSettings) -> dict[str, Any]:
    return {
        "article": article,
        settings.image_field: {"removeAll": True},
    }


def force_append_upload(item: dict[str, Any], settings: HoroshopSettings) -> dict[str, Any]:
    copied = dict(item)
    gallery = dict(copied.get(settings.image_field, {}))
    if "links" in gallery:
        gallery["override"] = False
    copied[settings.image_field] = gallery
    return copied


def import_log_by_article(response: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    logs = response.get("response", {}).get("log", [])
    result: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(logs, list):
        return result
    for item in logs:
        if not isinstance(item, dict):
            continue
        article = normalize_article(item.get("article"))
        info = item.get("info", [])
        if article and isinstance(info, list):
            result[article] = [entry for entry in info if isinstance(entry, dict)]
    return result


def import_article_succeeded(
    response_status: str,
    article_log: list[dict[str, Any]] | None,
) -> bool:
    if response_status == "OK":
        return True
    if not article_log:
        return False

    codes: set[int] = set()
    non_duplicate_error = False
    for entry in article_log:
        try:
            code = int(entry.get("code"))
            codes.add(code)
        except (TypeError, ValueError):
            code = None
        message = str(entry.get("message", "")).casefold()
        is_duplicate = "дубликат" in message or "дублікат" in message
        if code in IMAGE_ERROR_CODES and not is_duplicate:
            non_duplicate_error = True

    if non_duplicate_error:
        return False
    return bool(codes & SUCCESS_IMPORT_CODES)


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def current_epoch() -> float:
    return time.time()
