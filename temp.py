import os
import sqlite3

db_path = os.path.expanduser(r"~/.local/share/opencode/opencode.db")
output_path = os.path.expanduser(r"~/Desktop/opencode_history.txt")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [t[0] for t in cursor.fetchall()]

with open(output_path, "w", encoding="utf-8") as f:
    for table in tables:
        f.write(f"=== TABLE: {table} ===\n")
        try:
            cursor.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            for row in rows:
                f.write(str(row) + "\n")
        except Exception as e:
            f.write(f"Error reading {table}: {e}\n")
        f.write("\n\n")

print(f"Export complete: {output_path}")