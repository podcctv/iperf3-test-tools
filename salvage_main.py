
file_path = "temp_main.py"
output_path = "master-api/app/main.py"

def salvage():
    try:
        # Read as UTF-8 strictly (temp_main.py is binary safe output from git)
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Part 1: 0 to 2061 (exclusive)
        part1 = lines[:2060]
        
        # Part 2: 12382 to 16700 (exclusive)
        part2 = lines[12382:16700]
        
        # Part 3: 16921 to 17084 (exclusive)
        part3 = lines[16921:17084]
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.writelines(part1)
            f.write("\n\n# --- SALVAGED CODE SPLICE 1 ---\n\n")
            f.writelines(part2)
            f.write("\n\n# --- SALVAGED CODE SPLICE 2 ---\n\n")
            f.writelines(part3)
            
        print(f"Salvaged: {len(part1)} + {len(part2)} + {len(part3)} lines.")
        print(f"Total lines: {len(part1) + len(part2) + len(part3)}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    salvage()
