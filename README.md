# Class Action Scout 🔍⚖️

## סוכן אוטומטי לזיהוי הזדמנויות לתובענות ייצוגיות בישראל

סוכן AI שסורק מקורות בינלאומיים ומקומיים, מזהה עילות פוטנציאליות לתובענות ייצוגיות בישראל, ובודק שלא הוגשה כבר תביעה דומה בפנקס התובענות הייצוגיות.

---

## ארכיטקטורה

```
Sources (International + Israeli)
        │
        ▼
┌─────────────────────┐
│   Scraping Engine    │  BeautifulSoup / requests
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Stage 1: Classify  │  Claude AI — relevance scoring (1-10)
└─────────┬───────────┘
          ▼ (score >= 4)
┌─────────────────────┐
│  Stage 2: Analyze   │  Claude AI — deep legal analysis
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Stage 3: פנקס      │  Check if similar case already filed
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Report & Alert     │  Text / HTML / Email
└─────────────────────┘
```

## מקורות

| Source | Type | Description |
|--------|------|-------------|
| ClassAction.org | International | US class action filings & settlements |
| TopClassActions.com | International | Open settlements & lawsuits |
| TheMarker | Local | Israeli business/legal news |
| Globes | Local | Israeli financial news |
| Calcalist | Local | Israeli tech/business news |

## התקנה

```bash
# Clone
git clone <repo-url>
cd class-action-scout

# Virtual environment
python -m venv venv
source venv/bin/activate          # Linux/Mac
# venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Configure
export ANTHROPIC_API_KEY="sk-ant-..."

# Initialize DB
python -c "from database.models import init_database; from config.settings import DATABASE_URL, DATABASE_PATH; DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True); init_database(DATABASE_URL)"
```

## שימוש

### הרצה ידנית
```bash
# Full pipeline
python main.py --run-now

# Specific sources only
python main.py --run-now --sources classaction_org topclassactions

# Skip פנקס check (faster)
python main.py --run-now --skip-pinkas

# Generate report
python main.py --report --days 7
python main.py --report --days 30 --format html
```

### הגדרת Cron Job
```bash
crontab -e

# Daily run at 06:00
0 6 * * * cd /path/to/class-action-scout && /path/to/venv/bin/python main.py --run-now >> logs/cron.log 2>&1

# Weekly HTML report every Sunday at 08:00
0 8 * * 0 cd /path/to/class-action-scout && /path/to/venv/bin/python main.py --report --days 7 --format html >> logs/cron.log 2>&1
```

### שימוש ב-Scheduler הפנימי
```bash
python scheduler.py
# Runs daily at 06:00, weekly report Sunday 08:00
```

### Render Cron Job (cloud)
In `render.yaml`:
```yaml
services:
  - type: cron
    name: class-action-scout
    schedule: "0 6 * * *"
    buildCommand: pip install -r requirements.txt
    startCommand: python main.py --run-now
    envVars:
      - key: ANTHROPIC_API_KEY
        sync: false
```

## מבנה הפרויקט

```
class-action-scout/
├── config/
│   └── settings.py          # All configuration + known cases
├── scrapers/
│   └── scrapers.py          # Base + all scraper implementations
├── analysis/
│   ├── claude_analyzer.py   # Two-stage Claude AI analysis
│   └── prompts/
│       ├── classify.txt     # Stage 1 prompt
│       ├── legal_analysis.txt # Stage 2 prompt
│       └── pattern.txt      # Weekly pattern detection
├── registry/
│   └── pinkas_checker.py    # פנקס התובענות הייצוגיות
├── database/
│   └── models.py            # SQLAlchemy models
├── data/
│   └── scout.db             # SQLite database (auto-created)
├── logs/                    # Log files
├── reports/                 # Generated HTML reports
├── main.py                  # Main pipeline + CLI
├── scheduler.py             # Cron-like scheduler
├── requirements.txt
└── README.md
```

## Pipeline Details

### Stage 1: Classification
Claude evaluates each scraped item for Israel relevance (1-10):
- Is the company active in Israel?
- Is there a parallel cause of action under Israeli law?
- What's the estimated class size?

Items scoring ≥4 proceed to Stage 2.

### Stage 2: Deep Legal Analysis
Claude performs full legal analysis:
- Maps to specific Israeli statutes (חוק הגנת הצרכן, חוק הגנת הפרטיות, etc.)
- Assesses certification probability
- Identifies evidence needs
- Recommends next steps

### Stage 3: Registry Check
Searches פנקס התובענות הייצוגיות to verify no similar case has been filed.

### Known Cases
The firm's active/researched cases are tracked in `settings.py → KNOWN_CASES` to:
- Avoid re-discovering known opportunities
- Flag new developments in existing cases
- Detect related filings

## Customization

### Adding a new source
In `config/settings.py`:
```python
SOURCES["new_source"] = {
    "enabled": True,
    "url": "https://example.com/news",
    "type": "international",  # or "local"
}
```

Then add a scraper class in `scrapers/scrapers.py` or use `IsraeliNewsScraper` for generic sites.

### Adjusting thresholds
```python
MIN_RELEVANCE_SCORE = 4        # Lower = more leads, more API calls
HIGH_PRIORITY_THRESHOLD = 7    # Higher = fewer alerts
```

### Updating prompts
Edit files in `analysis/prompts/` — no code changes needed.

## Deployment on Render

### Prerequisites
- GitHub repository with this project
- Render account (render.com)
- PACER cookies generated locally (see Step 6)

### Step 1 — Push to GitHub
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/<your-org>/class-action-scout.git
git push -u origin main
```

### Step 2 — Create Render account and link GitHub
Sign up at render.com, then connect your GitHub account under **Account → GitHub**.

### Step 3 — Deploy with Blueprint
In the Render dashboard: **New → Blueprint** → select the repository.  
Render reads `render.yaml` and creates both services automatically (`scout-web` and `scout-daily`).

### Step 4 — Add environment variables
In the Render dashboard for **scout-web** (and separately for **scout-daily**), go to **Environment** and add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `OUTLOOK_CLIENT_ID` | Azure app client ID |
| `OUTLOOK_TENANT_ID` | Azure tenant ID |
| `PACER_USERNAME` | PacerMonitor login email |
| `PACER_PASSWORD` | PacerMonitor password |
| `SMTP_HOST` | e.g. `smtp.office365.com` |
| `SMTP_USER` | sender email |
| `SMTP_PASS` | SMTP password |
| `REPORT_RECIPIENTS` | `ohad@levin-telraz.co.il` |

### Step 5 — Trigger initial scan
Open the deployed dashboard URL and click **הרץ סריקה** to run the first pipeline.

### Step 6 — PACER cookies setup (one-time)
PacerMonitor requires a browser login with reCAPTCHA; this must be done locally once:

```bash
# On your local machine:
python -c "
from scrapers.pacer_monitor import PacerMonitorClient
PacerMonitorClient().login_interactive()
"
# Completes the reCAPTCHA in the opened browser window.
# Cookies are saved to data/pacer_cookies.json
```

Then upload the cookies file to the persistent disk via the Render shell:

```bash
# In Render dashboard → scout-web → Shell:
cat > /var/data/pacer_cookies.json << 'EOF'
<paste contents of data/pacer_cookies.json here>
EOF
```

Cookies are valid until PacerMonitor invalidates the session (typically weeks–months).  
Repeat this step when the PACER button returns "login failed".

### Cron schedule
The `scout-daily` cron runs at **03:00 UTC = 06:00 IST** (`"0 3 * * *"`).  
Adjust the `schedule` field in `render.yaml` if needed.

### SQLite + persistent disk
Both services share `/var/data/scout.db` on the same Render disk.  
The cron and web service write to the DB at different times; if you need concurrent
write safety, enable WAL mode by adding this to `database/models.py`:

```python
from sqlalchemy import event
@event.listens_for(engine, "connect")
def set_wal(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
```

---

## הערות חשובות

1. **אתיקה**: הסוכן כלי עזר בלבד — כל הזדמנות דורשת בדיקה משפטית מעמיקה
2. **Scraping**: הסוכן מכבד השהיות בין בקשות (robots.txt)
3. **עלויות API**: כל item = 1-2 API calls. ~20 items/day ≈ $1-2/day
4. **CSS Selectors**: אתרים משנים מבנה — עדכן selectors בהתאם
