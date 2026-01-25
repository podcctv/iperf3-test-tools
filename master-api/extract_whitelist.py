
def extract():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Body content from 8267 to 8643 (0-indexed: 8266 to 8643)
    # 8266 is body = ''', so stats at 8267.
    # 8644 is ''', so ends at 8643.
    
    # HTML Part: 8267 to 8333 (1-indexed) -> 8266 to 8333 (0-indexed)
    html_lines = lines[8266:8333]
    
    # JS Part: 8336 to 8639 (1-indexed) -> 8335 to 8639 (0-indexed)
    # (Skipping <script> tags at 8335 and 8640)
    js_lines = lines[8335:8639]
    
    with open('master-api/static/whitelist_partial.html', 'w', encoding='utf-8') as f:
        f.writelines(html_lines)
        
    js_content = "".join(js_lines)
    # Replace API_BASE and apiFetch
    js_content = js_content.replace("const API_BASE = '';", "")
    # Remove local apiFetch definition (it's async function apiFetch...)
    # finding start of apiFetch
    # 8338: async function apiFetch(url, options = {}) {
    # ...
    # 8348: }
    
    # I'll just comment it out or let import handle it.
    # But better to remove it to avoid conflict if I import with same name.
    
    final_js = "import { apiFetch } from './api.js';\n\n" + js_content
    
    with open('master-api/static/js/whitelist.js', 'w', encoding='utf-8') as f:
        f.write(final_js)

    print("Whitelist extraction complete.")

if __name__ == '__main__':
    extract()
