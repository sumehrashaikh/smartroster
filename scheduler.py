"""Simple rule-based monthly roster generation."""

import calendar
from datetime import date


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def generate_month_roster(employee_rows, year, month, holidays=None, leaves=None, comp_offs=None, saved_entries=None):
    """Return one roster row per assignment per calendar day.

    The generator applies process calendars, weekly offs, approved leaves,
    comp-offs and manager overrides in a deterministic order.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    holidays = holidays or {}
    leaves = leaves or {}
    comp_offs = comp_offs or {}
    saved_entries = saved_entries or {}
    roster = []
    for employee in employee_rows:
        for day_number in range(1, days_in_month + 1):
            current_date = date(year, month, day_number)
            weekday = WEEKDAYS[current_date.weekday()]
            key = (employee["employee_code"], current_date.isoformat())
            operating_calendar = employee["operating_calendar"] if "operating_calendar" in employee.keys() else "holidays_off"
            closed_for_calendar = (
                operating_calendar == "sundays_holidays_off" and (weekday == "Sunday" or current_date.isoformat() in holidays)
            ) or (
                operating_calendar == "holidays_off" and current_date.isoformat() in holidays
            )
            if key in saved_entries and saved_entries[key].get("source", "manager") == "manager":
                status = saved_entries[key]["status"]
            elif current_date.isoformat() in holidays:
                status = "Holiday" if operating_calendar != "365_days" else "Working"
            elif key in leaves:
                status = "Leave"
            elif key in comp_offs:
                status = "Weekly Off"
            elif closed_for_calendar:
                status = "Holiday"
            else:
                status = "Weekly Off" if weekday == employee["weekly_off"] else "Working"
            roster.append({
                "date": current_date,
                "date_label": current_date.strftime("%a, %d %b"),
                "weekday": weekday,
                "employee_id": employee["employee_id"] if "employee_id" in employee.keys() else None,
                "brand_id": employee["brand_id"] if "brand_id" in employee.keys() else None,
                "process_id": employee["process_id"] if "process_id" in employee.keys() else None,
                "employee_code": employee["employee_code"],
                "full_name": employee["full_name"],
                "pod_name": employee["pod_name"],
                "brand_name": employee["brand_name"],
                "process_name": employee["process_name"],
                "status": status,
                "notes": saved_entries.get(key, {}).get("notes") or holidays.get(current_date.isoformat()) or leaves.get(key),
            })
    return roster


def daily_summary(roster):
    """Summarize working and off-duty employees for each date."""
    summary = {}
    for row in roster:
        entry = summary.setdefault(row["date"], {"date_label": row["date"].strftime("%a, %d %b"), "working": 0, "off": 0, "leave": 0, "holiday": 0})
        if row["status"] == "Working":
            entry["working"] += 1
        elif row["status"] == "Leave":
            entry["leave"] += 1
        elif row["status"] == "Holiday":
            entry["holiday"] += 1
        else:
            entry["off"] += 1
    return list(summary.values())


def find_conflicts(roster, rules, holidays=None):
    """Validate coverage and overlapping responsibilities for a generated roster."""
    holidays = holidays or {}
    conflicts = []
    dates = sorted({row["date"] for row in roster})
    for rule in rules:
        for current_date in dates:
            is_holiday = current_date.isoformat() in holidays
            is_sunday = current_date.weekday() == 6
            calendar_type = rule["operating_calendar"]
            required = calendar_type == "365_days" or (
                calendar_type == "holidays_off" and not is_holiday
            ) or (
                calendar_type == "sundays_holidays_off" and not is_sunday and not is_holiday
            )
            if not required:
                continue
            matching = [
                row for row in roster
                if row["date"] == current_date
                and row["brand_name"] == rule["brand_name"]
                and row["process_name"] == rule["process_name"]
                and row["status"] == "Working"
            ]
            if len(matching) < rule["minimum_staff"]:
                conflicts.append({
                    "severity": "Critical",
                    "date_label": current_date.strftime("%a, %d %b"),
                    "title": f"Coverage gap: {rule['brand_name']} / {rule['process_name']}",
                    "detail": f"Needs {rule['minimum_staff']} working employee(s), but only {len(matching)} available.",
                })

    by_employee_date = {}
    for row in roster:
        if row["status"] == "Working":
            key = (row["employee_code"], row["date"])
            by_employee_date.setdefault(key, []).append(row)
    for (employee_code, current_date), rows in by_employee_date.items():
        if len(rows) > 1:
            conflicts.append({
                "severity": "Warning",
                "date_label": current_date.strftime("%a, %d %b"),
                "title": f"Overlapping work: {employee_code}",
                "detail": "This employee is scheduled for more than one brand/process on the same day.",
            })
    return conflicts
