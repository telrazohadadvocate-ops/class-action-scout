#!/usr/bin/env python3
"""
Relevance feedback from the dashboard — "is this a lead for us?".

  * PUT /api/leads/<id>/label stores yes / maybe / no, clears on "", rejects
    anything else, and keeps the note (trimmed, capped at 500 chars)
  * it needs a login, and does not touch status (workflow stays separate)
  * the label comes back in /api/leads so the card can show it
  * the dashboard markup has the buttons and calls the route
  * run_eval --with-db-labels picks the labels up; the gold file wins on overlap;
    a database from before the column existed still works

Offline, throwaway SQLite.  Usage:  python tests/test_lead_labels.py
"""
import os, sys, json, pathlib, sqlite3, tempfile

os.environ["PYTHONUTF8"] = "1"
os.environ.setdefault("DASHBOARD_PASSWORD", "test-only")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
TMP = tempfile.mkdtemp(prefix="scout_lbl_")

import config.settings as settings
settings.DATABASE_PATH = pathlib.Path(TMP) / "t.db"
settings.DATABASE_URL = "sqlite:///" + (pathlib.Path(TMP) / "t.db").as_posix()

import app as webapp
from database.models import Lead
import run_eval as RE

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

db = webapp.get_db()
ids = []
for t in ("Amazon ads overcharge", "Unrelated EPA story", "Meta teens"):
    l = Lead(title=t, relevance_score=5, priority_score=6, status="new", israel_applicable=True)
    db.add(l); db.flush(); ids.append(l.id)
db.commit()
a, b, c = ids

client = webapp.app.test_client()

print("1. route")
r = client.put(f"/api/leads/{a}/label", json={"label": "yes"})
check("needs login", r.status_code == 401)
with client.session_transaction() as s:
    s["authenticated"] = True

r = client.put(f"/api/leads/{a}/label", json={"label": "yes", "note": "  advertisers, we filed one  "})
check("yes stored", r.status_code == 200 and r.get_json()["userLabel"] == "yes")
check("note trimmed", r.get_json()["userLabelNote"] == "advertisers, we filed one")
r = client.put(f"/api/leads/{b}/label", json={"label": "no"})
check("no stored", r.get_json()["userLabel"] == "no")
r = client.put(f"/api/leads/{c}/label", json={"label": "good"})
check("bad label rejected", r.status_code == 400)
r = client.put(f"/api/leads/{c}/label", json={"label": "maybe"})
r = client.put(f"/api/leads/{c}/label", json={"label": ""})
check("empty label clears", r.get_json()["userLabel"] == "")
r = client.put(f"/api/leads/{a}/label", json={"note": "x" * 900})
check("note-only update keeps the label and caps the note",
      r.get_json()["userLabel"] == "yes" and len(r.get_json()["userLabelNote"]) == 500)
check("unknown lead → 404", client.put("/api/leads/999999/label", json={"label": "yes"}).status_code == 404)

fresh = webapp.get_db()
la = fresh.query(Lead).get(a)
check("status untouched by labelling", la.status == "new")
check("labelled time recorded", la.user_label_at is not None)
check("cleared label has no time", fresh.query(Lead).get(c).user_label_at is None)

r = client.get("/api/leads?hide_non_israeli=false&per_page=50")
body = r.get_json()
rows = body.get("leads", body) if isinstance(body, dict) else body
lab = {x["id"]: x.get("userLabel") for x in rows}
check("label returned by /api/leads", lab.get(a) == "yes" and lab.get(b) == "no")

print("\n2. dashboard markup")
html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
check("buttons rendered on the open card", "ליד בשבילנו?" in html and "setLabel(" in html)
check("calls the label route", "'/api/leads/'+id+'/label'" in html)
check("card tag for a labelled lead", "tag-lbl-" in html)

print("\n3. run_eval --with-db-labels")
dbfile = str(settings.DATABASE_PATH)
extra = RE.db_labelled_items(dbfile)
check("dashboard labels picked up", sorted((i["id"], i["label"]) for i in extra) == sorted([(a, "yes"), (b, "no")]))
check("gold file wins on overlap", [i["id"] for i in RE.db_labelled_items(dbfile, exclude_ids=[a])] == [b])
old = os.path.join(TMP, "old.db")
con = sqlite3.connect(old); con.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, title TEXT)"); con.commit(); con.close()
check("old database without the column → no labels, no crash", RE.db_labelled_items(old) == [])
items = RE.attach_db_rows(extra, dbfile)
check("labelled leads become eval items", {i["key"] for i in items} == {f"#{a}", f"#{b}"})

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
