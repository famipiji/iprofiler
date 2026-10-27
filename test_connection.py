import sys
from app import app, db
from sqlalchemy import inspect, text
from urllib.parse import quote_plus


print("Testing connection to dms_iprofiler...")
print("-" * 40)

try:
    with app.app_context():
        # Test 1: Basic connection
        result = db.session.execute(text('SELECT VERSION()')).fetchone()
        print(f"✓ Connected! MySQL version: {result[0]}")

        # Test 2: Correct database
        result = db.session.execute(text('SELECT DATABASE()')).fetchone()
        print(f"✓ Database: {result[0]}")

        # Test 3: Tables exist
        tables = inspect(db.engine).get_table_names()
        print(f"✓ Tables found: {tables}")

        # Test 4: Row counts
        for table in ['documents', 'document_types', 'document_type_fields']:
            if table in tables:
                count = db.session.execute(text(f'SELECT COUNT(*) FROM {table}')).fetchone()[0]
                print(f"  - {table}: {count} rows")
            else:
                print(f"  - {table}: NOT FOUND")

        print("-" * 40)
        print("All tests passed!")

except Exception as e:
    print(f"✗ Connection failed: {e}")
    print("\nCheck:")
    print("  1. MySQL service is running")
    print("  2. Username and password are correct")
    print("  3. Database dms_iprofiler exists")
    print("  4. iprofiler_user has been granted access")
    # This is a standalone diagnostic script (run directly: `python
    # test_connection.py`), not pytest-discovered - the print-and-continue
    # pattern used to exit 0 even on total failure, so a broken DB connection
    # looked like a passing check to anything (a shell script, CI step)
    # inspecting the exit code.
    sys.exit(1)