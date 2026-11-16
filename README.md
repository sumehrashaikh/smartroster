# Smart Roster Management System

A beginner-friendly Flask and SQLite application for planning, generating and validating monthly employee rosters across brands, processes and operational pods.

## Run locally

```powershell
python -m pip install -r requirements.txt
python database.py
python app.py
```

Open `http://127.0.0.1:5000`.

## Main screens

- `/` - company dashboard
- `/roster` - monthly roster generation, manager overrides, leaves and holidays
- `/manage` - add or remove employee-to-brand/process assignments
- `/views` - company-wide, pod, brand, process and employee views
- `/employees` - searchable employee directory
- `/rules` - configure operating calendars and minimum staffing
- `/calendar` - visual monthly employee schedule grid
- `/my-work` - role-aware employee task and update workspace
- `/export/tasks.csv?month=YYYY-MM` - export monthly work tasks

## How the solution works

Employees belong to pods and may have one or more assignments. Each assignment connects an employee to a brand and process. `brand_process_rules` stores the operating calendar and minimum staffing requirement for each brand/process combination. The scheduler starts with employee weekly offs, then applies public holidays, approved leaves and saved manager overrides.

Before a roster is treated as ready, validation checks each required brand/process/day combination. It flags coverage gaps when the number of available working employees is below the configured minimum and warns when one employee has overlapping responsibilities.

## Assessment decisions

1. Employees, brands, processes, pods and assignments are separate tables with foreign keys, so the structure can grow without changing scheduling code.
2. The generator is deterministic and explainable. Mandatory coverage rules are checked separately from manager-controlled preferences such as weekly offs.
3. A single-resource brand is represented by one assignment; multi-resource brands use several assignments, including shared processes where required.
4. If a single assigned employee is unavailable, the validator reports a coverage gap for every required day affected and the manager can add a backup assignment or override the day.
5. Unsatisfied requirements are shown as critical conflicts rather than silently publishing an invalid roster.
6. Date generation and validation are automated. Assignment changes, leave approval and final schedule overrides remain under manager control because they require operational judgment.
7. For a larger team, I would add pagination, background generation, role-based access, audit history and a stronger optimisation layer while keeping the same relational model.
8. With one additional week, I would add fair weekly-off distribution, shift windows, backup recommendations, CSV export and an approval workflow.

## Assumptions

The included fictional data demonstrates 30 employees, multiple pods, brands and processes. The initial rules intentionally include different calendars: 365-day operations, public-holiday exclusions, Sunday exclusions and minimum staffing requirements. The manager can change assignments, leave days, holidays and daily schedule overrides from the interface.

## Work operations

The roster page also acts as a daily operations board. Company cards show task completion and remaining work; selecting a company reveals its assigned people and queue. Managers can add daily, weekly or monthly tasks, assign them to employees, update task status, record employee work updates, identify idle employees, approve comp-offs, and move a roster through Draft, Submitted, Approved and Published states. Tasks for a selected month can be exported as CSV.

## Demo access

The seeded local demo users are:

- Manager: `manager` / `manager123`
- Pod lead: `podlead` / `podlead123`
- Employee: `employee` / `employee123`
- Company logins: `company_glowbeauty`, `company_urbanmart`, `company_freshbite`, `company_homenest`, `company_techzone` / `company123`

Change these credentials before deployment. Manager-only pages are protected, and employee users are limited to their personal workspace.

Company users are scoped to their own brand. They can submit work requests and optionally suggest an assigned employee. Managers can review those requests in the company work queue and assign them to one of that company's employees.
