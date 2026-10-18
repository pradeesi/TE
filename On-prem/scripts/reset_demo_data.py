#!/usr/bin/env python3
"""
Purpose: Restores the on-premises PostgreSQL database to its seeded baseline state
         (e.g., resetting Elena Rostova's ONT fiber router back to DEGRADED after a
         remote reboot action and optionally purging test call interactions).
Architecture/Context:
    - Executed inside the running `telecom-mcp-server` container or on the Ubuntu VM
      host against `DATABASE_URL` from `.env`:
        docker exec telecom-mcp-server python3 -m scripts.reset_demo_data --purge-rehearsal-interactions
Dependencies/Side Effects:
    - Uses `src.db.database.AsyncSessionLocal` and `src.db.demo_baselines`.
    - Updates rows in `devices` and optionally deletes non-seeded rows from `call_interactions`.
"""

import argparse
import asyncio
import copy
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db.database import AsyncSessionLocal  # noqa: E402
from src.db.demo_baselines import DEMO_DEVICE_BASELINES, baseline_last_reboot  # noqa: E402
from src.db.models import CallInteraction, Device  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("telecom_onprem.reset_demo")

SEEDED_AGENT_NAMES = (
    "Gemini Enterprise AI Assist",
    "Frontline Agent (Sarah J. - Ext: 4082)",
    "Frontline Agent (Security Red Team)",
)


async def reset_onprem_database(purge_interactions: bool, dry_run: bool) -> None:
    """
    Summary:
        Resets all seeded demo devices in the on-premises PostgreSQL database to their
        starting health status and telemetry, and optionally deletes ad-hoc call logs.

    Parameters:
        purge_interactions (bool): When True, deletes call_interactions rows not created by initial seeding.
        dry_run (bool): When True, prints planned changes without committing.

    Return Value:
        None.

    Exceptions/Errors:
        RuntimeError: If a baseline device serial number is missing from the database.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as session:
        for serial, baseline in DEMO_DEVICE_BASELINES.items():
            res = await session.execute(select(Device).where(Device.serial_number == serial))
            device = res.scalars().first()
            if device is None:
                raise RuntimeError(f"Seeded device '{serial}' was not found in the database.")

            target_last_reboot = baseline_last_reboot(serial, now=now)
            logger.info(
                "Device %s: status %s -> %s",
                serial,
                device.health_status,
                baseline["health_status"],
            )
            if not dry_run:
                device.health_status = baseline["health_status"]
                device.live_telemetry = copy.deepcopy(baseline["live_telemetry"])
                if target_last_reboot is not None:
                    device.last_reboot_time = target_last_reboot

        if purge_interactions:
            del_stmt = delete(CallInteraction).where(
                CallInteraction.agent_name.notin_(SEEDED_AGENT_NAMES)
            )
            if not dry_run:
                result = await session.execute(del_stmt)
                logger.info("Purged %d non-seeded call interaction record(s).", result.rowcount or 0)
            else:
                logger.info("[DRY-RUN] Would purge non-seeded call interaction records.")

        if dry_run:
            await session.rollback()
            logger.info("Dry run complete; no changes committed.")
        else:
            await session.commit()
            logger.info("On-premises demo baseline successfully restored.")


def main() -> int:
    """
    Summary:
        CLI entry point for resetting on-premises demo baseline state.

    Parameters:
        None (reads sys.argv).

    Return Value:
        int: Process exit code (0 on success, 1 on failure).

    Exceptions/Errors:
        Catches exceptions and logs at ERROR level before returning 1.
    """
    parser = argparse.ArgumentParser(
        description="Restore on-premises PostgreSQL demo devices and interactions to baseline."
    )
    parser.add_argument(
        "--purge-rehearsal-interactions",
        action="store_true",
        help="Also remove call_interactions logged during testing or rehearsals.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect current vs. baseline state without modifying the database.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            reset_onprem_database(
                purge_interactions=args.purge_rehearsal_interactions,
                dry_run=args.dry_run,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to reset on-premises database: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
