# MetricOrders Builder

Synchronize BigCommerce orders into the existing **MetricOrders** SharePoint list using
Python 3.12+ and GitHub Actions. Each manually started run updates orders by their BigCommerce ID,
preserving SharePoint item IDs, attachments, and unrelated columns. A manual rebuild
mode reproduces the exported flow's delete-and-reload behavior.

The implementation uses Python's standard library. No pip dependencies are needed.

## Target and field mapping

- Store API base URL from the flow: `https://api.bigcommerce.com/stores/jsj7fos9p1`.
- Site: `https://tamucs.sharepoint.com/teams/Team-TechHub`.
- List ID: `81de6f0c-29f3-4b76-ae63-2ad8546b7748`.
- Content type: `0x0100BF61754F0D55A24F8A4D1D3063D8F3C90062FB28D45AA3554C90E2016E9C700DA9`.

| SharePoint internal name | BigCommerce value |
| --- | --- |
| Title | id, stored as text; the stable order key |
| Spend | total_inc_tax, sent as a JSON number |
| Status | status |
| Department | billing_address.form_fields: Recipient College/Unit |
| orderdate | date_created, normalized to UTC |
| DeptCode | Either department-code label below |
| Shipping | shipping_cost_inc_tax, sent as a JSON number |

Accepted department-code labels:

- `Department Code (include system part prefix, e.g. 02-CHEM)`
- `Department Code (include system part prefix, e.g. 02-ABCD)`

Missing department fields become empty strings for that order. Integer form values are
converted to text. When both department-code labels occur, the last one wins, matching
the flow's sequential assignments.

The Power Automate connector's `{ContentType}/Id` is represented in Microsoft Graph
creates as `contentType: {id: ...}`. Existing items must already have the configured
content type. Successful runs read SharePoint again to verify the content type and
every mapped field.

## Set up unattended Microsoft access

The existing Power Automate SharePoint connection is a managed user connection; it
cannot be reused as a GitHub secret. Create an app registration in the Microsoft Entra
tenant that owns the SharePoint site:

1. Record the **Directory (tenant) ID** and **Application (client) ID**.
2. Create a client secret. Store its **value**, not its secret ID, in GitHub.
3. Add the Microsoft Graph **Application** permission `Sites.Selected`, and have a
   tenant administrator grant admin consent.
4. Have the administrator explicitly grant this application the `write` role on the
   Team-TechHub site. Admin consent alone does not grant access to any site.

For the administrator, the Graph requests are:

```http
GET https://graph.microsoft.com/v1.0/sites/tamucs.sharepoint.com:/teams/Team-TechHub

POST https://graph.microsoft.com/v1.0/sites/{resolved-site-id}/permissions
Content-Type: application/json

{
  "roles": ["write"],
  "grantedToIdentities": [
    {
      "application": {
        "id": "YOUR_APPLICATION_CLIENT_ID",
        "displayName": "MetricOrders Builder"
      }
    }
  ]
}
```

The administrative grant request must be made with an administrator-authorized identity
with `Sites.FullControl.All`; the sync application does not need that tenant-wide
permission. Your IT administrator can also create the site grant using their approved
Microsoft Graph/SharePoint tooling.

The script requests application tokens for `https://graph.microsoft.com/.default`.
It does not use a SharePoint audience token, a user password, or a delegated login.
Plan for client-secret renewal before expiration.

## Configure GitHub

In **Settings → Secrets and variables → Actions**, add:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | BIGCOMMERCE_API_TOKEN | Store API token with Orders read access |
| Secret | AZURE_TENANT_ID | Entra Directory/tenant GUID |
| Secret | AZURE_CLIENT_ID | Entra application/client GUID |
| Secret | AZURE_CLIENT_SECRET | Client-secret value |
| Variable | BIGCOMMERCE_STORE_URL | https://api.bigcommerce.com/stores/jsj7fos9p1 |

Optional variables `SHAREPOINT_SITE_URL`, `SHAREPOINT_LIST_ID`,
`SHAREPOINT_LIST_NAME`, and `ORDERDATE_CUTOFF` override the defaults in the workflow.
The local script also accepts `SHAREPOINT_CONTENT_TYPE_ID`, `BIGCOMMERCE_MIN_ID`,
`HTTP_TIMEOUT_SECONDS`, and `HTTP_MAX_ATTEMPTS`; to override those on GitHub, add
corresponding environment entries to the workflow.

No credentials from the exported flow are included in this repository.

## First run and manual operation

1. Put these files on the repository's default branch.
2. Pause the old Power Automate flow before allowing this Action to write.
3. In **Actions → Sync MetricOrders → Run workflow**, choose `upsert` and leave
   **dry_run** checked. Review the newest source date and the create/update counts.
4. Fix any reported column, content-type, duplicate-Title, or choice-field problems.
   The dry-run checks the full source and list before planning any writes.
5. Run `upsert` again with **dry_run** unchecked. Confirm that it succeeds and the
   newest expected order date appears in the list.
6. Run a second dry-run. If BigCommerce has not changed, creates and updates should
   both be zero.

The sync workflow has only a `workflow_dispatch` trigger. There is no cron schedule
or automatic sync on push. Start each sync through **Actions → Sync MetricOrders →
Run workflow**. The workflow must be present on the default branch for GitHub to show
the manual run button. The separate Test workflow runs offline tests on pushes and PRs.
`METRIC_ORDERS_SYNC_ENABLED` is no longer used or required.

An Actions concurrency group prevents overlap between manual sync and recovery runs in
this repository. It does not coordinate with Power Automate, local executions, or other
applications writing to SharePoint.

## Execution and development checks

The manually started sync and previews run on GitHub-hosted Ubuntu runners. No local
computer needs to remain on. Use the GitHub **Run workflow** form for all live previews,
syncs, and recovery runs. A preview makes authenticated read calls, without list writes.

The Python entry point is called by the workflow. `.env.example` documents environment
variable names; credentials are supplied by GitHub Secrets when the Action runs.
Failures return exit code 1 and fail the GitHub job.

For development, the following test command uses mocked APIs and never accesses live
BigCommerce or SharePoint:

```powershell
python -B -m unittest discover -s tests -v
```

## Sync behavior and SharePoint considerations

**Upsert (default):**

- Fetch every BigCommerce page from `min_id=3978`, with `is_deleted=false`,
  `limit=250`, and ascending ID order, until an empty array or HTTP 204.
- Skip orders marked deleted, independently of the API filter.
- Read all SharePoint pages, without the old flow's 9,999-item pagination target.
- Match by `Title`; create missing orders and PATCH only fields that changed.
- Preserve existing item IDs, attachments, and unmapped columns.
- Leave items for orders absent from the source in place, including orders later
  archived/deleted in BigCommerce. Use rebuild if those must be removed.
- Fail before writing if a source order matches multiple SharePoint Titles or an
  unexpected content type. Existing unrelated historical duplicates are not changed.

The preflight validates internal field names, supported column types, required fields,
text lengths, number ranges, allowed choices, target list name, and attached content
type. Updates and deletes include the snapshot's ETag in `If-Match`. A conflicting
edit returns 412 and stops the run, allowing a fresh dry-run and reconciliation.

For protection against writers outside this Action, enable **Enforce unique values**
on the existing Title column after resolving existing duplicates, if your list allows
it. The script reports when this is not enabled and does not change list settings.
Within serialized Action runs, duplicate checks, snapshot reconciliation, and avoiding
ambiguous create retries prevent duplicate imports.

**Rebuild (explicit manual recovery):**

In GitHub **Actions → Sync MetricOrders → Run workflow**, select `rebuild` and keep
**dry_run** checked to preview. Review the deletion count, then start another manual
run with **dry_run** unchecked only when that recovery is intended.

Rebuild deletes all list items whose `orderdate` is strictly greater than the cutoff,
then imports the source. All source orders are fetched and validated, and the complete
plan is checked, before any deletion. Historical matches surviving the cutoff are
upserted instead of duplicated. An empty or invalid source aborts without deletion.
SharePoint batches are not a transaction: a failure can leave a partial rebuild.
Rerunning rebuild restores its selected range. Upsert is preferred for routine runs.

The default cutoff deliberately follows the exported filter: midnight UTC at the
start of **December 31, 2025**. It includes December 31 timestamps after midnight,
not only 2026 orders. Change `ORDERDATE_CUTOFF` to the intended timestamp if needed.
This cutoff is used only by rebuild mode. Upsert uses order ID matching across the
entire list.

## Retries, performance, and reports

Graph writes use batches of up to 20 operations; successful subrequests are never
repeated when another subrequest is throttled. HTTP retries honor `Retry-After` and
BigCommerce's `X-Rate-Limit-Time-Reset-Ms`, with exponential backoff otherwise.
Batch subrequests are inspected even when the outer response is HTTP 200.

A lost connection, timeout, or server error during a create may mean that the item was
created. Such creates are not blindly retried. The run fails; a fresh upsert reads the
list and reconciles what already exists. Explicit 429 rejections are safe to retry.
ETag-protected PATCH/DELETE requests can be retried; a version conflict stops the run.

Unchanged runs issue no list writes. Changed orders are batched. Source and list
enumeration still scan all pages to catch updates to older orders. Actual runtime
depends on order volume and provider throttling; no live speed measurement has been
made. The GitHub job timeout is 90 minutes and can be adjusted if needed.

The final report contains counts, duration, source date range, newest existing
SharePoint date, planned versus completed mutations, and success/failure status.
It appears in logs, the GitHub Actions run summary, and a 30-day JSON artifact.
Reports contain no order bodies or customer addresses. On a hard runner termination,
a final report might not be written.

## Findings from the exported Power Automate flow

The inspected export has:

- A stop condition comparing the HTTP body to `null`. If exhausted pages return
  `[]`, the flow continues until another limit, instead of ending immediately.
  BigCommerce responses can also use 204; that body's actual connector behavior
  should be checked in run history.
- A `Do_until` timeout of `PT3H` and iteration cap of 1,600.
- Sequential per-item deletes and creates, with additional nested field actions.
  There is no configured concurrency for those loops.
- A GetItems pagination policy with `minimumItemCount=9999`. This does not establish
  that every matching list item was deleted.
- Strict Parse JSON schemas that can fail if an unexpected null or field type occurs.
- No order-date upper bound in the BigCommerce query.

These findings can explain long or partial imports, but the export alone does not
prove why the last imported date is **October 8, 2026**. Inspect the old run's last
successful HTTP page, source order ID, Parse JSON/CreateItem failures, and timeout
status. The Python preview reports source and SharePoint maximum dates to help narrow
this down. If the newest source date is later than October 8 but the list is not,
the gap is in the import path. If BigCommerce also stops at October 8, check the store
selection, token scope, archived orders, and actual source data.

## Modules and verification

- `metric_orders/bigcommerce.py`: order pagination and source transformation.
- `metric_orders/sharepoint.py`: app authentication, schema preflight, list paging,
  ETags, and Graph batching.
- `metric_orders/sync.py`: planning, idempotency, mode selection, and post-write verification.
- `metric_orders/http.py`: safe retry policy and redacted error reporting.
- `tests/test_sync.py`: offline tests with mocked APIs and an in-memory list.

The test suite covers repeated runs, interrupted creates, minimal updates, strict
cutoff handling, empty-source protection, duplicates, content types, ETags, all-page
enumeration beyond 9,999 items, batch limits, retries, and missing custom fields.
The CI workflow tests Python 3.12 and 3.14. Tenant permissions, actual field settings,
and live writes must be validated during the first authenticated run.

Microsoft references:
[Selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview),
[Graph JSON batching](https://learn.microsoft.com/en-us/graph/json-batching),
[Create list items](https://learn.microsoft.com/en-us/graph/api/listitem-create?view=graph-rest-1.0),
[Update list fields](https://learn.microsoft.com/en-us/graph/api/listitem-update?view=graph-rest-1.0).
