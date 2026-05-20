#!/usr/bin/env python3
"""
Class Action Scout — Scheduler
================================
Runs the scout on a cron-like schedule using the 'schedule' library.
For production, prefer OS-level cron (see crontab examples below).

Crontab setup:
  crontab -e
  # Daily run at 06:00
  0 6 * * * cd /path/to/class-action-scout && /path/to/venv/bin/python main.py --run-now >> logs/cron.log 2>&1
  # Weekly report every Sunday at 08:00
  0 8 * * 0 cd /path/to/class-action-scout && /path/to/venv/bin/python main.py --report --days 7 --format html >> logs/cron.log 2>&1
"""
import time
import logging

import schedule

from main import ClassActionScout
from config.settings import DAILY_RUN_HOUR, WEEKLY_REPORT_DAY, WEEKLY_REPORT_HOUR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/scheduler.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scheduler")

scout = ClassActionScout()


def daily_run():
    logger.info("=== DAILY RUN START ===")
    try:
        scout.run()
    except Exception as e:
        logger.error(f"Daily run failed: {e}", exc_info=True)


def weekly_report():
    logger.info("=== WEEKLY REPORT START ===")
    try:
        scout.print_report(days=7, format="html")
    except Exception as e:
        logger.error(f"Weekly report failed: {e}", exc_info=True)


# Schedule
daily_time = f"{DAILY_RUN_HOUR:02d}:00"
weekly_time = f"{WEEKLY_REPORT_HOUR:02d}:00"

schedule.every().day.at(daily_time).do(daily_run)
getattr(schedule.every(), WEEKLY_REPORT_DAY).at(weekly_time).do(weekly_report)

logger.info(f"Scheduler started. Daily at {daily_time}, weekly {WEEKLY_REPORT_DAY} at {weekly_time}")
logger.info("Press Ctrl+C to stop.")

while True:
    schedule.run_pending()
    time.sleep(60)
