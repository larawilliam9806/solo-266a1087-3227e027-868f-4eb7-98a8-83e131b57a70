PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    -- Bumped on every change to the boxes, lines or decisions.  The compare
    -- view caches a page's extracted rows under (job_id, version), so a
    -- repeated query never has to re-place pages that did not change.
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- One entry per newspaper issue ("期号") and the total number of pages in
-- that issue's price list.
CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_no    TEXT    NOT NULL UNIQUE,
    total_pages INTEGER NOT NULL CHECK (total_pages >= 1),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Attach an existing entry job to one page of one issue.
--   UNIQUE (issue_id, page_no): each page carries at most one task.
--   UNIQUE (job_id):            each task is mounted in at most one place.
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    job_id      INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (issue_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_pages_issue ON pages(issue_id, page_no);

CREATE TABLE IF NOT EXISTS boxes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_no      INTEGER NOT NULL,
    box_ref     TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL DEFAULT '',
    x1          REAL    NOT NULL,
    y1          REAL    NOT NULL,
    x2          REAL    NOT NULL,
    y2          REAL    NOT NULL,
    UNIQUE (job_id, row_no)
);
CREATE INDEX IF NOT EXISTS idx_boxes_job ON boxes(job_id);

CREATE TABLE IF NOT EXISTS lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    orientation TEXT    NOT NULL CHECK (orientation IN ('h','v')),
    position    REAL    NOT NULL,
    UNIQUE (job_id, orientation, position)
);
CREATE INDEX IF NOT EXISTS idx_lines_job ON lines(job_id);

-- Human decisions for boxes that do not sit wholly inside one cell.
-- `overlap_cells` is the JSON list of cell keys (left|top|right|bottom) that
-- intersected the box when the decision was made.  After any line edit the
-- list is recomputed: an unchanged list means the intersecting cells and all
-- of their boundaries are unchanged, so the decision can be reused.
CREATE TABLE IF NOT EXISTS decisions (
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    box_id       INTEGER NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL CHECK (kind IN ('cell','exclude')),
    target_cell  TEXT    NOT NULL DEFAULT '',
    overlap_cells TEXT   NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (job_id, box_id)
);

-- Cache of the rows extracted from one saved job version, plus the
-- export-readiness of that version.  An entry is reused only while
-- jobs.version is unchanged, so editing the grid, resolving a text box,
-- moving the task to another page or deleting it can never leave a stale
-- price in the comparison view.  Rows are additionally filtered per request
-- against the pages still mounted in the issue, so a task that was moved
-- away is withdrawn from the old issue immediately.
CREATE TABLE IF NOT EXISTS job_extracts (
    job_id       INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    job_version  INTEGER NOT NULL,
    grid_valid   INTEGER NOT NULL DEFAULT 0,
    pending      INTEGER NOT NULL DEFAULT 0,
    box_count    INTEGER NOT NULL DEFAULT 0,
    note         TEXT    NOT NULL DEFAULT '',
    rows_text    TEXT    NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
