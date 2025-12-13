"""
ASN Cache Service - Syncs data from PeeringDB for Tier classification.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
import requests
from sqlalchemy.orm import Session

from .models import AsnCache

logger = logging.getLogger(__name__)

PEERINGDB_API = "https://www.peeringdb.com/api/net"


def classify_tier(net_data: dict) -> str:
    """
    Classify network tier based on PeeringDB data.
    
    Tier 1: Global backbone (NSP with global scope, huge IX presence)
    Tier 2: Regional transit/ISP
    Tier 3: Local ISP
    IX: Internet Exchange points
    CDN: Content Delivery Networks
    """
    info_type = net_data.get("info_type", "")
    info_scope = net_data.get("info_scope", "")
    ix_count = net_data.get("ix_count", 0) or 0
    name = net_data.get("name", "").lower()
    
    # Known Tier 1 ASNs by name
    tier1_names = ["ntt", "telia", "cogent", "lumen", "level3", "gtt", "zayo", 
                   "hurricane", "he.net", "arelion", "centurylink"]
    for t1 in tier1_names:
        if t1 in name:
            return "T1"
    
    # IX detection
    if info_type == "Route Server" or "ix" in name.lower() or "exchange" in name.lower():
        return "IX"
    
    # CDN detection
    if info_type == "Content":
        return "CDN"
    
    # Tier 1: Global NSP with many IX connections
    if info_type == "NSP" and info_scope == "Global" and ix_count >= 50:
        return "T1"
    
    # Tier 2: Regional transit or NSP with moderate IX
    if info_type == "NSP" and (info_scope in ["Regional", "Europe", "North America", "Asia Pacific"] or ix_count >= 20):
        return "T2"
    
    # Tier 2: Major regional carriers
    if info_type in ["NSP", "Educational/Research"] and ix_count >= 10:
        return "T2"
    
    # Tier 3: Local ISP
    if info_type in ["Cable/DSL/ISP", "Enterprise"]:
        return "T3"
    
    # Default based on IX count
    if ix_count >= 30:
        return "T2"
    
    return "ISP"


def sync_peeringdb(db: Session, batch_size: int = 500) -> dict:
    """
    Sync ASN data from PeeringDB API.
    
    Returns dict with sync stats: {"total": N, "added": M, "updated": K, "errors": []}
    """
    stats = {"total": 0, "added": 0, "updated": 0, "errors": []}
    offset = 0
    
    logger.info("[ASN-SYNC] Starting PeeringDB sync...")
    
    while True:
        try:
            url = f"{PEERINGDB_API}?depth=0&limit={batch_size}&skip={offset}"
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            data = response.json()
            
            networks = data.get("data", [])
            if not networks:
                break
            
            for net in networks:
                stats["total"] += 1
                asn = net.get("asn")
                if not asn:
                    continue
                
                tier = classify_tier(net)
                
                # Upsert into database
                existing = db.query(AsnCache).filter(AsnCache.asn == asn).first()
                if existing:
                    existing.name = net.get("name", "Unknown")
                    existing.info_type = net.get("info_type")
                    existing.info_scope = net.get("info_scope")
                    existing.ix_count = net.get("ix_count", 0)
                    existing.tier = tier
                    existing.updated_at = datetime.now(timezone.utc)
                    stats["updated"] += 1
                else:
                    new_entry = AsnCache(
                        asn=asn,
                        name=net.get("name", "Unknown"),
                        info_type=net.get("info_type"),
                        info_scope=net.get("info_scope"),
                        ix_count=net.get("ix_count", 0),
                        tier=tier,
                        updated_at=datetime.now(timezone.utc)
                    )
                    db.add(new_entry)
                    stats["added"] += 1
            
            db.commit()
            offset += batch_size
            logger.info(f"[ASN-SYNC] Processed {offset} networks...")
            
            # PeeringDB has ~15k networks, stop if no more
            if len(networks) < batch_size:
                break
                
        except Exception as e:
            logger.error(f"[ASN-SYNC] Error at offset {offset}: {e}")
            stats["errors"].append(str(e))
            break
    
    logger.info(f"[ASN-SYNC] Complete: {stats}")
    return stats


def get_asn_info(db: Session, asn: int) -> Optional[dict]:
    """
    Get cached ASN info.
    Returns dict with name, tier, info_type, etc. or None if not found.
    """
    cached = db.query(AsnCache).filter(AsnCache.asn == asn).first()
    if cached:
        return {
            "asn": cached.asn,
            "name": cached.name,
            "tier": cached.tier,
            "info_type": cached.info_type,
            "info_scope": cached.info_scope,
            "ix_count": cached.ix_count,
            "updated_at": cached.updated_at.isoformat() if cached.updated_at else None
        }
    return None


def get_asn_count(db: Session) -> int:
    """Get total number of cached ASNs."""
    return db.query(AsnCache).count()
