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
python -m scripts.seed                  # creates bloodbank.db with demo data
uvicorn app.main:app --reload
```

**Linux / macOS**
```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.seed
uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

### Demo credentials (local demo only)

| Username   | Password       | Role            |
|------------|----------------|-----------------|
| `admin`    | `Admin@12345`  | Administrator   |
| `dr.mehta` | `Doctor@12345` | Medical Officer |
| `staff`    | `Staff@12345`  | Staff           |
| `platform` | `Platform@12345` | Platform Administrator (facility directory only) |

Change these passwords (*user menu → Change password*) and set `BBMS_SECRET_KEY` before running
the app anywhere other than your own machine.

### Run the tests
```bash
pytest -q
```
There are 445 tests. They use a temporary database per test and never touch `bloodbank.db`.

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

**Platform Administrator** (Haldwani BloodLink). This role manages the facility directory, its
sources, contacts and verification log, under `/platform/facilities`. It has **no access** to any
operational data: donors, donations, inventory, requests, reports or the operational API all return
403. Facility administrators cannot create, see, deactivate or reset platform administrators. On an
existing database, create one with:
```bash
python -m scripts.create_platform_admin --username <name> --full-name "<Full Name>"
```
The password is prompted for and never echoed.

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

`python -m scripts.seed` fills an **empty** database by replaying about six months of activity **through
the service layer**, so the demo data obeys every rule the app enforces. It includes:
- About 77 fictitious donors covering **all eight blood groups**. Three of them demonstrate eligibility
  rules: a first-time donor over 60, a woman with a donation only 100 days ago, and a repeat donor aged 63.
- Bags in every state: **available**, **quarantine** (awaiting screening), **near-expiry** (≤ 7 days),
  **expired**, **reserved**, **issued**, and **discarded** (one reactive screening).
- Requests in every status: pending (including an emergency), approved with reserved bags, partially
  issued, fulfilled, rejected and cancelled.

Running the script without `--reset` does nothing if the database already has data.

**Resetting a demo database is deliberately hard.** `--reset` drops every table, so it is refused unless
all of the following hold:
1. `--confirm-reset <database file name>` repeats the file name exactly, for example
   `python -m scripts.seed --reset --confirm-reset bloodbank.db`.
2. The database carries the seed script's demo marker (a SQLite `PRAGMA application_id` written only by
   this script). A database created any other way, such as production, can never be reset.
3. It holds only the fictional demo facility and the documented demo accounts.
4. A **verified backup** (`scripts/backup_db.py`) succeeds first. If it fails, nothing is dropped.
   Backups go to `backups/`, or `BBMS_BACKUP_DIR` if set.

Databases seeded before this protection existed have no marker and are therefore refused. To start
over, delete the demo file instead.

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
| 4 | Haldwani BloodLink Phase 4. Adds the `availability_publications` and `availability_entries` tables (append-only except for a single withdrawal; the publisher and the publication must belong to the same facility). Adds first-login fields to `users`: `must_change_password`, `password_set_at`, `setup_token_hash` and `setup_token_expires_at`. Additive only. |
| 3 | Haldwani BloodLink Phase 3. Adds `facility_id` to `users`, `donors`, `donations`, `blood_units`, `blood_requests` and `issues`, with indexes. All existing rows go to the fictional demo facility; platform admins get none. Donor phone becomes unique **per facility**. Adds 30 triggers that make `facility_id` required and unchangeable and reject every cross-facility link. Marks the demo facility as participating. To revert, restore the verified pre-v3 backup: SQLite cannot drop a column that has a foreign key. |
| 2 | Haldwani BloodLink Phase 1. Adds the `facilities`, `facility_sources`, `facility_contacts` and `facility_verifications` tables and their indexes. Adds triggers so that evidence must belong to the same facility, and so the verification log is append-only. Inserts one row: a **fictional**, never-listed "Demo Blood Centre (fictional)". No real facility data is inserted. |
| 1 | Adds `blood_requests.rejected_by_id` and `rejected_at`. Rejecting a request no longer overwrites `approved_by`. On legacy *rejected* rows, the officer stored in `approved_by` (who was actually the one who rejected the request) is moved to `rejected_by`. |

Back up `bloodbank.db` before upgrading a database that holds real data:
`python -m scripts.backup_db --label pre-upgrade`. This makes a consistent online copy in `backups/` and keeps it only if its integrity check passes and every table's row count and the schema version match the source.

## 9. Haldwani BloodLink: facility directory (Phase 1)
The system is being extended into a regional platform for Haldwani. Phase 1 adds a facility
directory with provenance. **No real facility is pre-loaded.** Every fact about a real blood bank must
be entered by a platform administrator together with the source it came from.

- **Sources.** Each source is typed: facility confirmation, government portal, government document,
  hospital website, OpenStreetMap, site visit or third-party directory. Web sources must include the
  URL that was consulted.
- **Contacts and coordinates must cite a source.** The database itself rejects coordinates without
  one. Coordinates outside India, which usually means latitude and longitude were swapped, are
  rejected.
- **Verification log.** It is append-only and kept per scope: identity, location, services and each
  contact. Trust comes only from this log.

  | Status | Requires |
  |---|---|
  | Source confirmed | An official source (government portal or document, or the facility's own website) |
  | Facility confirmed | Direct contact (call, email, letter or visit), recorded as a facility-confirmation source |
  | Field verified | Location only, after a site visit |

  Third-party directories and OpenStreetMap are leads and can never confirm anything. A positive
  check older than `BBMS_VERIFICATION_MAX_AGE_DAYS` (default 180) is shown as *outdated*.
- **Listing rules** (public display arrives in Phase 2). A facility can be listed only when:
  - its identity is confirmed and neither outdated nor disputed;
  - it has an address and a city;
  - it has a sourced public phone number;
  - it is not marked closed.

  An edit, verification or retired contact that breaks a rule unlists the facility automatically.
  **Map markers** additionally need sourced coordinates and a confirmed location.
- **Demo facility.** Existing operational data will be assigned (in Phase 3) to the fictional, never-listable
  "Demo Blood Centre (fictional)". The database enforces that it can never be listed.
- **Not yet built:** public pages and map (Phase 2), per-facility data isolation (Phase 3) and
  published stock (Phase 4). Facility stock visibility stays `hidden` until then.

### Phase 2: public directory and map
Public pages need no login and set **no cookies**:

| Page | Content |
|---|---|
| `/` (when signed out) | Landing page. Signed-in staff still get the dashboard, and platform admins the directory. |
| `/find` | Directory, map and availability filter |
| `/find/<slug>` | Facility page |
| `/find/about-data` | How the data is verified |
| `/api/public/facilities` | GeoJSON |

All visibility rules live in `app/services/public.py`:
- **Only listed facilities that meet every listing rule today are shown.** A confirmation that becomes outdated
  or disputed hides the facility automatically. The fictional demo facility is never shown.
- **Map pins appear only for sourced, verified locations.** Other listed facilities appear in the list, with
  their address and no pin. Unverified coordinates never reach the browser.
- **Contacts:** only active, public contacts are shown, each labelled *Confirmed* or *Not yet confirmed*.
  Direct communications (calls, emails, site visits) are summarised and never quoted.
- **Availability is never shown or estimated.** The blood group and component filter explains that no facility
  publishes stock yet, and points to e-RaktKosh. Publishing arrives in Phase 4.
- **Directions** are hand-off links to Google Maps and OpenStreetMap. This app does no routing.
- **Near me** sorts by straight-line distance using browser geolocation, entirely in the browser. No request is
  sent to the server.
- **Map tiles:** Leaflet 1.9.4 is pinned with SRI hashes, and the tiles are standard OpenStreetMap (attribution
  shown). Change `BBMS_MAP_TILE_URL` and `BBMS_MAP_ATTRIBUTION` for another provider. When no location is
  verified, the map opens on Haldwani's city centre (from OpenStreetMap Nominatim, way 112883732) for
  orientation only.

### Phase 3: multi-facility data isolation
Each blood bank works in its own **facility**. Every donor, donation, bag, request, issue and staff account
belongs to exactly one facility.
- **Who sees what.** Facility admins, medical officers and staff see and change only their own facility's
  records. Platform admins have **no** operational access.
- **No probing by ID.** A record of another facility returns the same 404 page as a record that does not
  exist, so IDs reveal nothing.
- **Enforced in layers.** Ownership is checked through `app/services/scope.py` (`facility_id_of`,
  `ensure_owned`, `scoped`), by `get_owned_or_404` in every route, and by database triggers. The
  triggers reject cross-facility links (for example reserving another facility's bag) and any change to
  `facility_id`.
- **Forms cannot choose a facility.** A record's facility comes from the signed-in account, never from
  the submitted form.
- **No cross-facility lookups.** Reservations use only the facility's own stock. Donor clearance must
  come from the facility's own medical officer. Dashboards, reports, charts and the API are all
  per-facility.
- **Onboarding.** A platform admin creates a facility's **administrator**
  (`/platform/facilities/<id>` → Onboarding), who then adds their own staff.
- **Uniqueness.** Usernames and bag IDs stay unique across all facilities. Donor phone is unique within a
  facility.
- **Platform-wide expiry sweep.** The automatic expiry sweep runs platform-wide. It is system maintenance
  that only marks overdue bags expired and returns no data.

### Phase 4: published blood availability
- **Source of truth: the facility's own inventory.**
  - Only **eligible** bags count: available, unexpired whole blood. The expiry date is checked directly,
    not only through the sweep.
  - Quarantined (awaiting screening), reserved, issued, expired and discarded bags are never counted.
- **Explicit publication.** A facility **administrator or medical officer** publishes an aggregate snapshot:
  bags per blood group, with no donor, patient, request or bag details. Who published and when are recorded.
  - Staff can view the page but not publish.
  - Platform admins cannot see or publish anything.
  - Nothing becomes public automatically.
- **Visibility.** Set by the facility administrator (`/availability`):

  | Setting | The public sees |
  |---|---|
  | `hidden` | Nothing (the default) |
  | `banded` | Available / Low (1–`BBMS_AVAILABILITY_LOW_MAX`, default 2) / Not available |
  | `exact` | The number of bags |

- **Never overstated.** The public figure is `min(published, eligible now)`, so stock issued after
  publication lowers it, and it is never shown higher than published.
- **Freshness.** A publication is valid for `min(facility validity, BBMS_AVAILABILITY_MAX_HOURS)`.
  - The default ceiling is **24 h**, matching the daily stock-update duty of licensed blood centres on e-RaktKosh.
  - Facilities may choose 1–24 h.
  - Once expired, the public sees "Not recently updated". After a withdrawal, it sees "Not published".
- **Where it is shown.** Only for listed, participating, open facilities with visibility switched on.
  The demo facility never shows availability.
- **Caching.** Public pages and the public API send `Cache-Control: no-cache`, so a browser never shows
  withdrawn or expired figures from its cache.
- **Products.** Inventory tracks whole blood only. Other components (`Product` enum) are shown as "not tracked".
- **Public API.** `GET /api/public/availability?group=&component=&facility=` returns only the facility slug
  and name, blood group, component, status, the units (`exact` only) and the timestamps.

**Account hardening (Phase 4).**
- **One-time setup link for onboarded facility administrators.**
  - Onboarding sets **no password**; it issues a single-use link (default 72 h, `BBMS_SETUP_LINK_VALID_HOURS`).
    Only a hash of the link's token is stored, and the platform admin never knows the password.
  - A new link can be issued while setup is pending; it invalidates the old one.
- **Forced password change.** Accounts created or reset by an administrator must choose their own password
  at first sign-in, and every other page redirects until they do.
- **Facility closure.** Marking a facility *closed* deactivates all its accounts and keeps every record.
  Reopening does not silently reactivate them: onboard a new administrator instead.
- **Setup tokens never reach the logs.** `app/log_redaction.py` protects every logging **handler**: the
  root handlers, every named logger's handlers and Python's last-resort handler. Records from child
  loggers such as `uvicorn.asgi` (which logs whole ASGI scopes at `--log-level trace`) are therefore covered.
  Each handler gets:
  - **Argument redaction:** strings inside arguments are redacted recursively, including dicts, lists and
    bytes, without changing their structure.
  - **A formatter wrapper:** redacts the **fully formatted** output, including tracebacks, and never raises.
    A record that can't be formatted is replaced by a placeholder, with its arguments withheld.
  - **A safe error handler:** a failing log write never prints the record's arguments.

  Any spelling of the path is caught: upper or lower case, repeated or back slashes, and `%2F`-encoded
  separators.

  Handlers are protected when the app is created and again at start-up. A logging handler added later
  by other code would need `log_redaction.protect_handler(handler)`.

  Every response that carries a setup token sends `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer`. That covers the setup page (GET and POST) and the platform page that
  displays a new link.
- **Reverse proxy.** A proxy keeps its own access log, so apply the same redaction there. For example, for
  nginx (this configuration is not exercised by the test suite):
  ```nginx
  map $request_uri $bloodlink_log_uri {
      "~^(?<p>/account/setup/)[^/?#]+(?<rest>.*)$"  "${p}[REDACTED]${rest}";
      default                                         $request_uri;
  }
  log_format bloodlink '$remote_addr - [$time_local] "$request_method $bloodlink_log_uri $server_protocol" '
                       '$status $body_bytes_sent "$http_user_agent"';
  access_log /var/log/nginx/bloodlink.access.log bloodlink;
  ```
  The browser history still records the link. That is acceptable because the link works once and expires.

## 10. Time zones
| Data | Stored as | Shown as |
|---|---|---|
| Timestamps: issue time, request created/approved/rejected, screening time | naive **UTC** | labelled "UTC" |
| Calendar dates: donation, collection, expiry, required-by, "today" | server **local date** | plain date |

The Issue History filter and the Issues and Requests reports compare their From/To dates with the
UTC timestamps, so the inputs are labelled "UTC date". **IST = UTC + 5:30**, so a bag issued between
00:00 and 05:29 IST appears under the previous UTC date. The Donation report filters on the local
donation date.

## 11. Known limitations and next steps
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

### Deployment limitations (open before any real deployment)
These were found in the Phase 4 security review and are **not yet resolved**:
- **CDN assets and browser security headers.**
  - Bootstrap, Bootstrap Icons and Chart.js load from jsdelivr **without integrity (SRI) hashes**, on staff
    and public pages alike. Only Leaflet is pinned with SRI.
  - There is no `Content-Security-Policy` or `Strict-Transport-Security` header. The app sets only
    `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`.
- **No rate limiting** on sign-in, the setup link, the public pages or the public API.
- **Backups are manual,** and there is **no restore script**. To restore, stop the app and replace
  `bloodbank.db` with a verified file from `backups/` (keep the current file first). Nothing schedules
  backups.
- **Reverse proxy.**
  - The nginx access-log redaction above has not been run in this project.
  - nginx's **error log** records request lines, for example on upstream timeouts, and cannot be redacted
    with `log_format`. Keep it at a level that doesn't log requests, or restrict access to it.
- **gunicorn.** Running under gunicorn has not been tested. Its log handlers should be covered, since every
  handler present at start-up is protected, but this is unverified.
- **HTTPS is required in production.** Cookies are `Secure`, so TLS must terminate in front of the app.
  Behind a proxy, run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy address>` so client
  addresses and the HTTPS scheme are correct.
- **Clock and time zone.** Expiry dates and publication validity rely on the server's local date and on
  naive UTC timestamps, so the server clock and time zone must be correct.
- **SQLite** handles one writer at a time. Several app workers queue their writes, which is fine for a few
  facilities but is not tested under load.
- **Third-party services.** The pages depend on the jsdelivr and cdnjs CDNs and on OpenStreetMap's tile
  servers, which are for light use. Switch provider (`BBMS_MAP_TILE_URL`) if traffic grows.
