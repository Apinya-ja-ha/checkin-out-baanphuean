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
    MessageTemplateAction,
    RichMenu, RichMenuArea, RichMenuBounds, RichMenuSize,
    FlexSendMessage, BubbleContainer,
    BoxComponent, TextComponent, ButtonComponent, SeparatorComponent,
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

_FONT_CACHE = "/tmp/NotoSansThai-Bold.ttf"
_FONT_URL = "https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSansThai/NotoSansThai-Bold.ttf"

_RM_W, _RM_H = 2500, 1686
_CW, _CH = _RM_W // 2, _RM_H // 3   # 1250 × 562 per cell


def _thai_font(size):
    try:
        from PIL import ImageFont
        if not os.path.exists(_FONT_CACHE):
            r = requests.get(_FONT_URL, timeout=10)
            if r.status_code == 200:
                with open(_FONT_CACHE, "wb") as fh:
                    fh.write(r.content)
        if os.path.exists(_FONT_CACHE):
            return ImageFont.truetype(_FONT_CACHE, size)
    except Exception:
        pass
    return None


def _build_rich_menu_image():
    """Return BytesIO PNG 2500×1686 with 6 colored buttons."""
    import io
    from PIL import Image, ImageDraw

    CELLS = [
        (0, 0, (41, 128, 185),  "เช็คอิน"),
        (1, 0, (41, 128, 185),  "เช็คเอาท์"),
        (0, 1, (41, 128, 185),  "เปลี่ยนห้อง"),
        (1, 1, (192, 57,  43),  "อื่นๆ"),
        (0, 2, (39, 174,  96),  "บันทึก"),
        (1, 2, (39, 174,  96),  "ยกเลิก"),
    ]
    PAD, RADIUS = 8, 24

    img = Image.new("RGB", (_RM_W, _RM_H), (20, 20, 20))
    draw = ImageDraw.Draw(img)
    font = _thai_font(110)

    for col, row, color, label in CELLS:
        x0 = col * _CW + PAD
        y0 = row * _CH + PAD
        x1 = x0 + _CW - PAD * 2
        y1 = y0 + _CH - PAD * 2
        draw.rounded_rectangle([x0, y0, x1, y1], radius=RADIUS, fill=color)

        if font:
            try:
                bb = draw.textbbox((0, 0), label, font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                draw.text(
                    (col * _CW + (_CW - tw) // 2, row * _CH + (_CH - th) // 2),
                    label, fill=(255, 255, 255), font=font
                )
            except Exception:
                pass

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def setup_rich_menu():
    """Create 2×3 Rich Menu, upload image, set as default for all users."""
    if not line_bot_api:
        return None
    try:
        # Remove old menus to stay under the 10-menu limit
        try:
            for m in line_bot_api.get_rich_menu_list():
                line_bot_api.delete_rich_menu(m.rich_menu_id)
        except Exception:
            pass

        menu = RichMenu(
            size=RichMenuSize(width=_RM_W, height=_RM_H),
            selected=True,
            name="BaanPhuean",
            chat_bar_text="เมนู",
            areas=[
                RichMenuArea(
                    bounds=RichMenuBounds(x=0,    y=0,       width=_CW, height=_CH),
                    action=MessageAction(label="เช็คอิน",    text="/checkin")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=_CW,  y=0,       width=_CW, height=_CH),
                    action=MessageAction(label="เช็คเอาท์", text="/checkout")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=0,    y=_CH,     width=_CW, height=_CH),
                    action=MessageAction(label="เปลี่ยนห้อง", text="/changeroom")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=_CW,  y=_CH,     width=_CW, height=_CH),
                    action=MessageAction(label="อื่นๆ",     text="/other")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=0,    y=_CH * 2, width=_CW, height=_CH),
                    action=MessageAction(label="บันทึก",    text="/save")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=_CW,  y=_CH * 2, width=_CW, height=_CH),
                    action=MessageAction(label="ยกเลิก",    text="/cancel")
                ),
            ]
        )

        menu_id = line_bot_api.create_rich_menu(rich_menu=menu)
        print(f"[OK] Rich Menu created: {menu_id}")

        try:
            line_bot_api.set_rich_menu_image(menu_id, "image/png", _build_rich_menu_image())
            print("[OK] Rich Menu image uploaded")
        except Exception as img_err:
            print(f"[WARN] Rich Menu image upload failed: {img_err}")

        line_bot_api.set_default_rich_menu(menu_id)
        print("[OK] Rich Menu set as default")
        return menu_id

    except Exception as e:
        print(f"[WARN] Rich Menu setup failed: {e}")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FLEX MESSAGE HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reply_flex(event, user_id, alt_text, bubble):
    msg = FlexSendMessage(alt_text=alt_text, contents=bubble)
    try:
        line_bot_api.reply_message(event.reply_token, msg)
    except Exception:
        try:
            line_bot_api.push_message(user_id, msg)
        except Exception as e:
            print(f"[WARN] Flex send error: {e}")


def _frow(label, value, value_color="#111111"):
    """Single label-value row for Flex body."""
    return BoxComponent(
        layout="horizontal",
        margin="sm",
        contents=[
            TextComponent(text=label, color="#888888", size="sm", flex=4),
            TextComponent(text=str(value), color=value_color,
                          weight="bold", size="sm", flex=5, align="end"),
        ]
    )


def _footer_buttons(confirm_text):
    return BoxComponent(
        layout="horizontal",
        spacing="sm",
        contents=[
            ButtonComponent(
                action=MessageAction(label="ยืนยัน", text=confirm_text),
                style="primary", color="#27AE60", flex=1, height="sm",
            ),
            ButtonComponent(
                action=MessageAction(label="ยกเลิก", text="/cancel"),
                style="secondary", flex=1, height="sm",
            ),
        ]
    )


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

ROOMS_SINGLE = [str(100+i) for i in range(1, 15)]              # 101-114 เตียงเดี่ยว/ชั่วคราว
ROOMS_TWIN   = [str(100+i) for i in range(15, 27) if i != 17]  # 115-126 ยกเว้น 117
ROOM_SPECIAL = ["117"]                                          # ห้องพิเศษ 400฿


def _send_carousel(event, user_id, room_list, prompt="เลือกห้องพัก", with_other=True):
    """Send room-selection carousel (3 rooms per card) + optional อื่นๆ card."""
    chunks = [room_list[i:i+3] for i in range(0, len(room_list), 3)]
    columns = []
    for chunk in chunks:
        label = f"ห้อง {chunk[0]}" if len(chunk) == 1 else f"ห้อง {chunk[0]}–{chunk[-1]}"
        actions = [
            MessageTemplateAction(label=f"ห้อง {r}", text=f"เลือกห้อง {r}")
            for r in chunk
        ]
        # pad to 3 actions (CarouselColumn requires equal action count across columns)
        while len(actions) < 3:
            actions.append(MessageTemplateAction(label=" ", text=f"เลือกห้อง {chunk[0]}"))
        columns.append(CarouselColumn(title="เลือกห้องพัก", text=label, actions=actions))

    if with_other:
        columns.append(CarouselColumn(
            title="ระบุเลขห้องเอง",
            text="กรอกเลขห้องที่ต้องการ",
            actions=[
                MessageTemplateAction(label="อื่นๆ (ระบุเอง)", text="เลือกห้อง อื่นๆ"),
                MessageTemplateAction(label=" ", text="เลือกห้อง อื่นๆ"),
                MessageTemplateAction(label=" ", text="เลือกห้อง อื่นๆ"),
            ]
        ))

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


def _parse_room(text):
    """Extract and normalize room number from any text. Returns '101'-'126' or None."""
    # Try 3-digit match first (from carousel tap: "เลือกห้อง 101")
    match = re.search(r'\b(1(?:0[1-9]|1\d|2[0-6]))\b', text)
    if match:
        return match.group(1)
    # Try bare 1-2 digit number (staff typing "1" or "24")
    bare = re.search(r'\b([1-9]|1\d|2[0-6])\b', text.strip())
    if bare:
        return HotelSheetService._normalize_room(bare.group(1))
    return None


def handle_checkin_room_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "อื่นๆ" in text:
        session["step"] = "custom_room"
        _reply(event, "🏠 ระบุเลขห้อง (1-26 หรือ 101-126):")
        return
    room = _parse_room(text)
    if not room:
        _reply(event, "❌ กรุณาเลือกห้องจากเมนู หรือพิมพ์เลขห้อง (1-26 หรือ 101-126)")
        return
    session["data"]["room"] = room
    session["step"] = "checkin_time"
    _reply(event, f"🕐 เวลาเช็คอินห้อง {room}?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("ระบุเอง", "ระบุเอง")])


def handle_checkin_custom_room_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    room = _parse_room(text)
    if not room:
        _reply(event, "❌ ระบุเป็นตัวเลข 1-26 หรือ 101-126 (เช่น 8 หรือ 108)")
        return
    session["data"]["room"] = room
    session["step"] = "checkin_time"
    _reply(event, f"🕐 เวลาเช็คอินห้อง {room}?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("ระบุเอง", "ระบุเอง")])


def handle_checkin_time_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "ตอนนี้" in text:
        session["data"]["checkin_time"] = datetime.now(TZ)
    elif "ระบุเอง" in text:
        session["step"] = "checkin_time_custom"
        _reply(event, "🕐 ระบุเวลาเช็คอิน (HH:MM เช่น 14:30):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ ระบุเอง")
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

    rows = [
        _frow("🏠 ห้อง", room, "#1A73E8"),
        _frow("📋 ประเภท", room_type),
    ]
    if room_type == "ค้างคืน":
        rows += [
            _frow("🛏️ เตียง", data.get("bed_type", "")),
            _frow("🌙 จำนวนคืน", f"{data.get('nights', 1)} คืน"),
            _frow("💰 ราคา", f"{rate:,}฿/คืน", "#E67E22"),
        ]
    else:
        rows += [
            _frow("⏱️ ระยะเวลา", f"{data.get('duration', '?')} ชม"),
            _frow("💰 ราคา", f"{rate:,}฿", "#E67E22"),
        ]
    rows += [
        SeparatorComponent(margin="md"),
        _frow("🕐 เวลาเช็คอิน", checkin_time.strftime("%H:%M"), "#27AE60"),
    ]

    bubble = BubbleContainer(
        header=BoxComponent(
            layout="vertical",
            padding_all="16px",
            background_color="#1A73E8",
            contents=[TextComponent(
                text="ยืนยันเช็คอิน",
                color="#FFFFFF", weight="bold", size="xl",
            )],
        ),
        body=BoxComponent(
            layout="vertical", padding_all="16px", contents=rows,
        ),
        footer=BoxComponent(
            layout="vertical", padding_all="12px",
            contents=[_footer_buttons("confirm_checkin")],
        ),
    )
    _reply_flex(event, user_id, f"ยืนยันเช็คอิน ห้อง {room}", bubble)


# ── CHECKOUT ───────────────────────────────────────────────────────

def handle_checkout_command(event, user_id):
    session = _get_or_create_session(user_id)
    session["command"] = "checkout"
    session["step"] = "room"
    if not _send_checkout_carousel(event, user_id):
        _clear_session(user_id)


def handle_checkout_room_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    room = _parse_room(text)
    if not room:
        _reply(event, "❌ กรุณาเลือกห้องจากเมนู หรือพิมพ์เลขห้อง (1-26 หรือ 101-126)")
        return
    session["data"]["room"] = room
    session["step"] = "checkout_time"
    _reply(event, f"🕐 เวลาเช็คเอาท์ห้อง {room}?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("ระบุเอง", "ระบุเอง")])


def handle_checkout_time_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    if "ตอนนี้" in text:
        session["data"]["checkout_time"] = datetime.now(TZ)
    elif "ระบุเอง" in text:
        session["step"] = "checkout_time_custom"
        _reply(event, "🕐 ระบุเวลาเช็คเอาท์ (HH:MM เช่น 16:45):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ ระบุเอง")
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

    bubble = BubbleContainer(
        header=BoxComponent(
            layout="vertical",
            padding_all="16px",
            background_color="#E67E22",
            contents=[TextComponent(
                text="ยืนยันเช็คเอาท์",
                color="#FFFFFF", weight="bold", size="xl",
            )],
        ),
        body=BoxComponent(
            layout="vertical", padding_all="16px",
            contents=[
                _frow("🏠 ห้อง", room, "#E67E22"),
                _frow("🕐 เวลาเช็คเอาท์", checkout_time.strftime("%H:%M"), "#27AE60"),
            ],
        ),
        footer=BoxComponent(
            layout="vertical", padding_all="12px",
            contents=[_footer_buttons("confirm_checkout")],
        ),
    )
    _reply_flex(event, user_id, f"ยืนยันเช็คเอาท์ ห้อง {room}", bubble)


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


# ── OTHER (sub-menu) ───────────────────────────────────────────────

def handle_other_command(event, user_id):
    """อื่นๆ button → show sub-menu."""
    _clear_session(user_id)
    _reply(event, "📋 เลือกรายการ:",
           quick_items=[
               ("🧹 แม่บ้าน", "แม่บ้าน"),
               ("📖 วิธีใช้", "วิธีใช้"),
               ("🆔 ไอดีของฉัน", "ไอดีของฉัน"),
               ("✏️ บันทึกอื่นๆ", "บันทึกอื่นๆ"),
           ])


def handle_other_submenu_step(event, user_id, text):
    """Route after sub-menu selection."""
    if "แม่บ้าน" in text:
        session = _get_or_create_session(user_id)
        session["command"] = "other"
        session["step"] = "maid_room"
        checked_in = hotel_service.get_checked_in_rooms()
        if checked_in:
            _send_carousel(event, user_id, checked_in, "เลือกห้องที่ทำเสร็จแล้ว", with_other=True)
        else:
            _reply(event, "🏠 ระบุเลขห้องที่ทำเสร็จ (1-26 หรือ 101-126):")

    elif "วิธีใช้" in text:
        handle_help_command(event, user_id)

    elif "ไอดีของฉัน" in text:
        _reply(event, f"🆔 ไอดีของคุณ:\n{user_id}")

    elif "บันทึกอื่นๆ" in text:
        session = _get_or_create_session(user_id)
        session["command"] = "other"
        session["step"] = "note"
        _reply(event, "✏️ พิมพ์บันทึก:")

    else:
        _reply(event, "❌ โปรดเลือกจากเมนูด้านบน")


def handle_maid_room_step(event, user_id, text):
    """Maid: select room that's done."""
    session = _get_or_create_session(user_id)
    if "อื่นๆ" in text:
        session["step"] = "maid_room_custom"
        _reply(event, "🏠 ระบุเลขห้อง (1-26 หรือ 101-126):")
        return
    room = _parse_room(text)
    if not room:
        _reply(event, "❌ กรุณาเลือกห้องจากเมนู หรือพิมพ์เลขห้อง")
        return
    session["data"]["maid_room"] = room
    session["step"] = "maid_time"
    _reply(event, f"🕐 ทำห้อง {room} เสร็จเวลาไหน?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("ระบุเอง", "ระบุเอง")])


def handle_maid_room_custom_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    room = _parse_room(text)
    if not room:
        _reply(event, "❌ ระบุเลขห้อง 1-26 หรือ 101-126")
        return
    session["data"]["maid_room"] = room
    session["step"] = "maid_time"
    _reply(event, f"🕐 ทำห้อง {room} เสร็จเวลาไหน?",
           quick_items=[("ตอนนี้", "ตอนนี้"), ("ระบุเอง", "ระบุเอง")])


def handle_maid_time_step(event, user_id, text):
    """Maid: get completion time."""
    session = _get_or_create_session(user_id)
    room = session["data"].get("maid_room", "?")

    if "ตอนนี้" in text:
        done_time = datetime.now(TZ)
    elif "ระบุเอง" in text:
        session["step"] = "maid_time_custom"
        _reply(event, "🕐 ระบุเวลาที่เสร็จ (HH:MM เช่น 10:30):")
        return
    else:
        _reply(event, "❌ โปรดเลือก ตอนนี้ หรือ ระบุเอง")
        return

    _save_maid_record(event, user_id, room, done_time)


def handle_maid_time_custom_step(event, user_id, text):
    session = _get_or_create_session(user_id)
    room = session["data"].get("maid_room", "?")
    try:
        t = datetime.strptime(text.strip(), "%H:%M").time()
        now = datetime.now(TZ)
        done_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        _save_maid_record(event, user_id, room, done_time)
    except ValueError:
        _reply(event, "❌ ใช้รูปแบบ HH:MM (เช่น 10:30)")


def _save_maid_record(event, user_id, room, done_time):
    """Save maid completion note; auto-checkout if room still checked-in."""
    time_str = done_time.strftime("%H:%M")
    note_text = f"แม่บ้านทำห้อง {room} เสร็จเวลา {time_str}"
    hotel_service.record_note(note_text, note_type="RoomComplete")

    # Auto-checkout if room is still checked-in
    checked_in = hotel_service.get_checked_in_rooms()
    if room in checked_in:
        result = hotel_service.record_checkout(room, done_time)
        if "error" not in result:
            _reply(event, f"✅ บันทึกแม่บ้านห้อง {room} เวลา {time_str}\n"
                          f"(เช็คเอาท์อัตโนมัติเนื่องจากยังไม่ได้เช็คเอาท์)")
        else:
            _reply(event, f"✅ บันทึกแม่บ้านห้อง {room} เวลา {time_str}")
    else:
        _reply(event, f"✅ บันทึกแม่บ้านห้อง {room} เวลา {time_str}")

    _clear_session(user_id)


def _ai_process_note(text):
    """Ask Claude to parse a free-text staff note. Returns dict or None."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic, json as _json
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"""วิเคราะห์ข้อความจาก staff โรงแรม ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่น

ข้อความ: "{text}"

ตอบในรูปแบบนี้เท่านั้น:
{{
  "category": "maid_done หรือ maintenance หรือ info หรือ other",
  "room": "เลขห้อง 3 หลัก เช่น 101 หรือ null",
  "time": "HH:MM หรือ null",
  "summary": "สรุป 1 ประโยคภาษาไทย",
  "auto_checkout": true หรือ false
}}

กฎ:
- maid_done = ทำห้องเสร็จ / ทำความสะอาดเสร็จ / เก็บห้องเสร็จ
- maintenance = ซ่อม / แอร์ / น้ำ / ไฟ / อุปกรณ์เสีย
- auto_checkout = true เฉพาะ maid_done + มีเลขห้อง
- room: 1→101, 15→115 (แปลงเป็น 3 หลักเสมอ)"""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return _json.loads(m.group())
    except Exception as e:
        print(f"[WARN] Claude AI error: {e}")
    return None


def handle_other_note_step(event, user_id, text):
    """Free-text note — process with Claude AI, fallback to plain save."""
    parsed = _ai_process_note(text)

    if parsed:
        category  = parsed.get("category", "other")
        room      = parsed.get("room")
        time_str  = parsed.get("time")
        summary   = parsed.get("summary") or text
        auto_co   = parsed.get("auto_checkout", False)

        hotel_service.record_note(summary, note_type=category.title() if category else "Other")
        lines = [f"✅ บันทึก: {summary}"]

        if auto_co and room:
            checkout_time = datetime.now(TZ)
            if time_str:
                try:
                    t = datetime.strptime(time_str, "%H:%M").time()
                    checkout_time = checkout_time.replace(
                        hour=t.hour, minute=t.minute, second=0, microsecond=0
                    )
                except ValueError:
                    pass
            if room in hotel_service.get_checked_in_rooms():
                result = hotel_service.record_checkout(room, checkout_time)
                if "error" not in result:
                    lines.append(
                        f"🔄 เช็คเอาท์ห้อง {room} อัตโนมัติ เวลา {checkout_time.strftime('%H:%M')}"
                    )
        _reply(event, "\n".join(lines))
    else:
        result = hotel_service.record_note(text, note_type="Other")
        if "error" in result:
            _reply(event, f"❌ เกิดข้อผิดพลาด: {result['error']}")
        else:
            _reply(event, "✅ บันทึกเสร็จแล้ว")

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
        elif current_step == "custom_room":
            handle_checkin_custom_room_step(event, user_id, text)
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
            room = _parse_room(text)
            if not room:
                _reply(event, "❌ กรุณาเลือกห้องจากเมนู หรือพิมพ์เลขห้อง (1-26 หรือ 101-126)")
                return
            session["data"]["old_room"] = room
            session["step"] = "new_room"
            _send_carousel(event, user_id, ROOMS_SINGLE + ROOMS_TWIN, "เลือกห้องใหม่")
        elif current_step == "new_room":
            new_room = _parse_room(text)
            if not new_room:
                _reply(event, "❌ กรุณาเลือกห้องจากเมนู หรือพิมพ์เลขห้อง (1-26 หรือ 101-126)")
                return
            old_room = session["data"]["old_room"]
            result = hotel_service.record_checkout(old_room, datetime.now(TZ))
            if "error" not in result:
                _reply(event, f"✅ เปลี่ยนห้องสำเร็จ\n🏠 ออกห้อง {old_room} → เข้าห้อง {new_room}\nกรุณาเช็คอินห้อง {new_room} ด้วย /checkin")
            else:
                _reply(event, f"✅ บันทึกเปลี่ยนห้อง {old_room} → {new_room}")
            _clear_session(user_id)

    elif current_command == "other":
        if current_step == "note":
            handle_other_note_step(event, user_id, text)
        elif current_step == "maid_room":
            handle_maid_room_step(event, user_id, text)
        elif current_step == "maid_room_custom":
            handle_maid_room_custom_step(event, user_id, text)
        elif current_step == "maid_time":
            handle_maid_time_step(event, user_id, text)
        elif current_step == "maid_time_custom":
            handle_maid_time_custom_step(event, user_id, text)

    elif not current_command:
        # No active session — check if it's a sub-menu reply from อื่นๆ
        submenu_keys = ["แม่บ้าน", "วิธีใช้", "ไอดีของฉัน", "บันทึกอื่นๆ"]
        if any(k in text for k in submenu_keys):
            handle_other_submenu_step(event, user_id, text)
        else:
            _reply(event, "ใช้ปุ่มเมนูด้านล่าง หรือพิมพ์ /help")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SERVER LIFECYCLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    # Run Flask (scheduler already started above)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
