"""
Report Generation — Charts and text summaries for check-in/check-out analytics.
"""

import io
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams

TZ = ZoneInfo("Asia/Bangkok")

_FONT_READY = False


def _set_thai_font():
    """Set Thai font for matplotlib charts."""
    global _FONT_READY
    if _FONT_READY:
        return
    available = {f.name for f in font_manager.fontManager.ttflist}
    for font_name in ["Garuda", "TH Sarabun New", "Sarabun", "Tahoma", "DejaVu Sans"]:
        if font_name in available:
            rcParams["font.family"] = font_name
            break
    rcParams["axes.unicode_minus"] = False
    _FONT_READY = True


def daily_summary_text(revenue_data, usage_data, occupancy_stats, empty_rooms, report_time="5pm"):
    """Generate text summary for daily reports (5 PM and 8 AM).

    Args:
        revenue_data: dict with "total", "overnight", "temporary"
        usage_data: dict with "total", "overnight", "temporary" (counts)
        occupancy_stats: dict of room stats (room -> {"used": N, "overnight": N, "temporary": N})
        empty_rooms: list of unused room numbers
        report_time: str ("5pm" or "8am")

    Returns:
        str formatted text summary
    """
    now = datetime.now(TZ)
    date_str = now.strftime("%d/%m/%Y")

    # Format occupancy rate
    total_rooms = 26
    used_count = sum(1 for stats in occupancy_stats.values() if stats["used"] > 0)
    occupancy_pct = int((used_count / total_rooms) * 100) if total_rooms > 0 else 0

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 สรุปประจำวัน ({date_str})",
        f"   เวลา {report_time.upper()}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 รายได้รวม: {revenue_data['total']:,}฿",
        f"   ├─ ค้างคืน ({usage_data['overnight']} ครั้ง): {revenue_data['overnight']:,}฿",
        f"   └─ ชั่วคราว ({usage_data['temporary']} ครั้ง): {revenue_data['temporary']:,}฿",
        "",
        f"📌 จำนวนการใช้: {usage_data['total']} ครั้ง",
        f"   ├─ ค้างคืน: {usage_data['overnight']} ครั้ง",
        f"   └─ ชั่วคราว: {usage_data['temporary']} ครั้ง",
        "",
        f"🏠 อัตราการเต็ม: {occupancy_pct}% ({used_count}/{total_rooms} ห้อง)",
        "",
    ]

    if empty_rooms:
        lines.append(f"⚠️  ห้องว่าง ({len(empty_rooms)} ห้อง): {', '.join(empty_rooms)}")
    else:
        lines.append("✅ ไม่มีห้องว่าง")

    return "\n".join(lines)


def weekly_summary_text(revenue_data, usage_data, occupancy_stats, empty_rooms, days=7):
    """Generate detailed weekly summary for /week command.

    Args:
        revenue_data: dict
        usage_data: dict
        occupancy_stats: dict
        empty_rooms: list
        days: int (7 for weekly, 30 for monthly)

    Returns:
        str formatted text summary
    """
    now = datetime.now(TZ)
    start_date = (now - timedelta(days=days - 1)).date()
    end_date = now.date()

    total_rooms = 26
    used_count = sum(1 for stats in occupancy_stats.values() if stats["used"] > 0)
    occupancy_pct = int((used_count / total_rooms) * 100) if total_rooms > 0 else 0

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 สรุปรายสัปดาห์ ({days} วันล่าสุด)",
        f"   {start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "💰 รายได้:",
        f"   รวมทั้งสิ้น: {revenue_data['total']:,}฿",
        f"   ├─ ค้างคืน: {revenue_data['overnight']:,}฿ ({usage_data['overnight']} ครั้ง)",
        f"   └─ ชั่วคราว: {revenue_data['temporary']:,}฿ ({usage_data['temporary']} ครั้ง)",
        "",
        "📌 จำนวนการใช้:",
        f"   รวม: {usage_data['total']} ครั้ง",
        f"   ├─ ค้างคืน: {usage_data['overnight']} ครั้ง",
        f"   └─ ชั่วคราว: {usage_data['temporary']} ครั้ง",
        "",
        "🏠 อัตราการเต็ม:",
        f"   {occupancy_pct}% ({used_count}/{total_rooms} ห้อง)",
        "",
    ]

    # Top 5 used rooms
    top_rooms = sorted(
        occupancy_stats.items(),
        key=lambda x: x[1]["used"],
        reverse=True
    )[:5]

    if top_rooms:
        lines.append("   ห้องที่ใช้มากที่สุด:")
        for room, stats in top_rooms:
            lines.append(f"   • ห้อง {room}: {stats['used']} ครั้ง ({stats['overnight']}+{stats['temporary']})")
        lines.append("")

    if empty_rooms:
        lines.append(f"⚠️  ห้องว่าง ({len(empty_rooms)} ห้อง):")
        lines.append(f"   {', '.join(empty_rooms)}")
    else:
        lines.append("✅ ไม่มีห้องว่าง")

    return "\n".join(lines)


def render_occupancy_chart(occupancy_stats, days=7, fmt="png"):
    """Generate stacked bar chart of room usage by type.

    Args:
        occupancy_stats: dict of room stats
        days: int (for chart title)
        fmt: str ("png" or "jpg")

    Returns:
        bytes (PNG/JPG image data)
    """
    _set_thai_font()

    # Prepare data
    rooms = sorted(occupancy_stats.keys())
    overnight_counts = [occupancy_stats[room]["overnight"] for room in rooms]
    temporary_counts = [occupancy_stats[room]["temporary"] for room in rooms]

    # Create figure
    fig, ax = plt.subplots(figsize=(14, 6))

    x_pos = range(len(rooms))
    bar_width = 0.6

    # Stacked bars
    ax.bar(x_pos, overnight_counts, bar_width, label="ค้างคืน", color="#4285F4")
    ax.bar(x_pos, temporary_counts, bar_width, bottom=overnight_counts,
           label="ชั่วคราว", color="#EA4335")

    ax.set_xlabel("ห้องพัก", fontsize=12, fontweight="bold")
    ax.set_ylabel("จำนวนครั้ง", fontsize=12, fontweight="bold")
    ax.set_title(f"การใช้งานห้องพัก ({days} วันล่าสุด)", fontsize=14, fontweight="bold")
    ax.set_xticks(x_pos)
    ax.set_xticklabels(rooms, rotation=45)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()

    # Save to bytes
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=100)
    buf.seek(0)
    plt.close(fig)

    return buf.getvalue()
