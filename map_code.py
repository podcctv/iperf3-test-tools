
import re

file_path = "temp_main.py"

def map_code_regions():
    try:
        # Force UTF-8 read (ignoring errors if binary garbage exists, but text should be fine)
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        print(f"Total lines: {len(lines)}")
        
        # We need to find:
        # 1. End of header block (Part 1). Before garbage.
        # 2. Start of middle block (Part 2). After garbage.
        # 3. End of middle block (Part 2). Before garbage.
        # 4. Start of footer block (Part 3). After garbage.
        # 5. End of footer block.
        
        # Heuristics:
        # HTML starts with <html or <!DOCTYPE or <head
        # Code starts with imports, def, class, @app
        
        ranges = []
        
        # Scan for HTML blocks
        html_starts = []
        html_ends = []
        
        for i, line in enumerate(lines):
            # Check for HTML start pattern
            if "<!DOCTYPE html>" in line or "<html" in line:
                # heuristic: if we are not already 'in html', this starts it
                # But multiple occurrences...
                html_starts.append(i+1)
            if "</html>" in line:
                html_ends.append(i+1)
                
        print(f"HTML Starts: {html_starts}")
        print(f"HTML Ends: {html_ends}")
        
        # Scan for specific anchors we know
        # Anchor 1: End of Part 1. 'def _probe_streaming_unlock' ? No, that's early.
        # Let's look for Start of Part 2.
        # Part 2 started with '@app.get("/auth/status")' in previous version.
        
        part2_start = -1
        for i, line in enumerate(lines):
            if '@app.get("/auth/status")' in line:
                part2_start = i
                print(f"Part 2 start anchor found at {i+1}")
                break
                
        # Part 3 started with 'class AlertConfigUpdate(BaseModel):' ?
        part3_start = -1
        for i, line in enumerate(lines):
            if 'class AlertConfigUpdate(BaseModel):' in line:
                part3_start = i
                print(f"Part 3 start anchor found at {i+1}")
                break
                
        # End of Part 1 is before HTML.
        # Start of Part 1 is 0.
        
        # We also need end of Part 2.
        # Part 2 ends before another HTML block.
        # In previous analysis, HTML starts again after Part 2.
        # Let's find invalid lines after Part 2 start.
        if part2_start != -1:
            for i in range(part2_start, len(lines)):
                if "<html" in line or "<!DOCTYPE" in lines[i]:
                    print(f"Part 2 seems to end around {i+1}")
                    break
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    map_code_regions()
