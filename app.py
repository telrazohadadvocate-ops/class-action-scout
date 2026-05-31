#!/usr/bin/env python3
"""
Class Action Scout — Web Application
"""
import sys, os, json, threading, argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    os.environ["PYTHONUTF8"] = "1"

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from config.settings import DATABASE_URL, DATABASE_PATH
from database.models import init_database, get_session, Lead, ScrapeLog

app = Flask(__name__)
CORS(app)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
init_database(DATABASE_URL)

def get_db():
    return get_session(DATABASE_URL)

def _lead_to_dict(lead):
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
        "status": lead.status or "new", "notes": lead.notes or "",
        "scrapedAt": lead.scraped_at.isoformat() if lead.scraped_at else "",
        "reviewedAt": lead.reviewed_at.isoformat() if lead.reviewed_at else "",
    }

@app.route("/")
def dashboard():
    return send_from_directory("templates", "dashboard.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

@app.route("/api/leads")
def get_leads():
    db = get_db()
    q = db.query(Lead)
    p = request.args.get("priority")
    if p and p != "all": q = q.filter(Lead.priority == p)
    s = request.args.get("status")
    if s and s != "all": q = q.filter(Lead.status == s)
    search = request.args.get("search", "")
    if search:
        like = f"%{search}%"
        q = q.filter((Lead.title.ilike(like))|(Lead.company.ilike(like))|(Lead.legal_analysis.ilike(like)))
    sort = request.args.get("sort", "strength")
    if sort == "strength": q = q.order_by(Lead.strength_score.desc().nullslast())
    elif sort == "relevance": q = q.order_by(Lead.relevance_score.desc().nullslast())
    elif sort == "date": q = q.order_by(Lead.scraped_at.desc())
    total = q.count()
    leads = q.offset(request.args.get("offset",0,type=int)).limit(request.args.get("limit",100,type=int)).all()
    return jsonify({"total": total, "leads": [_lead_to_dict(l) for l in leads]})

@app.route("/api/leads/<int:lid>")
def get_lead(lid):
    db = get_db()
    lead = db.query(Lead).get(lid)
    return jsonify(_lead_to_dict(lead)) if lead else (jsonify({"error":"not found"}),404)

@app.route("/api/leads/<int:lid>/status", methods=["PUT"])
def update_status(lid):
    db = get_db()
    lead = db.query(Lead).get(lid)
    if not lead: return jsonify({"error":"not found"}),404
    data = request.json or {}
    if "status" in data: lead.status = data["status"]; lead.reviewed_at = datetime.now(timezone.utc)
    if "notes" in data: lead.notes = data["notes"]
    db.commit()
    return jsonify(_lead_to_dict(lead))

@app.route("/api/stats")
def get_stats():
    db = get_db()
    last = db.query(ScrapeLog).order_by(ScrapeLog.completed_at.desc()).first()
    return jsonify({
        "total": db.query(Lead).count(),
        "high": db.query(Lead).filter(Lead.priority=="high").count(),
        "medium": db.query(Lead).filter(Lead.priority=="medium").count(),
        "new": db.query(Lead).filter(Lead.status=="new").count(),
        "pursuing": db.query(Lead).filter(Lead.status=="pursuing").count(),
        "last_run": last.completed_at.isoformat() if last and last.completed_at else None,
    })

@app.route("/api/run", methods=["POST"])
def trigger_run():
    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret:
        provided = request.headers.get("X-Cron-Secret", "")
        if provided != cron_secret:
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
def get_logs():
    db = get_db()
    logs = db.query(ScrapeLog).order_by(ScrapeLog.started_at.desc()).limit(50).all()
    return jsonify([{
        "source":l.source_name, "items_found":l.items_found, "items_new":l.items_new,
        "success":l.success, "started":l.started_at.isoformat() if l.started_at else None,
    } for l in logs])

@app.route("/api/known-cases")
def known_cases():
    from config.settings import KNOWN_CASES
    return jsonify(KNOWN_CASES)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"\n  Class Action Scout Dashboard\n  http://localhost:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=True)
