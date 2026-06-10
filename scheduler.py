"""
Scheduler — Daily automatic reports sent at 5 PM and 8 AM.
Uses APScheduler for cron-based scheduling.
"""

import os
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from linebot import LineBotApi
from linebot.models.send_messages import TextSendMessage

from hotel_service import HotelSheetService
import report as report_module

TZ = ZoneInfo("Asia/Bangkok")

scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Bangkok")


def send_daily_summary(report_time="5pm"):
    """Generate and send daily summary to hotel staff.

    Args:
        report_time: str ("5pm" or "8am")
    """
    try:
        # Get credentials from environment
        LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        HOTEL_STAFF_USER_ID = os.getenv("HOTEL_STAFF_USER_ID")

        if not LINE_CHANNEL_ACCESS_TOKEN or not HOTEL_STAFF_USER_ID:
            print(f"⚠️  Missing LINE config for {report_time} report")
            return

        # Initialize services
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        hotel_service = HotelSheetService()

        # Get data for TODAY (not the whole day, but whatever's been recorded)
        now = datetime.now(TZ)
        today = now.date()

        revenue_data = hotel_service.get_daily_revenue(today, today)
        usage_data = hotel_service.get_usage_count(today, today)
        occupancy_stats = hotel_service.get_occupancy_stats(today, today)
        empty_rooms = hotel_service.get_empty_rooms(today, today)

        # Generate text summary
        summary_text = report_module.daily_summary_text(
            revenue_data,
            usage_data,
            occupancy_stats,
            empty_rooms,
            report_time=report_time
        )

        # Send to staff
        msg = TextSendMessage(text=summary_text)
        line_bot_api.push_message(HOTEL_STAFF_USER_ID, msg)

        print(f"✅ Daily {report_time} summary sent to hotel staff")

    except Exception as e:
        print(f"❌ Error sending {report_time} summary: {e}")


def start_scheduler(app=None):
    """Start the scheduler with cron jobs.

    Args:
        app: Flask app instance (optional, for context)
    """
    if scheduler.running:
        print("⚠️  Scheduler is already running")
        return

    # Schedule 5 PM (17:00) daily
    scheduler.add_job(
        lambda: send_daily_summary("5pm"),
        CronTrigger(hour=17, minute=0, timezone="Asia/Bangkok"),
        id="daily_5pm_report",
        name="Daily 5 PM Report",
        replace_existing=True
    )

    # Schedule 8 AM (08:00) daily
    scheduler.add_job(
        lambda: send_daily_summary("8am"),
        CronTrigger(hour=8, minute=0, timezone="Asia/Bangkok"),
        id="daily_8am_report",
        name="Daily 8 AM Report",
        replace_existing=True
    )

    scheduler.start()
    print("✅ Scheduler started with 5 PM & 8 AM daily report jobs")


def stop_scheduler():
    """Stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        print("✅ Scheduler stopped")
