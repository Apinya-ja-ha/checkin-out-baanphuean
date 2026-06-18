"""
Checkin-Out บ้านเพื่อน — LINE OA bot for hotel room check-in/check-out tracking.

Features:
    /checkin    → Interactive flow to record room check-in
    /checkout   → Record room check-out
    /changeroom → Switch guest to different room
    /other      → Free-text note (housekeeper tasks, maintenance)
    /week       → Weekly report (admin only)
    /month      → Monthly report (admin only)
    /comonth    → Cumulative monthly report (admin only)
    /help       → Show help
    /cancel     → Cancel current operation
"""

import os
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models.events import MessageEvent
from linebot.models.messages import TextMessage
from linebot.models.send_messages import TextSendMessage, ImageSendMessage
from linebot.models import (
    QuickReply, QuickReplyButton, MessageAction,
    TemplateSendMessage, CarouselTemplate, CarouselColumn,
    MessageTemplateAction
)
import re
from dotenv import load_dotenv
import requests

from hotel_service import HotelSheetService
import report as report_module
import scheduler

load_dotenv()

app = Flask(__name__)
TZ = ZoneInfo("Asia/Bangkok")

# ── LINE configuration ─────────────────────────────────────────────
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ── Google Sheets ──────────────────────────────────────────────────
GOOGLE_SERVICE_ACCOUNT_KEY = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "./service-account.json")
hotel_service = HotelSheetService(GOOGLE_SERVICE_ACCOUNT_KEY)

# ── Admin authorization ────────────────────────────────────────────
ADMIN_USER_IDS = set(
    uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
)

# ── In-memory session management (single worker only!) ──────────────
user_sessions = {}

# ── Start scheduler on app init ────────────────────────────────────
scheduler.start_scheduler(app)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RICH MENU SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def setup_rich_menu():
    """Create Rich Menu with 6 buttons (2x3 layout)."""
    rich_menu = {
        "size": {"width": 2, "height": 3},
        "selected": True,
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/checkin", "label": "เช็คอิน"}
            },
            {
                "bounds": {"x": 1, "y": 0, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/checkout", "label": "เช็คเอาท์"}
            },
            {
                "bounds": {"x": 0, "y": 1, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/changeroom", "label": "เปลี่ยนห้อง"}
            },
            {
                "bounds": {"x": 1, "y": 1, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/other", "label": "อื่นๆ"}
            },
            {
                "bounds": {"x": 0, "y": 2, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/save", "label": "บันทึก"}
            },
            {
                "bounds": {"x": 1, "y": 2, "width": 1, "height": 1},
                "action": {"type": "message", "text": "/cancel", "label": "ยกเลิก"}
            }
        ]
    }

    try:
        headers = {
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        response = requests.post(
            "https://api.line.biz/v1/bot/richmenu",
            json=rich_menu,
            headers=headers
        )

        if response.status_code == 200:
            menu_id = response.json().get("richMenuId")
            print(f"✅ Rich Menu created: {menu_id}")
            return menu_id
        else:
            print(f"⚠️  Rich Menu creation failed: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Rich Menu error: {e}")
        return None


# Initialize Rich Menu on startup
rich_menu_id = setup_rich_menu()


def _reply(event, text, quick_items=None):
    """Send a reply with optional quick-reply buttons.
    Falls back to push_message if reply token is expired."""
    qr = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=lbl, text=val))
        for lbl, val in quick_items
    ]) if quick_items else None
    msg = TextSendMessage(text=text, quick_reply=qr)
    try:
        line_bot_api.reply_message(event.reply_token, msg)
    except Exception:
        try:
            line_bot_api.push_message(event.source.user_id, msg)
        except Exception:
            pass


def _push(user_id, text, quick_items=None):
    """Send a message proactively."""
    qr = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=lbl, text=val))
        for lbl, val in quick_items
    ]) if quick_items else None
    msg = TextSendMessage(text=text, quick_reply=qr)
    try:
        line_bot_api.push_message(user_id, msg)
    except Exception:
        pass


def _get_or_create_session(user_id):
    """Get or create a user session."""
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "step": None,
            "command": None,
            "data": {}
        }
    return user_sessions[user_id]


def _clear_session(user_id):
    """Clear user session."""
    if user_id in user_sessions:
        del user_sessions[user_id]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROOM LISTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROOMS_SINGLE = [str(100+i) for i in range(1, 15)]   # 101-114 เตียงเดี่ยว/ชั่วคราว
ROOMS_TWIN   = [str(100+i) for i in range(15, 27)]  # 115-126 เตียงคู่
ROOM_SPECIAL = ["117"]                               # ห้องพิเศษ 400฿


def _send_carousel(event, user_id, room_list, prompt="เลือกห้องพัก"):
    """Send room-selection carousel (3 rooms per card)."""
    chunks = [room_list[i:i+3] for i in range(0, len(room_list), 3)]
    columns = []
    for chunk in chunks:
        label = f"ห้อง {chunk[0]}" if len(chunk) == 1 else f"ห้อง {chunk[0]}–{chunk[-1]}"
        actions = [
            MessageTemplateAction(label=f"ห้อง {r}", text=f"เลือกห้อง {r}")
            for r in chunk
        ]
        columns.append(CarouselColumn(title="เลือกห้องพัก", text=label, actions=actions))

    msg = TemplateSendMessage(
        alt_text=prompt,
        template=CarouselTemplate(columns=columns)
    )
    try:
        line_bot_api.reply_message(event.reply_token, msg)
    except Exception:
        try:
            line_bot_api.push_message(user_id, msg)
        except Exception as e:
            print(f"[WARN] Carousel send error: {e}")


def _send_checkout_carousel(event, user_id):
    """Send carousel showing only checked-in rooms."""
    checkin_rooms = hotel_service.get_checked_in_rooms()
    if not checkin_rooms:
        _reply(event, "ℹ️ ไม่มีห้องที่เช็คอินอยู่ในขณะนี้")
        return False
    _send_carousel(event, user_id, checkin_rooms, "เลือกห้องที่ต้องการเช็คเอาท์")
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  COMMAND HANDLERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── CHECKIN ────────────────────────────────────────────────────────

def handle_checkin_command(event, user_id):
    session = _get_or_create_session(user_id)
    session["command"] = "checkin"
    session["step"] = "type"
    _reply(event, "🏨 เช็คอินประเภทไหน?",
           quick_items=[("ชั่วคราว", "ชั่วคราว"), ("ค้างคืน", "ค้างคืน")])


def handle_checkin_type_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "ชั่วคราว" in text:
        session["data"]["room_type"] = "ชั่วคราว"
        session["step"] = "duration"
        _reply(event, "⏱️ ระยะเวลา?",
               quick_items=[("2 ชม (180฿)", "2 ชม"), ("3 ชม (210฿)", "3 ชม"), ("อื่น", "อื่น")])
    elif "ค้างคืน" in text:
        session["data"]["room_type"] = "ค้างคืน"
        session["step"] = "nights"
        _reply(event, "🌙 พักกี่คืน?",
               quick_items=[("1 คืน", "1 คืน"), ("2 คืน", "2 คืน"), ("อื่น", "อื่น")])
    else:
        _reply(event, "❌ โปรดเลือก ชั่วคราว หรือ ค้างคืน")


def handle_checkin_nights_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "1 คืน" in text:
        session["data"]["nights"] = 1
    elif "2 คืน" in text:
        session["data"]["nights"] = 2
    elif "อื่น" in text:
        session["step"] = "custom_nights"
        _reply(event, "🌙 ระบุจำนวนคืน (เช่น 3):")
        return
    else:
        _reply(event, "❌ โปรดระบุจำนวนคืน")
        return
    session["step"] = "bed_type"
    _reply(event, "🛏️ ประเภทเตียง?",
           quick_items=[
               ("เตียงคู่ (500฿)", "เตียงคู่"),
               ("เตียงเดี่ยว (450฿)", "เตียงเดี่ยว"),
               ("ห้องพิเศษ (400฿)", "ห้องพิเศษ")
           ])


def handle_checkin_custom_nights_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    try:
        nights = int(text.strip())
        if nights <= 0:
            _reply(event, "❌ ระบุมากกว่า 0 คืน")
            return
        session["data"]["nights"] = nights
        session["step"] = "bed_type"
        _reply(event, "🛏️ ประเภทเตียง?",
               quick_items=[
                   ("เตียงคู่ (500฿)", "เตียงคู่"),
                   ("เตียงเดี่ยว (450฿)", "เตียงเดี่ยว"),
                   ("ห้องพิเศษ (400฿)", "ห้องพิเศษ")
               ])
    except ValueError:
        _reply(event, "❌ ระบุตัวเลข (เช่น 3)")


def handle_checkin_bed_type_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "เตียงคู่" in text:
        session["data"]["bed_type"] = "เตียงคู่"
        session["data"]["rate"] = 500
        room_list = ROOMS_TWIN
    elif "เตียงเดี่ยว" in text:
        session["data"]["bed_type"] = "เตียงเดี่ยว"
        session["data"]["rate"] = 450
        room_list = ROOMS_SINGLE
    elif "ห้องพิเศษ" in text:
        session["data"]["bed_type"] = "ห้องพิเศษ"
        session["data"]["rate"] = 400
        room_list = ROOM_SPECIAL
    else:
        _reply(event, "❌ โปรดเลือกประเภทเตียง")
        return
    session["step"] = "room"
    _send_carousel(event, user_id, room_list, "เลือกห้องพัก")


def handle_checkin_duration_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "2 ชม" in text:
        session["data"]["duration"] = 2
        session["data"]["rate"] = 180
    elif "3 ชม" in text:
        session["data"]["duration"] = 3
        session["data"]["rate"] = 210
    elif "อื่น" in text:
        session["step"] = "custom_duration"
        _reply(event, "⏱️ ระบุชั่วโมง (เช่น 1.5):")
        return
    else:
        _reply(event, "❌ โปรดเลือกระยะเวลา")
        return
    session["step"] = "room"
    _send_carousel(event, user_id, ROOMS_SINGLE, "เลือกห้องพัก (ชั่วคราว)")


def handle_checkin_custom_duration_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    try:
        hours = float(text.strip())
        if hours <= 0:
            _reply(event, "❌ ระบุมากกว่า 0 ชั่วโมง")
            return
        session["data"]["duration"] = hours
        session["step"] = "custom_rate"
        _reply(event, f"💰 ราคาเท่าไหร่?")
    except ValueError:
        _reply(event, "❌ ระบุตัวเลข (เช่น 1.5)")


def handle_checkin_custom_rate_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    try:
        rate = int(float(text.strip()))
        if rate <= 0:
            _reply(event, "❌ ราคาต้องมากกว่า 0 บาท")
            return
        session["data"]["rate"] = rate
        session["step"] = "room"
        _send_carousel(event, user_id, ROOMS_SINGLE, "เลือกห้องพัก")
    except ValueError:
        _reply(event, "❌ ระบุตัวเลข (เช่น 150)")


def handle_checkin_room_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    match = re.search(r'(\d{3})', text)
    if not match:
        _reply(event, "❌ กรุณาเลือกห้องจากเมนูด้านบน")
        return
    session["data"]["room"] = match.group(1)
    session["step"] = "checkin_time"
    _reply(event, f"🕐 เวลาเช็คอินห้อง {match.group(1)}?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("กำหนดเอง", "กำหนดเอง")])


def handle_checkin_time_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "ตอนนี้" in text:
        session["data"]["checkin_time"] = datetime.now(TZ)
    elif "กำหนดเอง" in text:
        session["step"] = "checkin_time_custom"
        _reply(event, "🕐 ระบุเวลาเช็คอิน (HH:MM เช่น 14:30):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ กำหนดเอง")
        return
    session["step"] = "confirm"
    _show_checkin_confirm(event, user_id, session)


def handle_checkin_time_custom_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    try:
        t = datetime.strptime(text.strip(), "%H:%M").time()
        now = datetime.now(TZ)
        session["data"]["checkin_time"] = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        session["step"] = "confirm"
        _show_checkin_confirm(event, user_id, session)
    except ValueError:
        _reply(event, "❌ ใช้รูปแบบ HH:MM (เช่น 14:30)")


def _show_checkin_confirm(event, user_id, session):
    data = session.get("data", {})
    room = data.get("room", "?")
    room_type = data.get("room_type", "?")
    rate = data.get("rate", 0)
    checkin_time = data.get("checkin_time", datetime.now(TZ))

    if room_type == "ค้างคืน":
        bed = data.get("bed_type", "")
        nights = data.get("nights", 1)
        summary = f"🏠 ห้อง: {room}\n🛏️ {bed}\n🌙 {nights} คืน\n💰 {rate}฿/คืน\n🕐 เวลา: {checkin_time.strftime('%H:%M')}"
    else:
        dur = data.get("duration", "?")
        summary = f"🏠 ห้อง: {room}\n⏱️ ชั่วคราว {dur} ชม\n💰 {rate}฿\n🕐 เวลา: {checkin_time.strftime('%H:%M')}"

    _reply(event, f"📋 ยืนยันข้อมูล:\n\n{summary}\n\nถูกต้องไหม?",
           quick_items=[("✅ ยืนยัน", "confirm_checkin"), ("❌ ยกเลิก", "/cancel")])


# ── CHECKOUT ───────────────────────────────────────────────────────

def handle_checkout_command(event, user_id):
    session = _get_or_create_session(user_id)
    session["command"] = "checkout"
    session["step"] = "room"
    if not _send_checkout_carousel(event, user_id):
        _clear_session(user_id)


def handle_checkout_room_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    match = re.search(r'(\d{3})', text)
    if not match:
        _reply(event, "❌ กรุณาเลือกห้องจากเมนูด้านบน")
        return
    session["data"]["room"] = match.group(1)
    session["step"] = "checkout_time"
    _reply(event, f"🕐 เวลาเช็คเอาท์ห้อง {match.group(1)}?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("เวลาอื่น", "เวลาอื่น")])


def handle_checkout_time_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "ตอนนี้" in text:
        session["data"]["checkout_time"] = datetime.now(TZ)
    elif "เวลาอื่น" in text:
        session["step"] = "checkout_time_custom"
        _reply(event, "🕐 ระบุเวลาเช็คเอาท์ (HH:MM เช่น 16:45):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ เวลาอื่น")
        return
    session["step"] = "confirm"
    _show_checkout_confirm(event, user_id, session)


def handle_checkout_time_custom_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    try:
        t = datetime.strptime(text.strip(), "%H:%M").time()
        now = datetime.now(TZ)
        session["data"]["checkout_time"] = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        session["step"] = "confirm"
        _show_checkout_confirm(event, user_id, session)
    except ValueError:
        _reply(event, "❌ ใช้รูปแบบ HH:MM")


def _show_checkout_confirm(event, user_id, session):
    data = session.get("data", {})
    room = data.get("room", "?")
    checkout_time = data.get("checkout_time", datetime.now(TZ))
    _reply(event, f"📋 ยืนยันเช็คเอาท์\n\n🏠 ห้อง: {room}\n🕐 เวลา: {checkout_time.strftime('%H:%M')}\n\nถูกต้องไหม?",
           quick_items=[("✅ ยืนยัน", "confirm_checkout"), ("❌ ยกเลิก", "/cancel")])


# ── CHANGEROOM ─────────────────────────────────────────────────────

def handle_changeroom_command(event, user_id):
    session = _get_or_create_session(user_id)
    session["command"] = "changeroom"
    session["step"] = "old_room"
    checkin_rooms = hotel_service.get_checked_in_rooms()
    if not checkin_rooms:
        _reply(event, "ℹ️ ไม่มีห้องที่เช็คอินอยู่ในขณะนี้")
        _clear_session(user_id)
        return
    _send_carousel(event, user_id, checkin_rooms, "เลือกห้องที่ต้องการเปลี่ยน (ห้องเก่า)")


def handle_other_command(event, user_id):
    """Handle /other (free-text notes)."""
    session = _get_or_create_session(user_id)
    session["command"] = "other"
    session["step"] = "note"

    _reply(event, "📝 บันทึกอะไร? (เช่น เปิดไฟซ่อมแอร์ ห้อง 105 หรือ ห้อง 108 ทำเสร็จเวลา 10:30)")


def handle_other_note_step(event, user_id, text):
    """Other: record note."""
    session = _get_or_create_session(user_id)

    result = hotel_service.record_note(text, note_type="Other")
    if "error" in result:
        _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
    else:
        _reply(event, "✓ บันทึกเสร็จแล้ว")

    _clear_session(user_id)


def handle_week_command(event, user_id):
    """Handle /week (admin only)."""
    if user_id not in ADMIN_USER_IDS:
        _reply(event, "❌ เฉพาะแอดมินเท่านั้น")
        return

    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=6)

        revenue_data = hotel_service.get_daily_revenue(start_date, end_date)
        usage_data = hotel_service.get_usage_count(start_date, end_date)
        occupancy_stats = hotel_service.get_occupancy_stats(start_date, end_date)
        empty_rooms = hotel_service.get_empty_rooms(start_date, end_date)

        summary_text = report_module.weekly_summary_text(
            revenue_data, usage_data, occupancy_stats, empty_rooms, days=7
        )

        _reply(event, summary_text)

    except Exception as e:
        _reply(event, f"❌ เกิดข้อผิดพลาด: {str(e)}")


def handle_help_command(event, user_id):
    """Handle /help."""
    help_text = """📋 วิธีใช้งาน:

กดปุ่มเมนูด้านล่างเพื่อ:
✅ เช็คอิน → บันทึกลูกค้าเข้าห้อง
❌ เช็คเอาท์ → บันทึกลูกค้าออกห้อง
🔄 เปลี่ยนห้อง → ย้ายลูกค้าไปห้องอื่น
📝 อื่นๆ → บันทึกหมายเหตุ / แจ้งทำห้องเสร็จ
🟢 บันทึก → ยืนยันและบันทึกข้อมูล
🟢 ยกเลิก → ยกเลิกการทำรายการ"""
    _reply(event, help_text)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WEBHOOK HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle LINE webhook."""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(403)

    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """Main message handler."""
    user_id = event.source.user_id
    text = event.message.text.strip()
    lower_text = text.lower()

    # Log incoming message for User ID tracking
    print(f"[MESSAGE] User: {user_id} | Text: {text}")

    # ── ID lookup shortcut ─────────────────────────────────────────
    if text.lower() in ["/id", "id", "ไอดี", "ID"]:
        _reply(event, f"🆔 ไอดีของคุณ:\n{user_id}")
        return

    # Get current session state
    session = user_sessions.get(user_id, {})
    current_step = session.get("step")
    current_command = session.get("command")

    # ── Top-level commands ─────────────────────────────────────────
    if text.startswith("/"):
        if "/checkin" in lower_text:
            handle_checkin_command(event, user_id)
        elif "/checkout" in lower_text:
            handle_checkout_command(event, user_id)
        elif "/other" in lower_text:
            handle_other_command(event, user_id)
        elif "/week" in lower_text:
            handle_week_command(event, user_id)
        elif "/month" in lower_text:
            _reply(event, "📊 รายงานรายเดือน (เร็วๆ นี้)")
        elif "/comonth" in lower_text:
            _reply(event, "📊 รายงานรวมเดือน (เร็วๆ นี้)")
        elif "/help" in lower_text:
            handle_help_command(event, user_id)
        elif "/changeroom" in lower_text:
            handle_changeroom_command(event, user_id)
        elif "/save" in lower_text:
            # Save button from Rich Menu - trigger confirm action
            if current_command == "checkin" and session.get("step") == "confirm":
                text = "confirm_checkin"
                # Re-process with confirm text
                if "confirm_checkin" in text:
                    data = session.get("data", {})
                    result = hotel_service.record_checkin(
                        room_number=data.get("room"),
                        room_type=data.get("room_type"),
                        checkin_time=data.get("checkin_time"),
                        duration_hours=data.get("duration"),
                        rate_baht=data.get("rate", 0)
                    )
                    if "error" in result:
                        _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
                    else:
                        _reply(event, f"✅ บันทึกสำเร็จ ห้อง {result['room']}")
                    _clear_session(user_id)
            else:
                _reply(event, "❌ ยังไม่มีข้อมูลให้บันทึก\nกรุณาเริ่มจากปุ่ม เช็คอิน / เช็คเอาท์ ก่อน")
        elif "/cancel" in lower_text:
            _clear_session(user_id)
            _reply(event, "✓ ยกเลิกเรียบร้อย")
        else:
            _reply(event, "❓ ไม่รู้จักคำสั่งนี้ กด ปุ่มเมนูด้านล่าง หรือพิมพ์ /help")

    # ── In-session flow ────────────────────────────────────────────
    elif current_command == "checkin":
        if current_step == "type":
            handle_checkin_type_step(event, user_id, text)
        elif current_step == "nights":
            handle_checkin_nights_step(event, user_id, text)
        elif current_step == "custom_nights":
            handle_checkin_custom_nights_step(event, user_id, text)
        elif current_step == "bed_type":
            handle_checkin_bed_type_step(event, user_id, text)
        elif current_step == "duration":
            handle_checkin_duration_step(event, user_id, text)
        elif current_step == "custom_duration":
            handle_checkin_custom_duration_step(event, user_id, text)
        elif current_step == "custom_rate":
            handle_checkin_custom_rate_step(event, user_id, text)
        elif current_step == "room":
            handle_checkin_room_step(event, user_id, text)
        elif current_step == "checkin_time":
            handle_checkin_time_step(event, user_id, text)
        elif current_step == "checkin_time_custom":
            handle_checkin_time_custom_step(event, user_id, text)
        elif current_step == "confirm":
            if "confirm_checkin" in text:
                data = session.get("data", {})
                result = hotel_service.record_checkin(
                    room_number=data.get("room"),
                    room_type=data.get("room_type"),
                    checkin_time=data.get("checkin_time"),
                    duration_hours=data.get("duration"),
                    rate_baht=data.get("rate", 0),
                    special_notes=f"{data.get('bed_type','')} {data.get('nights','')} คืน".strip()
                )
                if "error" in result:
                    _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
                else:
                    _reply(event, f"✅ เช็คอินสำเร็จ ห้อง {result['room']} ({result['rate']}฿)")
                _clear_session(user_id)
            elif "/cancel" in text:
                _clear_session(user_id)
                _reply(event, "✓ ยกเลิกเรียบร้อย")

    elif current_command == "checkout":
        if current_step == "room":
            handle_checkout_room_step(event, user_id, text)
        elif current_step == "checkout_time":
            handle_checkout_time_step(event, user_id, text)
        elif current_step == "checkout_time_custom":
            handle_checkout_time_custom_step(event, user_id, text)
        elif current_step == "confirm":
            if "confirm_checkout" in text:
                data = session.get("data", {})
                result = hotel_service.record_checkout(
                    room_number=data.get("room"),
                    checkout_time=data.get("checkout_time")
                )
                if "error" in result:
                    _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
                else:
                    _reply(event, f"✅ เช็คเอาท์สำเร็จ ห้อง {result['room']}")
                _clear_session(user_id)
            elif "/cancel" in text:
                _clear_session(user_id)
                _reply(event, "✓ ยกเลิกเรียบร้อย")

    elif current_command == "changeroom":
        if current_step == "old_room":
            match = re.search(r'(\d{3})', text)
            if not match:
                _reply(event, "❌ กรุณาเลือกห้องจากเมนูด้านบน")
                return
            session["data"]["old_room"] = match.group(1)
            session["step"] = "new_room"
            _send_carousel(event, user_id, ROOMS_SINGLE + ROOMS_TWIN, "เลือกห้องใหม่")
        elif current_step == "new_room":
            match = re.search(r'(\d{3})', text)
            if not match:
                _reply(event, "❌ กรุณาเลือกห้องจากเมนูด้านบน")
                return
            old_room = session["data"]["old_room"]
            new_room = match.group(1)
            result = hotel_service.record_checkout(old_room, datetime.now(TZ))
            if "error" not in result:
                _reply(event, f"✅ เปลี่ยนห้องสำเร็จ\n🏠 ออกห้อง {old_room} → เข้าห้อง {new_room}\nกรุณาเช็คอินห้อง {new_room} ด้วย /checkin")
            else:
                _reply(event, f"✅ บันทึกเปลี่ยนห้อง {old_room} → {new_room}")
            _clear_session(user_id)

    elif current_command == "other":
        if current_step == "note":
            handle_other_note_step(event, user_id, text)

    else:
        _reply(event, "ใช้ /checkin /checkout /other /week หรือ /help เพื่อเริ่มต้น")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SERVER LIFECYCLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    # Run Flask (scheduler already started above)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
