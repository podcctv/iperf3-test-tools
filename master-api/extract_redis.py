
def extract():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Body string starts at 17426 (body = r'''), so content starts 17427.
    # Ends at 17747 (''').
    # 0-indexed: 17426 -> 17427 content start.
    
    # HTML Part: 17427 to 17573 (1-indexed) -> 17426 to 17573 (0-indexed).
    html_lines = lines[17426:17573]
    
    # JS Part: 17575 to 17745 (1-indexed) -> 17574 to 17745 (0-indexed).
    # Skip script tags 17574 and 17746.
    js_lines = lines[17574:17745]
    
    with open('master-api/static/redis_partial.html', 'w', encoding='utf-8') as f:
        f.writelines(html_lines)
    
    js_content = "".join(js_lines)
    
    # Refactor fetch to apiFetch
    # Replace `fetch(${API_BASE}/api/` with `apiFetch(/api/`
    # Warning: js uses template literals: `${API_BASE}/api/cache/stats`
    # I will do simple string replacement for common patterns.
    
    js_content = js_content.replace("const API_BASE = '';", "")
    js_content = js_content.replace("${API_BASE}", "") # Remove API_BASE if used in templates
    
    # Replace fetch(url, options) with apiFetch.
    # But options often include credentials: 'include'. apiFetch adds it automatically.
    # This might require manual cleanup.
    
    final_js = "import { apiFetch } from './api.js';\n\n" + js_content
    
    with open('master-api/static/js/redis.js', 'w', encoding='utf-8') as f:
        f.write(final_js)

    print("Redis extraction complete.")

if __name__ == '__main__':
    extract()
