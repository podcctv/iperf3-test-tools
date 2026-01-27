
import re

file_path = "temp_main.py"

def list_functions():
    try:
        try:
            with open(file_path, 'r', encoding='utf-16') as f:
                lines = f.readlines()
        except UnicodeError:
            with open(file_path, 'r', encoding='utf-8') as f:
                 lines = f.readlines()

        print(f"Total lines: {len(lines)}")
        
        for i, line in enumerate(lines):
            line_strip = line.strip()
            if line_strip.startswith("def ") or line_strip.startswith("async def "):
                print(f"{i+1}: {line_strip}")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    list_functions()
