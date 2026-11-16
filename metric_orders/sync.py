from collections import defaultdict
from datetime import datetime, timezone
import logging
import time

from .errors import SyncError
from .models import changed_fields, parse_date

LOG = logging.getLogger(__name__)


def group_items(items):
    by_title = defaultdict(list)
    for item in items:
        title = str(item["fields"].get("Title") or "").strip()
        by_title[title].append(item)
    return by_title


def plan_sync(client, orders, items, mode):
    cutoff = parse_date(client.config.cutoff)
    deletions = []
    remaining = []
    for item in items:
        date = item["fields"].get("orderdate")
        if mode == "rebuild" and date and parse_date(date) > cutoff:
            deletions.append(client.operation("deleted", item=item))
        else:
            remaining.append(item)
    by_title = group_items(remaining)
    mutations = []
    unchanged = 0
    for title, desired in orders.items():
        matches = by_title.get(title, [])
        if len(matches) > 1:
            raise SyncError(f"Duplicate SharePoint Title {title}: {len(matches)} items; fix duplicates before upsert")
        if not matches:
            mutations.append(client.operation("created", fields=desired))
            continue
        item = matches[0]
        ct_id = (item.get("contentType") or {}).get("id", "")
        if ct_id.lower() != client.config.content_type.lower():
            raise SyncError(f"Order {title}: unexpected content type; correct the item or use rebuild mode")
        changes = changed_fields(item["fields"], desired)
        if changes:
            mutations.append(client.operation("updated", item=item, fields=changes))
        else:
            unchanged += 1
    return deletions, mutations, unchanged


def verify_sync(client, orders, deleted_ids=None):
    items = client.fetch_items()
    by_title = group_items(items)
    if deleted_ids and deleted_ids.intersection(item["id"] for item in items):
        raise SyncError("Post-write verification: some planned deletions remain")
    for title, desired in orders.items():
        matches = by_title.get(title, [])
        if len(matches) != 1:
            raise SyncError(f"Post-write verification: expected one item for order {title}, found {len(matches)}")
        item = matches[0]
        if changed_fields(item["fields"], desired):
            raise SyncError(f"Post-write verification: stored fields differ for order {title}")
        if (item.get("contentType") or {}).get("id", "").lower() != client.config.content_type.lower():
            raise SyncError(f"Post-write verification: content type differs for order {title}")
    return len(items)


def run_sync(bigcommerce, sharepoint, *, mode="upsert", dry_run=False, summary=None):
    if mode not in ("upsert", "rebuild"):
        raise SyncError("Mode must be upsert or rebuild")
    summary = summary if summary is not None else {}
    started = time.monotonic()
    summary.update({
        "status": "running", "mode": mode, "dry_run": dry_run,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "created": 0, "updated": 0, "deleted": 0, "unchanged": 0,
    })
    try:
        # Fetch and validate the complete source before considering any deletions.
        orders = bigcommerce.fetch_orders()
        summary.update({
            "source_pages": bigcommerce.pages, "source_orders_scanned": bigcommerce.scanned,
            "deleted_orders_skipped": bigcommerce.deleted_skipped, "orders_processed": len(orders),
        })
        if not orders:
            raise SyncError("No active source orders: refusing to write or rebuild an empty snapshot")
        dates = [fields["orderdate"] for fields in orders.values()]
        summary["oldest_source_order_utc"] = min(dates)
        summary["newest_source_order_utc"] = max(dates)
        LOG.info("Source order dates: %s through %s", min(dates), max(dates))
        sharepoint.preflight(orders)
        items = sharepoint.fetch_items()
        summary["sharepoint_items_before"] = len(items)
        current_dates = [parse_date(item["fields"]["orderdate"]) for item in items
                         if item["fields"].get("orderdate")]
        summary["newest_sharepoint_order_before_utc"] = (
            max(current_dates).isoformat() if current_dates else None)
        deletions, mutations, unchanged = plan_sync(sharepoint, orders, items, mode)
        summary["unchanged"] = unchanged
        summary["planned_deleted"] = len(deletions)
        summary["planned_created"] = sum(op["kind"] == "created" for op in mutations)
        summary["planned_updated"] = sum(op["kind"] == "updated" for op in mutations)
        LOG.info("Plan: %s deletes, %s creates, %s updates, %s unchanged",
                 len(deletions), summary["planned_created"], summary["planned_updated"], unchanged)
        if dry_run:
            summary["status"] = "dry-run"
            return summary

        def completed(kind):
            summary[kind] += 1

        # Keep deletes and inserts in separate phases for the explicit rebuild mode.
        sharepoint.apply(deletions, completed)
        sharepoint.apply(mutations, completed)
        deleted_ids = {op["url"].rsplit("/", 1)[-1] for op in deletions}
        summary["sharepoint_items_after"] = verify_sync(sharepoint, orders, deleted_ids)
        summary["status"] = "success"
        LOG.info("Verified all %s source orders in SharePoint", len(orders))
        return summary
    except Exception:
        summary["status"] = "failed"
        raise
    finally:
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
