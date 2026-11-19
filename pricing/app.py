"""Flask application: old-newspaper price-list entry.

Upload one page of OCR CSV, draw/confirm the ruling lines that split the page
into 品名 / 规格 / 批发价 columns, resolve every box that does not sit wholly
inside a single cell, then download the three-column CSV.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db
from . import layout
from .csvparse import CsvError, parse_csv

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get("DATABASE", "pricing.sqlite3"),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    if config:
        app.config.update(config)

    database = app.config["DATABASE"]
    if database != ":memory:":
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    db.init_db(database)

    # ------------------------------------------------------------------
    # Per-request connection
    # ------------------------------------------------------------------
    def get_db():
        if "db" not in g:
            g.db = db.connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exception=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def load_job_or_404(job_id):
        job = db.get_job(get_db(), job_id)
        if job is None:
            abort(404)
        return job

    # ------------------------------------------------------------------
    # State computation shared by pages and API
    # ------------------------------------------------------------------
    def job_state(job_id):
        connection = get_db()
        job = load_job_or_404(job_id)
        boxes = db.get_boxes(connection, job_id)
        horizontal, vertical = db.get_lines(connection, job_id)
        invalid = None
        grid = None
        if len(vertical) == 4 and len(horizontal) >= 2:
            grid = layout.Grid(horizontal, vertical)
        else:
            invalid = _invalid_message(len(horizontal), len(vertical))

        decisions = db.get_decisions(connection, job_id)
        if grid is None:
            evaluated = [
                {
                    "box_id": box["id"], "box_ref": box["box_ref"],
                    "text": box["text"], "x1": box["x1"], "y1": box["y1"],
                    "x2": box["x2"], "y2": box["y2"],
                    "status": "pending", "status_label": "待划格线",
                    "row": None, "col": None, "column": None,
                    "cell_key": "", "candidates": [], "decision": None,
                    "manual": False, "note": "格线无效，暂不归位",
                }
                for box in boxes
            ]
            pending = sum(1 for item in evaluated if (item["text"] or "").strip())
            rows = []
            if not invalid:
                invalid = "尚未确认格线"
        else:
            evaluated = layout.evaluate(grid, boxes, decisions)
            pending = layout.pending_count(evaluated)
            rows = layout.build_rows(evaluated)

        status_counts = {}
        for item in evaluated:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

        return {
            "job": job,
            "boxes": evaluated,
            "horizontal": horizontal,
            "vertical": vertical,
            "grid_valid": grid is not None,
            "invalid": invalid,
            "pending": pending,
            "rows": rows,
            "status_counts": status_counts,
            "can_export": grid is not None and pending == 0 and bool(boxes),
            "cells": grid.cells() if grid is not None else [],
        }

    def job_extract(job_id):
        """Exportable rows of a job with their page/row location, cached.

        A saved job version is extracted once; repeating the comparison does
        not re-place any page whose content did not change.  Any change to the
        job (grid lines, a box decision, a re-upload) bumps ``jobs.version``
        and invalidates the cached entry, so no stale price can survive a
        grid change, a page swap or a deleted task.
        """
        connection = get_db()
        job = db.get_job(connection, job_id)
        if job is None:
            return None
        cached = db.get_extract(connection, job_id, job["version"])
        if cached is not None:
            cached["cached"] = True
            return cached

        snapshot = db.load_job_snapshot(connection, job_id)
        if snapshot is None:
            return None
        boxes = snapshot["boxes"]
        horizontal, vertical = snapshot["lines"]
        if len(vertical) == 4 and len(horizontal) >= 2:
            grid = layout.Grid(horizontal, vertical)
        else:
            grid = None
        if grid is None:
            pending = sum(1 for box in boxes if (box["text"] or "").strip())
            rows = []
            note = _invalid_message(len(horizontal), len(vertical))
        else:
            evaluated = layout.evaluate(grid, boxes, snapshot["decisions"])
            pending = layout.pending_count(evaluated)
            rows = layout.build_rows(evaluated)
            note = ""
        extract = {
            "job_id": job_id,
            "job_version": snapshot["version"],
            "grid_valid": grid is not None,
            "pending": pending,
            "box_count": len(boxes),
            "note": note,
            "rows": [
                {"row_no": row["row"], "name": row["name"],
                 "spec": row["spec"], "price": row["price"]}
                for row in rows
            ],
            "cached": False,
        }
        db.save_extract(connection, job_id, snapshot["version"],
                        extract["grid_valid"], pending, len(boxes), note,
                        extract["rows"])
        return extract

    def issue_coverage(issue):
        """A single issue's mounted pages plus the rows each page exports.

        Each page's status and its exported prices come from one extract, so
        the prices, the missing pages and the unfinished pages shown for an
        issue always describe the same snapshot.
        """
        connection = get_db()
        pages = db.list_pages(connection, issue["id"])
        extracted = []
        for page in pages:
            extract = job_extract(page["job_id"])
            if extract is None:
                # The task was deleted between listing the page and reading
                # it; skip it rather than failing the whole comparison.
                continue
            extracted.append({"page": page, "extract": extract})
        coverage = layout.issue_page_coverage([
            {
                "page_no": entry["page"]["page_no"],
                "job_id": entry["page"]["job_id"],
                "filename": entry["page"]["filename"],
                "grid_valid": entry["extract"]["grid_valid"],
                "pending": entry["extract"]["pending"],
                "box_count": entry["extract"]["box_count"],
                "invalid": entry["extract"]["note"],
            }
            for entry in extracted
        ], issue["total_pages"])
        rows = []
        for entry in extracted:
            # Only a page that already meets the export condition contributes
            # prices; a half-checked page must never look published.
            if not (entry["extract"]["grid_valid"]
                    and entry["extract"]["pending"] == 0
                    and entry["extract"]["box_count"] > 0):
                continue
            for row in entry["extract"]["rows"]:
                rows.append({
                    "page_no": entry["page"]["page_no"],
                    "row_no": row["row_no"],
                    "name": row["name"],
                    "spec": row["spec"],
                    "price": row["price"],
                })
        # Flat structure understood directly by layout.compare_issues().
        return {
            **coverage,
            "issue_id": issue["id"],
            "issue_no": issue["issue_no"],
            "mounted_pages": pages,
            "rows": rows,
        }

    def _invalid_message(h_count, v_count):
        parts = []
        if v_count != 4:
            parts.append(f"竖线为 {v_count} 条，需要恰好 4 条")
        if h_count < 2:
            parts.append(f"横线为 {h_count} 条，至少需要 2 条")
        return "格线无效：" + "；".join(parts) if parts else "格线无效"

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        connection = get_db()
        jobs = db.list_jobs(connection)
        issues = db.list_issues(connection)
        return render_template("index.html", jobs=jobs, issues=issues)

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return jsonify({"ok": True})

    @app.post("/upload")
    def upload():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            flash("请选择一个 CSV 文件", "error")
            return redirect(url_for("index"))
        filename = os.path.basename(uploaded.filename)
        raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            flash("文件过大（上限 10MB）", "error")
            return redirect(url_for("index"))
        try:
            parsed = parse_csv(raw)
        except CsvError as error:
            for line in [error.message] + error.details:
                flash(line, "error")
            return redirect(url_for("index"))

        connection = get_db()
        job_id = db.create_job(connection, filename)
        db.add_boxes(connection, job_id, parsed.boxes)
        horizontal, vertical = _suggest_lines(parsed.boxes)
        db.set_lines(connection, job_id, horizontal, vertical)
        for warning in parsed.warnings[:20]:
            flash(warning, "warning")
        flash(f"已导入 {len(parsed.boxes)} 个文字框，请核对格线", "success")
        return redirect(url_for("job_view", job_id=job_id))

    @app.get("/jobs/<int:job_id>")
    def job_view(job_id):
        job = load_job_or_404(job_id)
        state = job_state(job_id)
        issues = db.list_issues(get_db())
        placement = db.page_of_job(get_db(), job_id)
        return render_template(
            "job.html", job=job, state=state, state_json=_json_state(state),
            issues=issues, placement=placement,
        )

    @app.post("/jobs/<int:job_id>/delete")
    def job_delete(job_id):
        load_job_or_404(job_id)
        db.delete_job(get_db(), job_id)
        flash("已删除该任务", "success")
        return redirect(url_for("index"))

    # ------------------------------------------------------------------
    # Issues and page placement
    # ------------------------------------------------------------------
    def _parse_issue_form():
        issue_no = " ".join((request.form.get("issue_no") or "").split())
        pages_raw = (request.form.get("total_pages") or "").strip()
        if not issue_no:
            raise ValueError("请填写期号")
        if len(issue_no) > 80:
            raise ValueError("期号过长（最多 80 个字符）")
        try:
            total_pages = int(pages_raw)
        except ValueError:
            raise ValueError("总页数必须是正整数") from None
        if total_pages < 1:
            raise ValueError("总页数必须是正整数")
        return issue_no, total_pages

    @app.post("/issues")
    def issue_create():
        try:
            issue_no, total_pages = _parse_issue_form()
            db.create_issue(get_db(), issue_no, total_pages)
        except (ValueError, db.PlacementError) as error:
            flash(getattr(error, "message", str(error)), "error")
            return redirect(url_for("index"))
        flash(f"已登记第 {issue_no} 期，共 {total_pages} 页", "success")
        return redirect(url_for("index"))

    @app.post("/issues/<int:issue_id>/pages")
    def issue_place(issue_id):
        """Mount an existing task on one page of this issue (add/swap)."""
        if db.get_issue(get_db(), issue_id) is None:
            abort(404)
        try:
            job_id = int(request.form.get("job_id"))
            page_no = int(request.form.get("page_no"))
        except (TypeError, ValueError):
            flash("请选择任务并填写页码", "error")
            return redirect(url_for("issue_view", issue_id=issue_id))
        try:
            result = db.place_job(get_db(), job_id, issue_id, page_no)
        except db.PlacementError as error:
            flash(error.message, "error")
            for line in error.details:
                flash(line, "error")
            # Placement failed: nothing moved, the previous state is kept.
            return redirect(url_for("issue_view", issue_id=issue_id))
        message = f"已把《{result['filename']}》挂到第 {page_no} 页"
        if result["displaced"] is not None:
            message += (f"；原任务《{result['displaced']['filename']}》已退回未挂页")
        flash(message, "success")
        return redirect(url_for("issue_view", issue_id=issue_id))

    @app.post("/issues/<int:issue_id>/total-pages")
    def issue_update(issue_id):
        if db.get_issue(get_db(), issue_id) is None:
            abort(404)
        try:
            total_pages = int((request.form.get("total_pages") or "").strip())
            if total_pages < 1:
                raise ValueError
        except ValueError:
            flash("总页数必须是正整数", "error")
            return redirect(request.referrer or url_for("index"))
        try:
            db.update_issue(get_db(), issue_id, total_pages)
        except db.PlacementError as error:
            flash(error.message, "error")
            for line in error.details:
                flash(line, "error")
            return redirect(request.referrer or url_for("index"))
        flash(f"总页数已改为 {total_pages}", "success")
        return redirect(request.referrer or url_for("index"))

    @app.post("/issues/<int:issue_id>/delete")
    def issue_delete(issue_id):
        if db.get_issue(get_db(), issue_id) is None:
            abort(404)
        db.delete_issue(get_db(), issue_id)
        flash("已删除该期（任务本身保留，回到未挂页状态）", "success")
        return redirect(request.referrer or url_for("index"))

    @app.get("/issues/<int:issue_id>")
    def issue_view(issue_id):
        issue = db.get_issue(get_db(), issue_id)
        if issue is None:
            abort(404)
        connection = get_db()
        pages = db.list_pages(connection, issue_id)
        extracts = {}
        for page in pages:
            extract = job_extract(page["job_id"])
            if extract is not None:
                extracts[page["job_id"]] = extract
        pages = [page for page in pages if page["job_id"] in extracts]
        coverage = layout.issue_page_coverage([
            {"page_no": page["page_no"], "job_id": page["job_id"],
             "filename": page["filename"],
             "grid_valid": extracts[page["job_id"]]["grid_valid"],
             "pending": extracts[page["job_id"]]["pending"],
             "box_count": extracts[page["job_id"]]["box_count"],
             "invalid": extracts[page["job_id"]]["note"]}
            for page in pages
        ], issue["total_pages"])
        return render_template(
            "issue.html", issue=issue, pages=pages, extracts=extracts,
            jobs=db.list_jobs(connection), coverage=coverage,
        )

    def _place(job_id, issue_id, page_no):
        """Shared placement endpoint body; keeps the old result on failure."""
        try:
            result = db.place_job(get_db(), job_id, issue_id, page_no)
        except db.PlacementError as error:
            return jsonify({
                "ok": False, "error": error.message, "details": error.details,
            }), 409
        displaced = result["displaced"]
        message = f"已挂到第 {result['page']['page_no']} 页"
        if displaced is not None:
            message += f"（原第 {displaced['page_no']} 页的《{displaced['filename']}》已撤下）"
        return jsonify({"ok": True, "message": message, "placement": result["page"],
                        "displaced": displaced})

    @app.post("/api/jobs/<int:job_id>/placement")
    def api_place_job(job_id):
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        try:
            issue_id = int(payload.get("issue_id"))
            page_no = int(payload.get("page_no"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "请选择期号和页码", "details": []}), 400
        return _place(job_id, issue_id, page_no)

    @app.post("/jobs/<int:job_id>/placement")
    def job_place(job_id):
        load_job_or_404(job_id)
        try:
            issue_id = int(request.form.get("issue_id"))
            page_no = int(request.form.get("page_no"))
        except (TypeError, ValueError):
            flash("请选择期号和页码", "error")
            return redirect(url_for("job_view", job_id=job_id))
        try:
            result = db.place_job(get_db(), job_id, issue_id, page_no)
        except db.PlacementError as error:
            flash(error.message, "error")
            for line in error.details:
                flash(line, "error")
            # Placement failed: the previous assignment is still in place.
            return redirect(url_for("job_view", job_id=job_id))
        displaced = result["displaced"]
        message = f"已挂到第 {result['page']['page_no']} 页"
        if displaced is not None:
            message += f"（原第 {displaced['page_no']} 页的《{displaced['filename']}》已撤下）"
        flash(message, "success")
        return redirect(url_for("job_view", job_id=job_id))

    @app.post("/jobs/<int:job_id>/unplace")
    def job_unplace(job_id):
        load_job_or_404(job_id)
        db.clear_job_page(get_db(), job_id)
        flash("已从页码上撤下（任务保留）", "success")
        return redirect(url_for("job_view", job_id=job_id))

    @app.post("/pages/<int:page_id>/delete")
    def page_delete(page_id):
        page = db.get_page(get_db(), page_id)
        if page is None:
            abort(404)
        db.delete_page(get_db(), page_id)
        flash("已撤下该页的任务（任务本身保留）", "success")
        return redirect(request.referrer or url_for("index"))

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------
    def compare_report(issue_a, issue_b):
        """Coverage plus comparison rows, both derived from the same extracts.

        The page list and each page's extract are read first; coverage and
        rows are then computed from that one ``extracted`` list, so the
        prices, missing pages and unfinished pages in a single response are
        always consistent with each other.
        """
        issue_a = issue_coverage(issue_a)
        issue_b = issue_coverage(issue_b)
        return (layout.compare_issues([issue_a, issue_b]),
                {issue_a["issue_id"]: issue_a, issue_b["issue_id"]: issue_b})

    @app.get("/compare")
    def compare():
        connection = get_db()
        issues = db.list_issues(connection)
        selected = []
        raw_values = [request.args.get("a"), request.args.get("b")]
        if not any(raw_values) and request.args.get("issues"):
            raw_values = (request.args.get("issues") or "").split(",")
        for raw in raw_values:
            raw = (raw or "").strip()
            if raw.isdigit() and int(raw) not in selected:
                selected.append(int(raw))
        if len(selected) > 2:
            error = "一次只能对照两期"
            selected = selected[:2]
        else:
            error = None
        report = None
        coverage_map = {}
        if len(selected) == 2:
            known = {issue["id"]: issue for issue in issues}
            if all(issue_id in known for issue_id in selected):
                report, coverage_map = compare_report(
                    known[selected[0]], known[selected[1]])
            else:
                error = error or "所选期号不存在"
        elif selected and error is None:
            error = "请选择两期进行对照"
        return render_template(
            "compare.html", issues=issues, selected=selected, report=report,
            error=error, coverage_map=coverage_map,
            labels=layout.COMPARE_LABELS,
        )

    # ------------------------------------------------------------------
    # JSON API
    # ------------------------------------------------------------------
    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id):
        return jsonify(_json_state(job_state(job_id)))

    @app.post("/api/jobs/<int:job_id>/lines")
    def api_set_lines(job_id):
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        try:
            horizontal, vertical = layout.normalize_layout(
                payload.get("horizontal", []), payload.get("vertical", [])
            )
        except layout.LayoutError as error:
            # Invalid lines: keep the previous layout and its assignments.
            return jsonify({
                "ok": False,
                "error": error.message,
                "details": error.details,
            }), 400
        db.set_lines(get_db(), job_id, horizontal, vertical)
        return jsonify(_json_state(job_state(job_id)))

    @app.post("/api/jobs/<int:job_id>/suggest-lines")
    def api_suggest_lines(job_id):
        load_job_or_404(job_id)
        boxes = db.get_boxes(get_db(), job_id)
        horizontal, vertical = _suggest_lines(boxes)
        return jsonify({"ok": True, "horizontal": horizontal, "vertical": vertical})

    @app.post("/api/jobs/<int:job_id>/decisions")
    def api_set_decision(job_id):
        load_job_or_404(job_id)
        payload = request.get_json(silent=True) or {}
        boxes = db.get_boxes(get_db(), job_id)
        box = next((item for item in boxes if item["id"] == payload.get("box_id")), None)
        if box is None:
            return jsonify({"ok": False, "error": "文字框不存在"}), 404
        state = job_state(job_id)
        if not state["grid_valid"]:
            return jsonify({"ok": False, "error": state["invalid"], "details": []}), 409
        grid = layout.Grid(state["horizontal"], state["vertical"])
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box["id"])
        if item["status"] not in ("pending", "stale"):
            return jsonify({
                "ok": False,
                "error": "该文字框已完整落在一格内，无需人工归位",
                "details": [],
            }), 409
        try:
            decision = layout.make_decision(
                grid, item, payload.get("kind"), payload.get("target_cell")
            )
        except layout.LayoutError as error:
            return jsonify({"ok": False, "error": error.message, "details": error.details}), 400
        db.set_decision(get_db(), job_id, box["id"], decision["kind"],
                        decision["target_cell"], decision["overlap_cells"])
        return jsonify(_json_state(job_state(job_id)))

    @app.delete("/api/jobs/<int:job_id>/decisions/<int:box_id>")
    def api_clear_decision(job_id, box_id):
        load_job_or_404(job_id)
        db.clear_decision(get_db(), job_id, box_id)
        return jsonify(_json_state(job_state(job_id)))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @app.get("/jobs/<int:job_id>/export.csv")
    def export_csv(job_id):
        state = job_state(job_id)
        if not state["grid_valid"]:
            abort(409, description=state["invalid"])
        if state["pending"] > 0:
            abort(409, description=f"还有 {state['pending']} 个文字框待核对，不能下载")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["品名", "规格", "批发价"])
        for row in state["rows"]:
            writer.writerow([row["name"], row["spec"], row["price"]])
        data = "﻿" + buffer.getvalue()
        name = Path(state["job"]["filename"]).stem or "pricelist"
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{_quote_filename(name + '-价目表.csv')}"
            },
        )

    @app.get("/api/compare")
    def api_compare():
        connection = get_db()
        selected = []
        for raw in (request.args.get("a"), request.args.get("b")):
            raw = (raw or "").strip()
            if raw.isdigit() and int(raw) not in selected:
                selected.append(int(raw))
        if len(selected) != 2:
            return jsonify({"ok": False, "error": "请选择两期进行对照",
                            "details": []}), 400
        issues = [db.get_issue(connection, issue_id) for issue_id in selected]
        if any(issue is None for issue in issues):
            return jsonify({"ok": False, "error": "所选期号不存在",
                            "details": []}), 404
        report, coverage_map = compare_report(issues[0], issues[1])
        return jsonify({
            "ok": True,
            "issues": [
                {
                    "issue_id": entry["issue_id"],
                    "issue_no": entry["issue_no"],
                    "total_pages": entry["total_pages"],
                    "complete": entry["complete"],
                    "missing_pages": entry["missing"],
                    "unfinished_pages": entry["unfinished"],
                    "mounted_pages": len(entry["mounted_pages"]),
                }
                for entry in coverage_map.values()
            ],
            "rows": report,
        })

    @app.errorhandler(413)
    def too_large(_error):
        flash("文件过大（上限 10MB）", "error")
        return redirect(url_for("index")), 303

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="找不到该任务"), 404

    @app.errorhandler(409)
    def conflict(error):
        return render_template("error.html", code=409,
                               message=getattr(error, "description", "操作被拒绝")), 409

    return app


def _json_state(state):
    job = state["job"]
    return {
        "ok": True,
        "job": {"id": job["id"], "filename": job["filename"]},
        "boxes": state["boxes"],
        "horizontal": state["horizontal"],
        "vertical": state["vertical"],
        "grid_valid": state["grid_valid"],
        "invalid": state["invalid"],
        "pending": state["pending"],
        "rows": state["rows"],
        "status_counts": state["status_counts"],
        "can_export": state["can_export"],
        "cells": state["cells"],
    }


def _quote_filename(name):
    return quote(name)


def _suggest_lines(boxes):
    """Propose ruling lines from the OCR boxes.

    Vertical: page borders plus two separators chosen so the boxes cluster
    into three columns (splits are only allowed inside empty horizontal gaps).
    Horizontal: page margins plus a separator between adjacent text bands
    that do not overlap vertically.
    """
    if not boxes:
        return [], []
    x1 = min(box["x1"] for box in boxes)
    x2 = max(box["x2"] for box in boxes)
    y1 = min(box["y1"] for box in boxes)
    y2 = max(box["y2"] for box in boxes)

    separators = _column_separators(boxes, x1, x2)
    vertical = sorted({round(x1, 2), round(x2, 2), *separators})
    if len(vertical) != 4 or x2 - x1 <= layout.EPS:
        # Degenerate page (a single point, or no usable x-spread): propose a
        # nominal 1000-unit-wide page instead of duplicate lines.
        left = x1
        step = 1000.0 / 3.0
        vertical = [round(left, 2), round(left + step, 2),
                    round(left + 2 * step, 2), round(left + 1000.0, 2)]

    # Horizontal: group boxes into visual bands first.
    ordered = sorted(boxes, key=lambda box: (box["y1"], box["x1"]))
    bands = []
    for box in ordered:
        placed = False
        for band in bands:
            if layout._same_visual_line(
                {"x1": box["x1"], "y1": box["y1"], "x2": box["x2"], "y2": box["y2"]},
                {"x1": band[0], "y1": band[1], "x2": band[2], "y2": band[3]},
            ):
                band[0] = min(band[0], box["x1"])
                band[1] = min(band[1], box["y1"])
                band[2] = max(band[2], box["x2"])
                band[3] = max(band[3], box["y2"])
                placed = True
                break
        if not placed:
            bands.append([box["x1"], box["y1"], box["x2"], box["y2"]])

    bands.sort(key=lambda band: band[1])
    horizontal = [round(y1, 2), round(y2, 2)]
    for previous, current in zip(bands, bands[1:]):
        if current[1] > previous[3]:
            horizontal.append(round((previous[3] + current[1]) / 2.0, 2))
    # Merge duplicate/near-duplicate lines.
    merged = []
    for value in sorted(horizontal):
        if not merged or abs(value - merged[-1]) > layout.EPS:
            merged.append(value)
    if len(merged) < 2:
        # A single point (or no vertical spread) still needs a page height.
        merged = [round(y1, 2), round(y1 + 1000.0, 2)]
    return merged, vertical


def _column_separators(boxes, low, high):
    """Two x separators that split the boxes into three column clusters.

    Splits are restricted to gaps between the boxes' x-spans, so no box is
    ever cut by a suggested separator.  The pair that minimises within-column
    spread (with a heavy penalty for crossing a box) wins; if the boxes do not
    form three clusters, equal thirds of the page width are used.
    """
    spans = sorted((box["x1"], box["x2"], (box["x1"] + box["x2"]) / 2.0)
                   for box in boxes)
    gaps = [index for index in range(len(spans) - 1)
            if spans[index + 1][0] - spans[index][1] > 0]
    best = None
    for first in gaps:
        for second in gaps:
            if second <= first:
                continue
            groups = [spans[:first + 1], spans[first + 1:second + 1], spans[second + 1:]]
            if any(not group for group in groups):
                continue
            split_a = (spans[first][1] + spans[first + 1][0]) / 2.0
            split_b = (spans[second][1] + spans[second + 1][0]) / 2.0
            score = 0.0
            for group in groups:
                centre = sum(entry[2] for entry in group) / len(group)
                score += sum((entry[2] - centre) ** 2 for entry in group)
                for start, end, _mid in group:
                    if start < split_a < end or start < split_b < end:
                        score += 1e9
            if best is None or score < best[0]:
                best = (score, split_a, split_b)
    if best is None:
        step = (high - low) / 3.0
        return [round(low + step, 2), round(low + 2 * step, 2)]
    separators = sorted({
        round(value, 2) for value in (best[1], best[2])
        if low + layout.EPS < value < high - layout.EPS
    })
    if len(separators) != 2:
        step = (high - low) / 3.0
        return [round(low + step, 2), round(low + 2 * step, 2)]
    return separators

