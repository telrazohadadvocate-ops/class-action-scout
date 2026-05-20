from database.models import get_session, Lead 
from config.settings import DATABASE_URL 
db = get_session(DATABASE_URL) 
leads = db.query(Lead).filter(Lead.strength_score != None).order_by(Lead.strength_score.desc()).all() 
for l in leads: 
    print(f"[{l.strength_score}/10] [{l.priority}] {l.company} - {l.title[:70]}") 
