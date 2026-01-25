
import os

def extract():
    with open('master-api/app/main.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Extract HTML Body
    # In view_file 1511:
    # Body starts at 12334 (div class="bg-slate-800...)
    # Ends at 12473 (div closing).
    # 0-indexed: 12334 -> 12333.
    # 12473 -> 12472.
    
    # Check boundaries in snippet:
    # 12331: body = '''
    # 12333: <!-- Database Management Card -->  (Line 12334 in view)
    # ...
    # 12474: </div>  (Line 12474 in one div?)
    # 12475: </div>
    # 12476: 
    # 12477: <script>
    
    # So HTML is 12334 to 12475.
    html_lines = lines[12333:12475] # 0-indexed
    
    with open('master-api/static/admin_partial.html', 'w', encoding='utf-8') as f:
        f.writelines(html_lines)
    
    # Extract JS
    # 12478: const apiFetch = ...
    # 12579: </script>
    
    js_lines = lines[12477:12578] # 0-indexed
    js_content = "".join(js_lines)
    
    # Replace apiFetch
    js_content = js_content.replace(
        "const apiFetch = (url, options = {}) => {", 
        "import { apiFetch } from './api.js';\n// const apiFetch replaced"
    )
    # The original had a body block with { ... }. I need to match carefully or just replace the first few lines.
    # Original:
    # const apiFetch = (url, options = {}) => {
    #   return fetch(url, {
    #     ...options,
    #     headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
    #   });
    # };
    #
    # I will replace the whole block if I can, or just comment it out.
    # Easier to just write the file manually if it's short.
    # But let's use the script to get the bulk of the logic (event listeners etc).
    
    # Let's simple remove the apiFetch definition function.
    # It ends at 12483 };.
    
    # Function to find end of apiFetch
    # It occupies lines 0 to 5 of js_lines.
    
    final_js = "import { apiFetch } from './api.js';\n\n" + "".join(js_lines[6:])
    
    with open('master-api/static/js/admin.js', 'w', encoding='utf-8') as f:
        f.write(final_js)

    print("Admin extraction complete.")

if __name__ == '__main__':
    extract()
