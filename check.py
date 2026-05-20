from database.models import get_session, Lead 
from config.settings import DATABASE_URL 
db = get_session(DATABASE_URL) 
for l in db.query(Lead).all(): 
    print(f"REL={l.relevance_score} STR={l.strength_score} - {l.company} - {l.title[:60]}") 
