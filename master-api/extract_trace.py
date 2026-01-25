
import os

def extract():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Extract CSS (approx lines 10481-10718 from view_file, 0-indexed is 10480-10717)
    # The view_file output is 1-indexed.
    # Line 10481 in view_file is `    css_content = '''`
    # Actual CSS starts at 10482.
    # Ends at 10718 `'''`.
    css_lines = lines[10481:10717] # 0-indexed: 10481 is line 10482.
    
    # Strip indentation
    css_content = "".join(css_lines)
    # It might be double indented.
    
    with open('master-api/static/css/trace.css', 'w', encoding='utf-8') as f:
        f.write(css_content)
        
    # Extract JS and HTML Body
    # body = ''' starts at 10737 (1-indexed). So 0-indexed 10736.
    # Ends at 12281 (1-indexed). So 0-indexed 12280.
    
    # JS starts at line 10914 (<script>) -> 10915 actual JS code.
    # HTML Body ends at 10913.
    
    # HTML Content: 10741 to 10912.
    html_lines = lines[10740:10912] # 0-indexed 10740 is 10741.
    
    with open('master-api/static/trace_partial.html', 'w', encoding='utf-8') as f:
        f.writelines(html_lines)
        
    # JS Content: 10915 to 12277.
    js_lines = lines[10914:12277] # 0-indexed 10914 is 10915.
    
    js_content = "".join(js_lines)
    # Replace apiFetch
    js_content = js_content.replace("const apiFetch = (url, opt = {}) => fetch(url, { credentials: 'include', ...opt });", "import { apiFetch } from './api.js';")
    
    with open('master-api/static/js/trace.js', 'w', encoding='utf-8') as f:
        f.write(js_content)
        
    print("Extraction complete.")

if __name__ == '__main__':
    extract()
