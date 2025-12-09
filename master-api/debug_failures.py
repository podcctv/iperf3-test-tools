from app.database import SessionLocal
from app.models import ScheduleResult
from sqlalchemy import select, desc

def inspect_failures():
    db = SessionLocal()
    try:
        results = db.scalars(
            select(ScheduleResult)
            .where(ScheduleResult.status == 'failed')
            .order_by(desc(ScheduleResult.executed_at))
            .limit(5)
        ).all()
        
        print(f"Found {len(results)} failed results:")
        for r in results:
            print(f"ID: {r.id} | Schedule: {r.schedule_id} | Time: {r.executed_at}")
            print(f"Error: {r.error_message}")
            print("-" * 50)
            
    finally:
        db.close()

if __name__ == "__main__":
    inspect_failures()
