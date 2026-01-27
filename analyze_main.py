
import re

file_path = "temp_main.py"

def analyze_structure():
    try:
        # Handling potentially weird encoding from the git show > file redirection in PowerShell
        # PowerShell '>', '>>' defaults to UTF-16LE.
        try:
            with open(file_path, 'r', encoding='utf-16') as f:
                lines = f.readlines()
        except UnicodeError:
            with open(file_path, 'r', encoding='utf-8') as f:
                 lines = f.readlines()

        print(f"Total lines: {len(lines)}")
        
        html_start = -1
        html_end = -1
        
        for i, line in enumerate(lines):
            if "<!DOCTYPE html>" in line:
                print(f"HTML Start found at line {i+1}")
                html_start = i
            if "</html>" in line:
                print(f"HTML End found at line {i+1}")
                html_end = i
                
        if html_start != -1:
            # Check what's after HTML
            if html_end != -1 and html_end < len(lines) - 1:
                print("Content exists after HTML block.")
                print(f"First 5 lines after HTML:")
                for j in range(html_end + 1, min(html_end + 6, len(lines))):
                    print(f"{j+1}: {lines[j].strip()}")
            else:
                print("No content or EOF after HTML block.")
                
        # Search for key function
        for i, line in enumerate(lines):
            if "def _check_node_health" in line:
                print(f"Found _check_node_health at line {i+1}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze_structure()
