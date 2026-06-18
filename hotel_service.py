"""
Hotel Service — Google Sheets I/O for check-in/check-out records.
Handles: room check-ins, check-outs, notes, and summary calculations.
"""

import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials

TZ = ZoneInfo("Asia/Bangkok")


class HotelSheetService:
    """Manage Google Sheets for check-in/check-out records."""

    def __init__(self, service_account_path=None, sheet_id=None):
        """Initialize with Google Service Account credentials and Sheet ID."""
        self.sheet_id = sheet_id or os.getenv("CHECKIN_SHEET_ID")
        self.sheet = None
        self._authenticate(service_account_path)

    def _authenticate(self, service_account_path):
        """Authenticate with Google Sheets using Service Account."""
        try:
            # Try environment variable first (for production)
            sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            if sa_json:
                creds_dict = json.loads(sa_json)
                creds = Credentials.from_service_account_info(
                    creds_dict,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
            else:
                # Fall back to file path
                path = service_account_path or os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "./service-account.json")
                creds = Credentials.from_service_account_file(
                    path,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )

            gc = gspread.authorize(creds)
            self.sheet = gc.open_by_key(self.sheet_id)
            self._ensure_headers()
        except Exception as e:
            print(f"❌ Failed to authenticate with Google Sheets: {e}")
            self.sheet = None

    def _ensure_headers(self):
        """Insert headers into sheets if missing. Runs on startup."""
        for title, headers in [("CheckIns", self.CHECKINS_HEADERS), ("Notes", self.NOTES_HEADERS)]:
            try:
                try:
                    ws = self.sheet.worksheet(title)
                except gspread.exceptions.WorksheetNotFound:
                    ws = self.sheet.add_worksheet(title=title, rows=1000, cols=20)

                first_row = ws.row_values(1)
                has_header = first_row and "Timestamp" in str(first_row[0])
                if not has_header:
                    ws.insert_row(headers, 1)
                    print(f"[OK] Headers inserted into {title}")
                else:
                    print(f"[OK] {title} headers already exist")
            except Exception as e:
                print(f"[WARN] Could not setup {title} headers: {e}")

    CHECKINS_HEADERS = [
        "Timestamp(System)", "Room#", "Type(ค้างคืน/ชั่วคราว)",
        "Check-In Time", "Check-Out Time", "Duration",
        "Rate(฿)", "Total Cost", "Status", "Notes", ""
    ]
    NOTES_HEADERS = [
        "Timestamp(System)", "Note Text", "Type",
        "Room#", "Completion Time", "Processed", "Category", "Shift"
    ]

    @staticmethod
    def _current_shift():
        """Return 'กะเช้า' (08-16) or 'กะเย็น' (17-07)."""
        h = datetime.now(TZ).hour
        return "กะเช้า" if 8 <= h < 17 else "กะเย็น"

    def _get_worksheet(self, title):
        """Get or create a worksheet by title, auto-inserting headers if missing."""
        if not self.sheet:
            return None
        try:
            ws = self.sheet.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=20)

        # Auto-insert headers if missing
        try:
            first_row = ws.row_values(1)
            has_header = first_row and "Timestamp" in str(first_row[0])
            if not has_header:
                headers = self.CHECKINS_HEADERS if title == "CheckIns" else self.NOTES_HEADERS
                ws.insert_row(headers, 1)
                print(f"[OK] Headers inserted into {title} sheet")
        except Exception as e:
            print(f"[WARN] Could not check/insert headers: {e}")

        return ws

    def record_checkin(self, room_number, room_type, checkin_time, duration_hours=None,
                       rate_baht=0, special_notes=""):
        """Record a check-in to the CheckIns sheet.

        Args:
            room_number: str (e.g., "101" or "1" which gets normalized)
            room_type: str ("ค้างคืน" or "ชั่วคราว")
            checkin_time: datetime object
            duration_hours: int/float (for temporary rooms)
            rate_baht: int (price in baht)
            special_notes: str (any special notes)

        Returns:
            dict with the recorded row data
        """
        if not self.sheet:
            return {"error": "Sheet not initialized"}

        # Normalize room number
        room_num = self._normalize_room(room_number)

        ws = self._get_worksheet("CheckIns")
        if not ws:
            return {"error": "Could not access CheckIns sheet"}

        now = datetime.now(TZ)
        checkin_str = checkin_time.strftime("%Y-%m-%d %H:%M") if checkin_time else now.strftime("%Y-%m-%d %H:%M")

        # Prepare row
        row = [
            now.strftime("%Y-%m-%d %H:%M:%S"),  # A: System timestamp
            room_num,                             # B: Room #
            room_type,                            # C: Type
            checkin_str,                          # D: Check-In Time
            "",                                   # E: Check-Out Time (empty for now)
            str(duration_hours) if duration_hours else "",  # F: Duration
            str(rate_baht),                       # G: Rate
            "",                                   # H: Total Cost (calculated at checkout)
            "checked-in",                         # I: Status
            special_notes,                        # J: Notes
            ""                                    # K: Reserved
        ]

        try:
            ws.append_row(row, value_input_option="RAW")
            return {
                "room": room_num,
                "type": room_type,
                "checkin_time": checkin_str,
                "rate": rate_baht,
                "status": "success"
            }
        except Exception as e:
            return {"error": str(e)}

    def record_checkout(self, room_number, checkout_time, actual_duration_hours=None,
                        final_cost=0):
        """Record a check-out by updating the last unclosed check-in for that room.

        Args:
            room_number: str (e.g., "101")
            checkout_time: datetime object
            actual_duration_hours: float
            final_cost: int (final price in baht)

        Returns:
            dict with status and updated row index
        """
        if not self.sheet:
            return {"error": "Sheet not initialized"}

        room_num = self._normalize_room(room_number)

        ws = self._get_worksheet("CheckIns")
        if not ws:
            return {"error": "Could not access CheckIns sheet"}

        try:
            all_rows = ws.get_all_records()

            # Find the last unclosed check-in for this room
            for i in range(len(all_rows) - 1, -1, -1):
                row = all_rows[i]
                if row.get("Room#") == room_num and row.get("Status") == "checked-in":
                    # Found it - update this row
                    row_index = i + 2  # +1 for header, +1 for 1-indexed

                    checkout_str = checkout_time.strftime("%Y-%m-%d %H:%M")

                    # Update columns E, F, H, I
                    ws.update_cell(row_index, 5, checkout_str)  # E: Check-Out Time
                    ws.update_cell(row_index, 6, str(actual_duration_hours) if actual_duration_hours else "")  # F: Duration
                    ws.update_cell(row_index, 8, str(final_cost))  # H: Total Cost
                    ws.update_cell(row_index, 9, "checked-out")  # I: Status

                    return {
                        "room": room_num,
                        "checkout_time": checkout_str,
                        "final_cost": final_cost,
                        "status": "success"
                    }

            return {"error": f"No open check-in found for room {room_num}"}

        except Exception as e:
            return {"error": str(e)}

    def record_note(self, note_text, note_type="Other"):
        """Record a free-text note (for /other command, housekeeping, maintenance, etc).

        Args:
            note_text: str (e.g., "ห้อง 108 ทำเสร็จเวลา 10:30")
            note_type: str ("RoomComplete", "Maintenance", "Other")

        Returns:
            dict with status
        """
        if not self.sheet:
            return {"error": "Sheet not initialized"}

        ws = self._get_worksheet("Notes")
        if not ws:
            return {"error": "Could not access Notes sheet"}

        now = datetime.now(TZ)

        # Try to extract room number and completion time from note
        room_num = self._extract_room_from_text(note_text)
        completion_time = self._extract_time_from_text(note_text)

        row = [
            now.strftime("%Y-%m-%d %H:%M:%S"),  # A: System timestamp
            note_text,                           # B: Note text
            note_type,                           # C: Type
            room_num or "",                      # D: Room# (if applicable)
            completion_time or "",               # E: Completion time (if room complete)
            "N",                                 # F: Processed
            "",                                  # G: Category
            self._current_shift(),               # H: Shift (กะเช้า/กะเย็น)
        ]

        try:
            ws.append_row(row, value_input_option="RAW")
            return {
                "note": note_text,
                "room": room_num,
                "status": "success"
            }
        except Exception as e:
            return {"error": str(e)}

    def get_shift_notes(self, shift_label, shift_date):
        """Return all note texts for a given shift on a given date.

        shift_label : 'กะเช้า' or 'กะเย็น'
        shift_date  : datetime.date  (the calendar date the shift STARTS on)
                      กะเช้า  = shift_date 08:00-16:59
                      กะเย็น  = shift_date 17:00 – shift_date+1 07:59
        """
        if not self.sheet:
            return []
        ws = self._get_worksheet("Notes")
        if not ws:
            return []
        try:
            all_rows = ws.get_all_values()
            if not all_rows:
                return []
            has_header = "Timestamp" in str(all_rows[0][0])
            start_row = 1 if has_header else 0

            from datetime import timedelta
            if shift_label == "กะเช้า":
                dt_from = datetime(shift_date.year, shift_date.month, shift_date.day, 8, 0, tzinfo=TZ)
                dt_to   = datetime(shift_date.year, shift_date.month, shift_date.day, 17, 0, tzinfo=TZ)
            else:  # กะเย็น
                dt_from = datetime(shift_date.year, shift_date.month, shift_date.day, 17, 0, tzinfo=TZ)
                next_day = shift_date + timedelta(days=1)
                dt_to   = datetime(next_day.year, next_day.month, next_day.day, 8, 0, tzinfo=TZ)

            notes = []
            for row in all_rows[start_row:]:
                if not row or not row[0]:
                    continue
                try:
                    ts = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    if dt_from <= ts < dt_to:
                        note_text = row[1] if len(row) > 1 else ""
                        if note_text:
                            notes.append(note_text)
                except (ValueError, IndexError):
                    continue
            return notes
        except Exception as e:
            print(f"[WARN] get_shift_notes error: {e}")
            return []

    def get_shift_checkins(self, shift_label, shift_date):
        """Return check-in records saved during a given shift.

        shift_label : 'กะเช้า' or 'กะเย็น'
        shift_date  : datetime.date (date the shift STARTED)
        Returns list of dicts: {room, type, checkin_time, rate, status}
        """
        if not self.sheet:
            return []
        ws = self._get_worksheet("CheckIns")
        if not ws:
            return []
        try:
            all_values = ws.get_all_values()
            if not all_values:
                return []

            has_header = "Timestamp" in str(all_values[0][0]) if all_values[0] else False
            start_row = 1 if has_header else 0

            from datetime import timedelta
            if shift_label == "กะเช้า":
                dt_from = datetime(shift_date.year, shift_date.month, shift_date.day, 8, 0, tzinfo=TZ)
                dt_to   = datetime(shift_date.year, shift_date.month, shift_date.day, 17, 0, tzinfo=TZ)
            else:
                dt_from = datetime(shift_date.year, shift_date.month, shift_date.day, 17, 0, tzinfo=TZ)
                next_day = shift_date + timedelta(days=1)
                dt_to   = datetime(next_day.year, next_day.month, next_day.day, 8, 0, tzinfo=TZ)

            records = []
            for row in all_values[start_row:]:
                if not row or not row[0]:
                    continue
                try:
                    ts = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    if dt_from <= ts < dt_to:
                        records.append({
                            "room":         row[1] if len(row) > 1 else "",
                            "type":         row[2] if len(row) > 2 else "",
                            "checkin_time": row[3] if len(row) > 3 else "",
                            "checkout_time":row[4] if len(row) > 4 else "",
                            "rate":         row[6] if len(row) > 6 else "",
                            "status":       row[8] if len(row) > 8 else "",
                        })
                except (ValueError, IndexError):
                    continue
            return records
        except Exception as e:
            print(f"[WARN] get_shift_checkins error: {e}")
            return []

    def get_checkin_summary(self, start_date, end_date):
        """Get all check-in records between start_date and end_date.

        Args:
            start_date: datetime.date
            end_date: datetime.date

        Returns:
            list of dicts (filtered records)
        """
        if not self.sheet:
            return []

        ws = self._get_worksheet("CheckIns")
        if not ws:
            return []

        try:
            all_values = ws.get_all_values()
            if not all_values:
                return []

            filtered = []

            # Check if first row has header or is data
            first_row = all_values[0]
            has_header = "Timestamp" in str(first_row[0]) if first_row else False
            start_row = 1 if has_header else 0

            for row_idx in range(start_row, len(all_values)):
                row = all_values[row_idx]
                if not row or not row[0]:
                    continue

                try:
                    ts_str = row[0]  # Column A = Timestamp
                    if not ts_str or "Timestamp" in ts_str:
                        continue

                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).date()

                    if start_date <= ts <= end_date:
                        # Convert to dict for compatibility
                        record = {
                            "Timestamp(System)": row[0] if len(row) > 0 else "",
                            "Room#": row[1] if len(row) > 1 else "",
                            "Type(ค้างคืน/ชั่วคราว)": row[2] if len(row) > 2 else "",
                            "Check-In Time": row[3] if len(row) > 3 else "",
                            "Check-Out Time": row[4] if len(row) > 4 else "",
                            "Duration": row[5] if len(row) > 5 else "",
                            "Rate(฿)": row[6] if len(row) > 6 else "",
                            "Total Cost": row[7] if len(row) > 7 else "",
                            "Status": row[8] if len(row) > 8 else "",
                            "Notes": row[9] if len(row) > 9 else "",
                        }
                        filtered.append(record)
                except (ValueError, AttributeError, IndexError):
                    continue

            return filtered
        except Exception as e:
            print(f"❌ Error reading CheckIns: {e}")
            return []

    def get_checked_in_rooms(self):
        """Return list of room numbers currently in 'checked-in' status."""
        if not self.sheet:
            return []
        ws = self._get_worksheet("CheckIns")
        if not ws:
            return []
        try:
            all_values = ws.get_all_values()
            if not all_values:
                return []
            first_row = all_values[0]
            has_header = first_row and "Timestamp" in str(first_row[0])
            start_row = 1 if has_header else 0
            rooms = []
            for row in all_values[start_row:]:
                if len(row) > 8 and row[8] == "checked-in" and row[1]:
                    room = row[1]
                    if room not in rooms:
                        rooms.append(room)
            rooms.sort()
            return rooms
        except Exception as e:
            print(f"[WARN] get_checked_in_rooms error: {e}")
            return []

    def get_occupancy_stats(self, start_date, end_date):
        """Calculate room occupancy statistics for the date range.

        Returns:
            dict with per-room stats: {"101": {"used": 5, "overnight": 2, "temporary": 3}, ...}
        """
        records = self.get_checkin_summary(start_date, end_date)

        stats = {}
        for i in range(1, 27):  # 26 rooms total
            room_num = f"{100 + i}"
            stats[room_num] = {
                "used": 0,
                "overnight": 0,
                "temporary": 0,
                "total_revenue": 0
            }

        for record in records:
            room = record.get("Room#", "")
            if not room or room not in stats:
                continue

            room_type = record.get("Type(ค้างคืน/ชั่วคราว)", "")
            rate_str = record.get("Rate(฿)", "0")

            try:
                rate = int(float(rate_str))
            except (ValueError, TypeError):
                rate = 0

            stats[room]["used"] += 1
            stats[room]["total_revenue"] += rate

            if room_type == "ค้างคืน":
                stats[room]["overnight"] += 1
            else:
                stats[room]["temporary"] += 1

        return stats

    def get_empty_rooms(self, start_date, end_date):
        """Get list of rooms that were never used during the period.

        Returns:
            list of room numbers (e.g., ["101", "105", ...])
        """
        stats = self.get_occupancy_stats(start_date, end_date)
        empty = [room for room, data in stats.items() if data["used"] == 0]
        return empty

    def get_daily_revenue(self, start_date, end_date):
        """Calculate total revenue and breakdown by room type.

        Returns:
            dict with "total", "overnight", "temporary" keys
        """
        records = self.get_checkin_summary(start_date, end_date)

        total = 0
        overnight = 0
        temporary = 0

        for record in records:
            rate_str = record.get("Rate(฿)", "0")
            try:
                rate = int(float(rate_str))
            except (ValueError, TypeError):
                rate = 0

            room_type = record.get("Type(ค้างคืน/ชั่วคราว)", "")
            total += rate

            if room_type == "ค้างคืน":
                overnight += rate
            else:
                temporary += rate

        return {
            "total": total,
            "overnight": overnight,
            "temporary": temporary
        }

    def get_usage_count(self, start_date, end_date):
        """Count total check-ins by room type.

        Returns:
            dict with "total", "overnight", "temporary" keys
        """
        records = self.get_checkin_summary(start_date, end_date)

        total = len(records)
        overnight = sum(1 for r in records if r.get("Type(ค้างคืน/ชั่วคราว)") == "ค้างคืน")
        temporary = total - overnight

        return {
            "total": total,
            "overnight": overnight,
            "temporary": temporary
        }

    @staticmethod
    def _normalize_room(room_input):
        """Normalize room number: 1 -> 101, 12 -> 112, etc."""
        try:
            num = int(str(room_input).strip())
            if 1 <= num <= 26:
                return f"{100 + num}"
            elif 101 <= num <= 126:
                return str(num)
            else:
                return str(room_input)
        except (ValueError, TypeError):
            return str(room_input)

    @staticmethod
    def _extract_room_from_text(text):
        """Extract room number from text like 'ห้อง 108 ทำเสร็จ'."""
        import re
        # Look for "ห้อง XXX" or just a 3-digit number in the text
        match = re.search(r'ห้อง\s*(\d{2,3})', text)
        if match:
            return match.group(1)

        # Also try just finding a number between 101-126
        match = re.search(r'(10[1-9]|11[0-9]|12[0-6])', text)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_time_from_text(text):
        """Extract time from text like '10:30' or 'เวลา 10:30'."""
        import re
        match = re.search(r'(\d{1,2}):(\d{2})', text)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return None
