#!/usr/bin/env python3
"""
Guard test for the dashboard's default view (app.py /api/leads + the markup in
templates/dashboard.html).

The dashboard opens on ~2,700 leads, so the defaults ARE the product: highest
priority_score first, with already-filed and non-Israeli leads hidden. Three
ways this rots silently, none of which look like a bug in review:

  1. A lead with israel_applicable IS NULL means "not scored yet" — mid-
     pipeline, or waiting on a re-score. Widening the filter to hide NULL as
     well makes leads vanish from the dashboard while a backfill runs.
  2. Same for already_filed_il IS NULL.
  3. The API defaults and the markup defaults are set in two different files.
     If they drift, a bare /api/leads stops matching what the dashboard shows,
     and the toggles come up out of sync with the rows underneath them.

Runs offline against a throwaway SQLite DB; no API keys needed.

Usage:  python tests/test_dashboard_defaults.py
"""
import os, re, sys, shutil, tempfile, pathlib

os.environ["PYTHONUTF8"] = "1"
os.environ.setdefault("DASHBOARD_PASSWORD", "test-only")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="scout_dash_")

# Point the app at a throwaway DB before it is imported — app.py reads these at
# import time and calls init_database().
import config.settings as settings
settings.DATABASE_PATH = pathlib.Path(TMP) / "t.db"
settings.DATABASE_URL = "sqlite:///" + (pathlib.Path(TMP) / "t.db").as_posix()

import app as webapp
from database.models import Lead

FAILURES = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


# id, title,             priority_score, already_filed_il, israel_applicable
FIXTURES = [
    ("high value IL",      9.1, False, True),
    ("mid value IL",       6.4, None,  True),   # already_filed not yet scored
    ("top but FILED",      9.8, True,  True),
    ("top but NON-IL",     9.5, False, False),
    ("unscored nexus",     7.2, False, None),   # israel_applicable not yet scored
]


def seed():
    db = webapp.get_db()
    for title, score, filed, israeli in FIXTURES:
        db.add(Lead(title=title, priority_score=score,
                    already_filed_il=filed, israel_applicable=israeli))
    db.commit()
    db.close()


def client():
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
    return c


def titles(c, qs=""):
    r = c.get("/api/leads" + qs)
    assert r.status_code == 200, "HTTP %s" % r.status_code
    body = r.get_json()
    return body["total"], [l["title"] for l in body["leads"]]


# ── tests ──────────────────────────────────────────────

def test_default_view():
    print("\n### the default view is high-value and actionable")
    c = client()
    total, got = titles(c)
    check("sorted by priority_score, highest first",
          got == ["high value IL", "unscored nexus", "mid value IL"])
    check("already-filed leads are hidden", "top but FILED" not in got)
    check("non-Israeli leads are hidden", "top but NON-IL" not in got)
    check("total reflects the filters, not the table size", total == 3)


def test_unscored_leads_stay_visible():
    """The judgment call: NULL is 'not scored yet', not 'no'."""
    print("\n### leads still awaiting a score are not hidden")
    c = client()
    _, got = titles(c)
    check("israel_applicable IS NULL stays visible", "unscored nexus" in got)
    check("already_filed_il IS NULL stays visible", "mid value IL" in got)


def test_filters_are_toggleable():
    print("\n### both filters can be switched off")
    c = client()
    total, got = titles(c, "?hide_filed=false")
    check("showing filed leads brings back the top-scored one",
          got[0] == "top but FILED" and total == 4)

    total, got = titles(c, "?hide_non_israeli=false")
    check("showing non-Israeli leads brings back the top-scored one",
          got[0] == "top but NON-IL" and total == 4)

    total, got = titles(c, "?hide_filed=false&hide_non_israeli=false")
    check("both off shows every lead", total == len(FIXTURES))
    check("still ordered by priority_score",
          got == ["top but FILED", "top but NON-IL", "high value IL",
                  "unscored nexus", "mid value IL"])


def test_other_sorts_still_work():
    print("\n### the other sort options still apply")
    c = client()
    _, by_date = titles(c, "?sort=date")
    check("sort=date is honoured, not silently ignored",
          by_date != ["high value IL", "unscored nexus", "mid value IL"])
    _, by_score = titles(c, "?sort=priority_score")
    check("sort=priority_score matches the default",
          by_score == ["high value IL", "unscored nexus", "mid value IL"])


def test_markup_matches_api_defaults():
    """
    The two files must agree, or the toggles come up out of sync with the rows.
    """
    print("\n### dashboard markup agrees with the API defaults")
    html = pathlib.Path(ROOT, "templates", "dashboard.html").read_text(encoding="utf-8")

    selected = re.findall(r'<option value="([^"]+)"[^>]*\bselected\b[^>]*>', html)
    check("priority_score is the pre-selected sort option",
          "priority_score" in selected)

    def toggle_on(el_id):
        m = re.search(r'<button class="([^"]*)" id="%s"' % el_id, html)
        return m is not None and "on" in m.group(1).split()

    check("hide-already-filed toggle renders as on", toggle_on("hideFiledToggle"))
    check("hide-non-Israeli toggle renders as on", toggle_on("hideNonIlToggle"))
    check("the new filter is sent with every query",
          "hide_non_israeli:" in html and "hideNonIlToggle" in html)

    # The API must default the same way, so a bare request matches the markup.
    src = pathlib.Path(ROOT, "app.py").read_text(encoding="utf-8")
    check('API defaults hide_filed to "true"',
          'request.args.get("hide_filed", "true")' in src)
    check('API defaults hide_non_israeli to "true"',
          'request.args.get("hide_non_israeli", "true")' in src)
    check('API defaults sort to priority_score',
          'request.args.get("sort", "priority_score")' in src)


def main():
    seed()
    test_default_view()
    test_unscored_leads_stay_visible()
    test_filters_are_toggleable()
    test_other_sorts_still_work()
    test_markup_matches_api_defaults()

    print()
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)   # open handles on Windows
    sys.exit(code)
