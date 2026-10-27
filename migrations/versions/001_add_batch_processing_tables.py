"""Add Batch, SourceFile, and DocumentJob tables for batch processing

Revision ID: 001_batch_processing
Revises:
Create Date: 2026-09-14 13:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001_batch_processing'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Using raw SQL with IF NOT EXISTS to handle db.create_all() race condition
    # (app.py runs db.create_all() on startup, which could create these tables
    # before the migration runs)

    op.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER NOT NULL AUTO_INCREMENT,
            client_id VARCHAR(100) NOT NULL DEFAULT 'default',
            source VARCHAR(50) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'draft',
            total_jobs INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            split_pending_count INTEGER NOT NULL DEFAULT 0,
            notify_email VARCHAR(255),
            created_at DATETIME NOT NULL,
            completed_at DATETIME,
            PRIMARY KEY (id),
            INDEX ix_batches_status (status)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS source_files (
            id INTEGER NOT NULL AUTO_INCREMENT,
            batch_id INTEGER NOT NULL,
            file_path VARCHAR(500) NOT NULL,
            page_count INTEGER,
            split_mode VARCHAR(50) NOT NULL DEFAULT 'none',
            status VARCHAR(50) NOT NULL DEFAULT 'queued',
            PRIMARY KEY (id),
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
            INDEX ix_source_files_batch_id (batch_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS document_jobs (
            id INTEGER NOT NULL AUTO_INCREMENT,
            batch_id INTEGER NOT NULL,
            source_file_id INTEGER NOT NULL,
            page_range VARCHAR(100),
            file_path VARCHAR(500) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            last_error TEXT,
            claimed_by VARCHAR(100),
            claimed_at DATETIME,
            priority INTEGER NOT NULL DEFAULT 0,
            result_ref INTEGER,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            PRIMARY KEY (id),
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
            FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE CASCADE,
            INDEX ix_document_jobs_batch_id (batch_id),
            INDEX ix_document_jobs_source_file_id (source_file_id),
            INDEX ix_document_jobs_claim_query (status, priority, created_at)
        )
    """)


def downgrade():
    # Drop tables in reverse order (children first)
    op.drop_table('document_jobs')
    op.drop_table('source_files')
    op.drop_table('batches')
