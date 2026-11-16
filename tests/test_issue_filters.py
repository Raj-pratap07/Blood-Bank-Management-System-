"""Regression tests: Issue History date filter returned 422 when a date input was left blank.

Root cause: browsers submit an empty <input type="date"> as `start=` (empty string).
FastAPI parameters typed `date | None` only default to None when the parameter is
*absent*; "" was validated as a date and rejected with 422.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import BloodGroup as G
from app.models import Gender, RequestPriority
from app.schemas.forms import BloodRequestIn, IssueIn
from app.services import requests as req_svc
from app.web import parse_date_range
from tests.conftest import login

# Three issues on fixed dates (issued_at overridden after creation).
ISSUE_TIMES = [datetime(2026, 9, 10, 9, 0), datetime(2026, 10, 15, 12, 0), datetime(2026, 11, 5, 23, 59)]


@pytest.fixture()
def issued(db, users, make_unit):
    """Return {date: unit_code} for three bags issued on known dates."""
    codes = {}
    for when in ISSUE_TIMES:
        unit = make_unit(G.A_POS)
        req = req_svc.create_request(db, BloodRequestIn(
            patient_name="Filter Patient", patient_age=50, patient_gender=Gender.MALE, blood_group=G.A_POS,
            units_required=1, priority=RequestPriority.ROUTINE, hospital_name="Test Hospital",
            requester_name="Dr Test", requester_phone="9876500001"), users["staff"])
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        issue = req_svc.issue_units(db, req, IssueIn(unit_ids=[unit.id], received_by="Nurse T",
                                                     crossmatch_confirmed=True), users["staff"])[0]
        issue.issued_at = when
        db.commit()
        codes[when.date()] = unit.unit_code
    return codes


def shown(html: str, codes: dict) -> set:
    return {d for d, code in codes.items() if code in html}


ALL = {t.date() for t in ISSUE_TIMES}
SEP, OCT, NOV = (t.date() for t in ISSUE_TIMES)


class TestIssueHistoryFilter:
    @pytest.mark.parametrize("query,expected", [
        ("", ALL),                                          # initial load, no params
        ("?start=&end=", ALL),                              # Filter clicked with both blank  (the bug)
        ("?start=2026-10-01&end=", {OCT, NOV}),             # From only
        ("?start=&end=2026-10-31", {SEP, OCT}),             # To only
        ("?start=2026-10-01&end=2026-10-31", {OCT}),        # both
        ("?start=2026-09-10&end=2026-11-05", ALL),          # bounds are inclusive (incl. 23:59 on To date)
        ("?start=2026-10-15&end=2026-10-15", {OCT}),        # single day
        ("?start=2026-12-01&end=", set()),                  # valid range with no matches
    ])
    def test_filters(self, client, issued, query, expected):
        login(client, "staff")
        r = client.get("/issues" + query)
        assert r.status_code == 200, r.text[:500]
        assert shown(r.text, issued) == expected

    def test_reversed_range_shows_message_not_422(self, client, issued):
        login(client, "staff")
        r = client.get("/issues?start=2026-11-30&end=2026-10-01")
        assert r.status_code == 400
        assert "cannot be later than To date" in r.text
        assert "Invalid request" not in r.text            # not the generic 422 error page
        assert shown(r.text, issued) == ALL                # filter not applied; history still visible
        assert 'value="2026-11-30"' in r.text              # user's input preserved

    @pytest.mark.parametrize("bad", ["?start=16/11/2026", "?end=2026-13-45", "?start=abc&end=2026-11-01"])
    def test_malformed_date_shows_message(self, client, issued, bad):
        login(client, "staff")
        r = client.get("/issues" + bad)
        assert r.status_code == 400
        assert "not a valid date" in r.text and "Invalid request" not in r.text

    def test_filter_values_preserved(self, client, issued):
        login(client, "staff")
        r = client.get("/issues?start=2026-10-01&end=2026-11-30")
        assert 'name="start" value="2026-10-01"' in r.text
        assert 'name="end" value="2026-11-30"' in r.text

    def test_requires_login(self, client):
        r = client.get("/issues?start=&end=", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


class TestReportDateFilters:
    """The printable reports share the same date-input pattern and root cause."""

    @pytest.mark.parametrize("path", ["/reports/issues", "/reports/donations", "/reports/requests"])
    @pytest.mark.parametrize("query", ["?start=&end=", "?start=2026-10-01&end=", "?start=&end=2026-11-30"])
    def test_blank_dates_accepted(self, client, issued, path, query):
        login(client, "staff")
        assert client.get(path + query).status_code == 200

    def test_issue_report_range(self, client, issued):
        login(client, "staff")
        r = client.get("/reports/issues?start=2026-10-01&end=2026-10-31")
        assert shown(r.text, issued) == {OCT}

    @pytest.mark.parametrize("path", ["/reports/issues", "/reports/donations", "/reports/requests"])
    def test_reversed_range_message(self, client, path):
        login(client, "staff")
        r = client.get(path + "?start=2026-11-30&end=2026-10-01")
        assert r.status_code == 200 and "cannot be later than To date" in r.text


class TestParseDateRange:
    def test_blank_values(self):
        for s, e in [(None, None), ("", ""), ("  ", "")]:
            rng = parse_date_range(s, e)
            assert (rng.start, rng.end, rng.error) == (None, None, None)

    def test_valid(self):
        rng = parse_date_range("2026-01-01", "2026-01-31")
        assert (rng.start, rng.end, rng.error) == (date(2026, 1, 1), date(2026, 1, 31), None)

    def test_reversed(self):
        rng = parse_date_range("2026-02-01", "2026-01-31")
        assert rng.start is None and rng.end is None and "cannot be later" in rng.error
        assert (rng.raw_start, rng.raw_end) == ("2026-02-01", "2026-01-31")

    def test_invalid(self):
        assert "To date" in parse_date_range("", "2026-02-30").error


class TestTimezoneLabels:
    """Issue/request timestamps are UTC; date filters on them must say so."""

    def test_issue_history_labels_utc(self, client):
        login(client, "staff")
        html = client.get("/issues").text
        assert "From (UTC date)" in html and "To (UTC date)" in html and "Issued at (UTC)" in html

    @pytest.mark.parametrize("path,label", [("/reports/issues", "From (UTC date)"),
                                            ("/reports/requests", "From (UTC date)")])
    def test_utc_reports_label_utc(self, client, path, label):
        login(client, "staff")
        assert label in client.get(path).text

    def test_donation_report_is_not_labelled_utc(self, client):
        login(client, "staff")
        html = client.get("/reports/donations").text
        assert "UTC date" not in html and "donation (collection) date" in html
