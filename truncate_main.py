
import os

target_file = r"c:\Users\Administrator\iperf3-test-tools\master-api\app\main.py"

def truncate_file(file_path):
    print(f"Processing {file_path}...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        new_lines = []
        found_cutoff = False
        for i, line in enumerate(lines):
            if "<!DOCTYPE html>" in line:
                print(f"Found cutoff at line {i+1}")
                found_cutoff = True
                break
            new_lines.append(line)
        
        if found_cutoff:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            print("Successfully truncated file.")
        else:
            print("Cutoff marker '<!DOCTYPE html>' not found.")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    truncate_file(target_file)
