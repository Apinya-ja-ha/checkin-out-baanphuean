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
    """Generate and send daily summary to hotel staff + admin.

    5pm report → covers กะเช้า (08:00-16:59) of today
    8am report → covers กะเย็น (17:00-07:59) that started yesterday
    """
    try:
        LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        HOTEL_STAFF_USER_ID       = os.getenv("HOTEL_STAFF_USER_ID")
        ADMIN_USER_IDS            = os.getenv("ADMIN_USER_IDS", "")

        if not LINE_CHANNEL_ACCESS_TOKEN or not HOTEL_STAFF_USER_ID:
            print(f"[WARN] Missing LINE config for {report_time} report")
            return

        line_bot_api  = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        hotel_service = HotelSheetService()

        now   = datetime.now(TZ)
        today = now.date()

        # Determine which shift this report covers
        if report_time == "5pm":
            shift_label = "กะเช้า"
            shift_date  = today          # กะเช้า of today
            data_date   = today
        else:  # 8am
            shift_label = "กะเย็น"
            shift_date  = today - timedelta(days=1)  # กะเย็น started yesterday
            data_date   = today - timedelta(days=1)

        revenue_data    = hotel_service.get_daily_revenue(data_date, data_date)
        usage_data      = hotel_service.get_usage_count(data_date, data_date)
        occupancy_stats = hotel_service.get_occupancy_stats(data_date, data_date)
        empty_rooms     = hotel_service.get_empty_rooms(data_date, data_date)
        shift_notes     = hotel_service.get_shift_notes(shift_label, shift_date)

        summary_text = report_module.daily_summary_text(
            revenue_data, usage_data, occupancy_stats, empty_rooms,
            report_time=report_time, shift_notes=shift_notes
        )

        msg = TextSendMessage(text=summary_text)

        # Send to hotel staff
        line_bot_api.push_message(HOTEL_STAFF_USER_ID, msg)

        # Also send to all admins
        for uid in ADMIN_USER_IDS.split(","):
            uid = uid.strip()
            if uid and uid != HOTEL_STAFF_USER_ID:
                try:
                    line_bot_api.push_message(uid, msg)
                except Exception:
                    pass

        print(f"[OK] Daily {report_time} ({shift_label}) summary sent")

    except Exception as e:
        print(f"[ERROR] send_daily_summary({report_time}): {e}")


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
