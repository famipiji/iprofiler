"""
Test script to demonstrate auto-increment behavior when clearing batches.

Tests two scenarios:
1. Full clear: All batches deleted → auto-increment resets to 1
2. Partial clear: Some batches remain (processing) → auto-increment NOT reset
"""

import sys
from datetime import datetime
from app import app, db
from document_model import Batch
from batch_service import clear_all_batches
from sqlalchemy import text

def get_auto_increment_value():
    """Query the current AUTO_INCREMENT value from the batches table."""
    # Create a fresh connection to ensure we read the latest value
    with db.engine.connect() as conn:
        # Force a fresh read by using a new connection
        result = conn.execute(text(
            "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'batches'"
        ))
        row = result.fetchone()
        value = row[0] if row else None
        return value

def get_next_batch_id():
    """
    Get the actual next batch ID that will be assigned by creating
    and immediately deleting a test batch.
    """
    test_batch = Batch(
        client_id="__test_probe__",
        status="open",
        source="test",
        created_at=datetime.utcnow()
    )
    db.session.add(test_batch)
    db.session.flush()  # Get the ID without committing
    next_id = test_batch.id
    db.session.rollback()  # Don't actually create it
    return next_id

def print_section(title):
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def main():
    with app.app_context():
        client_id = "test_tenant"

        # Clean slate - delete all test batches first
        print_section("SETUP: Cleaning existing test batches")
        existing = Batch.query.filter_by(client_id=client_id).all()
        for b in existing:
            db.session.delete(b)
        db.session.commit()
        print(f"Deleted {len(existing)} existing test batches")

        # Show initial state
        total_batches = Batch.query.count()
        auto_inc = get_auto_increment_value()
        print(f"\nInitial state:")
        print(f"  Total batches in DB (all tenants): {total_batches}")
        print(f"  AUTO_INCREMENT value: {auto_inc}")

        # ====================================================================
        # SCENARIO 1: Full clear (all batches deletable)
        # ====================================================================
        print_section("SCENARIO 1: Full Clear (no processing batches)")

        # Create 3 test batches
        print("\nCreating 3 test batches...")
        batch1 = Batch(
            client_id=client_id,
            label="Test Batch 1",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        batch2 = Batch(
            client_id=client_id,
            label="Test Batch 2",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        batch3 = Batch(
            client_id=client_id,
            label="Test Batch 3",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        db.session.add(batch1)
        db.session.add(batch2)
        db.session.add(batch3)
        db.session.commit()
        db.session.flush()  # Ensure all changes are flushed

        print(f"  Created batch #{batch1.id}: {batch1.label}")
        print(f"  Created batch #{batch2.id}: {batch2.label}")
        print(f"  Created batch #{batch3.id}: {batch3.label}")

        # Get auto-increment value (should be next_id = max(id) + 1)
        auto_inc_before = get_auto_increment_value()
        print(f"\nBefore clear:")
        print(f"  Total batches in DB: {Batch.query.count()}")
        print(f"  AUTO_INCREMENT value: {auto_inc_before}")

        # Clear all
        print("\nClearing all batches...")
        result = clear_all_batches(client_id=client_id)
        print(f"  Deleted: {result['deleted_count']} batches")
        print(f"  Skipped: {result['skipped_count']} batches (processing)")
        print(f"  Auto-increment reset: {result['auto_increment_reset']}")
        print(f"  Remaining total: {result['remaining_total']}")

        # Query auto-increment after clearing
        auto_inc_after = get_auto_increment_value()
        next_id = get_next_batch_id()
        print(f"\nAfter clear:")
        print(f"  Total batches in DB: {Batch.query.count()}")
        print(f"  AUTO_INCREMENT (from information_schema): {auto_inc_after}")
        print(f"  Next batch ID (tested via insert probe): {next_id}")
        print(f"\nExpected: Next batch ID should be 1 (table is empty)")
        print(f"Actual: Next batch ID = {next_id}")

        if next_id == 1:
            print("[PASS] Auto-increment correctly reset to 1")
        else:
            print(f"[FAIL] Next batch ID is {next_id}, expected 1")
            if auto_inc_after != next_id:
                print(f"[NOTE] information_schema shows {auto_inc_after}, which differs from actual {next_id}")

        # ====================================================================
        # SCENARIO 2: Partial clear (one batch processing)
        # ====================================================================
        print_section("SCENARIO 2: Partial Clear (one batch processing)")

        # Create 3 more batches
        print("\nCreating 3 new test batches...")
        batch4 = Batch(
            client_id=client_id,
            label="Test Batch 4",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        batch5 = Batch(
            client_id=client_id,
            label="Test Batch 5",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        batch6 = Batch(
            client_id=client_id,
            label="Test Batch 6",
            status="open",
            source="test",
            created_at=datetime.utcnow()
        )
        db.session.add(batch4)
        db.session.add(batch5)
        db.session.add(batch6)
        db.session.commit()

        print(f"  Created batch #{batch4.id}: {batch4.label}")
        print(f"  Created batch #{batch5.id}: {batch5.label}")
        print(f"  Created batch #{batch6.id}: {batch6.label}")

        # Set one batch to 'processing' status
        print(f"\nSetting batch #{batch5.id} to 'processing' status...")
        batch5.status = 'processing'
        db.session.commit()
        print(f"  Batch #{batch5.id} is now processing")

        auto_inc_before_2 = get_auto_increment_value()
        print(f"\nBefore clear:")
        print(f"  Total batches in DB: {Batch.query.count()}")
        print(f"  AUTO_INCREMENT value: {auto_inc_before_2}")

        # Clear all (should skip the processing batch)
        print("\nClearing all batches...")
        result2 = clear_all_batches(client_id=client_id)
        print(f"  Deleted: {result2['deleted_count']} batches")
        print(f"  Skipped: {result2['skipped_count']} batches (processing)")
        print(f"  Auto-increment reset: {result2['auto_increment_reset']}")
        print(f"  Remaining total: {result2['remaining_total']}")

        auto_inc_after_2 = get_auto_increment_value()
        next_id_2 = get_next_batch_id()
        remaining = Batch.query.filter_by(client_id=client_id).all()
        print(f"\nAfter clear:")
        print(f"  Total batches in DB: {Batch.query.count()}")
        print(f"  Remaining batches: {[f'#{b.id} ({b.status})' for b in remaining]}")
        print(f"  AUTO_INCREMENT (from information_schema): {auto_inc_after_2}")
        print(f"  Next batch ID (tested via insert probe): {next_id_2}")

        # The next ID should be > max existing ID
        max_existing_id = max([b.id for b in remaining]) if remaining else 0
        print(f"\nExpected: Next batch ID should be > {max_existing_id} (table not empty)")
        print(f"Actual: Next batch ID = {next_id_2}")

        if next_id_2 > max_existing_id:
            print(f"[PASS] Auto-increment preserved (next ID {next_id_2} > max existing ID {max_existing_id})")
        else:
            print(f"[FAIL] Next batch ID {next_id_2} should be > {max_existing_id}")

        # ====================================================================
        # CLEANUP
        # ====================================================================
        print_section("CLEANUP")
        print("\nCleaning up test data...")

        # Force-delete the remaining processing batch
        remaining = Batch.query.filter_by(client_id=client_id).all()
        for b in remaining:
            db.session.delete(b)
        db.session.commit()
        print(f"Deleted {len(remaining)} remaining test batches")

        # If table is now empty, reset auto-increment
        if Batch.query.count() == 0:
            db.session.execute(text("ALTER TABLE batches AUTO_INCREMENT = 1"))
            db.session.commit()
            print("Reset AUTO_INCREMENT to 1 (table is empty)")

        print("\nTest complete!")

if __name__ == "__main__":
    main()
