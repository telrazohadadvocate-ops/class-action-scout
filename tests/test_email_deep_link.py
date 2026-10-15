#!/usr/bin/env python3
"""
A lead in an email opens THAT lead in the dashboard, not the dashboard home.

  * the alert email's "view details" link carries ?lead=<id>
  * the daily summary links each top lead the same way
  * a logged-out click goes through /login and lands back on /?lead=<id>
  * /login never redirects to another site (open-redirect guard)
  * the dashboard reads ?lead=, fetches the lead if the list doesn't hold it,
    opens it and scrolls to it

Offline.  Usage:  python tests/test_email_deep_link.py
"""
import os, re, sys, pathlib, tempfile
from datetime import datetime, timezone

os.environ["PYTHONUTF8"] = "1"
os.environ["DASHBOARD_PASSWORD"] = "pw-test"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scout_link_")

import config.settings as settings
settings.DATABASE_PATH = pathlib.Path(TMP) / "t.db"
settings.DATABASE_URL = "sqlite:///" + (pathlib.Path(TMP) / "t.db").as_posix()
settings.DASHBOARD_PASSWORD = "pw-test"

import app as webapp
webapp.DASHBOARD_PASSWORD = "pw-test"
from alerts import email_sender, health

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

print("1. emails")
row = email_sender._build_lead_row({"id": 4871, "title": "flydubai", "company": "flydubai"})
check("alert link points at the lead", f"{email_sender.DASHBOARD_URL}/?lead=4871" in row)
row0 = email_sender._build_lead_row({"title": "no id"})
check("no id → dashboard home, no broken ?lead=", "?lead=" not in row0)
d = {"new": 1, "classified": 1, "passed": 1, "scored": 1, "hot": 1, "threshold": 7.0, "pending_retry": 0,
     "top": [{"id": 4871, "title": "flydubai", "company": "flydubai", "score": 8.8, "strength": 7.5, "israel": True}]}
_, h = health.build_digest_email(d, [], datetime(2026, 10, 15, tzinfo=timezone.utc))
check("daily summary links each top lead", f"{health.DASHBOARD_URL}/?lead=4871" in h)

print("\n2. login keeps the destination")
c = webapp.app.test_client()
r = c.get("/?lead=4871")
check("logged-out click → login with next", r.status_code == 302 and "next=" in r.headers["Location"]
      and "lead%3D4871" in r.headers["Location"])
r = c.get(r.headers["Location"])
check("login form carries the destination", 'name="next" value="/?lead=4871"' in r.get_data(as_text=True))
r = c.post("/login", data={"password": "pw-test", "next": "/?lead=4871"})
check("after login → back to the lead", r.status_code == 302 and r.headers["Location"].endswith("/?lead=4871"))
c2 = webapp.app.test_client()
for bad in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
    r = c2.post("/login", data={"password": "pw-test", "next": bad})
    check(f"no redirect off-site for {bad!r}", r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("") and "evil" not in r.headers["Location"])
r = webapp.app.test_client().post("/login", data={"password": "wrong", "next": "/?lead=1"})
check("wrong password keeps the destination in the form", 'value="/?lead=1"' in r.get_data(as_text=True))

print("\n3. dashboard opens the lead")
html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
check("reads ?lead= on load", "openLeadFromUrl()" in html and "get('lead')" in html)
check("fetches a lead missing from the list", "fetch('/api/leads/' + id)" in html)
check("cards have an anchor id to scroll to", 'id="lead-${l.id}"' in html)
init = re.search(r"async function init\(\) \{(.*?)\n\}", html, re.S).group(1)
check("runs after the first load of leads", init.index("loadLeads()") < init.index("openLeadFromUrl()"))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
