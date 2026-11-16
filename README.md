# Blood Bank Management System

A web application for a single blood centre, covering donor registration, donations with screening
quarantine, inventory tracked bag by bag with FEFO issue, blood requests that need medical approval,
issuing, and reports.

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 · SQLite · Jinja2 · Bootstrap 5 · Chart.js

> **Important — decision support only.** The donor-eligibility check and the ABO/Rh compatibility
> check in this software are *preliminary*. They do **not** replace medical examination, laboratory
> grouping, antibody screening, crossmatching or the authorisation of a qualified medical officer.
> All clinical thresholds are configurable and must be checked against the current national
> guidelines and the blood centre's SOP before any real use.

---

## 1. Quick start

Requires **Python 3.13** and an internet connection for the Bootstrap and Chart.js CDNs.

**Windows (PowerShell)**
```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m scripts.seed --reset          # creates bloodbank.db with demo data
uvicorn app.main:app --reload
```

**Linux / macOS**
```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.seed --reset
uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

### Demo credentials (local demo only)

| Username   | Password       | Role            |
|------------|----------------|-----------------|
| `admin`    | `Admin@12345`  | Administrator   |
| `dr.mehta` | `Doctor@12345` | Medical Officer |
| `staff`    | `Staff@12345`  | Staff           |

Change these passwords (*user menu → Change password*) and set `BBMS_SECRET_KEY` before running
the app anywhere other than your own machine.

### Run the tests
```bash
pytest -q
```
There are 212 tests. They use a temporary database per test and never touch `bloodbank.db`.

---

## 2. Roles and permissions

Every permission is enforced **server-side** by FastAPI dependencies and again in the service layer.
Hiding buttons in the UI is never the only protection.

| Action | Staff | Medical Officer | Admin |
|---|:-:|:-:|:-:|
| View dashboard, donors, inventory, requests, reports | ✔ | ✔ | ✔ |
| Register / edit donors | ✔ | ✔ | ✔ |
| Record donation (requires a named clearing officer) | ✔ | ✔ | ✔ |
| Clear a donor for donation (selected as "cleared by") | | ✔ | ✔ |
| Record screening and accept/reject a donation | | ✔ | ✔ |
| Approve a request and reserve bags; reject a request | | ✔ | ✔ |
| Issue reserved bags of an **approved** request | ✔ | ✔ | ✔ |
| Cancel a request | ✔ | ✔ | ✔ |
| Discard a bag; deactivate a donor | | ✔ | ✔ |
| Create, deactivate and reset staff accounts | | | ✔ |

---

## 3. Core workflows

```
Donor ──register──▶ Donation recorded ──▶ Bag in QUARANTINE
                    (preliminary eligibility        │
                     + vitals + named MO clearance) │ lab screening (MO/Admin)
                                                    ├─ accept (all non-reactive, ABO/Rh confirmed) ─▶ AVAILABLE
                                                    └─ reject ─▶ DISCARDED

Request (PENDING) ──MO/Admin approve──▶ APPROVED: compatible bags RESERVED
                                         (identical group first, then FEFO)
     ──issue (crossmatch confirmed)──▶ PARTIALLY_ISSUED / FULFILLED: bags ISSUED
     ──reject / cancel──▶ reserved bags released back to AVAILABLE

Any QUARANTINE/AVAILABLE/RESERVED bag past its expiry date ──▶ EXPIRED (automatic)
```

### Inventory rules
- **One row per physical bag.** Each bag gets a unique ID such as `WB261116-00042` (product, collection
  date, donation number), enforced by a database UNIQUE constraint.
- Each bag records its product, blood group, volume, collection date, expiry date and status
  (`quarantine`, `available`, `reserved`, `issued`, `expired`, `discarded`).
- **Whole blood is the only product in this MVP.** Expiry is the collection date plus
  `BBMS_SHELF_LIFE_DAYS`, which defaults to **35 days** (the usual CPDA-1 assumption, subject to the
  centre's SOP). A bag can be used up to and including its expiry date.
- **FEFO (First Expiry, First Out):** reservations take bags of the identical group first, then other
  compatible groups, earliest expiry first within each tier. Taking identical groups first conserves
  O-negative and other scarce groups.
- **Blocked:** expired, discarded, quarantined, already-reserved and issued bags can never be reserved
  or issued. An expiry sweep runs before every reserve or issue and whenever inventory pages load.
- **Transactions:** reserve, issue, release, screening and discard each run in one database
  transaction. Every bag update is a conditional `UPDATE … WHERE status = <expected>`, and the
  operation aborts unless exactly one row changed, so two users can never reserve or issue the same
  bag. Any failure part-way through rolls the whole operation back (see
  `test_rollback_when_issue_fails_midway`). `issues.blood_unit_id` is UNIQUE, so the database itself
  prevents a bag from being issued twice.

### Preliminary donor eligibility

These rules live in `app/services/eligibility.py`, with thresholds in `app/config.py`.

| Rule | Default | Env override |
|---|---|---|
| Age | 18–65 years | `BBMS_MIN_AGE`, `BBMS_MAX_AGE` |
| First-time donor upper age | 60 years | `BBMS_MAX_AGE_FIRST_TIME` |
| Weight, 350 ml collection | ≥ 45 kg | `BBMS_MIN_WEIGHT_KG` |
| Weight, 450 ml collection | > 55 kg | `BBMS_MIN_WEIGHT_450ML_KG` |
| Whole-blood donation interval | 90 days male, 120 days female (also used for "other") | `BBMS_INTERVAL_MALE_DAYS`, `BBMS_INTERVAL_FEMALE_DAYS` |
| Haemoglobin | ≥ 12.5 g/dL | `BBMS_MIN_HB` |
| BP / pulse / temperature | 100–140 / 60–90 mmHg, 60–100 bpm, ≤ 37.5 °C | `config.py` |
| Health questionnaire | must be satisfactory | — |

- A donor counts as **first-time** when there is no earlier donation here and no "last donation at
  another centre" date on their record.
- A donation that fails the preliminary check is **not recorded**, and the reasons are shown on screen.
- Every donation also records **which medical officer examined and cleared the donor**. The final
  decision always stays with that person.

**Source note:** these defaults follow India's MoHFW/NBTC blood donor selection guidelines as published
on the [NBTC eligibility page](http://nbtc.naco.gov.in/page/eligibility/) and the
[MoHFW guidelines document](https://www.mohfw-dohfw.gov.in/static/uploads/2025/10/e7a407f88b7a2c963eb378739de2a9ed.pdf).
While building the app we could not retrieve the full text of the 2025 revision to confirm each
threshold, especially the BP, pulse and temperature ranges and the 450 ml weight cut-off. **Check every
value against the official document before relying on it.** Each one can be changed through an
environment variable or `app/config.py` without touching any code.

### Preliminary compatibility (red cells)
`app/services/compatibility.py` holds the standard ABO/Rh recipient-to-donor matrix: O− can give to
everyone, and AB+ can receive from everyone. Whole blood also contains plasma, so many centres issue
only ABO-identical whole blood. This is one reason the app reserves identical groups first, and why
every issue requires the user to confirm that the **laboratory crossmatch** is compatible. Every screen
that shows compatibility carries the preliminary-check disclaimer.

---

## 4. Security
- **Authentication:** bcrypt password hashing (cost 12). Sessions are signed (`itsdangerous`) cookies
  with HttpOnly and SameSite=Lax set and an 8-hour lifetime. With `APP_ENV=production` the cookie is
  also marked **Secure** (sent over HTTPS only). The session is cleared on login to
  prevent session fixation, and login time is the same whether or not the username exists.
- **Authorisation:** `require_admin` and `require_authorizer` dependencies, plus checks inside the
  service layer.
- **CSRF:** a global dependency checks a per-session token on **every** POST, including login and
  logout. Requests without a valid token get 403.
- **Input validation:** Pydantic models validate every form on the server. The browser's HTML5
  validation is only a convenience.
- **Other measures:**
  - Jinja2 auto-escaping is on.
  - `?next=` redirects only go to local paths, which blocks open redirects.
  - Security headers are set: `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`.
  - The OpenAPI and docs endpoints are disabled.
  - An admin cannot deactivate their own account or the last active admin.

---

## 5. Project structure

```
app/
  config.py            settings + clinical thresholds (env-overridable)
  database.py          engine, session, Base, init_db
  migrations.py        additive SQLite schema migrations (PRAGMA user_version)
  main.py              app factory, middleware, exception handlers
  web.py               templates, flash, form validation, pagination, auth/CSRF dependencies
  models/              SQLAlchemy models: User, Donor, Donation, BloodUnit, BloodRequest, Issue, enums
  schemas/forms.py     Pydantic form validation
  services/            business logic (no HTTP code)
    eligibility.py     preliminary donor eligibility
    compatibility.py   preliminary ABO/Rh matrix
    donations.py       record donation → quarantine; screening accept/reject
    inventory.py       expiry sweep, stock summary, FEFO selection, discard
    requests.py        approve+reserve, issue, reject, cancel (transactional)
    reports.py, donors.py, users.py, security.py, tx.py, errors.py
  routers/             thin HTTP layer: auth, dashboard, donors, donations, inventory,
                       requests, issues, reports, users (admin), api (JSON for charts/search)
  templates/           base layout + partials/macros.html (reusable form fields, badges,
                       pagination, disclaimers) + one folder per feature
  static/              app.css, app.js (confirmations, live donor search), charts.js
scripts/seed.py        demo data generator (uses the service layer)
tests/                 test_services.py (business rules), test_web.py (HTTP, auth, CSRF, E2E)
```

### Data model
- `Donor` 1─* `Donation` 1─* `BloodUnit`. In this MVP each donation produces exactly one whole-blood bag.
- `BloodRequest` 1─* `BloodUnit` (reserved bags, via `reserved_for_request_id`)
- `BloodRequest` 1─* `Issue` *─1 `BloodUnit`. `Issue.blood_unit_id` is unique.
- `User` is linked as the person who recorded, cleared, screened, approved and issued.

### JSON API (read-only, login required)
- `GET /api/stats/inventory`
- `GET /api/stats/donations-trend?months=6`
- `GET /api/stats/requests-by-status`
- `GET /api/donors/search?q=`

---

## 6. Demo data

`python -m scripts.seed --reset` drops all tables and replays about six months of activity **through
the service layer**, so the demo data obeys every rule the app enforces. It includes:
- About 77 fictitious donors covering **all eight blood groups**. Three of them demonstrate eligibility
  rules: a first-time donor over 60, a woman with a donation only 100 days ago, and a repeat donor aged 63.
- Bags in every state: **available**, **quarantine** (awaiting screening), **near-expiry** (≤ 7 days),
  **expired**, **reserved**, **issued**, and **discarded** (one reactive screening).
- Requests in every status: pending (including an emergency), approved with reserved bags, partially
  issued, fulfilled, rejected and cancelled.

Running the script without `--reset` does nothing if the database already has data.

## 7. Configuration

| Variable | Default |
|---|---|
| `APP_ENV` | `development`. Set to `production` on a real server to enable start-up safety checks |
| `BBMS_SECRET_KEY` | development value. **Required in production:** with `APP_ENV=production` the app refuses to start if this is unset, the development value, or shorter than 32 characters |
| `BBMS_DATABASE_URL` | `sqlite:///./bloodbank.db` |
| `BBMS_SHELF_LIFE_DAYS` | `35` |
| `BBMS_NEAR_EXPIRY_DAYS` | `7` |
| `BBMS_SESSION_MAX_AGE` | `28800` (seconds, minimum 60) |
| `BBMS_SESSION_HTTPS_ONLY` | automatic: `true` when `APP_ENV=production`, otherwise `false`. Accepts true/false/1/0/yes/no/on/off |
| eligibility thresholds | see section 3 |

**Production secret key.** The secret key signs login sessions, so anyone who knows it can forge a
session. Generate a key and supply it through the server environment. Never commit it.
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
export APP_ENV=production
export BBMS_SECRET_KEY='<generated value>'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Local development needs neither variable.

**HTTPS is required in production.** With `APP_ENV=production`, session cookies get the `Secure`
flag, so browsers send them only over HTTPS. Over plain `http://`, login cannot keep a session and
forms fail with "Form expired". Put the app behind an HTTPS reverse proxy (for example nginx or Caddy
with a TLS certificate). `BBMS_SESSION_HTTPS_ONLY=false` disables the flag. That is unsafe because
cookies can then be captured on the network, and the app logs a warning at start-up.

**Start-up validation.** Every `BBMS_*` number is checked when the app starts (and when the seed
script runs). A malformed value such as `abc` or `nan`, or an out-of-range value, stops start-up with
a `ConfigurationError` that names the variable. Out-of-range examples: a shelf life outside 1–365
days, a near-expiry window not shorter than the shelf life, minimum age above maximum age, or a zero
interval. These are software sanity limits, not clinical validation of the values chosen.

## 8. Database schema changes (migrations)
`init_db()` runs on every start-up. It does two things:
1. `create_all()` creates any missing tables. A fresh database gets the latest schema.
2. `app/migrations.py` applies **additive, idempotent** changes to existing SQLite databases.
   Progress is tracked with `PRAGMA user_version`, all pending steps run in one transaction, and no
   table or row is ever dropped.

| Version | Change |
|---|---|
| 1 | Adds `blood_requests.rejected_by_id` and `rejected_at`. Rejecting a request no longer overwrites `approved_by`. On legacy *rejected* rows, the officer stored in `approved_by` (who was actually the one who rejected the request) is moved to `rejected_by`. |

Back up `bloodbank.db` before upgrading a database that holds real data. For example:
`python -c "import sqlite3; s=sqlite3.connect('bloodbank.db'); d=sqlite3.connect('backup.db'); s.backup(d)"`

## 9. Time zones
| Data | Stored as | Shown as |
|---|---|---|
| Timestamps: issue time, request created/approved/rejected, screening time | naive **UTC** | labelled "UTC" |
| Calendar dates: donation, collection, expiry, required-by, "today" | server **local date** | plain date |

The Issue History filter and the Issues and Requests reports compare their From/To dates with the
UTC timestamps, so the inputs are labelled "UTC date". **IST = UTC + 5:30**, so a bag issued between
00:00 and 05:29 IST appears under the previous UTC date. The Donation report filters on the local
donation date.

## 10. Known limitations and next steps
- Only one product (whole blood). Component separation (PRBC, FFP, platelets), each with its own
  shelf life and compatibility rules, is out of scope.
- No audit-log table yet. Who did what is stored on each record (recorded_by, cleared_by,
  screened_by, approved_by, rejected_by, issued_by), but field-level edit history is not. A request's
  `decision_note` holds only the latest note (approval, rejection or cancellation).
- Admins cannot yet edit an existing account's role or name. They can create, activate/deactivate
  and reset passwords.
- Bootstrap and Chart.js load from a CDN. For fully offline use, put copies in `app/static/vendor`.
- SQLite suits a single-site deployment. For several concurrent sites, move to PostgreSQL by changing
  `BBMS_DATABASE_URL` and adopting Alembic (the built-in migration runner is SQLite-only).
- Hand-edited URLs with non-numeric IDs or blank `page=` parameters return a generic
  "Invalid request" page. No link or form in the app produces them.
