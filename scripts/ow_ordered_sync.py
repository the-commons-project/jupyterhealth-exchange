#!/usr/bin/env python3
"""Run inside the OW container to sync Oura sandbox data in the correct order.

Usage (from host):
    docker exec <ow_container> python /root_project/scripts/ow_ordered_sync.py <user_id> [--days 30]

Syncs in order: heart_rate -> spo2 -> sleep -> activity -> readiness -> rest.
Each data type uses its own DB session so a failure in one doesn't cascade.
Heart rate is chunked into 30-day windows (Oura API limit).
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID

sys.path.insert(0, "/root_project")

from app.database import SessionLocal
from app.services.providers.oura.strategy import OuraStrategy

SYNC_ORDER = [
    "heart_rate",
    "spo2",
    "sleep",
    "activity",
    "cardiovascular_age",
    "readiness",
    "sleep_score",
    "vo2_max",
]

HR_CHUNK_DAYS = 29  # Oura limits heartrate to <= 30 days


def sync_heart_rate(oura, user_id, start_time, end_time):
    """Sync heart rate in 29-day chunks to stay within Oura's 30-day limit."""
    total = 0
    chunk_start = start_time
    while chunk_start < end_time:
        chunk_end = min(chunk_start + timedelta(days=HR_CHUNK_DAYS), end_time)
        with SessionLocal() as db:
            data = oura.get_heart_rate_data(db, user_id, chunk_start, chunk_end)
            count = oura.save_heart_rate_data(db, user_id, data)
            db.commit()
            total += count
        chunk_start = chunk_end
    return total


def sync_data_type(oura, user_id, data_type, start_time, end_time):
    """Sync a single data type in its own DB session."""
    with SessionLocal() as db:
        tasks = {
            "spo2": lambda: oura.save_spo2_data(
                db, user_id, oura.get_spo2_data(db, user_id, start_time, end_time)
            ),
            "sleep": lambda: oura.save_sleep_data(
                db, user_id, oura.normalize_sleeps(
                    oura.get_sleep_data(db, user_id, start_time, end_time), user_id
                )
            ),
            "activity": lambda: oura.save_activity_data(
                db, user_id, oura.normalize_activity_samples(
                    oura.get_activity_samples(db, user_id, start_time, end_time), user_id
                )
            ),
            "cardiovascular_age": lambda: oura.save_cardiovascular_age_data(
                db, user_id, oura.normalize_cardiovascular_age_samples(
                    oura.get_cardiovascular_age_samples(db, user_id, start_time, end_time), user_id
                )
            ),
            "readiness": lambda: oura.save_readiness_data(
                db, user_id, oura.normalize_readiness(
                    oura.get_readiness_data(db, user_id, start_time, end_time), user_id
                )
            ),
            "sleep_score": lambda: oura.save_daily_sleep_scores(
                db, user_id, oura.normalize_daily_sleep_scores(
                    oura.get_daily_sleep_score_data(db, user_id, start_time, end_time), user_id
                )
            ),
            "vo2_max": lambda: oura.save_vo2_data(
                db, user_id, oura.get_vo2_data(db, user_id, start_time, end_time)
            ),
        }
        fn = tasks.get(data_type)
        if fn:
            result = fn()
            db.commit()
            return result
    return 0


def main():
    parser = argparse.ArgumentParser(description="Sync Oura sandbox data in correct order")
    parser.add_argument("user_id", type=str, help="OW user UUID")
    parser.add_argument("--days", type=int, default=30, help="Days of history to sync (default: 30)")
    args = parser.parse_args()

    user_id = UUID(args.user_id)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=args.days)

    print(f"User: {user_id}")
    print(f"Window: {start_time.date()} to {end_time.date()} ({args.days} days)")
    print()

    strategy = OuraStrategy()
    oura = strategy.data_247

    for data_type in SYNC_ORDER:
        try:
            if data_type == "heart_rate":
                count = sync_heart_rate(oura, user_id, start_time, end_time)
            else:
                count = sync_data_type(oura, user_id, data_type, start_time, end_time)
            print(f"  {data_type}: {count} records")
        except Exception as e:
            err = str(e).split("\n")[0][:200]
            print(f"  {data_type}: ERROR - {err}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
