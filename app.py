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
from linebot.models import QuickReply, QuickReplyButton, MessageAction
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
#  COMMAND HANDLERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def handle_checkin_command(event, user_id):
    """Handle /checkin flow."""
    session = _get_or_create_session(user_id)
    session["command"] = "checkin"
    session["step"] = "room"

    _reply(event, "📍 ห้องหมายเลขเท่าไหร่? (1-26 หรือ 101-126)")


def handle_checkin_room_step(event, user_id, text):
    """Checkin: get room number."""
    session = _get_or_create_session(user_id)
    room_num = text.strip()

    # Validate room number
    if not HotelSheetService._normalize_room(room_num):
        _reply(event, "❌ ห้องหมายเลขไม่ถูกต้อง (ใช้ 1-26 หรือ 101-126)")
        return

    session["data"]["room"] = HotelSheetService._normalize_room(room_num)
    session["step"] = "type"

    _reply(event, "🛏️  ประเภทห้องไหน?",
           quick_items=[("ค้างคืน", "ค้างคืน"), ("ชั่วคราว", "ชั่วคราว")])


def handle_checkin_type_step(event, user_id, text):
    """Checkin: get room type."""
    session = _get_or_create_session(user_id)

    if "ค้างคืน" in text:
        room_type = "ค้างคืน"
        session["step"] = "bed_type"
        _reply(event, "🛏️  ประเภทเตียงไหน?",
               quick_items=[("เตียงเดี่ยว (450฿)", "เดี่ยว"), ("เตียงคู่ (500฿)", "คู่")])

    elif "ชั่วคราว" in text:
        room_type = "ชั่วคราว"
        session["data"]["room_type"] = room_type
        session["step"] = "duration"
        _reply(event, "⏱️  ระยะเวลา?",
               quick_items=[("2 ชม (180฿)", "2 ชม (180฿)"), ("3 ชม (270฿)", "3 ชม (270฿)"), ("อื่น", "อื่น")])
    else:
        _reply(event, "❌ โปรดเลือก ค้างคืน หรือ ชั่วคราว")


def handle_checkin_bed_type_step(event, user_id, text):
    """Checkin overnight: get bed type."""
    session = _get_or_create_session(user_id)

    if "เดี่ยว" in text:
        session["data"]["bed_type"] = "เตียงเดี่ยว"
        session["data"]["rate"] = 450
        session["data"]["room_type"] = "ค้างคืน"
    elif "คู่" in text:
        session["data"]["bed_type"] = "เตียงคู่"
        session["data"]["rate"] = 500
        session["data"]["room_type"] = "ค้างคืน"
    else:
        _reply(event, "❌ โปรดเลือก เตียงเดี่ยว หรือ เตียงคู่")
        return

    session["step"] = "checkin_time"
    _reply(event, "🕐 เวลาเช็คอิน?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("กำหนดเอง", "กำหนดเอง")])


def handle_checkin_duration_step(event, user_id, text):
    """Checkin temporary: get duration."""
    session = _get_or_create_session(user_id)

    if "2 ชม" in text:
        session["data"]["duration"] = 2
        session["data"]["rate"] = 180
    elif "3 ชม" in text:
        session["data"]["duration"] = 3
        session["data"]["rate"] = 270
    elif "อื่น" in text:
        session["step"] = "custom_duration"
        _reply(event, "⏱️  ระบุชั่วโมง (เช่น 1.5):")
        return
    else:
        _reply(event, "❌ โปรดเลือก 2 ชม, 3 ชม, หรือ อื่น")
        return

    session["step"] = "checkin_time"
    _reply(event, "🕐 เวลาเช็คอิน?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("กำหนดเอง", "กำหนดเอง")])


def handle_checkin_custom_duration_step(event, user_id, text):
    """Checkin temporary: get custom hours."""
    session = _get_or_create_session(user_id)

    try:
        hours = float(text.strip())
        if hours <= 0:
            _reply(event, "❌ ระบุเวลามากกว่า 0 ชั่วโมง")
            return
        session["data"]["duration"] = hours
        session["step"] = "custom_rate"
        _reply(event, f"💰 ราคาเท่าไหร่ (เช่น {int(hours * 100)})?")
    except ValueError:
        _reply(event, "❌ ระบุตัวเลข (เช่น 1.5)")


def handle_checkin_custom_rate_step(event, user_id, text):
    """Checkin temporary: get custom rate."""
    session = _get_or_create_session(user_id)

    try:
        rate = int(float(text.strip()))
        if rate <= 0:
            _reply(event, "❌ ราคาต้องมากกว่า 0 บาท")
            return
        session["data"]["rate"] = rate
        session["data"]["room_type"] = "ชั่วคราว"
        session["step"] = "checkin_time"
        _reply(event, "🕐 เวลาเช็คอิน?",
               quick_items=[("ตอนนี้", "now"), ("กำหนดเอง", "custom")])
    except ValueError:
        _reply(event, "❌ ระบุตัวเลข (เช่น 150)")


def handle_checkin_time_step(event, user_id, text):
    """Checkin: get check-in time."""
    session = _get_or_create_session(user_id)

    if "ตอนนี้" in text:
        session["data"]["checkin_time"] = datetime.now(TZ)
    elif "กำหนดเอง" in text:
        session["step"] = "checkin_time_custom"
        _reply(event, "🕐 เช็คอินเวลา (HH:MM เช่น 14:30):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ กำหนดเอง")
        return

    session["step"] = "confirm"
    _show_checkin_confirm(event, user_id, session)


def handle_checkin_time_custom_step(event, user_id, text):
    """Checkin: get custom check-in time."""
    session = _get_or_create_session(user_id)

    try:
        time_obj = datetime.strptime(text.strip(), "%H:%M").time()
        now = datetime.now(TZ)
        checkin_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
        session["data"]["checkin_time"] = checkin_time
        session["step"] = "confirm"
        _show_checkin_confirm(event, user_id, session)
    except ValueError:
        _reply(event, "❌ ใช้รูปแบบ HH:MM (เช่น 14:30)")


def _show_checkin_confirm(event, user_id, session):
    """Show check-in confirmation."""
    data = session.get("data", {})
    room = data.get("room", "?")
    room_type = data.get("room_type", "?")
    rate = data.get("rate", 0)
    bed_type = data.get("bed_type", "")
    checkin_time = data.get("checkin_time", datetime.now(TZ))

    if room_type == "ค้างคืน":
        summary = f"ห้อง: {room}\nประเภท: {room_type} ({bed_type})\nราคา: {rate}฿\nเวลา: {checkin_time.strftime('%H:%M')}"
    else:
        duration = data.get("duration", "?")
        summary = f"ห้อง: {room}\nประเภท: {room_type}\nระยะเวลา: {duration} ชม\nราคา: {rate}฿\nเวลา: {checkin_time.strftime('%H:%M')}"

    _reply(event, f"✓ ยืนยันข้อมูล:\n\n{summary}\n\nถูกต้องไหม?",
           quick_items=[("ยืนยัน", "confirm_checkin"), ("แก้ไข", "/checkin"), ("ยกเลิก", "/cancel")])


def handle_changeroom_command(event, user_id):
    """Handle /changeroom flow."""
    session = _get_or_create_session(user_id)
    session["command"] = "changeroom"
    session["step"] = "old_room"

    _reply(event, "🏠 ห้องเก่า (ออก) - ห้องหมายเลขเท่าไหร่?")


def handle_checkout_command(event, user_id):
    """Handle /checkout flow."""
    session = _get_or_create_session(user_id)
    session["command"] = "checkout"
    session["step"] = "room"

    _reply(event, "📍 ห้องหมายเลขเท่าไหร่? (1-26 หรือ 101-126)")


def handle_checkout_room_step(event, user_id, text):
    """Checkout: get room number."""
    session = _get_or_create_session(user_id)
    room_num = text.strip()

    if not HotelSheetService._normalize_room(room_num):
        _reply(event, "❌ ห้องหมายเลขไม่ถูกต้อง")
        return

    session["data"]["room"] = HotelSheetService._normalize_room(room_num)
    session["step"] = "checkout_time"

    _reply(event, "🕐 เช็คเอาท์เวลา?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("เวลาอื่น", "เวลาอื่น")])


def handle_checkout_time_step(event, user_id, text):
    """Checkout: get check-out time."""
    session = _get_or_create_session(user_id)

    if "ตอนนี้" in text:
        session["data"]["checkout_time"] = datetime.now(TZ)
    elif "เวลาอื่น" in text:
        session["step"] = "checkout_time_custom"
        _reply(event, "🕐 เช็คเอาท์เวลา (HH:MM เช่น 16:45):")
        return

    session["step"] = "confirm"
    _show_checkout_confirm(event, user_id, session)


def handle_checkout_time_custom_step(event, user_id, text):
    """Checkout: get custom check-out time."""
    session = _get_or_create_session(user_id)

    try:
        time_obj = datetime.strptime(text.strip(), "%H:%M").time()
        now = datetime.now(TZ)
        checkout_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
        session["data"]["checkout_time"] = checkout_time
        session["step"] = "confirm"
        _show_checkout_confirm(event, user_id, session)
    except ValueError:
        _reply(event, "❌ ใช้รูปแบบ HH:MM")


def _show_checkout_confirm(event, user_id, session):
    """Show check-out confirmation."""
    data = session.get("data", {})
    room = data.get("room", "?")
    checkout_time = data.get("checkout_time", datetime.now(TZ))

    _reply(event, f"✓ ยืนยันเช็คเอาท์\n\nห้อง: {room}\nเวลา: {checkout_time.strftime('%H:%M')}\n\nถูกต้องไหม?",
           quick_items=[("ยืนยัน", "confirm_checkout"), ("แก้ไข", "/checkout"), ("ยกเลิก", "/cancel")])


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
        if current_step == "room":
            handle_checkin_room_step(event, user_id, text)
        elif current_step == "type":
            handle_checkin_type_step(event, user_id, text)
        elif current_step == "bed_type":
            handle_checkin_bed_type_step(event, user_id, text)
        elif current_step == "duration":
            handle_checkin_duration_step(event, user_id, text)
        elif current_step == "custom_duration":
            handle_checkin_custom_duration_step(event, user_id, text)
        elif current_step == "custom_rate":
            handle_checkin_custom_rate_step(event, user_id, text)
        elif current_step == "checkin_time":
            handle_checkin_time_step(event, user_id, text)
        elif current_step == "checkin_time_custom":
            handle_checkin_time_custom_step(event, user_id, text)
        elif current_step == "confirm":
            if "confirm_checkin" in text:
                # Save to Sheets
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
                    _reply(event, f"✅ เช็คอินสำเร็จ ห้อง {result['room']} ({result['rate']}฿)")
                _clear_session(user_id)

    elif current_command == "checkout":
        if current_step == "room":
            handle_checkout_room_step(event, user_id, text)
        elif current_step == "checkout_time":
            handle_checkout_time_step(event, user_id, text)
        elif current_step == "checkout_time_custom":
            handle_checkout_time_custom_step(event, user_id, text)
        elif current_step == "confirm":
            if "confirm_checkout" in text:
                # Save to Sheets (simplified - assume 1 hour default if not tracked)
                data = session.get("data", {})
                result = hotel_service.record_checkout(
                    room_number=data.get("room"),
                    checkout_time=data.get("checkout_time"),
                    actual_duration_hours=1.0,  # TODO: calculate from check-in
                    final_cost=0  # TODO: calculate from check-in
                )
                if "error" in result:
                    _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
                else:
                    _reply(event, f"✅ เช็คเอาท์สำเร็จ ห้อง {result['room']}")
                _clear_session(user_id)

    elif current_command == "other":
        if current_step == "note":
            handle_other_note_step(event, user_id, text)

    else:
        # No active session - just echo/help
        _reply(event, "ใช้ /checkin /checkout /other /week หรือ /help เพื่อเริ่มต้น")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SERVER LIFECYCLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    # Run Flask (scheduler already started above)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
