from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import math

from .errors import SyncError

DEPT = "Recipient College/Unit"
DEPT_CODES = (
    "Department Code (include system part prefix, e.g. 02-CHEM)",
    "Department Code (include system part prefix, e.g. 02-ABCD)",
)


def parse_date(value):
    if not isinstance(value, str) or not value.strip():
        raise SyncError("Missing or invalid orderdate/date_created")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            raise SyncError("Invalid orderdate/date_created") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso_date(value):
    return parse_date(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def money(value):
    if value is None or isinstance(value, bool):
        raise SyncError("Missing or invalid order amount")
    try:
        amount = Decimal(str(value))
        result = float(amount)
    except (InvalidOperation, ValueError, OverflowError):
        raise SyncError("Invalid order amount") from None
    if not amount.is_finite() or not math.isfinite(result):
        raise SyncError("Non-finite order amount")
    return result


def is_deleted(order):
    value = order.get("is_deleted", False)
    if value in (True, 1, "true", "True"):
        return True
    if value in (False, 0, None, "false", "False"):
        return False
    raise SyncError("Invalid is_deleted value")


def order_fields(order):
    order_id = order.get("id")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
        raise SyncError("Invalid BigCommerce order ID")
    try:
        values = order.get("billing_address") or {}
        form_fields = values.get("form_fields") or []
        if not isinstance(form_fields, list):
            raise SyncError(f"Order {order_id}: form_fields must be an array")
        dept = ""
        code = ""
        # Locals start empty for every order, including absent billing fields.
        for field in form_fields:
            if not isinstance(field, dict):
                raise SyncError(f"Order {order_id}: invalid custom form field")
            name = str(field.get("name", "")).strip()
            value = field.get("value")
            if value is not None and not isinstance(value, (str, int)):
                raise SyncError(f"Order {order_id}: custom field value must be text or integer")
            text = "" if value is None else str(value)
            if name == DEPT:
                dept = text
            elif name in DEPT_CODES:
                code = text
        status = order.get("status")
        if not isinstance(status, str) or not status.strip():
            raise SyncError(f"Order {order_id}: missing status")
        return {
            "Title": str(order_id),
            "Spend": money(order.get("total_inc_tax")),
            "Status": status,
            "Department": dept,
            "orderdate": iso_date(order.get("date_created")),
            "DeptCode": code,
            "Shipping": money(order.get("shipping_cost_inc_tax")),
        }
    except (AttributeError, TypeError):
        raise SyncError(f"Order {order_id}: invalid billing_address") from None


def changed_fields(current, desired):
    changes = {}
    for key, wanted in desired.items():
        actual = current.get(key)
        try:
            if key == "orderdate":
                equal = actual is not None and parse_date(actual) == parse_date(wanted)
            elif key in ("Spend", "Shipping"):
                equal = actual is not None and Decimal(str(actual)) == Decimal(str(wanted))
            else:
                equal = ("" if actual is None else str(actual)) == str(wanted)
        except (SyncError, InvalidOperation):
            equal = False
        if not equal:
            changes[key] = wanted
    return changes
