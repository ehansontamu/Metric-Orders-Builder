import logging
import time
from urllib.parse import quote, urlencode, urlparse

from .config import FIELDS
from .errors import AmbiguousWriteError, ApiError, SyncError
from .http import HttpClient, TRANSIENT, retry_delay

LOG = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"


class SharePointClient:
    def __init__(self, config, http=None, auth_http=None, sleep=time.sleep):
        self.config = config
        self.http = http or HttpClient("Microsoft Graph", config.timeout, config.attempts)
        self.auth_http = auth_http or HttpClient("Microsoft identity", config.timeout, config.attempts)
        self.sleep = sleep
        self.token = None
        self.expires_at = 0
        self.list_path = None
        self.columns = {}

    def _token(self):
        if self.token and time.monotonic() < self.expires_at:
            return self.token
        body = urlencode({
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")
        result = self.auth_http.request(
            "POST",
            f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        if not isinstance(result, dict) or not result.get("access_token"):
            raise SyncError("Microsoft identity: no access token returned")
        self.token = result["access_token"]
        self.expires_at = time.monotonic() + max(0, int(result.get("expires_in", 3600)) - 120)
        return self.token

    def _request(self, method, path, *, body=None, retry_safe=True):
        url = GRAPH + path if path.startswith("/") else path
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com"
                or not parsed.path.startswith("/v1.0/")):
            raise SyncError("Microsoft Graph returned an unexpected pagination URL")
        for attempt in range(2):
            try:
                return self.http.request(
                    method, url,
                    headers={"Authorization": "Bearer " + self._token(),
                             "Accept": "application/json"},
                    body=body, retry_safe=retry_safe,
                )
            except ApiError as error:
                if error.status != 401 or attempt:
                    raise
                # A rejected outer request has not executed any mutations.
                self.token = None
        raise SyncError("Microsoft Graph authentication failed")

    def _collection(self, path):
        seen_urls = set()
        values = []
        while path:
            if path in seen_urls:
                raise SyncError("Microsoft Graph repeated a pagination link")
            seen_urls.add(path)
            result = self._request("GET", path)
            if not isinstance(result, dict) or not isinstance(result.get("value"), list):
                raise SyncError("Microsoft Graph: expected a paginated collection")
            values.extend(result["value"])
            path = result.get("@odata.nextLink")
        return values

    def preflight(self, orders):
        site_url = urlparse(self.config.site_url)
        path = f"/sites/{site_url.hostname}:{quote(site_url.path, safe='/')}"
        site = self._request("GET", path)
        if not isinstance(site, dict) or not site.get("id"):
            raise SyncError("SharePoint site could not be resolved")
        self.list_path = f"/sites/{quote(site['id'], safe=',')}/lists/{self.config.list_id}"
        metadata = self._request("GET", self.list_path + "?$select=id,displayName")
        if metadata.get("displayName") != self.config.list_name:
            raise SyncError("Target list name does not match SHAREPOINT_LIST_NAME")
        content_types = self._collection(self.list_path + "/contentTypes")
        if not any(item.get("id", "").lower() == self.config.content_type.lower()
                   for item in content_types):
            raise SyncError("Configured content type is not attached to the target list")
        columns = self._collection(self.list_path + "/columns")
        self.columns = {column["name"]: column for column in columns}
        missing = set(FIELDS) - self.columns.keys()
        if missing:
            raise SyncError("Missing internal SharePoint columns: " + ", ".join(sorted(missing)))
        for name in FIELDS:
            column = self.columns[name]
            if column.get("readOnly"):
                raise SyncError(f"SharePoint column {name} is read-only")
            if name in ("Spend", "Shipping"):
                compatible = "number" in column or "currency" in column
            elif name == "orderdate":
                compatible = "dateTime" in column
            else:
                compatible = "text" in column or "choice" in column
                if "choice" in column and column["choice"].get("displayAs") == "checkBoxes":
                    compatible = False
            if not compatible:
                raise SyncError(f"SharePoint column {name} has an unsupported type")
        # Content-type column requirements can differ from the list's defaults.
        ct_columns = self._collection(
            self.list_path + f"/contentTypes/{self.config.content_type}/columns")
        required_columns = {**self.columns}
        required_columns.update({column["name"]: column for column in ct_columns})
        for name, column in required_columns.items():
            if (column.get("required") and not column.get("readOnly")
                    and not column.get("hidden") and name not in FIELDS
                    and name != "ContentType" and name != "ContentTypeId"
                    and not column.get("defaultValue")):
                raise SyncError(f"Unmapped required SharePoint column: {name}")
        for title, fields in orders.items():
            for name, value in fields.items():
                column = required_columns.get(name, self.columns[name])
                if column.get("required") and value == "":
                    raise SyncError(f"Order {title}: required column {name} is empty")
                if "text" in column:
                    maximum = column["text"].get("maxLength")
                    if maximum and len(value) > maximum:
                        raise SyncError(f"Order {title}: {name} exceeds SharePoint maximum length")
                if "choice" in column:
                    choice = column["choice"]
                    if value and not choice.get("allowTextEntry", False) and value not in choice.get("choices", []):
                        raise SyncError(f"Order {title}: {name} is not an allowed SharePoint choice")
                if "number" in column:
                    definition = column["number"]
                    if (definition.get("minimum") is not None and value < definition["minimum"]
                            or definition.get("maximum") is not None and value > definition["maximum"]):
                        raise SyncError(f"Order {title}: {name} is outside the SharePoint numeric range")
        if not self.columns["Title"].get("enforceUniqueValues"):
            LOG.warning("Title does not enforce unique values. Keep Power Automate and other writers paused.")
        LOG.info("SharePoint preflight: list, columns, content type, and %s orders validated", len(orders))

    def fetch_items(self):
        if not self.list_path:
            raise SyncError("SharePoint preflight must run before item enumeration")
        query = urlencode({
            "$select": "id,eTag,contentType",
            "$expand": "fields($select=" + ",".join(FIELDS) + ")",
            "$top": "500",
        })
        items = self._collection(self.list_path + "/items?" + query)
        seen_ids = set()
        for item in items:
            if not item.get("id") or not isinstance(item.get("fields"), dict):
                raise SyncError("SharePoint returned an invalid list item")
            if item["id"] in seen_ids:
                raise SyncError("SharePoint returned a repeated list item; retry from a fresh snapshot")
            seen_ids.add(item["id"])
        LOG.info("SharePoint: read %s items across all pages", len(items))
        return items

    def operation(self, kind, *, item=None, fields=None):
        if kind not in ("created", "updated", "deleted"):
            raise ValueError("Unknown mutation")
        headers = {"Content-Type": "application/json"}
        operation = {"kind": kind, "headers": headers}
        if kind == "created":
            operation.update({
                "method": "POST", "url": self.list_path + "/items",
                "body": {"contentType": {"id": self.config.content_type}, "fields": fields},
            })
        else:
            etag = item.get("eTag") or item["fields"].get("@odata.etag")
            if not etag:
                raise SyncError(f"SharePoint item {item['id']}: no ETag; refusing an unguarded write")
            headers["If-Match"] = etag
            operation.update({
                "method": "PATCH" if kind == "updated" else "DELETE",
                "url": self.list_path + "/items/" + quote(str(item["id"]), safe="")
                       + ("/fields" if kind == "updated" else ""),
            })
            if kind == "updated":
                operation["body"] = fields
        return operation

    def apply(self, operations, on_success):
        for start in range(0, len(operations), 20):
            self._batch(operations[start:start + 20], on_success)
            LOG.info("SharePoint: completed %s/%s planned operations",
                     min(start + 20, len(operations)), len(operations))

    def _batch(self, operations, on_success):
        pending = {str(index): operation for index, operation in enumerate(operations, start=1)}
        for attempt in range(self.config.attempts):
            requests = [{"id": key, **{k: v for k, v in op.items() if k != "kind"}}
                        for key, op in pending.items()]
            contains_create = any(op["kind"] == "created" for op in pending.values())
            result = self._request("POST", "/$batch", body={"requests": requests},
                                   retry_safe=not contains_create)
            if not isinstance(result, dict) or not isinstance(result.get("responses"), list):
                raise AmbiguousWriteError("Graph returned an invalid batch response; rerun to reconcile")
            responses = result["responses"]
            by_id = {response.get("id"): response for response in responses}
            if len(by_id) != len(responses) or by_id.keys() != pending.keys():
                raise AmbiguousWriteError("Graph batch response IDs did not match; rerun to reconcile")
            failed = {}
            delays = []
            errors = []
            for key, op in pending.items():
                response = by_id[key]
                status = response.get("status", 0)
                if 200 <= status < 300 or (op["kind"] == "deleted" and status == 404):
                    on_success(op["kind"])
                    continue
                headers = response.get("headers") or {}
                if status == 429 or (op["kind"] != "created" and status in TRANSIENT):
                    if attempt + 1 < self.config.attempts:
                        failed[key] = op
                        delays.append(retry_delay(headers, attempt))
                        continue
                if op["kind"] == "created" and status in TRANSIENT - {429}:
                    errors.append(AmbiguousWriteError(
                        f"Create returned HTTP {status}; rerun to reconcile before retrying"))
                elif status == 412:
                    errors.append(SyncError("SharePoint version conflict (412); an item changed during the run"))
                elif status == 409:
                    errors.append(SyncError("SharePoint conflict (409); check duplicate Titles or list constraints"))
                else:
                    # Do not include provider error messages containing list field values.
                    errors.append(SyncError(f"Graph batch {op['kind']}: HTTP {status}"))
                    LOG.error("Graph batch operation failed with HTTP %s", status)
            if errors:
                raise errors[0]
            if not failed:
                return
            pending = failed
            delay = max(delays)
            LOG.warning("Graph batch: retrying %s failed operations only in %.1fs", len(pending), delay)
            self.sleep(delay)
        raise SyncError("Graph batch retries exhausted")
