import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bershka


def product():
    return {
        "id": 123, "name": "Test sweatshirt",
        "detail": {"colors": [{"id": "800", "name": "Black", "sizes": [
            {"sku": 456, "name": "M", "visibilityValue": "SHOW", "price": "189000"},
            {"sku": 457, "name": "L", "visibilityValue": "SOLD_OUT", "price": "189000"},
        ]}]},
    }


class ProductTests(unittest.TestCase):
    def test_not_found_entries_do_not_break_other_products(self):
        payload = {"products": [{"key": "_ERR_PRODUCT_NOT_FOUND", "description": "Item not found"}, product()]}
        self.assertEqual(set(bershka.product_index(payload)), {123})

    def test_unexpected_payload_is_not_an_empty_success(self):
        with self.assertRaises(bershka.ResponseError):
            bershka.product_index({"error": "unavailable"})

    def test_bundle_sizes_are_read_and_duplicates_are_removed(self):
        item = product()
        bundle = {"id": 999, "detail": {"colors": []}, "bundleProductSummaries": [item, copy.deepcopy(item)]}
        variants = bershka.parse_variants(bundle, {})
        self.assertEqual([v.sku for v in variants], ["456", "457"])
        self.assertEqual(variants[0].price, 189000)

    def test_color_and_case_insensitive_size_filters(self):
        self.assertEqual(len(bershka.parse_variants(product(), {"colors": [800], "sizes": ["m"]})), 1)
        self.assertEqual(bershka.parse_variants(product(), {"colors": [700]}), [])

    def test_first_observation_and_unknown_status_do_not_trigger_restock(self):
        variant = bershka.parse_variants(product(), {})[0]
        self.assertEqual(bershka.changes(variant, None, {"restock"}), [])
        self.assertEqual(bershka.changes(variant, {"status": "UNKNOWN"}, {"restock"}), [])
        self.assertEqual(bershka.changes(variant, {"status": "SOLD_OUT"}, {"restock"}), ["back in stock"])

    def test_missing_price_cannot_create_a_false_price_drop(self):
        item = product()
        item["detail"]["colors"][0]["sizes"][0]["price"] = None
        variant = bershka.parse_variants(item, {})[0]
        self.assertEqual(bershka.changes(variant, {"price_minor": 200000}, {"price_drop"}), [])

    def test_notification_preferences_are_respected(self):
        variant = bershka.parse_variants(product(), {})[0]
        old = {"status": "SOLD_OUT", "price_minor": 200000}
        events = bershka.changes(variant, old, {"price_drop"})
        self.assertEqual(len(events), 1)
        self.assertIn("2000.00 -> 1890.00", events[0])


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.cfg = {"products": [{"productId": 123}]}

    @patch("bershka.fetch_products", return_value={})
    def test_removed_product_keeps_its_previous_state(self, fetch):
        original = {"123": {"456": {"status": "SHOW", "price_minor": 189000}}}
        bershka.save_state(self.path, original)
        self.assertEqual(bershka.run_once(self.cfg, Mock(), self.path, no_notify=True), 1)
        self.assertEqual(bershka.read_json(self.path), original)

    @patch("bershka.send_telegram")
    @patch("bershka.fetch_products")
    def test_dry_run_neither_writes_nor_notifies(self, fetch, notify):
        fetch.return_value = {123: product()}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}):
            self.assertEqual(bershka.run_once(self.cfg, Mock(), self.path, dry_run=True), 0)
        self.assertFalse(self.path.exists())
        notify.assert_not_called()

    @patch("bershka.send_telegram", return_value=False)
    @patch("bershka.fetch_products")
    def test_failed_delivery_leaves_state_available_for_retry(self, fetch, notify):
        fetch.return_value = {123: product()}
        original = {"123": {"456": {"status": "SOLD_OUT", "price_minor": 189000}}}
        bershka.save_state(self.path, original)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}):
            self.assertEqual(bershka.run_once(self.cfg, Mock(), self.path), 1)
        self.assertEqual(bershka.read_json(self.path), original)
        notify.assert_called_once()

    @patch("bershka.fetch_products")
    def test_unchanged_second_check_does_not_send_a_notification(self, fetch):
        fetch.return_value = {123: product()}
        bershka.run_once(self.cfg, Mock(), self.path, no_notify=True)
        with patch("bershka.send_telegram") as notify:
            bershka.run_once(self.cfg, Mock(), self.path, no_notify=True)
        notify.assert_not_called()

    def test_duplicate_product_ids_are_rejected(self):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({"products": [{"productId": 1}, {"productId": 1}]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            bershka.load_config(path)

    def test_notification_error_does_not_log_the_token(self):
        with patch("bershka.requests.post", side_effect=bershka.requests.ConnectionError("url contains secret")):
            with self.assertLogs("bershka", level="ERROR") as logs:
                self.assertFalse(bershka.send_telegram(["Stock changed"], "secret-token", "test"))
        self.assertNotIn("secret-token", " ".join(logs.output))
        self.assertNotIn("url contains secret", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
