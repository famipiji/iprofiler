"""
Unit tests for batch_service.py

Tests the batch creation and status management functions without requiring
a running LM Studio/Ollama server - just the database.
"""

import unittest
import os
import tempfile
from datetime import datetime

from app import app
from extensions import db
from document_model import Batch, SourceFile, DocumentJob
import batch_service


class BatchServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up test database - runs once before all tests in this class."""
        # Use the same MySQL DB as the main app, but ensure we're in test mode
        cls.app = app
        cls.app_context = cls.app.app_context()
        cls.app_context.push()

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        cls.app_context.pop()

    def setUp(self):
        """Clean test data before each test."""
        # Clean up any test batches/jobs from previous runs
        DocumentJob.query.filter(DocumentJob.batch_id.in_(
            db.session.query(Batch.id).filter(Batch.client_id == "test_client")
        )).delete(synchronize_session=False)
        SourceFile.query.filter(SourceFile.batch_id.in_(
            db.session.query(Batch.id).filter(Batch.client_id == "test_client")
        )).delete(synchronize_session=False)
        Batch.query.filter_by(client_id="test_client").delete()
        db.session.commit()

    def tearDown(self):
        """Clean up after each test."""
        db.session.rollback()
        # Clean up test data
        DocumentJob.query.filter(DocumentJob.batch_id.in_(
            db.session.query(Batch.id).filter(Batch.client_id == "test_client")
        )).delete(synchronize_session=False)
        SourceFile.query.filter(SourceFile.batch_id.in_(
            db.session.query(Batch.id).filter(Batch.client_id == "test_client")
        )).delete(synchronize_session=False)
        Batch.query.filter_by(client_id="test_client").delete()
        db.session.commit()

    def test_create_batch_with_three_files_split_none(self):
        """
        Task 2 verification step: create a batch with 3 files, split_mode='none',
        and assert 3 DocumentJob rows exist with Batch.total_jobs == 3.
        """
        # Create temporary file paths (we don't need real files for this test)
        with tempfile.TemporaryDirectory() as tmpdir:
            files = [
                {"file_path": os.path.join(tmpdir, "file1.pdf"), "page_count": 5},
                {"file_path": os.path.join(tmpdir, "file2.pdf"), "page_count": 3},
                {"file_path": os.path.join(tmpdir, "file3.pdf"), "page_count": 10},
            ]

            batch = batch_service.create_batch(
                files=files,
                source="manual_upload",
                client_id="test_client",
                split_mode="none",
            )

            # Verify batch was created
            self.assertIsNotNone(batch.id)
            self.assertEqual(batch.client_id, "test_client")
            self.assertEqual(batch.source, "manual_upload")
            self.assertEqual(batch.status, "open")
            self.assertEqual(batch.total_jobs, 3)
            self.assertEqual(batch.completed_count, 0)
            self.assertEqual(batch.failed_count, 0)

            # Verify SourceFile rows were created
            source_files = SourceFile.query.filter_by(batch_id=batch.id).all()
            self.assertEqual(len(source_files), 3)
            for sf in source_files:
                self.assertEqual(sf.split_mode, "none")
                self.assertEqual(sf.status, "queued")

            # Verify DocumentJob rows were created (one per file for split_mode='none')
            jobs = DocumentJob.query.filter_by(batch_id=batch.id).all()
            self.assertEqual(len(jobs), 3)
            for job in jobs:
                self.assertEqual(job.status, "queued")
                self.assertEqual(job.attempts, 0)
                self.assertEqual(job.max_attempts, 3)
                self.assertIsNone(job.page_range)  # Whole file
                self.assertEqual(job.priority, 0)

    def test_create_batch_rejects_empty_files_list(self):
        """Cannot create a batch with zero files."""
        with self.assertRaises(ValueError) as ctx:
            batch_service.create_batch(
                files=[],
                source="manual_upload",
                client_id="test_client",
            )
        self.assertIn("zero files", str(ctx.exception))

    def test_create_batch_rejects_unsupported_split_mode(self):
        """Phase 1 only supports split_mode='none'."""
        with self.assertRaises(NotImplementedError) as ctx:
            batch_service.create_batch(
                files=[{"file_path": "/tmp/test.pdf"}],
                source="manual_upload",
                client_id="test_client",
                split_mode="mrn_barcode",  # Phase 2 feature
            )
        self.assertIn("Phase 2", str(ctx.exception))

    def test_get_batch_status(self):
        """get_batch_status returns rollup fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch = batch_service.create_batch(
                files=[{"file_path": os.path.join(tmpdir, "test.pdf")}],
                source="api",
                client_id="test_client",
                notify_email="test@example.com",
            )

            status = batch_service.get_batch_status(batch.id)

            self.assertEqual(status["id"], batch.id)
            self.assertEqual(status["client_id"], "test_client")
            self.assertEqual(status["source"], "api")
            self.assertEqual(status["status"], "open")
            self.assertEqual(status["total_jobs"], 1)
            self.assertEqual(status["completed_count"], 0)
            self.assertEqual(status["failed_count"], 0)
            self.assertEqual(status["notify_email"], "test@example.com")
            self.assertIsNotNone(status["created_at"])

    def test_get_batch_status_nonexistent_batch(self):
        """get_batch_status returns None for nonexistent batch."""
        status = batch_service.get_batch_status(999999)
        self.assertIsNone(status)

    def test_update_batch_status_marks_completed(self):
        """update_batch_status marks batch as completed when all jobs succeed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch = batch_service.create_batch(
                files=[
                    {"file_path": os.path.join(tmpdir, "f1.pdf")},
                    {"file_path": os.path.join(tmpdir, "f2.pdf")},
                ],
                source="manual_upload",
                client_id="test_client",
            )

            # Simulate both jobs completing successfully
            jobs = DocumentJob.query.filter_by(batch_id=batch.id).all()
            for job in jobs:
                job.status = "succeeded"
                job.finished_at = datetime.utcnow()
            db.session.commit()

            # Update batch status
            batch_service.update_batch_status(batch.id)

            # Verify batch is now completed
            batch = Batch.query.get(batch.id)
            self.assertEqual(batch.status, "completed")
            self.assertEqual(batch.completed_count, 2)
            self.assertEqual(batch.failed_count, 0)
            self.assertIsNotNone(batch.completed_at)

    def test_update_batch_status_marks_completed_with_errors(self):
        """update_batch_status marks batch as completed_with_errors when some jobs fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch = batch_service.create_batch(
                files=[
                    {"file_path": os.path.join(tmpdir, "f1.pdf")},
                    {"file_path": os.path.join(tmpdir, "f2.pdf")},
                ],
                source="manual_upload",
                client_id="test_client",
            )

            # Simulate one job succeeding, one failing
            jobs = DocumentJob.query.filter_by(batch_id=batch.id).all()
            jobs[0].status = "succeeded"
            jobs[1].status = "dead_letter"
            jobs[1].last_error = "Failed after 3 attempts"
            db.session.commit()

            # Update batch status
            batch_service.update_batch_status(batch.id)

            # Verify batch is completed_with_errors
            batch = Batch.query.get(batch.id)
            self.assertEqual(batch.status, "completed_with_errors")
            self.assertEqual(batch.completed_count, 1)
            self.assertEqual(batch.failed_count, 1)
            self.assertIsNotNone(batch.completed_at)

    def test_update_batch_status_marks_processing(self):
        """update_batch_status marks batch as processing when some jobs are done but others pending."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch = batch_service.create_batch(
                files=[
                    {"file_path": os.path.join(tmpdir, "f1.pdf")},
                    {"file_path": os.path.join(tmpdir, "f2.pdf")},
                ],
                source="manual_upload",
                client_id="test_client",
            )

            # Simulate one job completed, one still queued
            jobs = DocumentJob.query.filter_by(batch_id=batch.id).all()
            jobs[0].status = "succeeded"
            jobs[1].status = "queued"  # Still pending
            db.session.commit()

            # Update batch status
            batch_service.update_batch_status(batch.id)

            # Verify batch is processing (not completed yet)
            batch = Batch.query.get(batch.id)
            self.assertEqual(batch.status, "processing")
            self.assertEqual(batch.completed_count, 1)
            self.assertEqual(batch.failed_count, 0)
            self.assertIsNone(batch.completed_at)  # Not done yet


if __name__ == "__main__":
    unittest.main()
