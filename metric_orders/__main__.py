import argparse
import json
import logging
import os
from pathlib import Path

from .bigcommerce import BigCommerceClient
from .config import Config
from .errors import SyncError
from .sharepoint import SharePointClient
from .sync import run_sync


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synchronize BigCommerce orders to MetricOrders")
    parser.add_argument("--mode", choices=("upsert", "rebuild"), default="upsert")
    parser.add_argument("--dry-run", action="store_true", help="Read and validate; perform no list writes")
    parser.add_argument("--summary-json", type=Path, help="Write a counts-only report")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = {"status": "failed", "mode": args.mode, "dry_run": args.dry_run}
    result = 0
    try:
        config = Config.from_env()
        run_sync(BigCommerceClient(config), SharePointClient(config),
                 mode=args.mode, dry_run=args.dry_run, summary=summary)
    except SyncError as error:
        logging.error("%s", error)
        summary["error"] = str(error)
        result = 1
    except Exception:
        # Avoid tracebacks that could reveal tokens or sensitive API payloads.
        logging.error("Unexpected failure; consult the last completed operation and rerun a dry-run")
        summary["error"] = "Unexpected failure"
        result = 1
    finally:
        logging.info("Summary: %s", json.dumps(summary, sort_keys=True))
        try:
            if args.summary_json:
                args.summary_json.parent.mkdir(parents=True, exist_ok=True)
                args.summary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            if os.getenv("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                    handle.write("## MetricOrders sync\n\n")
                    handle.write("| Metric | Result |\n| --- | --- |\n")
                    for key, value in summary.items():
                        # Restrict report values to one Markdown table cell.
                        text = str(value).replace("|", "\\|").replace("\n", " ")
                        handle.write(f"| {key} | {text} |\n")
        except OSError:
            logging.error("Unable to write the summary report")
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
