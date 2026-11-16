import calendar
import csv
import io
import os
from functools import wraps
from datetime import date

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from database import get_connection, initialize_database
from scheduler import daily_summary, find_conflicts, generate_month_roster


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "local-development-secret-change-before-deployment")
initialize_database()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT u.id, u.username, u.role, u.employee_id, u.company_id, e.full_name, b.name AS company_name FROM users u LEFT JOIN employees e ON e.id=u.employee_id LEFT JOIN brands b ON b.id=u.company_id WHERE u.id=? AND u.active=1",
            (user_id,),
        ).fetchone()
    finally:
        connection.close()


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(roles=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.path))
            if roles and user["role"] not in roles:
                return "You do not have permission to access this page.", 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        connection = get_connection()
        try:
            user = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        finally:
            connection.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            destination = request.args.get("next") or request.form.get("next") or url_for("home")
            return redirect(destination)
        return render_template("login.html", error="Username or password is incorrect.")
    return render_template("login.html", error=None, next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def materialize_monthly_schedule(connection, year, month):
    """Persist the generated baseline for every employee/day in a month."""
    employees_with_assignments = connection.execute(
        """SELECT e.id AS employee_id, a.brand_id, a.process_id, e.employee_code, e.full_name, e.weekly_off,
                  p.name AS pod_name, b.name AS brand_name, pr.name AS process_name,
                  COALESCE(r.operating_calendar, 'holidays_off') AS operating_calendar
           FROM employees e JOIN pods p ON p.id=e.pod_id LEFT JOIN assignments a ON a.employee_id=e.id
           LEFT JOIN brands b ON b.id=a.brand_id LEFT JOIN processes pr ON pr.id=a.process_id
           LEFT JOIN brand_process_rules r ON r.brand_id=a.brand_id AND r.process_id=a.process_id
           WHERE e.active=1 ORDER BY e.id, a.id"""
    ).fetchall()
    holidays = {row["holiday_date"]: row["name"] for row in connection.execute("SELECT holiday_date,name FROM holidays")}
    leaves = {(row["employee_code"], row["leave_date"]): row["reason"] for row in connection.execute("SELECT e.employee_code,l.leave_date,l.reason FROM leaves l JOIN employees e ON e.id=l.employee_id WHERE l.status='Approved'")}
    comp_offs = {(row["employee_code"], row["comp_date"]): row["reason"] for row in connection.execute("SELECT e.employee_code,c.comp_date,c.reason FROM comp_offs c JOIN employees e ON e.id=c.employee_id WHERE c.status='Approved'")}
    saved_entries = {(row["employee_code"], row["work_date"]): {"status": row["status"], "notes": row["notes"], "source": row["source"]} for row in connection.execute("SELECT e.employee_code,r.work_date,r.status,r.notes,r.source FROM roster_entries r JOIN employees e ON e.id=r.employee_id")}
    generated = generate_month_roster(employees_with_assignments, year, month, holidays=holidays, leaves=leaves, comp_offs=comp_offs, saved_entries=saved_entries)
    seen = set()
    for row in generated:
        key = (row["employee_id"], row["date"].isoformat())
        if not row["employee_id"] or key in seen:
            continue
        seen.add(key)
        connection.execute(
            """INSERT INTO roster_entries (employee_id, work_date, status, brand_id, process_id, notes, source)
               VALUES (?, ?, ?, ?, ?, ?, 'generated')
               ON CONFLICT(employee_id, work_date) DO UPDATE SET status=excluded.status,
               brand_id=excluded.brand_id, process_id=excluded.process_id,
               notes=excluded.notes, source='generated'
               WHERE roster_entries.source='generated'""",
            (row["employee_id"], row["date"].isoformat(), "Weekly Off" if row["status"] == "Weekly off" else row["status"], row["brand_id"], row["process_id"], row["notes"]),
        )
    connection.execute("INSERT OR IGNORE INTO roster_periods (period, status) VALUES (?, 'Draft')", (f"{year:04d}-{month:02d}",))
    connection.commit()
    return len(seen)


@app.route("/company", methods=["GET", "POST"])
@login_required({"company"})
def company_portal():
    user = current_user()
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    if request.method == "POST":
        selected_month = request.form.get("month", selected_month)
    connection = get_connection()
    try:
        if request.method == "POST":
            assigned_employee_id = request.form.get("assigned_employee_id", type=int) or None
            if assigned_employee_id:
                allowed = connection.execute("SELECT 1 FROM assignments WHERE employee_id=? AND brand_id=?", (assigned_employee_id, user["company_id"])).fetchone()
                if not allowed:
                    return "That employee is not assigned to this company.", 403
            connection.execute(
                """INSERT INTO work_tasks (brand_id, process_id, title, description, cadence, work_date, due_date, assigned_employee_id, status, priority, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user["company_id"], request.form.get("process_id", type=int) or None, request.form.get("title", "").strip(), request.form.get("description", "").strip() or None, request.form.get("cadence", "Daily"), request.form.get("work_date", date.today().isoformat()), request.form.get("due_date") or None, assigned_employee_id, "Unassigned" if not assigned_employee_id else "In Progress", request.form.get("priority", "Medium"), request.form.get("notes", "").strip() or None),
            )
            connection.commit()
            return redirect(url_for("company_portal", month=selected_month, saved=1))
        tasks = connection.execute(
            """SELECT t.*, p.name AS process_name, e.full_name AS employee_name FROM work_tasks t
               LEFT JOIN processes p ON p.id=t.process_id LEFT JOIN employees e ON e.id=t.assigned_employee_id
               WHERE t.brand_id=? AND substr(t.work_date,1,7)=? ORDER BY t.work_date, t.id DESC""",
            (user["company_id"], selected_month),
        ).fetchall()
        company = connection.execute("SELECT id, name, description FROM brands WHERE id=?", (user["company_id"],)).fetchone()
        processes = connection.execute("SELECT id, name FROM processes ORDER BY name").fetchall()
        employees = connection.execute(
            """SELECT DISTINCT e.id, e.full_name, e.employee_code FROM assignments a JOIN employees e ON e.id=a.employee_id WHERE a.brand_id=? ORDER BY e.full_name""",
            (user["company_id"],),
        ).fetchall()
        return render_template("company.html", company=company, tasks=tasks, processes=processes, employees=employees, selected_month=selected_month, saved=request.args.get("saved") == "1")
    finally:
        connection.close()


@app.route("/")
@login_required()
def home():
    if current_user()["role"] == "employee":
        return redirect(url_for("my_work"))
    if current_user()["role"] == "company":
        return redirect(url_for("company_portal"))
    connection = get_connection()
    try:
        stats = {
            "employees": connection.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0],
            "brands": connection.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
            "processes": connection.execute("SELECT COUNT(*) FROM processes").fetchone()[0],
            "pods": connection.execute("SELECT COUNT(*) FROM pods").fetchone()[0],
        }
        assignments = connection.execute(
            """
            SELECT e.full_name, e.employee_code, p.name AS pod_name,
                   b.name AS brand_name, pr.name AS process_name
            FROM assignments a
            JOIN employees e ON e.id = a.employee_id
            JOIN pods p ON p.id = e.pod_id
            JOIN brands b ON b.id = a.brand_id
            JOIN processes pr ON pr.id = a.process_id
            ORDER BY e.id LIMIT 6
            """
        ).fetchall()
        return render_template("index.html", stats=stats, assignments=assignments)
    finally:
        connection.close()


@app.route("/employees")
@login_required()
def employees():
    if current_user()["role"] in {"employee", "company"}:
        return redirect(url_for("my_work"))
    search = request.args.get("search", "").strip()
    connection = get_connection()
    try:
        query = """
            SELECT e.employee_code, e.full_name, e.email, e.weekly_off,
                   p.name AS pod_name, p.leader_name,
                   b.name AS brand_name, pr.name AS process_name, u.username AS login_id
            FROM employees e
            JOIN pods p ON p.id = e.pod_id
            LEFT JOIN assignments a ON a.employee_id = e.id
            LEFT JOIN brands b ON b.id = a.brand_id
            LEFT JOIN processes pr ON pr.id = a.process_id
            LEFT JOIN users u ON u.employee_id = e.id AND u.role = 'employee'
        """
        parameters = []
        if search:
            query += " WHERE e.full_name LIKE ? OR e.employee_code LIKE ? OR p.name LIKE ?"
            term = f"%{search}%"
            parameters = [term, term, term]
        query += " ORDER BY e.id"
        employee_rows = connection.execute(query, parameters).fetchall()
        return render_template("employees.html", employees=employee_rows, search=search)
    finally:
        connection.close()


@app.route("/my-work", methods=["GET", "POST"])
@login_required()
def my_work():
    user = current_user()
    if user["role"] == "company":
        return redirect(url_for("company_portal"))
    selected_employee_id = user["employee_id"] if user["role"] != "manager" else (request.args.get("employee_id", type=int) or user["employee_id"])
    connection = get_connection()
    try:
        if request.method == "POST":
            if request.form.get("action") == "preference":
                if user["role"] != "manager" and selected_employee_id != user["employee_id"]:
                    return "You do not have permission to update this preference.", 403
                connection.execute(
                    """INSERT INTO employee_preferences (employee_id, preferred_off_days, max_weekly_hours, notes)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(employee_id) DO UPDATE SET preferred_off_days=excluded.preferred_off_days, max_weekly_hours=excluded.max_weekly_hours, notes=excluded.notes""",
                    (selected_employee_id, request.form.get("preferred_off_days", "").strip() or None, float(request.form.get("max_weekly_hours") or 40), request.form.get("preference_notes", "").strip() or None),
                )
                connection.commit()
                return redirect(url_for("my_work", employee_id=selected_employee_id, saved=1))
            if user["role"] == "manager" or selected_employee_id == user["employee_id"]:
                connection.execute(
                    """INSERT INTO employee_work_updates (employee_id, work_date, summary, status, hours)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(employee_id, work_date) DO UPDATE SET summary=excluded.summary, status=excluded.status, hours=excluded.hours""",
                    (selected_employee_id, request.form.get("work_date"), request.form.get("summary", "").strip(), request.form.get("status", "Working"), float(request.form.get("hours") or 0)),
                )
                connection.commit()
                return redirect(url_for("my_work", employee_id=selected_employee_id, saved=1))
        if not selected_employee_id:
            selected_employee_id = connection.execute("SELECT id FROM employees WHERE active=1 ORDER BY id LIMIT 1").fetchone()[0]
        employee = connection.execute(
            """SELECT e.*, p.name AS pod_name, p.leader_name FROM employees e JOIN pods p ON p.id=e.pod_id WHERE e.id=?""",
            (selected_employee_id,),
        ).fetchone()
        tasks = connection.execute(
            """SELECT t.*, b.name AS brand_name, p.name AS process_name FROM work_tasks t
               JOIN brands b ON b.id=t.brand_id LEFT JOIN processes p ON p.id=t.process_id
               WHERE t.assigned_employee_id=? ORDER BY t.work_date DESC, t.priority DESC""",
            (selected_employee_id,),
        ).fetchall()
        today = date.today().isoformat()
        today_update = connection.execute("SELECT * FROM employee_work_updates WHERE employee_id=? AND work_date=?", (selected_employee_id, today)).fetchone()
        work_stats = {
            "total": len(tasks),
            "active": sum(1 for task in tasks if task["status"] == "In Progress"),
            "blocked": sum(1 for task in tasks if task["status"] == "Blocked"),
            "completed": sum(1 for task in tasks if task["status"] == "Completed"),
        }
        preference = connection.execute("SELECT * FROM employee_preferences WHERE employee_id=?", (selected_employee_id,)).fetchone()
        all_employees = connection.execute("SELECT id, full_name, employee_code FROM employees WHERE active=1 ORDER BY full_name").fetchall() if user["role"] == "manager" else []
        return render_template("my_work.html", employee=employee, tasks=tasks, preference=preference, all_employees=all_employees, saved=request.args.get("saved") == "1", today=today, today_update=today_update, work_stats=work_stats)
    finally:
        connection.close()


@app.route("/calendar")
@login_required()
def calendar_view():
    if current_user()["role"] in {"employee", "company"}:
        return redirect(url_for("my_work"))
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        year, month = (int(value) for value in selected_month.split("-"))
        if month not in range(1, 13):
            raise ValueError
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")
        year, month = (int(value) for value in selected_month.split("-"))
    connection = get_connection()
    try:
        employees_with_assignments = connection.execute(
            """SELECT e.id AS employee_id, a.brand_id, a.process_id, e.employee_code, e.full_name, e.weekly_off, p.name AS pod_name,
                      b.name AS brand_name, pr.name AS process_name,
                      COALESCE(r.operating_calendar, 'holidays_off') AS operating_calendar
               FROM employees e JOIN pods p ON p.id=e.pod_id LEFT JOIN assignments a ON a.employee_id=e.id
               LEFT JOIN brands b ON b.id=a.brand_id LEFT JOIN processes pr ON pr.id=a.process_id
               LEFT JOIN brand_process_rules r ON r.brand_id=a.brand_id AND r.process_id=a.process_id
               WHERE e.active=1 ORDER BY e.full_name"""
        ).fetchall()
        holidays = {row["holiday_date"]: row["name"] for row in connection.execute("SELECT holiday_date,name FROM holidays")}
        leaves = {(row["employee_code"], row["leave_date"]): row["reason"] for row in connection.execute("SELECT e.employee_code,l.leave_date,l.reason FROM leaves l JOIN employees e ON e.id=l.employee_id WHERE l.status='Approved'")}
        comp_offs = {(row["employee_code"], row["comp_date"]): row["reason"] for row in connection.execute("SELECT e.employee_code,c.comp_date,c.reason FROM comp_offs c JOIN employees e ON e.id=c.employee_id WHERE c.status='Approved'")}
        saved_entries = {(row["employee_code"], row["work_date"]): {"status": row["status"], "notes": row["notes"], "source": row["source"]} for row in connection.execute("SELECT e.employee_code,r.work_date,r.status,r.notes,r.source FROM roster_entries r JOIN employees e ON e.id=r.employee_id")}
        generated = generate_month_roster(employees_with_assignments, year, month, holidays=holidays, leaves=leaves, comp_offs=comp_offs, saved_entries=saved_entries)
        day_dates = [date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)]
        grouped = {}
        for row in generated:
            grouped.setdefault(row["employee_code"], {"employee_code": row["employee_code"], "full_name": row["full_name"], "pod_name": row["pod_name"], "days": {}})
            grouped[row["employee_code"]]["days"].setdefault(row["date"], row)
        calendar_rows = [{**item, "days": [item["days"].get(day, {"status": "Working"}) for day in day_dates]} for item in grouped.values()]
        return render_template("calendar.html", selected_month=selected_month, month_label=date(year, month, 1).strftime("%B %Y"), days=day_dates, calendar_rows=calendar_rows)
    finally:
        connection.close()


@app.route("/roster", methods=["GET", "POST"])
@login_required()
def roster():
    if current_user()["role"] in {"employee", "company"}:
        return redirect(url_for("my_work"))
    selected_month = request.form.get("month") if request.method == "POST" else request.args.get("month")
    selected_month = selected_month or date.today().strftime("%Y-%m")
    try:
        year, month = (int(value) for value in selected_month.split("-"))
        if month not in range(1, 13):
            raise ValueError
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")
        year, month = (int(value) for value in selected_month.split("-"))
    focus_date = request.form.get("focus") if request.method == "POST" else request.args.get("focus")
    default_focus = date.today().isoformat() if selected_month == date.today().strftime("%Y-%m") else date(year, month, 1).isoformat()
    focus_date = focus_date or default_focus
    try:
        date.fromisoformat(focus_date)
    except ValueError:
        focus_date = date(year, month, 1).isoformat()

    connection = get_connection()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "materialize" and current_user()["role"] == "manager":
                materialize_monthly_schedule(connection, year, month)
                return redirect(url_for("roster", month=selected_month, focus=focus_date, saved=1))
            if action in {"task", "comp_off", "holiday", "employee_update"} and current_user()["role"] != "manager":
                return "Only a manager can change this roster data.", 403
            if action == "period_status" and current_user()["role"] not in {"manager", "pod_lead"}:
                return "Only a manager or pod lead can update roster status.", 403
            if request.form.get("action") == "period_status":
                connection.execute(
                    """INSERT INTO roster_periods (period, status, reviewer_note)
                       VALUES (?, ?, ?)
                       ON CONFLICT(period) DO UPDATE SET status=excluded.status,
                       reviewer_note=excluded.reviewer_note, updated_at=CURRENT_TIMESTAMP""",
                    (selected_month, request.form.get("period_status", "Draft"), request.form.get("reviewer_note", "").strip() or None),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, focus=focus_date, brand=request.form.get("brand", ""), saved=1))
            if request.form.get("action") == "task":
                connection.execute(
                    """INSERT INTO work_tasks
                       (brand_id, process_id, title, description, cadence, work_date,
                        due_date, assigned_employee_id, status, priority, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        request.form.get("brand_id", type=int), request.form.get("process_id", type=int) or None,
                        request.form.get("title", "").strip(), request.form.get("description", "").strip() or None,
                        request.form.get("cadence", "Daily"), request.form.get("work_date", focus_date if "focus_date" in locals() else selected_month + "-01"),
                        request.form.get("due_date", "") or None, request.form.get("assigned_employee_id", type=int) or None,
                        request.form.get("status", "Unassigned"), request.form.get("priority", "Medium"), request.form.get("notes", "").strip() or None,
                    ),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, focus=request.form.get("work_date", ""), brand=request.form.get("brand_id", ""), saved=1))
            if request.form.get("action") == "update_task":
                connection.execute(
                    "UPDATE work_tasks SET status=?, assigned_employee_id=?, notes=? WHERE id=?",
                    (request.form.get("status", "Unassigned"), request.form.get("assigned_employee_id", type=int) or None, request.form.get("notes", "").strip() or None, request.form.get("task_id", type=int)),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, focus=request.form.get("focus", ""), brand=request.form.get("brand", ""), saved=1))
            if request.form.get("action") == "employee_update":
                connection.execute(
                    """INSERT INTO employee_work_updates (employee_id, work_date, summary, status, hours)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(employee_id, work_date) DO UPDATE SET summary=excluded.summary, status=excluded.status, hours=excluded.hours""",
                    (request.form.get("employee_id", type=int), request.form.get("work_date", ""), request.form.get("summary", "").strip(), request.form.get("update_status", "Working"), float(request.form.get("hours") or 0)),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, focus=request.form.get("work_date", ""), saved=1))
            if request.form.get("action") == "comp_off":
                connection.execute(
                    """INSERT INTO comp_offs (employee_id, comp_date, reason)
                       VALUES (?, ?, ?)
                       ON CONFLICT(employee_id, comp_date) DO UPDATE SET reason=excluded.reason""",
                    (request.form.get("employee_id", type=int), request.form.get("comp_date", ""), request.form.get("comp_reason", "").strip() or None),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, focus=request.form.get("comp_date", ""), saved=1))
            if request.form.get("action") == "holiday":
                holiday_date = request.form.get("holiday_date", "")
                holiday_name = request.form.get("holiday_name", "").strip()
                connection.execute(
                    "INSERT INTO holidays (holiday_date, name) VALUES (?, ?) ON CONFLICT(holiday_date) DO UPDATE SET name=excluded.name",
                    (holiday_date, holiday_name),
                )
                connection.commit()
                return redirect(url_for("roster", month=selected_month, saved=1))
            if not action and current_user()["role"] != "manager":
                return "Only a manager can edit an employee schedule.", 403
            employee_id = request.form.get("employee_id", type=int)
            work_date = request.form.get("work_date", "")
            status = request.form.get("status", "Working")
            brand_id = request.form.get("brand_id", type=int) or None
            process_id = request.form.get("process_id", type=int) or None
            notes = request.form.get("notes", "").strip() or None
            connection.execute(
                """INSERT INTO roster_entries
                   (employee_id, work_date, status, brand_id, process_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(employee_id, work_date) DO UPDATE SET
                   status=excluded.status, brand_id=excluded.brand_id,
                   process_id=excluded.process_id, notes=excluded.notes""",
                (employee_id, work_date, status, brand_id, process_id, notes),
            )
            if status == "Leave":
                connection.execute(
                    """INSERT INTO leaves (employee_id, leave_date, reason)
                       VALUES (?, ?, ?)
                       ON CONFLICT(employee_id, leave_date) DO UPDATE SET reason=excluded.reason""",
                    (employee_id, work_date, notes),
                )
            else:
                connection.execute(
                    "DELETE FROM leaves WHERE employee_id=? AND leave_date=?",
                    (employee_id, work_date),
                )
            connection.commit()
            return redirect(url_for("roster", month=selected_month, saved=1))

        employees_with_assignments = connection.execute(
            """
            SELECT e.id AS employee_id, a.brand_id, a.process_id, e.employee_code, e.full_name, e.weekly_off, p.name AS pod_name,
                   b.name AS brand_name, pr.name AS process_name,
                   COALESCE(r.operating_calendar, 'holidays_off') AS operating_calendar
            FROM employees e
            JOIN pods p ON p.id = e.pod_id
            LEFT JOIN assignments a ON a.employee_id = e.id
            LEFT JOIN brands b ON b.id = a.brand_id
            LEFT JOIN processes pr ON pr.id = a.process_id
            LEFT JOIN brand_process_rules r ON r.brand_id = a.brand_id AND r.process_id = a.process_id
            WHERE e.active = 1 ORDER BY e.id
            """
        ).fetchall()
        holiday_rows = connection.execute("SELECT holiday_date, name FROM holidays").fetchall()
        leave_rows = connection.execute(
            """SELECT e.employee_code, l.leave_date, l.reason
               FROM leaves l JOIN employees e ON e.id = l.employee_id
               WHERE l.status = 'Approved'"""
        ).fetchall()
        comp_off_rows = connection.execute(
            """SELECT e.employee_code, c.comp_date, c.reason
               FROM comp_offs c JOIN employees e ON e.id=c.employee_id
               WHERE c.status='Approved'"""
        ).fetchall()
        saved_rows = connection.execute(
            """SELECT e.employee_code, r.work_date, r.status, r.notes, r.source
               FROM roster_entries r JOIN employees e ON e.id = r.employee_id"""
        ).fetchall()
        holidays = {row["holiday_date"]: row["name"] for row in holiday_rows}
        leaves = {(row["employee_code"], row["leave_date"]): row["reason"] for row in leave_rows}
        comp_offs = {(row["employee_code"], row["comp_date"]): row["reason"] for row in comp_off_rows}
        saved_entries = {
            (row["employee_code"], row["work_date"]): {"status": row["status"], "notes": row["notes"], "source": row["source"]}
            for row in saved_rows
        }
        generated_roster = generate_month_roster(
            employees_with_assignments, year, month,
            holidays=holidays, leaves=leaves, comp_offs=comp_offs, saved_entries=saved_entries,
        )
        days = daily_summary(generated_roster)
        rules = connection.execute(
            """SELECT r.operating_calendar, r.minimum_staff, b.name AS brand_name, p.name AS process_name
               FROM brand_process_rules r JOIN brands b ON b.id=r.brand_id JOIN processes p ON p.id=r.process_id"""
        ).fetchall()
        conflicts = find_conflicts(generated_roster, rules, holidays)
        month_start = date(year, month, 1).isoformat()
        month_end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        company_cards = connection.execute(
            """SELECT b.id, b.name,
               COUNT(t.id) AS total_tasks,
               COALESCE(SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END), 0) AS completed_tasks,
               COALESCE(SUM(CASE WHEN t.status <> 'Completed' OR t.status IS NULL THEN 1 ELSE 0 END), 0) AS remaining_tasks
               FROM brands b LEFT JOIN work_tasks t ON t.brand_id=b.id AND t.work_date BETWEEN ? AND ?
               GROUP BY b.id ORDER BY b.name""",
            (month_start, month_end),
        ).fetchall()
        selected_brand_id = request.args.get("brand", type=int)
        selected_brand = next((row for row in company_cards if row["id"] == selected_brand_id), None)
        brand_tasks = []
        brand_people = []
        if selected_brand_id:
            brand_tasks = connection.execute(
                """SELECT t.*, p.name AS process_name, e.full_name AS employee_name
                   FROM work_tasks t LEFT JOIN processes p ON p.id=t.process_id
                   LEFT JOIN employees e ON e.id=t.assigned_employee_id
                   WHERE t.brand_id=? AND t.work_date BETWEEN ? AND ? ORDER BY t.work_date, t.priority DESC, t.id""",
                (selected_brand_id, month_start, month_end),
            ).fetchall()
            brand_people = connection.execute(
                """SELECT DISTINCT e.id AS employee_id, e.full_name, e.employee_code, p.name AS process_name, e.weekly_off
                   FROM assignments a JOIN employees e ON e.id=a.employee_id
                   JOIN processes p ON p.id=a.process_id WHERE a.brand_id=? ORDER BY e.full_name""",
                (selected_brand_id,),
            ).fetchall()
        idle_employees = connection.execute(
            """SELECT e.id, e.full_name, e.employee_code, p.name AS pod_name
               FROM employees e JOIN pods p ON p.id=e.pod_id
               WHERE e.active=1
                 AND NOT EXISTS (SELECT 1 FROM work_tasks t WHERE t.assigned_employee_id=e.id AND t.work_date=? AND t.status IN ('In Progress','Blocked'))
                 AND NOT EXISTS (SELECT 1 FROM employee_work_updates u WHERE u.employee_id=e.id AND u.work_date=? AND u.status IN ('Working','Done'))
               ORDER BY e.full_name""",
            (focus_date, focus_date),
        ).fetchall()
        work_updates = connection.execute(
            """SELECT u.*, e.full_name, e.employee_code FROM employee_work_updates u
               JOIN employees e ON e.id=u.employee_id WHERE u.work_date=? ORDER BY e.full_name""",
            (focus_date,),
        ).fetchall()
        period = connection.execute("SELECT period, status, reviewer_note, updated_at FROM roster_periods WHERE period=?", (selected_month,)).fetchone()
        if period is None:
            connection.execute("INSERT OR IGNORE INTO roster_periods (period, status) VALUES (?, 'Draft')", (selected_month,))
            connection.commit()
            period = connection.execute("SELECT period, status, reviewer_note, updated_at FROM roster_periods WHERE period=?", (selected_month,)).fetchone()
        employees = connection.execute("SELECT id, full_name, employee_code FROM employees WHERE active = 1 ORDER BY id").fetchall()
        brands = connection.execute("SELECT id, name FROM brands ORDER BY name").fetchall()
        processes = connection.execute("SELECT id, name FROM processes ORDER BY name").fetchall()
        return render_template(
            "roster.html", selected_month=selected_month,
            days=days, month_label=date(year, month, 1).strftime("%B %Y"),
            employees=employees, brands=brands, processes=processes,
            saved=request.args.get("saved") == "1", conflicts=conflicts,
            focus_date=focus_date, company_cards=company_cards, selected_brand=selected_brand,
            brand_tasks=brand_tasks, brand_people=brand_people, idle_employees=idle_employees,
            work_updates=work_updates, period=period,
        )
    finally:
        connection.close()


@app.route("/manage", methods=["GET", "POST"])
@login_required({"manager"})
def manage():
    connection = get_connection()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add_brand":
                connection.execute("INSERT OR IGNORE INTO brands (name, description) VALUES (?, ?)", (request.form.get("name", "").strip(), request.form.get("description", "").strip() or None))
            elif action == "add_process":
                connection.execute("INSERT OR IGNORE INTO processes (name, description) VALUES (?, ?)", (request.form.get("name", "").strip(), request.form.get("description", "").strip() or None))
            elif action == "add_pod":
                connection.execute("INSERT OR IGNORE INTO pods (name, leader_name) VALUES (?, ?)", (request.form.get("name", "").strip(), request.form.get("leader_name", "").strip()))
            elif action == "add_employee":
                connection.execute("""INSERT OR IGNORE INTO employees (employee_code, full_name, email, weekly_off, pod_id) VALUES (?, ?, ?, ?, ?)""", (request.form.get("employee_code", "").strip(), request.form.get("full_name", "").strip(), request.form.get("email", "").strip(), request.form.get("weekly_off", "Sunday"), request.form.get("pod_id", type=int)))
            elif action == "delete_assignment":
                connection.execute("DELETE FROM assignments WHERE id = ?", (request.form.get("assignment_id", type=int),))
            else:
                connection.execute(
                    """INSERT OR IGNORE INTO assignments (employee_id, brand_id, process_id)
                       VALUES (?, ?, ?)""",
                    (request.form.get("employee_id", type=int), request.form.get("brand_id", type=int), request.form.get("process_id", type=int)),
                )
            connection.commit()
            return redirect(url_for("manage", saved=1))
        assignments = connection.execute(
            """SELECT a.id, e.full_name, e.employee_code, b.name AS brand_name, p.name AS process_name
               FROM assignments a JOIN employees e ON e.id=a.employee_id
               JOIN brands b ON b.id=a.brand_id JOIN processes p ON p.id=a.process_id
               ORDER BY b.name, e.full_name"""
        ).fetchall()
        employees = connection.execute("SELECT id, full_name, employee_code FROM employees WHERE active=1 ORDER BY full_name").fetchall()
        brands = connection.execute("SELECT id, name FROM brands ORDER BY name").fetchall()
        processes = connection.execute("SELECT id, name FROM processes ORDER BY name").fetchall()
        pods = connection.execute("SELECT id, name FROM pods ORDER BY name").fetchall()
        return render_template("manage.html", assignments=assignments, employees=employees, brands=brands, processes=processes, pods=pods, saved=request.args.get("saved") == "1")
    finally:
        connection.close()


@app.route("/rules", methods=["GET", "POST"])
@login_required({"manager"})
def rules():
    connection = get_connection()
    try:
        if request.method == "POST":
            connection.execute(
                """UPDATE brand_process_rules SET operating_calendar=?, minimum_staff=?, notes=? WHERE id=?""",
                (request.form.get("operating_calendar"), request.form.get("minimum_staff", type=int) or 1, request.form.get("notes", "").strip() or None, request.form.get("rule_id", type=int)),
            )
            connection.commit()
            return redirect(url_for("rules", saved=1))
        rows = connection.execute(
            """SELECT r.id, r.operating_calendar, r.minimum_staff, r.notes,
               b.name AS brand_name, p.name AS process_name
               FROM brand_process_rules r JOIN brands b ON b.id=r.brand_id
               JOIN processes p ON p.id=r.process_id ORDER BY b.name, p.name"""
        ).fetchall()
        return render_template("rules.html", rules=rows, saved=request.args.get("saved") == "1")
    finally:
        connection.close()


@app.route("/views")
@login_required()
def views():
    if current_user()["role"] in {"employee", "company"}:
        return redirect(url_for("my_work"))
    mode = request.args.get("mode", "company")
    value = request.args.get("value", "")
    connection = get_connection()
    try:
        filters = {"company": ("", []), "pod": ("p.id = ?", [value]), "brand": ("b.id = ?", [value]), "process": ("pr.id = ?", [value]), "employee": ("e.id = ?", [value])}
        condition, parameters = filters.get(mode, filters["company"])
        query = """SELECT e.full_name, e.employee_code, p.name AS pod_name, b.name AS brand_name, pr.name AS process_name, e.weekly_off FROM assignments a JOIN employees e ON e.id=a.employee_id JOIN pods p ON p.id=e.pod_id JOIN brands b ON b.id=a.brand_id JOIN processes pr ON pr.id=a.process_id"""
        if condition:
            query += " WHERE " + condition
        query += " ORDER BY e.full_name, b.name, pr.name"
        rows = connection.execute(query, parameters).fetchall()
        options = {
            "pod": connection.execute("SELECT id, name FROM pods ORDER BY name").fetchall(),
            "brand": connection.execute("SELECT id, name FROM brands ORDER BY name").fetchall(),
            "process": connection.execute("SELECT id, name FROM processes ORDER BY name").fetchall(),
            "employee": connection.execute("SELECT id, full_name AS name FROM employees WHERE active=1 ORDER BY full_name").fetchall(),
        }
        return render_template("views.html", rows=rows, mode=mode, value=value, options=options, row_count=len(rows))
    finally:
        connection.close()


@app.route("/api/summary")
@login_required()
def api_summary():
    connection = get_connection()
    try:
        result = {
            "employees": connection.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0],
            "brands": connection.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
            "processes": connection.execute("SELECT COUNT(*) FROM processes").fetchone()[0],
            "pods": connection.execute("SELECT COUNT(*) FROM pods").fetchone()[0],
        }
        return jsonify(result)
    finally:
        connection.close()


@app.route("/healthz")
def healthz():
    connection = get_connection()
    try:
        connection.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "database": "connected"})
    except Exception:
        return jsonify({"status": "error", "database": "unavailable"}), 503
    finally:
        connection.close()


@app.route("/export/tasks.csv")
@login_required()
def export_tasks():
    if current_user()["role"] in {"employee", "company"}:
        return "You do not have permission to export the team task list.", 403
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        year, month = (int(value) for value in selected_month.split("-"))
        if month not in range(1, 13):
            raise ValueError
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")
        year, month = (int(value) for value in selected_month.split("-"))
    start = date(year, month, 1).isoformat()
    end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    connection = get_connection()
    try:
        rows = connection.execute(
            """SELECT t.work_date, b.name AS brand, p.name AS process, t.title,
               t.cadence, t.priority, t.status, e.full_name AS assigned_to,
               t.due_date, t.notes FROM work_tasks t JOIN brands b ON b.id=t.brand_id
               LEFT JOIN processes p ON p.id=t.process_id LEFT JOIN employees e ON e.id=t.assigned_employee_id
               WHERE t.work_date BETWEEN ? AND ? ORDER BY t.work_date, b.name, t.id""",
            (start, end),
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Work date", "Brand", "Process", "Task", "Cadence", "Priority", "Status", "Assigned to", "Due date", "Notes"])
        writer.writerows([tuple(row) for row in rows])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=tasks-{selected_month}.csv"})
    finally:
        connection.close()


if __name__ == "__main__":
    app.run(debug=True)
