"""
Data Cleanup Module

Automatically removes old data to keep database size manageable and queries fast.
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import PingHistory, TestResult, AlertHistory

logger = logging.getLogger(__name__)


async def cleanup_old_data():
    """
    Remove old data according to retention policies:
    - Ping history: 30 days
    - Test results: 90 days  
    - Alert history: 60 days
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        
        # Cleanup ping history (30 days)
        ping_cutoff = now - timedelta(days=30)
        ping_deleted = db.query(PingHistory).filter(
            PingHistory.timestamp < ping_cutoff
        ).delete(synchronize_session=False)
        
        # Cleanup test results (90 days)
        test_cutoff = now - timedelta(days=90)
        test_deleted = db.query(TestResult).filter(
            TestResult.timestamp < test_cutoff
        ).delete(synchronize_session=False)
        
        # Cleanup alert history (60 days)
        alert_cutoff = now - timedelta(days=60)
        alert_deleted = db.query(AlertHistory).filter(
            AlertHistory.created_at < alert_cutoff
        ).delete(synchronize_session=False)
        
        db.commit()
        
        logger.info(
            f"Data cleanup completed: "
            f"{ping_deleted} ping records, "
            f"{test_deleted} test results, "
            f"{alert_deleted} alerts removed"
        )
        
        return {
            "ping_deleted": ping_deleted,
            "test_deleted": test_deleted,
            "alert_deleted": alert_deleted
        }
        
    except Exception as e:
        logger.error(f"Data cleanup failed: {e}")
        db.rollback()
        return None
    finally:
        db.close()
