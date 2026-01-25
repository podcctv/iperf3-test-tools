
import re

def cleanup():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    functions_to_remove = [
        '_sidebar_css',
        '_sidebar_html',
        '_sidebar_js',
        '_login_html',
        '_tests_page_html',
        '_whitelist_html',
        '_schedules_html',
        '_trace_html',
        '_admin_html_deprecated',
        '_admin_html',
        '_redis_monitoring_html'
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
    
    # We also want to remove `async def login_page` which calls _login_html
    # But it might not be decorated if it's reused? Usually it is.
    # I'll just check for '@app.get("/web")' which handles login logic?
    # Wait, /web logic:
    # @app.get("/web")
    # async def dashboard_page(...):
    #    if not authenticated: return HTMLResponse(_login_html())
    #    return HTMLResponse(_dashboard_html())
    
    # So removing matched routes should cover it.
    
    # Identify ranges to remove
    remove_indices = set()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        matched = False
        
        # Check functions
        match = re.search(r'^def\s+(_\w+)\s*\(', line)
        if match:
            func_name = match.group(1)
            if func_name in functions_to_remove:
                # Found start of function to remove
                # Find end: next line starting with non-whitespace char that is NOT a comment/decorator?
                # Actually, next top-level definition starts with `def ` or `class ` or `@` or variable assignment.
                # Simplified: remove until next top-level definition found (indentation 0).
                
                start = i
                i += 1
                while i < len(lines):
                    next_line = lines[i]
                    if next_line.strip() and not next_line.startswith(' ') and not next_line.startswith('#') and not next_line.startswith(')'):
                        # Potential start of next block
                        # Special case: decorators @...
                        # Or closing parenthesis of definition (handled by not startswith ')')
                        break
                    i += 1
                
                # Mark range
                for idx in range(start, i):
                    remove_indices.add(idx)
                matched = True
        
        if not matched:
            # Check routes
            for route_pattern in routes_to_remove:
                if re.search(route_pattern, line):
                    # Found route decorator
                    # Remove until end of FUNCTION attached to it.
                    # Decorator might cover multiple lines? @app.get(....)
                    
                    start = i
                    # Scan until we pass the function definition and body
                    
                    # 1. Find 'def' or 'async def'
                    while i < len(lines) and 'def ' not in lines[i]:
                        i += 1
                    
                    if i < len(lines):
                        # Found def line
                        i += 1 # Enter body
                        while i < len(lines):
                            next_line = lines[i]
                            if next_line.strip() and not next_line.startswith(' ') and not next_line.startswith('#') and not next_line.startswith(')'):
                                break
                            i += 1
                    
                    for idx in range(start, i):
                        remove_indices.add(idx)
                    matched = True
                    break
        
        if not matched:
            i += 1
            
    # Write back keeping lines NOT in remove_indices
    new_lines = [line for idx, line in enumerate(lines) if idx not in remove_indices]
    
    with open('master-api/app/main.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
        
    print(f"Removed {len(remove_indices)} lines.")

if __name__ == '__main__':
    cleanup()
