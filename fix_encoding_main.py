
import os

target_file = r"c:\Users\Administrator\iperf3-test-tools\master-api\app\main.py"

def fix_encoding(file_path):
    print(f"Processing {file_path}...")
    try:
        with open(file_path, 'rb') as f:
            content_bytes = f.read()
        
        # Try convert from utf-8 first (in case it is already utf-8)
        try:
            content_str = content_bytes.decode('utf-8')
            print("File is already valid UTF-8.")
        except UnicodeDecodeError:
            print("File is NOT valid UTF-8. Trying GB18030/GBK...")
            try:
                content_str = content_bytes.decode('gb18030')
                print("Successfully decoded as GB18030.")
            except UnicodeDecodeError:
                print("Failed to decode as GB18030. Trying replace...")
                content_str = content_bytes.decode('utf-8', errors='replace')
        
        # Write back as UTF-8
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content_str)
        print("Successfully wrote back as UTF-8.")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    fix_encoding(target_file)
