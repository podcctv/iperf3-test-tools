
import re

def analyze_main():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    functions_to_remove = [
        '_dashboard_html',
        '_schedules_html',
        '_trace_html',
        '_admin_html',
        '_admin_html_deprecated',
        '_whitelist_html',
        '_redis_monitoring_html',
        '_tests_page_html',
        '_sidebar_css',
        '_sidebar_html',
        '_sidebar_js',
        '_login_html'
    ]
    
    routes_to_remove = [
        r'@app\.get\("/web"',
        r'@app\.get\("/web/schedules"',
        r'@app\.get\("/web/trace"',
        r'@app\.get\("/web/admin"',
        r'@app\.get\("/web/whitelist"',
        r'@app\.get\("/web/redis"',
        r'@app\.get\("/web/tests"'
    ]
    
    ranges_to_remove = []
    
    current_func = None
    start_line = -1
    
    # Simple parser to find function blocks
    # Assumes valid python indentation
    
    for i, line in enumerate(lines):
        # Check function defs
        match = re.search(r'^def\s+(_\w+)\s*\(', line)
        if match:
            func_name = match.group(1)
            if func_name in functions_to_remove:
                # Found start
                current_func = func_name
                start_line = i
                # Note: decorators usage usually precedes def, but these are helper functions mostly without decorators
                # EXCEPT: async def page routes. 
                # But helper functions are def _helper:
                pass
        
        # If we are in a removal block, check for end
        # End is when indentation returns to 0 and it's not a comment/empty
        # But wait, inside function there can be empty lines.
        # This parsing is tricky.
        
        # Better approach:
        # 1. Helper functions (def _...): Remove until next top-level def/class/@app.
        # 2. Routes (@app.get... async def ...): Remove the decorator AND the function.
    
    # Let's try to just identifying line numbers for inspection first.
    
    print("--- Functions Found ---")
    for i, line in enumerate(lines):
        for func in functions_to_remove:
            if f"def {func}" in line:
                print(f"Line {i+1}: {line.strip()}")
                
    print("\n--- Routes Found ---")
    for i, line in enumerate(lines):
        if line.strip().startswith('@app.get("/web'):
            print(f"Line {i+1}: {line.strip()}")

if __name__ == '__main__':
    analyze_main()
