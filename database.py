"""Create and seed the SQLite database for the Smart Roster project."""

from pathlib import Path
import sqlite3
import calendar
from datetime import date

from werkzeug.security import generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "smart_roster.db"


def get_connection():
    """Open a database connection and return rows like dictionaries."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_tables(connection):
    """Create the five core tables if they do not already exist."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS processes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS pods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            leader_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_code TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            weekly_off TEXT NOT NULL,
            pod_id INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (pod_id) REFERENCES pods(id)
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            brand_id INTEGER NOT NULL,
            process_id INTEGER NOT NULL,
            start_date TEXT,
            end_date TEXT,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
            FOREIGN KEY (brand_id) REFERENCES brands(id),
            FOREIGN KEY (process_id) REFERENCES processes(id),
            UNIQUE (employee_id, brand_id, process_id)
        );

        CREATE TABLE IF NOT EXISTS holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holiday_date TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_date TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'Approved',
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
            UNIQUE (employee_id, leave_date)
        );

        CREATE TABLE IF NOT EXISTS roster_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Working', 'Leave', 'Weekly Off', 'Holiday')),
            brand_id INTEGER,
            process_id INTEGER,
            notes TEXT,
            source TEXT NOT NULL DEFAULT 'manager' CHECK(source IN ('manager', 'generated')),
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
            FOREIGN KEY (brand_id) REFERENCES brands(id),
            FOREIGN KEY (process_id) REFERENCES processes(id),
            UNIQUE (employee_id, work_date)
        );

        CREATE TABLE IF NOT EXISTS brand_process_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_id INTEGER NOT NULL,
            process_id INTEGER NOT NULL,
            operating_calendar TEXT NOT NULL DEFAULT 'holidays_off',
            minimum_staff INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
            FOREIGN KEY (process_id) REFERENCES processes(id) ON DELETE CASCADE,
            UNIQUE (brand_id, process_id)
        );

        CREATE TABLE IF NOT EXISTS comp_offs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            comp_date TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'Approved',
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
            UNIQUE (employee_id, comp_date)
        );

        CREATE TABLE IF NOT EXISTS work_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_id INTEGER NOT NULL,
            process_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            cadence TEXT NOT NULL DEFAULT 'Daily' CHECK(cadence IN ('Daily', 'Weekly', 'Monthly')),
            work_date TEXT NOT NULL,
            due_date TEXT,
            assigned_employee_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Unassigned' CHECK(status IN ('Unassigned', 'In Progress', 'Completed', 'Blocked')),
            priority TEXT NOT NULL DEFAULT 'Medium' CHECK(priority IN ('Low', 'Medium', 'High')),
            notes TEXT,
            FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
            FOREIGN KEY (process_id) REFERENCES processes(id),
            FOREIGN KEY (assigned_employee_id) REFERENCES employees(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS employee_work_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Working' CHECK(status IN ('Working', 'Blocked', 'Done', 'Idle')),
            hours REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE,
            UNIQUE (employee_id, work_date)
        );

        CREATE TABLE IF NOT EXISTS roster_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft', 'Submitted', 'Approved', 'Published')),
            reviewer_note TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('manager', 'pod_lead', 'employee', 'company')),
            employee_id INTEGER,
            company_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE SET NULL,
            FOREIGN KEY (company_id) REFERENCES brands(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS employee_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL UNIQUE,
            preferred_off_days TEXT,
            max_weekly_hours REAL NOT NULL DEFAULT 40,
            notes TEXT,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        """
    )
    roster_columns = {row[1] for row in connection.execute("PRAGMA table_info(roster_entries)")}
    if "source" not in roster_columns:
        connection.execute("ALTER TABLE roster_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'manager'")
    user_schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if user_schema and "'company'" not in (user_schema[0] or ""):
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE users RENAME TO users_legacy")
        connection.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('manager', 'pod_lead', 'employee', 'company')),
            employee_id INTEGER,
            company_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE SET NULL,
            FOREIGN KEY (company_id) REFERENCES brands(id) ON DELETE SET NULL
        )""")
        connection.execute("INSERT INTO users (id, username, password_hash, role, employee_id, active) SELECT id, username, password_hash, role, employee_id, active FROM users_legacy")
        connection.execute("DROP TABLE users_legacy")
        connection.execute("PRAGMA foreign_keys=ON")


def seed_sample_data(connection):
    """Insert assessment-friendly sample data once, without creating duplicates."""
    brands = [
        ("GlowBeauty", "Beauty and personal-care customer support"),
        ("UrbanMart", "Online marketplace and order support"),
        ("FreshBite", "Food delivery customer operations"),
        ("HomeNest", "Home and lifestyle products"),
        ("TechZone", "Consumer electronics support"),
    ]
    processes = [
        ("Customer Support", "Customer questions and issue resolution"),
        ("Order Management", "Order changes, cancellations and tracking"),
        ("ACC", "Account, complaint and customer-care escalation"),
        ("NDR", "Non-delivery report follow-up"),
        ("Returns and Refunds", "Returns, refunds and replacement requests"),
    ]
    pods = [
        ("Alpha Pod", "Aarav Sharma"),
        ("Bravo Pod", "Diya Mehta"),
        ("Charlie Pod", "Kabir Singh"),
        ("Delta Pod", "Ananya Rao"),
        ("Echo Pod", "Rohan Kapoor"),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO brands (name, description) VALUES (?, ?)", brands
    )
    connection.executemany(
        "INSERT OR IGNORE INTO processes (name, description) VALUES (?, ?)", processes
    )
    connection.executemany(
        "INSERT OR IGNORE INTO pods (name, leader_name) VALUES (?, ?)", pods
    )

    employee_names = [
        "Aisha Khan", "Rahul Verma", "Zoya Ali", "Vikram Patel", "Meera Nair",
        "Arjun Das", "Sana Sheikh", "Karan Malhotra", "Ishita Roy", "Nikhil Jain",
        "Priya Menon", "Aditya Rao", "Neha Kapoor", "Sameer Joshi", "Riya Shah",
        "Manish Gupta", "Tanya Bose", "Yash Thakur", "Anjali Iyer", "Dev Bhatia",
        "Simran Kaur", "Aman Sethi", "Pooja Reddy", "Harsh Agarwal", "Nisha Pillai",
        "Mohit Chawla", "Kavya Sinha", "Ritesh Kulkarni", "Ira Sen", "Varun Bansal",
    ]
    weekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    pod_rows = connection.execute("SELECT id FROM pods ORDER BY id").fetchall()
    employees = [
        (
            f"EMP{i:03}", name, f"{name.lower().replace(' ', '.')}@d-operations.example",
            weekdays[(i - 1) % len(weekdays)], pod_rows[(i - 1) % len(pod_rows)][0]
        )
        for i, name in enumerate(employee_names, start=1)
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO employees
           (employee_code, full_name, email, weekly_off, pod_id)
           VALUES (?, ?, ?, ?, ?)""",
        employees,
    )

    employee_rows = connection.execute("SELECT id FROM employees ORDER BY id").fetchall()
    brand_rows = connection.execute("SELECT id FROM brands ORDER BY id").fetchall()
    process_rows = connection.execute("SELECT id FROM processes ORDER BY id").fetchall()
    assignments = [
        (employee[0], brand_rows[(index // 2) % len(brand_rows)][0], process_rows[index % len(process_rows)][0])
        for index, employee in enumerate(employee_rows)
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO assignments (employee_id, brand_id, process_id) VALUES (?, ?, ?)",
        assignments,
    )

    brand_rows = connection.execute("SELECT id FROM brands ORDER BY id").fetchall()
    process_rows = connection.execute("SELECT id FROM processes ORDER BY id").fetchall()
    rules = []
    for brand_index, brand in enumerate(brand_rows):
        for process_index, process in enumerate(process_rows):
            calendar_type = "365_days" if brand_index == 0 and process_index == 0 else (
                "sundays_holidays_off" if process_index in (2, 3) else "holidays_off"
            )
            minimum_staff = 2 if process_index == 0 and brand_index in (0, 1) else 1
            rules.append((brand[0], process[0], calendar_type, minimum_staff, "Seeded assessment rule"))
    connection.executemany(
        """INSERT OR IGNORE INTO brand_process_rules
           (brand_id, process_id, operating_calendar, minimum_staff, notes)
           VALUES (?, ?, ?, ?, ?)""",
        rules,
    )

    first_employee_id = employee_rows[0][0]
    users = [
        ("manager", generate_password_hash("manager123"), "manager", None, None),
        ("podlead", generate_password_hash("podlead123"), "pod_lead", first_employee_id, None),
        ("employee", generate_password_hash("employee123"), "employee", first_employee_id, None),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO users (username, password_hash, role, employee_id, company_id) VALUES (?, ?, ?, ?, ?)",
        users,
    )
    employee_login_rows = [
        (f"emp{employee['employee_code'][-3:]}", generate_password_hash("welcome123"), "employee", employee["id"], None)
        for employee in connection.execute("SELECT id, employee_code FROM employees ORDER BY id").fetchall()
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO users (username, password_hash, role, employee_id, company_id) VALUES (?, ?, ?, ?, ?)",
        employee_login_rows,
    )
    company_login_rows = [
        (f"company_{brand['name'].lower()}", generate_password_hash("company123"), "company", None, brand["id"])
        for brand in connection.execute("SELECT id, name FROM brands ORDER BY id").fetchall()
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO users (username, password_hash, role, employee_id, company_id) VALUES (?, ?, ?, ?, ?)",
        company_login_rows,
    )
    employee_pref_rows = connection.execute("SELECT id, weekly_off FROM employees ORDER BY id").fetchall()
    connection.executemany(
        "INSERT OR IGNORE INTO employee_preferences (employee_id, preferred_off_days, max_weekly_hours) VALUES (?, ?, ?)",
        [(employee["id"], employee["weekly_off"], 40) for employee in employee_pref_rows],
    )

    if connection.execute("SELECT COUNT(*) FROM work_tasks").fetchone()[0] == 0:
        today = date.today().isoformat()
        task_rows = [
            (brand_rows[0][0], process_rows[0][0], "Review priority customer tickets", "Clear the oldest open tickets.", "Daily", today, today, employee_rows[0][0], "In Progress", "High", None),
            (brand_rows[1][0], process_rows[1][0], "Audit pending orders", "Check delayed and cancelled orders.", "Daily", today, today, employee_rows[2][0], "In Progress", "Medium", None),
            (brand_rows[2][0], process_rows[2][0], "Prepare retention follow-up list", "Create the weekly callback list.", "Weekly", today, today, employee_rows[4][0], "Unassigned", "Medium", None),
            (brand_rows[3][0], process_rows[3][0], "Review NDR cases", "Resolve today's non-delivery cases.", "Daily", today, today, employee_rows[6][0], "Completed", "Medium", "Sample completed task"),
            (brand_rows[4][0], process_rows[4][0], "Process refund queue", "Validate refund requests and escalate exceptions.", "Monthly", today, today, None, "Unassigned", "High", None),
        ]
        connection.executemany(
            """INSERT INTO work_tasks
               (brand_id, process_id, title, description, cadence, work_date, due_date,
                assigned_employee_id, status, priority, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            task_rows,
        )

    current_period = date.today().strftime("%Y-%m")
    year, month = date.today().year, date.today().month
    month_start = date(year, month, 1)
    assignment_rows = connection.execute(
        """SELECT e.id AS employee_id, e.weekly_off, a.brand_id, a.process_id
           FROM employees e LEFT JOIN assignments a ON a.employee_id=e.id
           WHERE e.active=1 ORDER BY e.id, a.id"""
    ).fetchall()
    first_assignment = {}
    for row in assignment_rows:
        first_assignment.setdefault(row["employee_id"], row)
    schedule_rows = []
    for employee_id, assignment in first_assignment.items():
        for day_number in range(1, calendar.monthrange(year, month)[1] + 1):
            work_day = date(year, month, day_number)
            status = "Weekly Off" if work_day.strftime("%A") == assignment["weekly_off"] else "Working"
            schedule_rows.append((employee_id, work_day.isoformat(), status, assignment["brand_id"], assignment["process_id"], "Auto-generated monthly baseline", "generated"))
    connection.executemany(
        """INSERT INTO roster_entries
           (employee_id, work_date, status, brand_id, process_id, notes, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(employee_id, work_date) DO NOTHING""",
        schedule_rows,
    )
    connection.execute("INSERT OR IGNORE INTO roster_periods (period, status) VALUES (?, 'Draft')", (current_period,))


def initialize_database():
    """Create tables and seed data, then close the connection safely."""
    connection = get_connection()
    try:
        create_tables(connection)
        seed_sample_data(connection)
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    initialize_database()
    print(f"Database ready: {DATABASE_PATH}")
