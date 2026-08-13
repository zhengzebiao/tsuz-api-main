from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session as DbSession

from app.core.database import SessionLocal
from app.core.logging import configure_logging
from app.main import create_app
from app.services.permission_scanner import scan_permission_routes
from app.services.permission_sync_service import PermissionSyncService

logger = logging.getLogger(__name__)

CHECK_DIFFERENCES_EXIT_CODE = 1
COMMAND_ERROR_EXIT_CODE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize declared API permissions.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the synchronization plan without writing PostgreSQL or Redis",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="return non-zero when synchronization differences exist",
    )
    return parser


def run(*, dry_run: bool = False, check: bool = False) -> int:
    if dry_run and check:
        raise ValueError("--dry-run and --check cannot be used together")

    configure_logging()
    db: DbSession | None = None
    try:
        application = create_app()
        scan_result = scan_permission_routes(application)
        db = SessionLocal()
        service = PermissionSyncService(db)
        plan = service.build_plan(scan_result)
        if dry_run or check:
            print(json.dumps(plan.to_dict(), sort_keys=True))
            return CHECK_DIFFERENCES_EXIT_CODE if check and plan.has_changes else 0

        summary = service.apply_plan(plan)
        db.commit()
        print(json.dumps(summary.to_dict(), sort_keys=True))
        logger.info("permission synchronization completed summary=%s", summary.to_dict())
        return 0
    except Exception as exc:
        if db is not None:
            db.rollback()
        logger.error("permission synchronization failed type=%s", type(exc).__name__)
        print(f"permission synchronization failed: {type(exc).__name__}")
        return COMMAND_ERROR_EXIT_CODE
    finally:
        if db is not None:
            db.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(dry_run=args.dry_run, check=args.check)
    except (ValueError, OSError) as exc:
        print(f"permission synchronization failed: {type(exc).__name__}")
        return COMMAND_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
