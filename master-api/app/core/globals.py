from typing import Dict, Tuple, Optional
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.agent_store import AgentConfigStore
from app.state_store import StateStore
from app.core.health import NodeHealthMonitor, BackboneLatencyMonitor, ZHEJIANG_TARGETS
from app.core.scheduler import scheduler

# ============================================================================
# Global Store Instances
# ============================================================================

agent_store = AgentConfigStore(settings.agent_config_file)
state_store = StateStore(settings.state_file, settings.state_recent_tests)

# ============================================================================
# Global Monitor Instances
# ============================================================================

health_monitor = NodeHealthMonitor(settings.health_check_interval)
backbone_monitor = BackboneLatencyMonitor(ZHEJIANG_TARGETS, interval_seconds=60)

# ============================================================================
# Caches & State
# ============================================================================

# IP -> (data, timestamp)
geo_cache: Dict[str, Tuple[Optional[dict], float]] = {}
GEO_CACHE_TTL_SECONDS = 60 * 60 * 6

# Whitelist hash tracking
last_whitelist_hash: str | None = None

# UI State
ip_privacy_state: Dict[int, bool] = {}
streaming_status_cache: Dict[int, dict] = {}

# Templates
templates = Jinja2Templates(directory="static")  # Adjusted directory to match typical setup or static folder

# Helper to get node status (used by dashboard)
def get_node_status():
    # This might need to be imported or defined here.
    # If it depends on things in main.py, we have a cycle.
    # Refactoring `get_node_status` might be complex if it uses database.
    # Provide a placeholder or move logic here if simple.
    # For now, we will leave it to be imported or refactored later.
    pass
