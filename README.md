# Checkin-Out บ้านเพื่อน — Hotel Room Check-In/Check-Out LINE Bot

A LINE bot for tracking hotel room check-ins/check-outs with automatic daily reports and admin analytics.

## Features

- **Interactive Check-In Flow** (`/checkin`) — Record guest room check-ins with flexible pricing
- **Check-Out Tracking** (`/checkout`) — Track when guests leave
- **Room Change** (`/changeroom`) — Move guest to different room
- **Free-Text Notes** (`/other`) — Log housekeeping tasks, maintenance notes
- **Automatic Daily Reports** — 5 PM & 8 AM summaries sent to hotel staff
- **Admin Reports** (`/week`, `/month`, `/comonth`) — Detailed analytics (revenue, occupancy, empty rooms)
- **26 Rooms Supported** — Room numbers 1-26 or 101-126

## Room Types & Pricing

### Temporary (ชั่วคราว)
- 2 hours = 180฿
- 3 hours = 270฿
- Custom: User specifies hours + price

### Overnight (ค้างคืน)
- Single bed = 450฿
- Double bed = 500฿

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in:

```env
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
GOOGLE_SERVICE_ACCOUNT_JSON=...
CHECKIN_SHEET_ID=1o3J00Ci42lqJtOZ46eaBnP8bs55l_FcgMT9ZorxhPr4
ADMIN_USER_IDS=Uxxxxxxxxxxxxxxxxxxx
HOTEL_STAFF_USER_ID=Uxxxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=...  (optional)
```

### 3. Google Sheets Setup

The bot uses your existing Google Sheet (ID provided above).

**Required sheets:**
- `CheckIns` — Room check-in/check-out records
- `Notes` — Housekeeping notes and maintenance logs
- `Staff` (optional) — Staff roster

### 4. Run Locally

```bash
python app.py
```

Bot will:
- Start Flask server on port 5000
- Initialize APScheduler for 5 PM & 8 AM reports
- Listen for LINE webhook events

### 5. Deploy to Production

Deploy to Heroku/Railway/PythonAnywhere:

```bash
git push heroku main
```

Configure LINE OA webhook URL → `https://<your-domain>/webhook`

## Commands

| Command | Usage | Notes |
|---------|-------|-------|
| `/checkin` | Record guest check-in | Interactive flow |
| `/checkout` | Record guest check-out | Requires room number |
| `/changeroom` | Move guest to different room | Flow: old room → new room |
| `/other` | Log notes (housekeeping, maintenance) | Free-text note |
| `/week` | Weekly report (chart + summary) | Admin only |
| `/month` | Monthly report | Admin only |
| `/comonth` | Cumulative monthly report | Admin only |
| `/help` | Show available commands | |
| `/cancel` | Cancel current operation | |

## Architecture

```
app.py                  # Flask + LINE webhook + command routing
├── hotel_service.py    # Google Sheets I/O + room logic
├── report.py           # Report generation (text + charts)
├── scheduler.py        # Daily scheduled reports (5 PM, 8 AM)
└── requirements.txt    # Python dependencies
```

## Data Flow

```
User sends /checkin
    ↓
app.py routes to handle_checkin_command()
    ↓
Interactive flow: room → type → duration → time → confirm
    ↓
hotel_service.record_checkin() writes to Sheets
    ↓
Bot replies: "✅ เช็คอินสำเร็จ"

(Daily at 5 PM & 8 AM)
    ↓
scheduler.send_daily_summary()
    ↓
hotel_service.get_daily_revenue() + .get_usage_count() + .get_occupancy_stats()
    ↓
report.daily_summary_text() formats summary
    ↓
line_bot_api.push_message() sends to hotel staff
```

## Session Management

Uses in-memory user sessions for multi-step flows:
```python
user_sessions = {
    "user_id": {
        "command": "checkin",
        "step": "room",
        "data": {"room": "101", "type": "ค้างคืน", ...}
    }
}
```

⚠️ **Important:** Single worker only (`gunicorn --workers 1`). Multiple workers will lose session state.

## Report Priority

Admin reports (`/week`, `/month`, `/comonth`) show (in order):

1. **Total Revenue** (฿) - Breakdown by overnight vs. temporary
2. **Usage Count** - Number of times each room type was used
3. **Occupancy Rate** (%) - Rooms used vs. total (26 rooms)
4. **Empty Rooms** - List of unused rooms during period

## Timezone

All timestamps use Asia/Bangkok timezone (TZ environment variable).

## Error Handling

- Graceful fallback to `push_message` if reply token expires
- Validation on room numbers (1-26 or 101-126)
- Price validation (> 0, < 1000฿)
- Google Sheets auth errors logged to console

## Development

### Testing

```bash
# Manual testing in LINE app:
# 1. /checkin → follow prompts
# 2. Check Google Sheets for recorded data
# 3. /week → verify chart generation
# 4. Wait for 5 PM for automatic report (dev: set clock forward)
```

### Logging

All operations logged to stdout:
- ✅ Successful actions
- ❌ Errors
- ⚠️  Warnings

## Known Limitations

- Chart images sent to admin reports are currently text-based (could add matplotlib charts later)
- Check-out doesn't auto-calculate final cost (currently 0฿) — needs link to check-in record
- No user-friendly room photo management (could add photo upload)
- No duplicate-booking detection yet (could flag room used 2x in same 8-hour shift)

## Future Enhancements

- [ ] Bar charts for weekly/monthly reports (matplotlib)
- [ ] Housekeeper cleanup time tracking
- [ ] Room maintenance history
- [ ] Occupancy forecasting
- [ ] Guest notes (who, contact, special requests)
- [ ] Multi-language support (Thai + English)

## Support

For issues or questions, contact the development team.

---

**Created:** 2026-06-10  
**Bot Name:** Checkin-Out บ้านเพื่อน  
**Owner:** Apinya (Baan Phuen Resort)
