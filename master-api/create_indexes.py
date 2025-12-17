"""
Database Index Creation Script for Performance Optimization

This script adds indexes to critical tables to improve query performance.
Run this after deployment to optimize database operations.
"""

from sqlalchemy import create_engine, text, inspect
import logging

# Database URL (matches main app configuration)
from app.config import settings
from app.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def index_exists(engine, table_name: str, index_name: str) -> bool:
    """Check if an index already exists."""
    inspector = inspect(engine)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def create_indexes():
    """Create performance-critical indexes if they don't exist."""
    
    indexes_to_create = [
        # test_results table indexes
        ("test_results", "idx_test_results_schedule", "schedule_id"),
        ("test_results", "idx_test_results_timestamp", "timestamp"),
        ("test_results", "idx_test_results_src_node", "src_node_id"),
        ("test_results", "idx_test_results_dst_node", "dst_node_id"),
        
        # alerts table indexes  
        ("alerts", "idx_alerts_created", "created_at"),
        ("alerts", "idx_alerts_type", "alert_type"),
        ("alerts", "idx_alerts_node", "node_id"),
        
        # ping_history table indexes
        ("ping_history", "idx_ping_history_timestamp", "timestamp"),
        ("ping_history", "idx_ping_history_node", "node_id"),
        
        # trace_results table indexes
        ("trace_results", "idx_trace_results_timestamp", "timestamp"),
        ("trace_results", "idx_trace_results_node", "node_id"),
    ]
    
    with engine.connect() as conn:
        for table_name, index_name, column_name in indexes_to_create:
            try:
                # Check if table exists first
                inspector = inspect(engine)
                if table_name not in inspector.get_table_names():
                    logger.warning(f"Table {table_name} does not exist, skipping index {index_name}")
                    continue
                
                # Check if index already exists
                if index_exists(engine, table_name, index_name):
                    logger.info(f"Index {index_name} already exists on {table_name}, skipping")
                    continue
                
                # Create the index
                sql = f"CREATE INDEX {index_name} ON {table_name}({column_name})"
                conn.execute(text(sql))
                conn.commit()
                logger.info(f"✓ Created index: {index_name} on {table_name}({column_name})")
                
            except Exception as e:
                logger.error(f"✗ Failed to create index {index_name}: {e}")
                conn.rollback()
    
    logger.info("Database index creation completed!")


if __name__ == "__main__":
    logger.info("Starting database index creation...")
    create_indexes()
