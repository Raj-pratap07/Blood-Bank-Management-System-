"""Populate the database with realistic demo data.

Usage:
    python -m scripts.seed           # only if the database is empty
    python -m scripts.seed --reset   # DROP all tables and re-seed

All records are created through the service layer (same validation, eligibility,
screening, FEFO and transaction rules as the web app), replayed along a
chronological timeline over the last ~6 months so the data is internally
consistent. Names and phone numbers are fictitious.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine, init_db
from app.models import BloodGroup as G
from app.models import BloodRequest, Donation, Donor, Gender, Issue, RequestPriority, Role, User
from app.schemas.forms import BloodRequestIn, DonationIn, DonorIn, IssueIn, ScreeningIn, UserIn
from app.services import donations as donation_svc
from app.services import donors as donor_svc
from app.services import requests as req_svc
from app.services import users as user_svc
from app.services.errors import ServiceError
from app.services.inventory import expire_overdue_units

DEMO_USERS = [
    ("admin", "System Administrator", Role.ADMIN, "Admin@12345"),
    ("dr.mehta", "Dr. Kavita Mehta", Role.MEDICAL_OFFICER, "Doctor@12345"),
    ("staff", "Rohit Negi", Role.STAFF, "Staff@12345"),
]

FIRST_M = ["Aarav", "Vivek", "Rahul", "Karan", "Amit", "Sandeep", "Manish", "Deepak", "Ankit", "Harish",
           "Suresh", "Nitin", "Pankaj", "Gaurav", "Rakesh", "Yash", "Mohit", "Arjun", "Vikas", "Tarun"]
FIRST_F = ["Priya", "Neha", "Anjali", "Pooja", "Sneha", "Kavya", "Ritu", "Swati", "Meena", "Divya",
           "Shalini", "Asha", "Nisha", "Kiran"]
LAST = ["Sharma", "Rawat", "Negi", "Bisht", "Joshi", "Verma", "Gupta", "Chauhan", "Thapa", "Pant",
        "Bhatt", "Singh", "Kumar", "Dobhal", "Semwal", "Uniyal", "Agarwal", "Kapoor"]
CITIES = ["Dehradun", "Dehradun", "Dehradun", "Haridwar", "Rishikesh", "Roorkee", "Mussoorie", "Vikasnagar"]
HOSPITALS = [("Doon Medical College Hospital", "Patel Nagar, Dehradun"),
             ("Shri Mahant Indiresh Hospital", "Patel Nagar, Dehradun"),
             ("Max Super Speciality Hospital", "Malsi, Dehradun"),
             ("AIIMS Rishikesh", "Virbhadra Road, Rishikesh"),
             ("Coronation District Hospital", "Dehradun")]
DOCTORS = ["Dr. S. Rawat", "Dr. A. Kapoor", "Dr. N. Joshi", "Dr. P. Bhatt", "Dr. R. Thapa"]
INDICATIONS = ["Elective hip replacement", "Road traffic accident — trauma", "Severe anaemia",
               "Post-partum haemorrhage", "Upper GI bleed", "Cardiac surgery (CABG)", "Thalassaemia transfusion",
               "Caesarean section — standby"]

# Realistic Indian ABO/Rh mix (approximate) but every group represented.
GROUP_WEIGHTS = {G.O_POS: 30, G.B_POS: 30, G.A_POS: 21, G.AB_POS: 8, G.O_NEG: 3, G.B_NEG: 3, G.A_NEG: 3, G.AB_NEG: 2}


def at(d: date, hour: int = 10) -> datetime:
    return datetime.combine(d, time(hour, random.randint(0, 59)))


class Seeder:
    def __init__(self, db, today: date):
        self.db = db
        self.today = today
        self.rnd = random.Random(42)
        self.phone = 9837000100
        self.users: dict[str, User] = {}
        self.donors: list[Donor] = []

    # ------------------------------------------------------------------ helpers
    def next_phone(self) -> str:
        self.phone += self.rnd.randint(3, 97)
        return str(self.phone)

    def make_donor(self, group: G, gender: Gender | None = None, age: int | None = None, **extra) -> Donor:
        r = self.rnd
        gender = gender or (Gender.MALE if r.random() < 0.65 else Gender.FEMALE)
        first = r.choice(FIRST_M if gender == Gender.MALE else FIRST_F)
        last = r.choice(LAST)
        age = age or r.randint(19, 55)
        dob = self.today - timedelta(days=age * 365 + r.randint(10, 300))
        weight = round(r.uniform(58, 88) if gender == Gender.MALE else r.uniform(48, 72), 1)
        city = r.choice(CITIES)
        donor = donor_svc.create_donor(self.db, DonorIn(
            full_name=f"{first} {last}", gender=gender, date_of_birth=dob, blood_group=group,
            phone=self.next_phone(), email=f"{first.lower()}.{last.lower()}{r.randint(1, 99)}@example.com",
            address=f"{r.randint(1, 250)}, {r.choice(['Rajpur Road', 'Clement Town', 'Prem Nagar', 'Ballupur', 'Raipur', 'Bell Road'])}",
            city=city, weight_kg=weight, **extra))
        donor.created_at = at(self.today - timedelta(days=200 + r.randint(0, 200)))
        self.donors.append(donor)
        return donor

    def vitals(self, donor: Donor) -> dict:
        r = self.rnd
        return dict(
            volume_ml=450 if donor.weight_kg > 60 and r.random() < 0.5 else 350,
            weight_kg=donor.weight_kg, hemoglobin_g_dl=round(r.uniform(12.8, 16.2), 1),
            bp_systolic=r.randint(108, 136), bp_diastolic=r.randint(68, 86), pulse_bpm=r.randint(64, 92),
            temperature_c=round(r.uniform(36.4, 37.1), 1), questionnaire_ok=True,
        )

    def donate(self, donor: Donor, days_ago: int, outcome: str = "accept") -> Donation | None:
        d = self.today - timedelta(days=days_ago)
        mo = self.users["dr.mehta"]
        try:
            donation = donation_svc.record_donation(self.db, donor, DonationIn(
                donor_id=donor.id, donation_date=d, cleared_by_id=mo.id, **self.vitals(donor)),
                self.rnd.choice([self.users["staff"], mo]))
        except ServiceError:
            return None  # e.g. interval not met — skip, exactly as the app would
        donation.created_at = at(d, 9)
        if outcome in ("accept", "reject"):
            screen_day = min(d + timedelta(days=1), self.today)
            neg = "non_reactive"
            data = ScreeningIn(
                hiv_result=neg, hbv_result="reactive" if outcome == "reject" else neg, hcv_result=neg,
                syphilis_result=neg, malaria_result=neg, abo_rh_confirmed=True, decision=outcome,
                screening_notes="HBsAg reactive on repeat testing; donor referred for counselling."
                if outcome == "reject" else "All TTI markers non-reactive.")
            donation_svc.screen_donation(self.db, donation, data, mo, on=screen_day)
            donation.screened_at = at(screen_day, 15)
        self.db.commit()
        return donation

    def request(self, days_ago: int, group: G, units: int, priority=RequestPriority.ROUTINE) -> BloodRequest:
        r = self.rnd
        hosp, addr = r.choice(HOSPITALS)
        gender = r.choice([Gender.MALE, Gender.FEMALE])
        name = f"{r.choice(FIRST_M if gender == Gender.MALE else FIRST_F)} {r.choice(LAST)}"
        req = req_svc.create_request(self.db, BloodRequestIn(
            patient_name=name, patient_age=r.randint(4, 78), patient_gender=gender, blood_group=group,
            units_required=units, priority=priority, hospital_name=hosp, hospital_address=addr,
            doctor_name=r.choice(DOCTORS), requester_name=f"{r.choice(FIRST_M)} {r.choice(LAST)}",
            requester_phone=self.next_phone(), reason=r.choice(INDICATIONS)), self.users["staff"])
        req.created_at = at(self.today - timedelta(days=days_ago), 8)
        self.db.commit()
        return req

    def approve(self, req: BloodRequest, days_ago: int) -> int:
        on = self.today - timedelta(days=days_ago)
        try:
            n = req_svc.approve_and_reserve(self.db, req, self.users["dr.mehta"],
                                            note="Approved; issue after crossmatch.", on=on)
        except ServiceError:
            return 0
        req.approved_at = at(on, 11)
        self.db.commit()
        return n

    def issue(self, req: BloodRequest, days_ago: int, count: int | None = None) -> None:
        on = self.today - timedelta(days=days_ago)
        ids = [u.id for u in req.reserved_units][: count or None]
        if not ids:
            return
        issues = req_svc.issue_units(self.db, req, IssueIn(
            unit_ids=ids, received_by=f"{self.rnd.choice(FIRST_F)} (ward nurse)", crossmatch_confirmed=True,
            notes="Crossmatch compatible (IAT)."), self.users["staff"], on=on)
        for i in issues:
            i.issued_at = at(on, 13)
        self.db.commit()

    # ------------------------------------------------------------------ scenario
    def run(self) -> None:
        r = self.rnd
        admin = None
        for username, name, role, pw in DEMO_USERS:
            if admin is None:  # bootstrap the first admin directly
                from app.services.security import hash_password
                admin = User(username=username, full_name=name, role=role, password_hash=hash_password(pw))
                self.db.add(admin)
                self.db.commit()
                self.users[username] = admin
            else:
                self.users[username] = user_svc.create_user(
                    self.db, UserIn(username=username, full_name=name, role=role, password=pw), admin)

        groups = list(GROUP_WEIGHTS)
        weights = list(GROUP_WEIGHTS.values())

        # Donor pool: guarantee every group, then a realistic mix.
        pool = [self.make_donor(g) for g in groups for _ in range(2)]
        pool += [self.make_donor(r.choices(groups, weights)[0]) for _ in range(44)]
        # Eligibility demonstrators
        self.make_donor(G.O_POS, Gender.MALE, age=62)                       # first-time donor > 60
        self.make_donor(G.B_POS, Gender.FEMALE, age=29,                     # female, 100 days < 120
                        last_external_donation_date=self.today - timedelta(days=100))
        self.make_donor(G.A_POS, Gender.MALE, age=63,                       # repeat donor 63: eligible
                        last_external_donation_date=self.today - timedelta(days=400))
        self.db.commit()

        r.shuffle(pool)
        it = iter(pool)
        timeline: list[tuple[int, str, object]] = []  # (days_ago, kind, payload); processed oldest first

        # Older collections (most get issued by historical requests, rest expire).
        for _ in range(20):
            timeline.append((r.randint(100, 175), "donate", (next(it), "accept")))
        # Collections that will be expired today (36-60 days old).
        for _ in range(7):
            timeline.append((r.randint(37, 60), "donate", (next(it), "accept")))
        # Recent collections: all eight groups available, some near expiry.
        recent_donors = [d for d in pool if d not in [p[0] for _, _, p in timeline]]
        by_group: dict[G, list[Donor]] = {}
        for d in pool:
            by_group.setdefault(d.blood_group, []).append(d)
        used: set[int] = {p[0].id for _, _, p in timeline}
        for g in groups:  # one near-expiry + one fresh per group where possible
            candidates = [d for d in by_group[g] if d.id not in used]
            for days_ago in (r.randint(29, 33), r.randint(4, 24)):
                if candidates:
                    donor = candidates.pop()
                    used.add(donor.id)
                    timeline.append((days_ago, "donate", (donor, "accept")))
        for donor in [d for d in recent_donors if d.id not in used][:6]:
            used.add(donor.id)
            timeline.append((r.randint(3, 27), "donate", (donor, "accept")))
        # Repeat male donors from the older cohort, now past 90 days.
        for days_ago, kind, (donor, _) in list(timeline):
            if kind == "donate" and days_ago >= 100 and donor.gender == Gender.MALE and r.random() < 0.35:
                timeline.append((r.randint(2, 12), "donate", (donor, "accept")))
        # Quarantine (awaiting screening) and one screening rejection.
        for g in (G.O_POS, G.B_POS, G.A_POS, G.O_POS, G.AB_POS, G.B_NEG):
            timeline.append((r.randint(0, 2), "donate", (self.make_donor(g), "quarantine")))
        timeline.append((9, "donate", (self.make_donor(G.B_POS), "reject")))
        for g in (G.O_POS, G.O_POS, G.O_POS, G.O_POS, G.O_POS, G.B_POS, G.A_POS):  # common groups, fresh stock
            timeline.append((r.randint(3, 15), "donate", (self.make_donor(g), "accept")))
        self.db.commit()

        # Historical requests (fulfilled / cancelled / rejected).
        for days_ago, group, units, prio in [(95, G.O_POS, 2, RequestPriority.URGENT), (88, G.B_POS, 2, RequestPriority.ROUTINE),
                                             (80, G.A_POS, 1, RequestPriority.ROUTINE), (72, G.B_POS, 3, RequestPriority.EMERGENCY),
                                             (64, G.O_POS, 2, RequestPriority.ROUTINE), (55, G.AB_POS, 1, RequestPriority.URGENT),
                                             (40, G.O_POS, 2, RequestPriority.URGENT), (25, G.B_POS, 1, RequestPriority.ROUTINE),
                                             (12, G.A_POS, 2, RequestPriority.URGENT), (6, G.O_NEG, 1, RequestPriority.EMERGENCY)]:
            timeline.append((days_ago, "fulfil", (group, units, prio)))
        timeline.append((50, "cancel", (G.A_POS, 1)))
        timeline.append((30, "reject", (G.B_NEG, 2)))

        timeline.sort(key=lambda e: -e[0])
        for days_ago, kind, payload in timeline:
            if kind == "donate":
                donor, outcome = payload
                self.donate(donor, days_ago, outcome)
            elif kind == "fulfil":
                group, units, prio = payload
                req = self.request(days_ago, group, units, prio)
                if self.approve(req, days_ago):
                    self.issue(req, days_ago)
                else:
                    req_svc.cancel_request(self.db, req, self.users["staff"], "No compatible stock at the time.")
            elif kind == "cancel":
                req = self.request(days_ago, *payload)
                self.approve(req, days_ago)
                req_svc.cancel_request(self.db, req, self.users["staff"], "Surgery postponed by the hospital.")
            elif kind == "reject":
                req = self.request(days_ago, *payload)
                req_svc.reject_request(self.db, req, self.users["dr.mehta"],
                                       "Incomplete request form: patient sample not received.")

        # Current open requests.
        expire_overdue_units(self.db, self.today)
        approved = self.request(1, G.B_POS, 2, RequestPriority.URGENT)
        self.approve(approved, 0)                                              # APPROVED, bags reserved
        partial = self.request(2, G.O_POS, 3, RequestPriority.ROUTINE)
        if self.approve(partial, 1):
            self.issue(partial, 1, count=1)                                    # PARTIALLY_ISSUED
        self.request(0, G.A_POS, 2, RequestPriority.EMERGENCY)                 # PENDING
        self.request(0, G.AB_NEG, 2, RequestPriority.URGENT)                   # PENDING (scarce group)
        self.request(1, G.O_NEG, 1, RequestPriority.ROUTINE)                   # PENDING
        expire_overdue_units(self.db, self.today)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop all tables and re-seed")
    args = parser.parse_args(argv)

    if args.reset:
        import app.models  # noqa: F401
        Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(User)):
            print("Database already contains data. Re-run with --reset to wipe and re-seed.")
            return 1
        Seeder(db, date.today()).run()
        counts = {m.__name__: db.scalar(select(func.count()).select_from(m))
                  for m in (User, Donor, Donation, BloodRequest, Issue)}
    print("Seed complete:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("Demo logins: " + ", ".join(f"{u} / {p} ({r.label})" for u, _, r, p in DEMO_USERS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
