import sqlite3
import json
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional
from models import ApplicationResult

DB_PATH = Path(__file__).parent / "applications.db"

class ApplicationTracker:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE,
                    title TEXT,
                    company TEXT,
                    location TEXT,
                    job_url TEXT,
                    status TEXT,
                    reason TEXT,
                    screening_log TEXT,
                    screenshot_path TEXT,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def is_already_applied(self, job_id: str, title: Optional[str] = None, company: Optional[str] = None) -> bool:
        """Returns True if the job has already been processed or applied to (checked by ID and title+company)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 1. Match by job_id
            if job_id:
                cursor.execute(
                    "SELECT status FROM applications WHERE job_id = ? AND status IN ('applied', 'dry_run_success')",
                    (str(job_id),)
                )
                if cursor.fetchone() is not None:
                    return True

            # 2. Match by normalized (title, company)
            if title and company and company.strip().lower() != "unknown":
                cursor.execute(
                    "SELECT status FROM applications WHERE LOWER(TRIM(title)) = LOWER(TRIM(?)) AND LOWER(TRIM(company)) = LOWER(TRIM(?)) AND status IN ('applied', 'dry_run_success')",
                    (title.strip(), company.strip())
                )
                if cursor.fetchone() is not None:
                    return True

            return False

    def record_application(self, result: ApplicationResult):
        """Records or updates the result of an application attempt."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            screening_json = json.dumps(result.screening_log, ensure_ascii=False)
            cursor.execute("""
                INSERT INTO applications (job_id, title, company, location, job_url, status, reason, screening_log, screenshot_path, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    screening_log = excluded.screening_log,
                    screenshot_path = excluded.screenshot_path,
                    applied_at = excluded.applied_at
            """, (
                result.job_id,
                result.title,
                result.company,
                result.location,
                result.job_url,
                result.status,
                result.reason,
                screening_json,
                result.screenshot_path,
                result.applied_at
            ))
            conn.commit()

    def get_daily_count(self) -> int:
        """Counts how many applications have been successfully submitted (or dry-run passed) today."""
        today_str = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM applications 
                WHERE status IN ('applied', 'dry_run_success')
                AND DATE(applied_at) = DATE(?)
            """, (today_str,))
            row = cursor.fetchone()
            return row[0] if row else 0

    def get_stats(self) -> Dict[str, Any]:
        """Returns summary statistics of all applications."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) FROM applications GROUP BY status")
            breakdown = dict(cursor.fetchall())
            
            cursor.execute("SELECT COUNT(*) FROM applications")
            total = cursor.fetchone()[0]

            return {
                "total_records": total,
                "breakdown": breakdown,
                "today_count": self.get_daily_count()
            }
