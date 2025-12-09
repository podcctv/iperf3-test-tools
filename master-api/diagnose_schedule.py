"""
Diagnostic script to check schedule execution issues.

Run this to diagnose why scheduled tests are failing.
"""
from app.database import SessionLocal
from app.models import TestSchedule, Node
from sqlalchemy import select

def diagnose_schedule(schedule_id: int = 1):
    db = SessionLocal()
    try:
        schedule = db.get(TestSchedule, schedule_id)
        if not schedule:
            print(f"❌ Schedule {schedule_id} not found")
            return
        
        print(f"✓ Schedule found: {schedule.name}")
        print(f"  - Enabled: {schedule.enabled}")
        print(f"  - Protocol: {schedule.protocol}")
        print(f"  - Duration: {schedule.duration}s")
        print(f"  - Interval: {schedule.interval_seconds}s")
        print(f"  - Port: {schedule.port}")
        print()
        
        src_node = db.get(Node, schedule.src_node_id)
        dst_node = db.get(Node, schedule.dst_node_id)
        
        if not src_node:
            print(f"❌ Source node (ID: {schedule.src_node_id}) not found")
            return
        if not dst_node:
            print(f"❌ Destination node (ID: {schedule.dst_node_id}) not found")
            return
        
        print(f"✓ Source Node: {src_node.name}")
        print(f"  - IP: {src_node.ip}")
        print(f"  - Agent Port: {src_node.agent_port}")
        print(f"  - iperf Port: {src_node.iperf_port}")
        print()
        
        print(f"✓ Destination Node: {dst_node.name}")
        print(f"  - IP: {dst_node.ip}")
        print(f"  - Agent Port: {dst_node.agent_port}")
        print(f"  - iperf Port: {dst_node.iperf_port}")
        print()
        
        # Check what port will be used
        test_port = schedule.port or dst_node.iperf_port
        print(f"📊 Test Configuration:")
        print(f"  - Will test from {src_node.name} ({src_node.ip})")
        print(f"  - To {dst_node.name} ({dst_node.ip}:{test_port})")
        print(f"  - Protocol: {schedule.protocol}")
        print()
        
        print("🔍 Diagnostic Steps:")
        print(f"1. Check if agent on {src_node.name} is reachable:")
        print(f"   curl http://{src_node.ip}:{src_node.agent_port}/health")
        print()
        print(f"2. Check if iperf3 server is running on {dst_node.name}:")
        print(f"   curl http://{dst_node.ip}:{dst_node.agent_port}/health")
        print(f"   (Look for 'server_running': true and 'port': {test_port})")
        print()
        print(f"3. Test connectivity manually:")
        print(f"   From {src_node.name}, try: iperf3 -c {dst_node.ip} -p {test_port} -t 5")
        print()
        print(f"4. Check recent failures:")
        print(f"   curl http://localhost:9000/debug/failures")
        
    finally:
        db.close()

if __name__ == "__main__":
    import sys
    schedule_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    diagnose_schedule(schedule_id)
