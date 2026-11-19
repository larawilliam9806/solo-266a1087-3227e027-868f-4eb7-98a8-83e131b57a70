"""SQLite storage for uploaded boxes, ruling lines and human decisions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class PlacementError(ValueError):
    """A page assignment was rejected; the previous assignment is kept."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])


def connect(path, timeout=15.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL + a busy timeout keep two gunicorn workers from tripping over each
    # other's short writes.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def _begin(connection, immediate=True):
    """Start a write transaction unless one is already open."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")


def _rollback(connection):
    if connection.in_transaction:
        connection.rollback()


def init_db(path):
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(connection)
        connection.commit()
    finally:
        connection.close()


def _migrate(connection):
    """Bring a database created by an older version up to date.

    ``CREATE TABLE IF NOT EXISTS`` adds the new issue/page/cache tables but
    cannot add a column to an existing table, so the version counter that the
    extract cache relies on is added here.
    """
    columns = {row["name"] for row in
               connection.execute("PRAGMA table_info(jobs)").fetchall()}
    if "version" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_job(connection, job_id):
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def list_jobs(connection):
    rows = connection.execute(
        "SELECT j.*, (SELECT COUNT(*) FROM boxes b WHERE b.job_id = j.id) AS box_count,"
        " (SELECT COUNT(*) FROM lines l WHERE l.job_id = j.id) AS line_count,"
        " p.issue_id AS issue_id, i.issue_no AS issue_no, p.page_no AS page_no"
        " FROM jobs j"
        " LEFT JOIN pages p ON p.job_id = j.id"
        " LEFT JOIN issues i ON i.id = p.issue_id"
        " ORDER BY j.id DESC"
    ).fetchall()
    return _rows_to_dicts(rows)


def create_job(connection, filename):
    cursor = connection.execute(
        "INSERT INTO jobs (filename) VALUES (?)", (filename,)
    )
    connection.commit()
    return cursor.lastrowid


def delete_job(connection, job_id):
    connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    connection.commit()


def touch_job(connection, job_id):
    """Mark a job's saved version as changed (bumps its extract cache key)."""
    connection.execute(
        "UPDATE jobs SET version = version + 1 WHERE id = ?", (job_id,)
    )


# ---------------------------------------------------------------------------
# Issues and page placement
# ---------------------------------------------------------------------------

def list_issues(connection):
    rows = connection.execute(
        "SELECT i.*,"
        " (SELECT COUNT(*) FROM pages p WHERE p.issue_id = i.id) AS mounted,"
        " (SELECT COUNT(*) FROM pages p"
        "   WHERE p.issue_id = i.id AND p.page_no > i.total_pages) AS out_of_range"
        " FROM issues i ORDER BY i.issue_no"
    ).fetchall()
    return _rows_to_dicts(rows)


def get_issue(connection, issue_id):
    row = connection.execute(
        "SELECT * FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    return dict(row) if row else None


def create_issue(connection, issue_no, total_pages):
    _begin(connection)
    try:
        cursor = connection.execute(
            "INSERT INTO issues (issue_no, total_pages) VALUES (?, ?)",
            (issue_no, total_pages),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise PlacementError(f"期号“{issue_no}”已存在") from None
    return cursor.lastrowid


def update_issue(connection, issue_id, total_pages):
    """Change an issue's page count.

    Lowering the count below a mounted page is refused: it would leave a task
    hanging off the end of the issue and silently skew the “published” rule.
    """
    _begin(connection)
    try:
        issue = get_issue(connection, issue_id)
        if issue is None:
            raise PlacementError("期号不存在")
        row = connection.execute(
            "SELECT MAX(page_no) AS highest FROM pages WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        highest = row["highest"] or 0
        if total_pages < highest:
            raise PlacementError(
                f"总页数不能少于已挂页的最大页码 {highest}",
                ["请先把第 %d 页的任务换到别处或删除" % highest],
            )
        connection.execute(
            "UPDATE issues SET total_pages = ? WHERE id = ?", (total_pages, issue_id)
        )
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    return get_issue(connection, issue_id)


def delete_issue(connection, issue_id):
    connection.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
    connection.commit()


def get_page(connection, page_id):
    row = connection.execute(
        "SELECT * FROM pages WHERE id = ?", (page_id,)
    ).fetchone()
    return dict(row) if row else None


def page_of_job(connection, job_id):
    row = connection.execute(
        "SELECT p.*, i.issue_no, i.total_pages FROM pages p"
        " JOIN issues i ON i.id = p.issue_id WHERE p.job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def list_pages(connection, issue_id):
    rows = connection.execute(
        "SELECT p.*, j.filename, j.version AS job_version"
        " FROM pages p JOIN jobs j ON j.id = p.job_id"
        " WHERE p.issue_id = ? ORDER BY p.page_no", (issue_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def place_job(connection, job_id, issue_id, page_no):
    """Attach a job to one page (insert, replace, or move in one transaction).

    A page holds at most one task and a task is mounted at most once.  When
    the page already carries another task that task is replaced; the caller
    is told which one via the return value so it can report it.
    """
    _begin(connection)
    try:
        issue = get_issue(connection, issue_id)
        if issue is None:
            raise PlacementError("期号不存在")
        if not isinstance(page_no, int) or page_no < 1:
            raise PlacementError("页码必须是正整数")
        if page_no > issue["total_pages"]:
            raise PlacementError(
                f"页码 {page_no} 超出该期总页数 {issue['total_pages']}",
                ["请先修正期号的总页数，或改挂到范围内的页码"],
            )
        job = get_job(connection, job_id)
        if job is None:
            raise PlacementError("任务不存在")

        occupied = connection.execute(
            "SELECT p.id, p.job_id, j.filename FROM pages p"
            " JOIN jobs j ON j.id = p.job_id"
            " WHERE p.issue_id = ? AND p.page_no = ?",
            (issue_id, page_no),
        ).fetchone()
        displaced = None
        if occupied is not None and occupied["job_id"] != job_id:
            displaced = {
                "job_id": occupied["job_id"], "filename": occupied["filename"],
                "page_no": page_no, "issue_id": issue_id,
            }
            # This is a page swap; the previous holder becomes uncatalogued.
            connection.execute("DELETE FROM pages WHERE id = ?", (occupied["id"],))

        # A task can only be in one place: drop its old mounting.
        connection.execute("DELETE FROM pages WHERE job_id = ?", (job_id,))
        connection.execute(
            "INSERT INTO pages (issue_id, page_no, job_id) VALUES (?, ?, ?)",
            (issue_id, page_no, job_id),
        )
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    return {"displaced": displaced, "page": page_of_job(connection, job_id),
            "filename": job["filename"]}


def clear_job_page(connection, job_id):
    connection.execute("DELETE FROM pages WHERE job_id = ?", (job_id,))
    connection.commit()


def delete_page(connection, page_id):
    connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))
    connection.commit()


# ---------------------------------------------------------------------------
# Extract cache
# ---------------------------------------------------------------------------

def get_extract(connection, job_id, version):
    row = connection.execute(
        "SELECT * FROM job_extracts WHERE job_id = ? AND job_version = ?",
        (job_id, version),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    try:
        record["rows"] = json.loads(record.pop("rows_text") or "[]")
    except ValueError:
        return None
    return record


def save_extract(connection, job_id, version, grid_valid, pending, box_count,
                 note, rows):
    """Store an extract only while the job is still at ``version``.

    The conditional ``SELECT`` makes this a single atomic statement: if the
    job changed after the snapshot was taken, nothing is cached and the next
    request recomputes from the newer data.
    """
    cursor = connection.execute(
        "INSERT OR REPLACE INTO job_extracts"
        " (job_id, job_version, grid_valid, pending, box_count, note, rows_text)"
        " SELECT ?, ?, ?, ?, ?, ?, ? FROM jobs WHERE id = ? AND version = ?",
        (job_id, version, int(bool(grid_valid)), int(pending), int(box_count),
         note or "", json.dumps(rows, ensure_ascii=False), job_id, version),
    )
    connection.commit()
    return cursor.rowcount > 0


def load_job_snapshot(connection, job_id):
    """Read everything needed for one extract from a single snapshot.

    The job row, its boxes, grid lines and decisions are read inside one read
    transaction, so a concurrent edit can never mix versions into one result.
    """
    _begin(connection, immediate=False)
    try:
        job = get_job(connection, job_id)
        if job is None:
            _rollback(connection)
            return None
        snapshot = {
            "version": job["version"],
            "boxes": get_boxes(connection, job_id),
            "lines": get_lines(connection, job_id),
            "decisions": get_decisions(connection, job_id),
        }
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    return snapshot


def invalidate_extract(connection, job_id):
    """Drop a job's cached extract; callers inside a transaction omit commit."""
    connection.execute(
        "DELETE FROM job_extracts WHERE job_id = ?", (job_id,)
    )


def replace_box(connection, job_id, row):
    """Insert one OCR box; UNIQUE(job_id,row_no) makes re-upload idempotent."""
    connection.execute(
        "INSERT OR REPLACE INTO boxes"
        " (job_id, row_no, box_ref, text, x1, y1, x2, y2)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, row["row_no"], row["box_ref"], row["text"],
         row["x1"], row["y1"], row["x2"], row["y2"]),
    )


def add_boxes(connection, job_id, rows):
    _begin(connection)
    try:
        for row in rows:
            replace_box(connection, job_id, row)
        touch_job(connection, job_id)
        invalidate_extract(connection, job_id)
        connection.commit()
    except Exception:
        _rollback(connection)
        raise


def get_boxes(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM boxes WHERE job_id = ? ORDER BY row_no", (job_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def set_lines(connection, job_id, horizontal, vertical):
    _begin(connection)
    try:
        connection.execute("DELETE FROM lines WHERE job_id = ?", (job_id,))
        for position in horizontal:
            connection.execute(
                "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'h', ?)",
                (job_id, position),
            )
        for position in vertical:
            connection.execute(
                "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'v', ?)",
                (job_id, position),
            )
        touch_job(connection, job_id)
        invalidate_extract(connection, job_id)
        connection.commit()
    except Exception:
        _rollback(connection)
        raise


def get_lines(connection, job_id):
    rows = connection.execute(
        "SELECT orientation, position FROM lines WHERE job_id = ?"
        " ORDER BY orientation, position", (job_id,)
    ).fetchall()
    horizontal = [row["position"] for row in rows if row["orientation"] == "h"]
    vertical = [row["position"] for row in rows if row["orientation"] == "v"]
    return horizontal, vertical


def get_decisions(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM decisions WHERE job_id = ?", (job_id,)
    ).fetchall()
    decisions = {}
    for row in rows:
        record = dict(row)
        try:
            record["overlap_cells"] = json.loads(record["overlap_cells"] or "[]")
        except ValueError:
            record["overlap_cells"] = []
        decisions[record["box_id"]] = record
    return decisions


def set_decision(connection, job_id, box_id, kind, target_cell, overlap_cells):
    _begin(connection)
    try:
        connection.execute(
            "INSERT INTO decisions"
            " (job_id, box_id, kind, target_cell, overlap_cells)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (job_id, box_id) DO UPDATE SET"
            " kind = excluded.kind, target_cell = excluded.target_cell,"
            " overlap_cells = excluded.overlap_cells,"
            " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (job_id, box_id, kind, target_cell,
             json.dumps(sorted(overlap_cells), ensure_ascii=False)),
        )
        touch_job(connection, job_id)
        invalidate_extract(connection, job_id)
        connection.commit()
    except Exception:
        _rollback(connection)
        raise


def clear_decision(connection, job_id, box_id):
    _begin(connection)
    try:
        connection.execute(
            "DELETE FROM decisions WHERE job_id = ? AND box_id = ?", (job_id, box_id)
        )
        touch_job(connection, job_id)
        invalidate_extract(connection, job_id)
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
