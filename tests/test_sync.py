import copy
from datetime import timezone
import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from metric_orders.bigcommerce import BigCommerceClient
from metric_orders.config import Config, CONTENT_TYPE, FIELDS
from metric_orders.errors import AmbiguousWriteError, ApiError, SyncError
from metric_orders.http import HttpClient, retry_delay
from metric_orders.models import DEPT, DEPT_CODES, changed_fields, order_fields, parse_date
from metric_orders.sharepoint import GRAPH, SharePointClient
from metric_orders.sync import plan_sync, run_sync

CONFIG = Config(
    "https://api.bigcommerce.com/stores/teststore", "test-token",
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002", "test-secret",
)


def order(order_id=3978, date="Thu, 08 Oct 2026 12:30:00 +0000", forms=None):
    return {
        "id": order_id, "total_inc_tax": "123.4500", "status": "Completed",
        "date_created": date, "shipping_cost_inc_tax": "6.5000", "is_deleted": False,
        "billing_address": {"form_fields": [] if forms is None else forms},
    }


def item(fields=None, item_id="1", etag='"version-1"', content_type=CONTENT_TYPE):
    return {"id": item_id, "eTag": etag, "contentType": {"id": content_type},
            "fields": copy.deepcopy(fields if fields is not None else order_fields(order()))}


def client():
    result = SharePointClient(CONFIG, http=Mock(), auth_http=Mock(), sleep=Mock())
    result.list_path = "/sites/site-id/lists/" + CONFIG.list_id
    return result


class ModelsTests(unittest.TestCase):
    def test_dates_support_iso_rfc_offsets_and_utc(self):
        expected = parse_date("2026-10-08T12:30:00Z")
        self.assertEqual(parse_date("Thu, 08 Oct 2026 12:30:00 +0000"), expected)
        self.assertEqual(parse_date("2026-10-08T07:30:00-05:00"), expected)
        self.assertEqual(parse_date("2025-12-31").tzinfo, timezone.utc)

    def test_both_department_names_and_integer_values(self):
        for label in DEPT_CODES:
            fields = order_fields(order(forms=[
                {"name": DEPT, "value": "Science"},
                {"name": label, "value": 42},
            ]))
            self.assertEqual(fields["Department"], "Science")
            self.assertEqual(fields["DeptCode"], "42")
            self.assertEqual(fields["Spend"], 123.45)
            self.assertEqual(fields["Shipping"], 6.5)
            self.assertEqual(fields["Title"], "3978")

    def test_missing_fields_do_not_leak_previous_order(self):
        order_fields(order(forms=[{"name": DEPT, "value": "Science"}]))
        fields = order_fields(order(3979))
        self.assertEqual(fields["Department"], "")
        self.assertEqual(fields["DeptCode"], "")

    def test_invalid_dates_and_nonfinite_money_fail(self):
        for date in ("invalid", None, ""):
            with self.assertRaises(SyncError):
                order_fields(order(date=date))
        for value in ("NaN", "Infinity", None, True):
            data = order()
            data["total_inc_tax"] = value
            with self.assertRaises(SyncError):
                order_fields(data)

    def test_compare_dates_currency_and_empty_values(self):
        desired = order_fields(order())
        actual = {**desired, "Spend": "123.4500", "Shipping": "6.500000",
                  "orderdate": "2026-10-08T07:30:00-05:00",
                  "Department": None, "DeptCode": None}
        self.assertEqual(changed_fields(actual, desired), {})
        actual["Status"] = "Pending"
        self.assertEqual(changed_fields(actual, desired), {"Status": "Completed"})


class BigCommerceTests(unittest.TestCase):
    def test_pagination_stops_only_when_empty_and_skips_deleted(self):
        http = Mock()
        archived = order(3979)
        archived["is_deleted"] = True
        http.request.side_effect = [[order(), archived], [order(3980)], []]
        bc = BigCommerceClient(CONFIG, http)
        result = bc.fetch_orders()
        self.assertEqual(set(result), {"3978", "3980"})
        self.assertEqual(bc.pages, 2)
        self.assertEqual(bc.scanned, 3)
        self.assertEqual(bc.deleted_skipped, 1)
        urls = [call.args[1] for call in http.request.call_args_list]
        self.assertIn("page=1", urls[0])
        self.assertIn("page=3", urls[-1])
        self.assertIn("min_id=3978", urls[0])
        self.assertIn("limit=250", urls[0])
        self.assertIn("is_deleted=false", urls[0])

    def test_204_is_end_of_pagination(self):
        http = Mock()
        http.request.side_effect = [[order()], None]
        self.assertEqual(len(BigCommerceClient(CONFIG, http).fetch_orders()), 1)

    def test_repeated_source_ids_abort(self):
        http = Mock()
        http.request.side_effect = [[order()], [order()]]
        with self.assertRaisesRegex(SyncError, "repeated"):
            BigCommerceClient(CONFIG, http).fetch_orders()

    def test_wrong_api_shape_aborts(self):
        http = Mock()
        http.request.return_value = {"error": "failed"}
        with self.assertRaises(SyncError):
            BigCommerceClient(CONFIG, http).fetch_orders()


class PlanTests(unittest.TestCase):
    def test_upsert_keeps_item_ids_and_changes_only_modified_fields(self):
        sp = client()
        desired = order_fields(order())
        existing = item({**desired, "Status": "Pending"})
        deletes, mutations, unchanged = plan_sync(sp, {"3978": desired}, [existing], "upsert")
        self.assertEqual(deletes, [])
        self.assertEqual(unchanged, 0)
        self.assertEqual(mutations[0]["body"], {"Status": "Completed"})
        self.assertEqual(mutations[0]["headers"]["If-Match"], '"version-1"')
        self.assertTrue(mutations[0]["url"].endswith("/items/1/fields"))

    def test_unchanged_item_generates_no_mutation(self):
        desired = order_fields(order())
        self.assertEqual(plan_sync(client(), {"3978": desired}, [item()], "upsert"), ([], [], 1))

    def test_duplicate_title_blocks_before_writing(self):
        sp = client()
        with self.assertRaisesRegex(SyncError, "Duplicate"):
            plan_sync(sp, {"3978": order_fields(order())}, [item(), item(item_id="2")], "upsert")
        sp.http.request.assert_not_called()

    def test_content_type_mismatch_and_missing_etag_block(self):
        fields = order_fields(order())
        with self.assertRaisesRegex(SyncError, "content type"):
            plan_sync(client(), {"3978": fields}, [item(content_type="0x01")], "upsert")
        with self.assertRaisesRegex(SyncError, "ETag"):
            plan_sync(client(), {"3978": fields},
                      [item({**fields, "Spend": 1}, etag=None)], "upsert")

    def test_rebuild_cutoff_is_strict_and_preserves_historical_matches(self):
        sp = client()
        old = order_fields(order(3978, "2025-12-31T00:00:00Z"))
        new = order_fields(order(3979, "2025-12-31T00:00:01Z"))
        deletes, mutations, unchanged = plan_sync(
            sp, {"3978": old, "3979": new}, [item(old), item(new, "2")], "rebuild")
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0]["url"].endswith("/items/2"))
        self.assertEqual(mutations[0]["kind"], "created")
        self.assertEqual(unchanged, 1)
        self.assertEqual(mutations[0]["body"]["contentType"]["id"], CONTENT_TYPE)


class BatchTests(unittest.TestCase):
    def test_only_failed_throttled_subrequest_is_retried(self):
        sp = client()
        sp._request = Mock(side_effect=[
            {"responses": [{"id": "2", "status": 429, "headers": {"Retry-After": "2"}},
                           {"id": "1", "status": 201}]},
            {"responses": [{"id": "2", "status": 201}]},
        ])
        operations = [sp.operation("created", fields=order_fields(order(i))) for i in (3978, 3979)]
        completed = Mock()
        sp.apply(operations, completed)
        self.assertEqual(completed.call_count, 2)
        retry = sp._request.call_args_list[1].kwargs["body"]["requests"]
        self.assertEqual([request["id"] for request in retry], ["2"])
        self.assertFalse(sp._request.call_args_list[0].kwargs["retry_safe"])
        sp.sleep.assert_called_once_with(2)

    def test_create_503_is_not_replayed_but_success_is_counted(self):
        sp = client()
        sp._request = Mock(return_value={
            "responses": [{"id": "1", "status": 201}, {"id": "2", "status": 503}]})
        completed = Mock()
        with self.assertRaises(AmbiguousWriteError):
            sp.apply([sp.operation("created", fields={}), sp.operation("created", fields={})], completed)
        completed.assert_called_once_with("created")
        self.assertEqual(sp._request.call_count, 1)

    def test_patch_503_retries_and_412_stops(self):
        sp = client()
        sp._request = Mock(side_effect=[
            {"responses": [{"id": "1", "status": 503}]},
            {"responses": [{"id": "1", "status": 200}]},
        ])
        sp.apply([sp.operation("updated", item=item(), fields={"Status": "Completed"})], Mock())
        self.assertTrue(sp._request.call_args.kwargs["retry_safe"])
        sp._request = Mock(return_value={"responses": [{"id": "1", "status": 412}]})
        with self.assertRaisesRegex(SyncError, "version conflict"):
            sp.apply([sp.operation("updated", item=item(), fields={})], Mock())
        self.assertEqual(sp._request.call_count, 1)

    def test_missing_batch_response_aborts(self):
        sp = client()
        sp._request = Mock(return_value={"responses": []})
        with self.assertRaises(AmbiguousWriteError):
            sp.apply([sp.operation("created", fields={})], Mock())

    def test_batches_never_exceed_20(self):
        sp = client()
        sizes = []
        def request(method, path, **kwargs):
            requests = kwargs["body"]["requests"]
            sizes.append(len(requests))
            return {"responses": [{"id": req["id"], "status": 201} for req in requests]}
        sp._request = request
        sp.apply([sp.operation("created", fields={}) for _ in range(45)], Mock())
        self.assertEqual(sizes, [20, 20, 5])


class SharePointTests(unittest.TestCase):
    def test_enumeration_has_no_9999_item_cap(self):
        sp = client()
        pages = []
        for page in range(21):
            result = {"value": [item(item_id=str(page * 500 + i)) for i in range(500)]}
            if page < 20:
                result["@odata.nextLink"] = GRAPH + f"/sites/site/items?page={page + 1}"
            pages.append(result)
        sp._request = Mock(side_effect=pages)
        self.assertEqual(len(sp.fetch_items()), 10500)
        self.assertEqual(sp._request.call_count, 21)

    def test_untrusted_nextlink_and_repeated_links_fail(self):
        sp = client()
        with self.assertRaisesRegex(SyncError, "unexpected"):
            sp._request("GET", "https://example.com/v1.0/items")
        sp._request = Mock(return_value={"value": [], "@odata.nextLink": "/same"})
        with self.assertRaisesRegex(SyncError, "repeated"):
            sp._collection("/same")

    def test_preflight_rejects_renamed_internal_field_and_bad_choices(self):
        columns = [{"name": name, "text": {}} for name in FIELDS]
        for column in columns:
            if column["name"] in ("Spend", "Shipping"):
                column.pop("text")
                column["currency"] = {}
            if column["name"] == "orderdate":
                column.pop("text")
                column["dateTime"] = {}
        sp = client()
        sp._request = Mock(side_effect=[{"id": "site-id"}, {"displayName": "MetricOrders"}])
        sp._collection = Mock(side_effect=[[{"id": CONTENT_TYPE}], columns, []])
        sp.preflight({"3978": order_fields(order())})
        bad = copy.deepcopy(columns)
        next(c for c in bad if c["name"] == "DeptCode")["name"] = "DeptCodeRenamed"
        sp._request = Mock(side_effect=[{"id": "site-id"}, {"displayName": "MetricOrders"}])
        sp._collection = Mock(side_effect=[[{"id": CONTENT_TYPE}], bad])
        with self.assertRaisesRegex(SyncError, "internal"):
            sp.preflight({"3978": order_fields(order())})
        bad = copy.deepcopy(columns)
        status = next(c for c in bad if c["name"] == "Status")
        status.pop("text")
        status["choice"] = {"choices": ["Pending"], "allowTextEntry": False}
        sp._request = Mock(side_effect=[{"id": "site-id"}, {"displayName": "MetricOrders"}])
        sp._collection = Mock(side_effect=[[{"id": CONTENT_TYPE}], bad, []])
        with self.assertRaisesRegex(SyncError, "allowed"):
            sp.preflight({"3978": order_fields(order())})


class FakeSharePoint(SharePointClient):
    def __init__(self, items=None):
        super().__init__(CONFIG)
        self.list_path = "/sites/site/lists/list"
        self.items = copy.deepcopy(items or [])
        self.applied = []
        self.next_id = 100

    def preflight(self, orders):
        pass

    def fetch_items(self):
        return copy.deepcopy(self.items)

    def apply(self, operations, on_success):
        self.applied.extend(operations)
        for operation in operations:
            if operation["kind"] == "created":
                self.items.append(item(operation["body"]["fields"], str(self.next_id)))
                self.next_id += 1
            else:
                item_id = operation["url"].split("/items/")[1].split("/")[0]
                if operation["kind"] == "deleted":
                    self.items = [entry for entry in self.items if entry["id"] != item_id]
                else:
                    next(entry for entry in self.items if entry["id"] == item_id)["fields"].update(operation["body"])
            on_success(operation["kind"])


def fake_bigcommerce(data):
    bc = Mock()
    bc.fetch_orders.return_value = data
    bc.pages = 1
    bc.scanned = len(data)
    bc.deleted_skipped = 0
    return bc


class IntegrationTests(unittest.TestCase):
    def test_two_runs_update_and_create_once(self):
        desired = order_fields(order())
        sp = FakeSharePoint([item({**desired, "Spend": 1})])
        bc = fake_bigcommerce({"3978": desired, "3979": order_fields(order(3979))})
        first = run_sync(bc, sp)
        second = run_sync(bc, sp)
        self.assertEqual((first["created"], first["updated"], first["deleted"]), (1, 1, 0))
        self.assertEqual((second["created"], second["updated"], second["unchanged"]), (0, 0, 2))
        self.assertEqual(len(sp.items), 2)
        self.assertEqual(sp.items[0]["id"], "1")

    def test_dry_run_reports_without_mutation(self):
        sp = FakeSharePoint()
        report = run_sync(fake_bigcommerce({"3978": order_fields(order())}), sp, dry_run=True)
        self.assertEqual(report["status"], "dry-run")
        self.assertEqual(report["planned_created"], 1)
        self.assertEqual(report["created"], 0)
        self.assertEqual(sp.applied, [])

    def test_empty_or_invalid_source_never_deletes(self):
        for source in ({}, None):
            sp = FakeSharePoint([item()])
            bc = fake_bigcommerce({})
            if source is None:
                bc.fetch_orders.side_effect = SyncError("Invalid source")
            with self.assertRaises(SyncError):
                run_sync(bc, sp, mode="rebuild")
            self.assertEqual(sp.applied, [])
            self.assertEqual(len(sp.items), 1)

    def test_rebuild_is_idempotent_and_deletes_before_creates(self):
        source = {"3978": order_fields(order())}
        sp = FakeSharePoint([item(), item(item_id="2")])
        first = run_sync(fake_bigcommerce(source), sp, mode="rebuild")
        second = run_sync(fake_bigcommerce(source), sp, mode="rebuild")
        self.assertEqual((first["deleted"], first["created"]), (2, 1))
        self.assertEqual((second["deleted"], second["created"]), (1, 1))
        self.assertEqual(len(sp.items), 1)
        self.assertEqual([op["kind"] for op in sp.applied[:3]], ["deleted", "deleted", "created"])

    def test_rerun_reconciles_create_that_succeeded_before_connection_lost(self):
        sp = FakeSharePoint()
        source = {"3978": order_fields(order())}
        original = sp.apply
        def interrupted(operations, completed):
            original(operations, completed)
            if operations:
                raise AmbiguousWriteError("Connection lost after create")
        sp.apply = interrupted
        summary = {}
        with self.assertRaises(AmbiguousWriteError):
            run_sync(fake_bigcommerce(source), sp, summary=summary)
        self.assertEqual(summary["status"], "failed")
        sp.apply = original
        retry = run_sync(fake_bigcommerce(source), sp)
        self.assertEqual(retry["created"], 0)
        self.assertEqual(retry["unchanged"], 1)
        self.assertEqual(len(sp.items), 1)

    def test_postwrite_verification_catches_ignored_content_type(self):
        sp = FakeSharePoint()
        original = sp.apply
        def wrong_content_type(operations, completed):
            original(operations, completed)
            for entry in sp.items:
                entry["contentType"]["id"] = "0x01"
        sp.apply = wrong_content_type
        with self.assertRaisesRegex(SyncError, "content type"):
            run_sync(fake_bigcommerce({"3978": order_fields(order())}), sp)


class HttpTests(unittest.TestCase):
    def test_retry_after_and_bigcommerce_reset_header(self):
        self.assertEqual(retry_delay({"Retry-After": "7"}, 0), 7)
        self.assertEqual(retry_delay({"X-Rate-Limit-Time-Reset-Ms": "1250"}, 0), 1.25)

    def test_safe_get_retries_429_and_5xx(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"[]"
        opener = Mock(side_effect=[
            HTTPError("https://example.com", 429, "Too many", {"Retry-After": "1"}, io.BytesIO(b"{}")),
            HTTPError("https://example.com", 503, "Unavailable", {}, io.BytesIO(b"{}")),
            response,
        ])
        sleep = Mock()
        http = HttpClient("test", opener=opener, sleep=sleep)
        self.assertEqual(http.request("GET", "https://example.com"), [])
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_unsafe_create_does_not_retry_network_error_or_503(self):
        for error in (URLError("lost"),
                      HTTPError("https://example.com", 503, "Unavailable", {}, io.BytesIO(b"{}"))):
            opener = Mock(side_effect=error)
            with self.assertRaises(AmbiguousWriteError):
                HttpClient("test", opener=opener).request(
                    "POST", "https://example.com", body={}, retry_safe=False)
            self.assertEqual(opener.call_count, 1)

    def test_secret_and_api_error_body_not_exposed(self):
        error = HTTPError("https://example.com", 403, "Forbidden", {},
                          io.BytesIO(b'{"error":{"code":"accessDenied","message":"secret-customer"}}'))
        with self.assertRaises(ApiError) as result:
            HttpClient("test", opener=Mock(side_effect=error)).request("GET", "https://example.com")
        self.assertIn("accessDenied", str(result.exception))
        self.assertNotIn("secret-customer", str(result.exception))


class ConfigTests(unittest.TestCase):
    def test_missing_credentials_are_reported_by_name(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(SyncError, "BIGCOMMERCE"):
            Config.from_env()

    def test_valid_env_and_invalid_store_url(self):
        env = {
            "BIGCOMMERCE_STORE_URL": CONFIG.store_url,
            "BIGCOMMERCE_API_TOKEN": CONFIG.api_token,
            "AZURE_TENANT_ID": CONFIG.tenant_id,
            "AZURE_CLIENT_ID": CONFIG.client_id,
            "AZURE_CLIENT_SECRET": CONFIG.client_secret,
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(Config.from_env().min_id, 3978)
        env["BIGCOMMERCE_STORE_URL"] = "http://example.com"
        with patch.dict("os.environ", env, clear=True), self.assertRaises(SyncError):
            Config.from_env()


if __name__ == "__main__":
    unittest.main()
