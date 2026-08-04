"""Read-only storage inspection for the engineering handoff."""

import glob
import json
import os
import sqlite3


output = {}
for path in glob.glob(os.path.join(os.path.dirname(__file__), "data", "*.db")):
    connection = sqlite3.connect(path)
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    output[os.path.basename(path)] = {"tables": tables}
    for table in tables:
        output[os.path.basename(path)][table] = [
            {"name": row[1], "type": row[2], "primary_key": bool(row[5])}
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
    connection.close()

print(json.dumps(output, indent=2))
