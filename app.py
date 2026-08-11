#!/usr/bin/env python3
"""
Class Action Scout — Web Application
"""
import sys, os, json, threading, argparse
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

if sys.platform == "win32":
    os.environ["PYTHONUTF8"] = "1"

from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for, render_template
from flask_cors import CORS
from sqlalchemy import func
from config.settings import (
    DATABASE_URL, DATABASE_PATH, DASHBOARD_PASSWORD, FLASK_SECRET_KEY,
    VOYAGE_API_KEY, MIN_RELEVANCE_SCORE,
)
from database.models import init_database, get_session, Lead, ScrapeLog
from analysis.value_estimator import nis_bucket

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=7)
CORS(app)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
init_database(DATABASE_URL)

def get_db():
    return get_session(DATABASE_URL)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def _lead_to_dict(lead, duplicate_count=1, merged_sources=None, suspected_title=None):
    return {
        "id": lead.id, "title": lead.title, "company": lead.company or "",
        "sector": lead.sector or "", "source": lead.source_name,
        "sourceUrl": lead.source_url or "", "sourceType": lead.source_type or "",
        "relevanceScore": lead.relevance_score, "strengthScore": lead.strength_score,
        "priority": lead.priority or "low", "operatesInIsrael": lead.operates_in_israel,
        "israeliLawBasis": lead.israeli_law_basis or "",
        "estimatedClassSize": lead.estimated_class_size or "",
        "legalAnalysis": lead.legal_analysis or "",
        "recommendedAction": lead.recommended_action or "",
        "matchesExpertise": lead.matches_expertise or False,
        "expertiseArea": lead.expertise_area or "",
        "isDuplicate": lead.is_duplicate_of_known or False,
        "knownCaseRef": lead.known_case_ref or "",
        "pinkasExists": lead.pinkas_exists or False,
        "dedupGroupId": lead.dedup_group_id or "",
        "duplicateCount": duplicate_count,
        "status": lead.status or "new", "notes": lead.notes or "",
        "scrapedAt": lead.scraped_at.isoformat() if lead.scraped_at else "",
        "reviewedAt": lead.reviewed_at.isoformat() if lead.reviewed_at else "",
        # Value estimation (rough triage — not legal/financial advice)
        "priorityScore": lead.priority_score,
        "israelApplicable": lead.israel_applicable,
        "valueBucket": nis_bucket(lead.value_low, lead.value_high),
        "valueLow": lead.value_low,
        "valueHigh": lead.value_high,
        "estClassSize": lead.est_class_size,
        "estDamagePerMember": lead.est_damage_per_member,
        "valueConfidence": lead.value_confidence or "",
        "valueReasoning": lead.value_reasoning or "",
        # Already-filed detection + richer reasoning
        "alreadyFiledIl": lead.already_filed_il,
        "alreadyFiledDetails": lead.already_filed_details or "",
        "relevanceReasoning": lead.relevance_reasoning or "",
        "scoreReasoning": _parse_score_reasoning(lead.score_reasoning),
        "mergedSources": merged_sources or [],
        # Suspected-duplicate review
        "suspectedDupOf": lead.suspected_dup_of,
        "suspectedDupScore": lead.suspected_dup_score,
        "dupReview": lead.dup_review or "",
        "suspectedMatchTitle": suspected_title or "",
    }


def _parse_score_reasoning(raw):
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) and any(val.values()) else None
    except Exception:
        return None

# ── Auth routes ────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if DASHBOARD_PASSWORD and pwd == DASHBOARD_PASSWORD:
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "סיסמה שגויה"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Page routes ────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    return send_from_directory("templates", "dashboard.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# ── API routes ─────────────────────────────────────────

@app.route("/api/leads")
@login_required
def get_leads():
    db = get_db()
    q = db.query(Lead)
    if request.args.get("hide_duplicates", "true").lower() == "true":
        q = q.filter((Lead.is_duplicate_of_known == False) | Lead.is_duplicate_of_known.is_(None))
    p = request.args.get("priority")
    if p and p != "all": q = q.filter(Lead.priority == p)
    s = request.args.get("status")
    if s and s != "all": q = q.filter(Lead.status == s)
    # Time-period filter on scraped_at (stored naive UTC)
    period = request.args.get("period", "all")
    if period in ("today", "week", "month"):
        now = datetime.now(timezone.utc)
        if period == "today":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            since = now - timedelta(days=7)
        else:
            since = now - timedelta(days=30)
        q = q.filter(Lead.scraped_at >= since.replace(tzinfo=None))
    # Hide leads where an Israeli class action was already filed
    if request.args.get("hide_filed", "false").lower() == "true":
        q = q.filter((Lead.already_filed_il == False) | Lead.already_filed_il.is_(None))
    # Show only leads flagged as suspected duplicates awaiting review
    if request.args.get("review", "").lower() == "pending":
        q = q.filter(Lead.dup_review == "pending")
    search = request.args.get("search", "")
    if search:
        like = f"%{search}%"
        q = q.filter((Lead.title.ilike(like))|(Lead.company.ilike(like))|(Lead.legal_analysis.ilike(like)))
    sort = request.args.get("sort", "strength")
    if sort == "strength": q = q.order_by(Lead.strength_score.desc().nullslast())
    elif sort == "priority_score": q = q.order_by(Lead.priority_score.desc().nullslast())
    elif sort == "relevance": q = q.order_by(Lead.relevance_score.desc().nullslast())
    elif sort == "date": q = q.order_by(Lead.scraped_at.desc())
    total = q.count()
    leads = q.offset(request.args.get("offset",0,type=int)).limit(request.args.get("limit",100,type=int)).all()
    group_counts = dict(
        db.query(Lead.dedup_group_id, func.count(Lead.id))
        .filter(Lead.dedup_group_id.isnot(None))
        .group_by(Lead.dedup_group_id)
        .all()
    ) if leads else {}
    # For canonical leads whose cluster has >1 member, list every merged source
    merged_map = {}
    multi_gids = [
        l.dedup_group_id for l in leads
        if l.dedup_group_id and group_counts.get(l.dedup_group_id, 1) > 1
    ]
    if multi_gids:
        for gid, title, sname, surl in (
            db.query(Lead.dedup_group_id, Lead.title, Lead.source_name, Lead.source_url)
            .filter(Lead.dedup_group_id.in_(set(multi_gids)))
            .all()
        ):
            merged_map.setdefault(gid, []).append(
                {"title": title, "source": sname or "", "url": surl or ""}
            )
    # Titles for suspected-duplicate matches shown on flagged leads
    susp_ids = [l.suspected_dup_of for l in leads if l.suspected_dup_of and (l.dup_review or "") == "pending"]
    susp_titles = {}
    if susp_ids:
        for sid, stitle in db.query(Lead.id, Lead.title).filter(Lead.id.in_(set(susp_ids))).all():
            susp_titles[sid] = stitle
    return jsonify({"total": total, "leads": [
        _lead_to_dict(
            l, group_counts.get(l.dedup_group_id, 1), merged_map.get(l.dedup_group_id),
            susp_titles.get(l.suspected_dup_of),
        )
        for l in leads
    ]})

@app.route("/api/leads/<int:lid>")
@login_required
def get_lead(lid):
    db = get_db()
    lead = db.query(Lead).get(lid)
    if not lead:
        return jsonify({"error": "not found"}), 404
    cnt = db.query(func.count(Lead.id)).filter(Lead.dedup_group_id == lead.dedup_group_id).scalar() if lead.dedup_group_id else 1
    merged = None
    if lead.dedup_group_id and cnt > 1:
        merged = [
            {"title": t, "source": sn or "", "url": su or ""}
            for t, sn, su in db.query(Lead.title, Lead.source_name, Lead.source_url)
            .filter(Lead.dedup_group_id == lead.dedup_group_id).all()
        ]
    return jsonify(_lead_to_dict(lead, cnt, merged))

@app.route("/api/leads/<int:lid>/status", methods=["PUT"])
@login_required
def update_status(lid):
    db = get_db()
    lead = db.query(Lead).get(lid)
    if not lead: return jsonify({"error":"not found"}),404
    data = request.json or {}
    if "status" in data: lead.status = data["status"]; lead.reviewed_at = datetime.now(timezone.utc)
    if "notes" in data: lead.notes = data["notes"]
    db.commit()
    cnt = db.query(func.count(Lead.id)).filter(Lead.dedup_group_id == lead.dedup_group_id).scalar() if lead.dedup_group_id else 1
    return jsonify(_lead_to_dict(lead, cnt))

@app.route("/api/leads/<int:lid>/resolve-duplicate", methods=["POST"])
@login_required
def resolve_duplicate(lid):
    db = get_db()
    lead = db.query(Lead).get(lid)
    if not lead:
        return jsonify({"error": "not found"}), 404
    action = (request.json or {}).get("action")
    if action == "merge":
        match = db.query(Lead).get(lead.suspected_dup_of) if lead.suspected_dup_of else None
        if not match:
            return jsonify({"error": "no suspected match to merge into"}), 400
        lead.is_duplicate_of_known = True
        lead.dedup_group_id = match.dedup_group_id or str(match.id)
        lead.known_case_ref = match.title
        lead.dup_review = "merged"
    elif action == "separate":
        # Keep it as its own lead; record the decision so it won't re-flag
        lead.dup_review = "separate"
    else:
        return jsonify({"error": "action must be 'merge' or 'separate'"}), 400
    db.commit()
    return jsonify({"status": "ok", "dupReview": lead.dup_review})


@app.route("/api/stats")
@login_required
def get_stats():
    db = get_db()
    last = db.query(ScrapeLog).order_by(ScrapeLog.completed_at.desc()).first()
    # Dedup health — embedding coverage among analysis-eligible canonical leads
    elig = db.query(Lead).filter(
        Lead.relevance_score >= MIN_RELEVANCE_SCORE,
        (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
    )
    elig_total = elig.count()
    elig_embedded = elig.filter(Lead.embedding.isnot(None)).count()
    coverage = (elig_embedded / elig_total) if elig_total else 1.0
    dedup_enabled = bool(VOYAGE_API_KEY)
    return jsonify({
        "total": db.query(Lead).count(),
        "high": db.query(Lead).filter(Lead.priority=="high").count(),
        "medium": db.query(Lead).filter(Lead.priority=="medium").count(),
        "new": db.query(Lead).filter(Lead.status=="new").count(),
        "pursuing": db.query(Lead).filter(Lead.status=="pursuing").count(),
        "duplicates_merged": db.query(Lead).filter(Lead.is_duplicate_of_known==True).count(),
        "pending_review": db.query(Lead).filter(Lead.dup_review=="pending").count(),
        "last_run": last.completed_at.isoformat() if last and last.completed_at else None,
        # Dedup health signal for the dashboard banner
        "dedup_enabled": dedup_enabled,
        "dedup_coverage": round(coverage, 3),
        "dedup_healthy": dedup_enabled and coverage >= 0.9,
    })

@app.route("/api/run", methods=["POST"])
def trigger_run():
    authed = session.get("authenticated", False)
    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not authed and not (cron_secret and provided == cron_secret):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    def run():
        try:
            from main import ClassActionScout
            ClassActionScout().run(sources=data.get("sources"), skip_pinkas=data.get("skip_pinkas", True))
        except Exception as e: print(f"Pipeline error: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/reanalyze", methods=["POST"])
@login_required
def trigger_reanalyze():
    data = request.get_json(silent=True) or {}  # noqa: F841
    db = get_db()
    pending_count = db.query(Lead).filter(
        Lead.relevance_score.isnot(None),
        (Lead.strength_score.is_(None) | Lead.priority.is_(None)),
    ).count()
    def run():
        try:
            from main import ClassActionScout
            result = ClassActionScout().reanalyze_pending()
            print(f"Reanalyze complete: {result}")
        except Exception as e:
            print(f"Reanalyze error: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "pending_count": pending_count})

@app.route("/api/run-pacer", methods=["POST"])
@login_required
def trigger_pacer():
    data = request.get_json(silent=True) or {}  # noqa: F841
    db = get_db()
    lead_count = db.query(Lead).filter(Lead.strength_score >= 5).count()
    def run():
        try:
            from main import ClassActionScout
            result = ClassActionScout().run_pacer_enrichment()
            print(f"PACER enrichment complete: {result}")
        except Exception as e:
            print(f"PACER enrichment error: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "lead_count": lead_count})

@app.route("/api/scrape-logs")
@login_required
def get_logs():
    db = get_db()
    logs = db.query(ScrapeLog).order_by(ScrapeLog.started_at.desc()).limit(50).all()
    return jsonify([{
        "source":l.source_name, "items_found":l.items_found, "items_new":l.items_new,
        "success":l.success, "started":l.started_at.isoformat() if l.started_at else None,
    } for l in logs])

@app.route("/api/known-cases")
@login_required
def known_cases():
    from config.settings import KNOWN_CASES
    return jsonify(KNOWN_CASES)

@app.route("/api/test-email")
def test_email():
    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not (cron_secret and provided == cron_secret):
        return jsonify({"error": "unauthorized"}), 401
    from alerts.email_sender import send_alert_email
    dummy = [{
        "id": 0,
        "title": "בדיקת מערכת התראות — Class Action Scout",
        "company": "Acme Corp (Test)",
        "source_name": "test",
        "recommended_action": "זוהי הודעת בדיקה לאימות תצורת Microsoft Graph. אם קיבלת מייל זה, ההגדרות תקינות.",
        "strength_score": 9,
    }]
    ok = send_alert_email(dummy)
    if ok:
        return jsonify({"status": "sent"})
    return jsonify({"status": "error", "detail": "Graph auth missing or token expired — run scripts/setup_outlook.py to (re)authenticate with the Mail.Send scope. Check OUTLOOK_CLIENT_ID and the token at OUTLOOK_TOKEN_PATH (/var/data/outlook_token.json on Render)."}), 500

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"\n  Class Action Scout Dashboard\n  http://localhost:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=True)
