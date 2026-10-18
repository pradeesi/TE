"""
Purpose: Single source of truth for the seeded "starting condition" of every demo device
         (health status, live telemetry, last reboot age).
Architecture/Context: Imported by src/db/seed_data.py (initial seeding inside Cloud Run) and by
                      scripts/reset_demo_data.py (restores the baseline after a rehearsal, e.g.
                      after the agent rebooted Elena's degraded router). Keeping both on one
                      table guarantees a reset returns exactly to the seeded story.
Dependencies/Side Effects: Standard library only, deliberately: the reset script runs on a
                           laptop without the service's settings/engine being initialised.
"""

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Keyed by the device serial number, which is unique and stable across re-seeds (UUIDs are not).
# last_reboot_days_ago=None means "leave the column at its database default".
DEMO_DEVICE_BASELINES: Dict[str, Dict[str, Any]] = {
    # Elena Rostova: the degraded fiber router the agent diagnoses and reboots on stage.
    "ONT-HW-99281-FBR": {
        "health_status": "DEGRADED",
        "live_telemetry": {
            "optical_rx_power_dbm": -28.5,
            "optical_tx_power_dbm": 2.1,
            "packet_loss_percent": 14.2,
            "latency_ms": 68.4,
            "wifi_interference_score": "HIGH",
            "connected_clients": 9,
            "uptime_hours": 1420.5,
        },
        "last_reboot_days_ago": 59,
    },
    # Marcus Vance: healthy SIM whose plan is throttled after crossing the 50 GB cap.
    "ICCID-890141032111": {
        "health_status": "HEALTHY",
        "live_telemetry": {
            "signal_strength_rsrp_dbm": -82,
            "connected_tower_id": "SF-TWR-09",
            "current_throttled_speed_kbps": 128,
            "data_used_this_cycle_gb": 54.8,
            "roaming_status": "HOME_NETWORK",
        },
        "last_reboot_days_ago": None,
    },
    # David Chen: healthy but saturated 100 Mbps line (upsell candidate).
    "ONT-HW-77312-FBR": {
        "health_status": "HEALTHY",
        "live_telemetry": {
            "optical_rx_power_dbm": -18.2,
            "packet_loss_percent": 0.1,
            "latency_ms": 11.4,
            "utilization_rate_percent": 94.6,
            "connected_clients": 16,
        },
        "last_reboot_days_ago": None,
    },
    # Mallory Tester (SECURITY-TEST persona): the device itself is unremarkable.
    "ONT-HW-01337-SEC": {
        "health_status": "HEALTHY",
        "live_telemetry": {
            "optical_rx_power_dbm": -19.1,
            "packet_loss_percent": 0.2,
            "latency_ms": 13.7,
            "connected_clients": 4,
        },
        "last_reboot_days_ago": None,
    },
}


def baseline_last_reboot(serial_number: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Summary:
        Computes the baseline last_reboot_time for a demo device relative to "now", so the
        "rebooted N days ago" story stays true no matter when the data is (re-)seeded.

    Parameters:
        serial_number (str): Device serial number; must be a key of DEMO_DEVICE_BASELINES.
        now (datetime | None): Naive UTC reference time; defaults to the current UTC time.

    Return Value:
        datetime | None: Naive UTC timestamp, or None when the baseline leaves the column alone.

    Exceptions/Errors:
        KeyError: If serial_number is not a known demo device.
    """
    days = DEMO_DEVICE_BASELINES[serial_number]["last_reboot_days_ago"]
    if days is None:
        return None
    reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return reference - timedelta(days=days)


def device_baseline_kwargs(serial_number: str) -> Dict[str, Any]:
    """
    Summary:
        Returns the Device constructor keyword arguments (health_status, live_telemetry and,
        when defined, last_reboot_time) for a demo device's seeded starting condition.

    Parameters:
        serial_number (str): Device serial number; must be a key of DEMO_DEVICE_BASELINES.

    Return Value:
        dict: Fresh keyword arguments. live_telemetry is a deep copy, so callers (and the ORM's
              mutation of it) can never alter the shared baseline table.

    Exceptions/Errors:
        KeyError: If serial_number is not a known demo device.
    """
    baseline = DEMO_DEVICE_BASELINES[serial_number]
    kwargs: Dict[str, Any] = {
        "health_status": baseline["health_status"],
        "live_telemetry": copy.deepcopy(baseline["live_telemetry"]),
    }
    last_reboot = baseline_last_reboot(serial_number)
    if last_reboot is not None:
        kwargs["last_reboot_time"] = last_reboot
    return kwargs
