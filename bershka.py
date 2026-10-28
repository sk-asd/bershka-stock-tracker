"""Watch Bershka TR sizes and prices; notify only when a saved observation changes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("bershka")
API_URL = "https://www.bershka.com/itxrest/3/catalog/store/44109521/40259537/productsArray"
OUT_OF_STOCK = {"SOLD_OUT", "COMING_SOON", "BACK_SOON"}
EVENT_TYPES = {"restock", "sold_out", "price_drop", "price_rise"}


class ResponseError(ValueError):
    pass


@dataclass(frozen=True)
class Variant:
    sku: str
    color_id: str
    color: str
    size: str
    status: str
    price: int | None

    def snapshot(self) -> dict:
        return {"status": self.status, "price_minor": self.price}


def minor_units(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = int(str(value))
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def product_index(payload: dict) -> dict[int, dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ResponseError("Expected a products array in the API response")
    products = {}
    for item in payload["products"]:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            products[item["id"]] = item
    return products


def colors_in(product: dict):
    yield from (product.get("detail") or {}).get("colors", [])
    for child in product.get("bundleProductSummaries", []):
        yield from colors_in(child)


def parse_variants(product: dict, selection: dict) -> list[Variant]:
    wanted_colors = {str(c) for c in selection.get("colors", [])}
    wanted_sizes = {s.strip().casefold() for s in selection.get("sizes", [])}
    variants = {}
    for color in colors_in(product):
        color_id = str(color.get("id", ""))
        if wanted_colors and color_id not in wanted_colors:
            continue
        for size in color.get("sizes", []):
            name = str(size.get("name") or "")
            if wanted_sizes and name.casefold() not in wanted_sizes:
                continue
            if size.get("sku") is None:
                continue
            variant = Variant(
                str(size["sku"]), color_id, color.get("name") or color_id,
                name, size.get("visibilityValue") or "UNKNOWN", minor_units(size.get("price")),
            )
            variants[variant.sku] = variant
    return list(variants.values())


def changes(variant: Variant, previous: dict | None, enabled: set[str]) -> list[str]:
    if previous is None:
        return []
    events = []
    old_status = previous.get("status")
    if "restock" in enabled and old_status in OUT_OF_STOCK and variant.status == "SHOW":
        events.append("back in stock")
    if "sold_out" in enabled and old_status == "SHOW" and variant.status == "SOLD_OUT":
        events.append("sold out")
    old_price = previous.get("price_minor")
    if old_price is not None and variant.price is not None:
        if "price_drop" in enabled and variant.price < old_price:
            events.append(f"price dropped: {old_price / 100:.2f} -> {variant.price / 100:.2f} TRY")
        if "price_rise" in enabled and variant.price > old_price:
            events.append(f"price increased: {old_price / 100:.2f} -> {variant.price / 100:.2f} TRY")
    return events


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods={"GET"}, respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_products(session: requests.Session, ids: list[int]) -> dict[int, dict]:
    response = session.get(API_URL, params={
        "productIds": ",".join(map(str, ids)), "languageId": -43, "appId": 1,
    }, timeout=(10, 30))
    response.raise_for_status()
    return product_index(response.json())


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def load_config(path: Path) -> dict:
    cfg = read_json(path)
    if not isinstance(cfg.get("products"), list) or not cfg["products"]:
        raise ValueError("Add at least one product to the configuration")
    if not isinstance(cfg.get("interval_seconds", 600), int) or cfg.get("interval_seconds", 600) < 30:
        raise ValueError("interval_seconds must be an integer of at least 30")
    ids = set()
    for product in cfg["products"]:
        if not isinstance(product, dict):
            raise ValueError("Each product must be a JSON object")
        pid = product.get("productId")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid in ids:
            raise ValueError("Each product needs a unique, positive integer productId")
        ids.add(pid)
        for field in ("colors", "sizes"):
            if not isinstance(product.get(field, []), list):
                raise ValueError(f"{field} must be a list")
        if any(not isinstance(s, str) for s in product.get("sizes", [])):
            raise ValueError("Size names must be strings")
        events = product.get("notify_on", ["restock", "price_drop"])
        if not isinstance(events, list) or any(not isinstance(e, str) or e not in EVENT_TYPES for e in events):
            raise ValueError("Unsupported notify_on event")
    return cfg


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def send_telegram(messages: list[str], token: str, chat_id: str) -> bool:
    chunks, chunk = [], ""
    for message in messages:
        if len(chunk) + len(message) + 2 > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk += message[:3500] + "\n"
    if chunk:
        chunks.append(chunk)
    for text in chunks:
        try:
            response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text.strip()}, timeout=(10, 20))
            if not response.ok or not response.json().get("ok"):
                LOG.error("Telegram rejected the notification (HTTP %s)", response.status_code)
                return False
        except (requests.RequestException, ValueError):
            LOG.error("Telegram delivery failed; keeping the previous state for a later retry")
            return False
    return True


def run_once(cfg: dict, session: requests.Session, state_path: Path,
             *, dry_run: bool = False, no_notify: bool = False) -> int:
    products = fetch_products(session, [p["productId"] for p in cfg["products"]])
    state = read_json(state_path) if state_path.exists() else {}
    updated = dict(state)
    messages = []
    failures = 0
    for selection in cfg["products"]:
        pid = selection["productId"]
        product = products.get(pid)
        if product is None:
            LOG.warning("Product %s was not returned; it may have been removed. Previous state kept.", pid)
            failures += 1
            continue
        variants = parse_variants(product, selection)
        if not variants:
            LOG.warning("Product %s has no matching sizes. Previous state kept.", pid)
            failures += 1
            continue
        name = product.get("name") or str(pid)
        previous = state.get(str(pid), {})
        snapshot = dict(previous)
        enabled = set(selection.get("notify_on", ["restock", "price_drop"]))
        for variant in variants:
            price = f"{variant.price / 100:.2f} TRY" if variant.price is not None else "price unavailable"
            LOG.info("%s | %s | %s | %s | %s", name, variant.color, variant.size, variant.status, price)
            for event in changes(variant, previous.get(variant.sku), enabled):
                messages.append(f"{name} / {variant.color} / {variant.size}: {event}\n"
                                f"https://www.bershka.com/tr/c0p{pid}.html?colorId={variant.color_id}")
            snapshot[variant.sku] = variant.snapshot()
        updated[str(pid)] = snapshot
    for message in messages:
        LOG.info("Change: %s", message)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not no_notify and bool(token) != bool(chat_id):
        raise ValueError("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")
    if dry_run:
        LOG.info("Dry run: no notifications sent, no state written")
        return int(failures > 0)
    if messages and not no_notify and token and chat_id:
        if not send_telegram(messages, token, chat_id):
            return 1
    save_state(state_path, updated)
    LOG.info("Check complete: %s change(s), %s unavailable product(s)", len(messages), failures)
    return int(failures > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bershka TR stock and price watcher")
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--watch", action="store_true", help="repeat checks until interrupted")
    parser.add_argument("--dry-run", action="store_true", help="read stock without saving state or sending messages")
    parser.add_argument("--no-notify", action="store_true", help="disable Telegram even if environment variables are set")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    with make_session() as session:
        while True:
            try:
                status = run_once(cfg, session, args.state, dry_run=args.dry_run, no_notify=args.no_notify)
            except KeyboardInterrupt:
                return 0
            except requests.RequestException as exc:
                LOG.error("Product request failed (%s)", type(exc).__name__)
                status = 1
            except (OSError, ValueError) as exc:
                LOG.error("Cannot complete check: %s", exc)
                status = 1
            if not args.watch:
                return status
            try:
                time.sleep(cfg.get("interval_seconds", 600))
            except KeyboardInterrupt:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
