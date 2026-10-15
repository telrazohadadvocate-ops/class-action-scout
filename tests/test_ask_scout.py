#!/usr/bin/env python3
"""
Ask Scout — questions about one lead, answered with web search.
Offline: fake Anthropic client, background job run inline.

  * POST /ask stores the question + a pending answer; the job fills it with the
    text, the cited sources and the call's cost
  * the field-check button sends the preset question
  * one question at a time per lead; empty questions refused; login required
  * the monthly budget stops new questions (429), and spend shows in the chat
  * an API failure becomes a readable error; out of credit is named
  * a pending answer left by a restart turns into an error after a few minutes
  * the prompt carries the lead, the firm's cases and the field-check order;
    earlier turns go back to Claude as history; pause_turn is continued
  * the daily summary shows the month's spend

Usage:  python tests/test_ask_scout.py
"""
import os, sys, json, pathlib, tempfile
from types import SimpleNamespace as NS
from datetime import datetime, timedelta, timezone

os.environ["PYTHONUTF8"] = "1"
os.environ.setdefault("DASHBOARD_PASSWORD", "test-only")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scout_ask_")

import config.settings as settings
settings.DATABASE_PATH = pathlib.Path(TMP) / "t.db"
settings.DATABASE_URL = "sqlite:///" + (pathlib.Path(TMP) / "t.db").as_posix()

import app as webapp
from database.models import Lead, LeadChat
from analysis import ask_scout
from alerts import health

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self
    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception): raise r
        return r

def reply(text, urls=(), stop="end_turn", tin=4000, tout=500, searches=2):
    cits = [NS(url=u, title=f"title {i}") for i, u in enumerate(urls)]
    return NS(content=[NS(type="server_tool_use"), NS(type="text", text=text, citations=cits)],
              stop_reason=stop,
              usage=NS(input_tokens=tin, output_tokens=tout, server_tool_use=NS(web_search_requests=searches)))

CLIENT = [None]
webapp._anthropic_client = lambda key: CLIENT[0]
webapp._run_in_background = lambda fn, *a: fn(*a)

db = webapp.get_db()
lead = Lead(title="Amazon Sponsored Ads overcharge", company="Amazon", relevance_score=8,
            priority_score=6.6, legal_analysis="advertisers charged above second price",
            raw_content="FTC says Amazon misled advertisers", dedup_group_id="g1")
other = Lead(title="Amazon ads auction suit — Bloomberg", company="Amazon", dedup_group_id="g1")
db.add_all([lead, other]); db.commit()
LID = lead.id

c = webapp.app.test_client()
print("1. access")
check("needs login", c.post(f"/api/leads/{LID}/ask", json={"question": "x"}).status_code == 401)
with c.session_transaction() as s: s["authenticated"] = True

print("\n2. a question and its answer")
CLIENT[0] = FakeClient([reply("**שורה תחתונה:** כן, נמכר בישראל.", urls=["https://a.example/1", "https://b.example/2"])])
r = c.post(f"/api/leads/{LID}/ask", json={"question": "נמכר בישראל?"})
check("accepted", r.status_code == 200 and [m["role"] for m in r.get_json()["messages"]] == ["user", "assistant"])
d = c.get(f"/api/leads/{LID}/chat").get_json()
a = d["messages"][-1]
check("answer filled in", a["status"] == "done" and "שורה תחתונה" in a["content"])
check("sources kept", [x["url"] for x in a["sources"]] == ["https://a.example/1", "https://b.example/2"])
expected = 4000 * 3e-6 + 500 * 15e-6 + 2 * 0.01
check("cost recorded and spend reported", abs(d["spent"] - round(expected, 2)) < 0.01 and d["budget"] == settings.ASK_MONTHLY_BUDGET_USD)
call = CLIENT[0].calls[0]
check("web search tool offered", call["tools"][0]["type"].startswith("web_search"))
check("no user_location (the API rejects country IL)", "user_location" not in call["tools"][0])
check("system prompt carries the lead, a related article and the firm's cases",
      "Amazon Sponsored Ads overcharge" in call["system"] and "Bloomberg" in call["system"] and "Buy Box" in call["system"])

print("\n3. follow-up keeps history; field check preset")
CLIENT[0] = FakeClient([reply("המשך"), ])
c.post(f"/api/leads/{LID}/ask", json={"question": "ומי היבואן?"})
msgs = CLIENT[0].calls[0]["messages"]
check("earlier turn sent as history", [m["role"] for m in msgs] == ["user", "assistant", "user"] and msgs[-1]["content"] == "ומי היבואן?")
CLIENT[0] = FakeClient([reply("בדיקה")])
c.post(f"/api/leads/{LID}/ask", json={"preset": "field_check"})
check("field check sends the preset question", CLIENT[0].calls[0]["messages"][-1]["content"] == ask_scout.FIELD_CHECK_QUESTION)

print("\n4. pause_turn is continued")
CLIENT[0] = FakeClient([reply("חלק א. ", stop="pause_turn"), reply("חלק ב.")])
c.post(f"/api/leads/{LID}/ask", json={"question": "ארוך"})
last = c.get(f"/api/leads/{LID}/chat").get_json()["messages"][-1]
check("both parts joined", last["content"] == "חלק א. חלק ב." and len(CLIENT[0].calls) == 2)

print("\n5. refusals and failures")
check("empty question refused", c.post(f"/api/leads/{LID}/ask", json={"question": "  "}).status_code == 400)
check("unknown lead 404", c.post("/api/leads/999999/ask", json={"question": "x"}).status_code == 404)
db2 = webapp.get_db(); db2.add(LeadChat(lead_id=LID, role="assistant", status="pending", content="")); db2.commit()
check("second question while one is pending → 409", c.post(f"/api/leads/{LID}/ask", json={"question": "x"}).status_code == 409)
p = db2.query(LeadChat).filter_by(status="pending").first()
p.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30); db2.commit()
d = c.get(f"/api/leads/{LID}/chat").get_json()
check("stale pending turns into an error", not any(m["status"] == "pending" for m in d["messages"]))
CLIENT[0] = FakeClient([RuntimeError("Error code: 400 - Your credit balance is too low")])
c.post(f"/api/leads/{LID}/ask", json={"question": "x"})
last = c.get(f"/api/leads/{LID}/chat").get_json()["messages"][-1]
check("API failure → readable error naming the credit", last["status"] == "error" and "קרדיט" in last["content"])
CLIENT[0] = FakeClient([RuntimeError("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tools.0: something is not supported.'}}")])
c.post(f"/api/leads/{LID}/ask", json={"question": "y"})
last = c.get(f"/api/leads/{LID}/chat").get_json()["messages"][-1]
check("other API errors show the API's own message", last["status"] == "error" and "something is not supported" in last["content"])
db3 = webapp.get_db(); db3.add(LeadChat(lead_id=LID, role="assistant", status="done", content="x", cost_usd=999)); db3.commit()
r = c.post(f"/api/leads/{LID}/ask", json={"question": "x"})
check("over the monthly budget → 429 with a message", r.status_code == 429 and "תקרה" in r.get_json()["message"])

print("\n6. prompt and markup")
txt = ask_scout.build_system_prompt(lead, ["x {y}"], settings.KNOWN_CASES)
check("prompt formats with braces in lead text", "x {y}" in txt)
check("prompt asks for facts with sources and the field-check order", "לא מצאתי" in txt and "FIELD CHECK" in txt)
html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
check("panel on the open lead", 'id="chat-${l.id}"' in html and "בדיקת שטח" in html)
check("panel loads when a lead opens", "if (expandedId === id) loadChat(id);" in html)

print("\n7. daily summary")
d = {"new": 0, "classified": 0, "passed": 0, "scored": 0, "hot": 0, "threshold": 6.5, "pending_retry": 0,
     "top": [], "near": [], "ask_spent": 3.4, "ask_budget": 20.0}
_, h = health.build_digest_email(d, [], datetime.now(timezone.utc))
check("month's spend shown", "$3.40" in h and "שאל את Scout" in h)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
