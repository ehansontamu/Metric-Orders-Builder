from dataclasses import dataclass
import os
import re
from urllib.parse import urlparse

from .errors import SyncError
from .models import parse_date

CONTENT_TYPE = (
    "0x0100BF61754F0D55A24F8A4D1D3063D8F3C900"
    "62FB28D45AA3554C90E2016E9C700DA9"
)
FIELDS = ("Title", "Spend", "Status", "Department", "orderdate", "DeptCode", "Shipping")


@dataclass(frozen=True)
class Config:
    store_url: str
    api_token: str
    tenant_id: str
    client_id: str
    client_secret: str
    site_url: str = "https://tamucs.sharepoint.com/teams/Team-TechHub"
    list_id: str = "81de6f0c-29f3-4b76-ae63-2ad8546b7748"
    list_name: str = "MetricOrders"
    content_type: str = CONTENT_TYPE
    min_id: int = 3978
    cutoff: str = "2025-12-31"
    timeout: float = 60.0
    attempts: int = 6

    @classmethod
    def from_env(cls):
        required = ("BIGCOMMERCE_STORE_URL", "BIGCOMMERCE_API_TOKEN",
                    "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise SyncError("Missing environment variables: " + ", ".join(missing))
        try:
            config = cls(
                *(os.environ[name].strip() for name in required),
                site_url=os.getenv("SHAREPOINT_SITE_URL", cls.site_url).rstrip("/"),
                list_id=os.getenv("SHAREPOINT_LIST_ID", cls.list_id),
                list_name=os.getenv("SHAREPOINT_LIST_NAME", cls.list_name),
                content_type=os.getenv("SHAREPOINT_CONTENT_TYPE_ID", CONTENT_TYPE),
                min_id=int(os.getenv("BIGCOMMERCE_MIN_ID", "3978")),
                cutoff=os.getenv("ORDERDATE_CUTOFF", "2025-12-31"),
                timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "60")),
                attempts=int(os.getenv("HTTP_MAX_ATTEMPTS", "6")),
            )
        except ValueError:
            raise SyncError("Invalid numeric configuration") from None
        store = urlparse(config.store_url)
        if (store.scheme != "https" or store.netloc != "api.bigcommerce.com"
                or not re.fullmatch(r"/stores/[a-zA-Z0-9]+/?", store.path)
                or store.query or store.fragment):
            raise SyncError("BIGCOMMERCE_STORE_URL must be https://api.bigcommerce.com/stores/<hash>")
        site = urlparse(config.site_url)
        if (site.scheme != "https" or not site.hostname
                or not site.hostname.endswith(".sharepoint.com")
                or site.username or site.password or site.port or site.query or site.fragment):
            raise SyncError("SHAREPOINT_SITE_URL must be a SharePoint Online HTTPS site URL")
        guid = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        for name, value in (("tenant", config.tenant_id), ("client", config.client_id),
                            ("list", config.list_id)):
            if not re.fullmatch(guid, value):
                raise SyncError(f"The {name} ID must be a GUID")
        if not re.fullmatch(r"0x[0-9a-fA-F]+", config.content_type):
            raise SyncError("SHAREPOINT_CONTENT_TYPE_ID must be a hexadecimal content type ID")
        if config.min_id < 1 or not 1 <= config.attempts <= 10 or not 1 <= config.timeout <= 300:
            raise SyncError("Invalid min_id, HTTP timeout (1-300), or attempts (1-10)")
        parse_date(config.cutoff)
        return config
