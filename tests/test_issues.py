"""Issue/page placement, comparison and cache-invalidation tests."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import create_app, db  # noqa: E402
from pricing import layout  # noqa: E402

# Two clean rows: every box falls wholly inside the pinned grid below.
PAGE = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
"""

# Same products, sugar base price changed, plus a new product on row 3.
PAGE_2 = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.20,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,冰糖,10,210,90,240
7,统货,110,210,190,240
8,6.60,210,210,290,240
"""

H = [0, 100, 200, 300]
V = [0, 100, 200, 300]
H2 = [0, 100, 200, 300, 400]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def upload(self, body=PAGE, filename="page.csv"):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def set_lines(self, job_id, horizontal=H, vertical=V):
        response = self.client.post(
            f"/api/jobs/{job_id}/lines",
            json={"horizontal": horizontal, "vertical": vertical},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def make_issue(self, issue_no, total_pages):
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                return db.create_issue(connection, issue_no, total_pages)
            finally:
                connection.close()

    def issue(self, issue_id):
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                return db.get_issue(connection, issue_id)
            finally:
                connection.close()

    def mount(self, job_id, issue_id, page_no):
        return self.client.post(
            f"/api/jobs/{job_id}/placement",
            json={"issue_id": issue_id, "page_no": page_no},
        )

    def page_of(self, job_id):
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                return db.page_of_job(connection, job_id)
            finally:
                connection.close()

    def compare(self, issue_a, issue_b):
        response = self.client.get(f"/compare?a={issue_a}&b={issue_b}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def compare_json(self, issue_a, issue_b):
        response = self.client.get(f"/api/compare?a={issue_a}&b={issue_b}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def column(self, data, name, spec, issue_index):
        row = next(entry for entry in data["rows"]
                   if entry["name"] == name and entry["spec"] == spec)
        return row["columns"][issue_index]

    def complete_issue(self, issue_no, body=PAGE, total_pages=1, filename=None,
                       horizontal=H):
        job = self.upload(body=body, filename=filename or issue_no + ".csv")
        self.set_lines(job, horizontal=horizontal)
        issue_id = self.make_issue(issue_no, total_pages)
        response = self.mount(job, issue_id, 1)
        self.assertEqual(response.status_code, 200)
        return job, issue_id


class IssueTests(BaseCase):
    def test_issue_requires_valid_page_count(self):
        response = self.client.post(
            "/issues", data={"issue_no": "1998-01-01", "total_pages": "0"},
            follow_redirects=True)
        self.assertIn("总页数必须是正整数".encode(), response.data)

    def test_issue_no_must_be_unique(self):
        self.client.post("/issues", data={"issue_no": "A", "total_pages": "2"})
        response = self.client.post(
            "/issues", data={"issue_no": "A", "total_pages": "3"},
            follow_redirects=True)
        self.assertIn("已存在".encode(), response.data)

    def test_total_pages_cannot_drop_below_mounted_page(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 3)
        self.assertEqual(self.mount(job, issue_id, 3).status_code, 200)
        response = self.client.post(
            f"/issues/{issue_id}/total-pages", data={"total_pages": "2"},
            follow_redirects=True)
        self.assertIn("不能少于".encode(), response.data)
        self.assertEqual(self.issue(issue_id)["total_pages"], 3)

    def test_issue_page_renders(self):
        issue_id = self.make_issue("A", 2)
        response = self.client.get(f"/issues/{issue_id}")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("共 2 页", body)
        self.assertIn("缺页：第 1、2 页", body)


class PlacementTests(BaseCase):
    def test_page_accepts_only_one_task(self):
        first = self.upload(filename="one.csv")
        second = self.upload(filename="two.csv")
        self.set_lines(first)
        self.set_lines(second)
        issue_id = self.make_issue("A", 2)
        self.assertEqual(self.mount(first, issue_id, 1).status_code, 200)
        displaced = self.mount(second, issue_id, 1)
        self.assertEqual(displaced.status_code, 200)
        self.assertEqual(displaced.get_json()["displaced"]["job_id"], first)
        self.assertEqual(self.page_of(second)["page_no"], 1)
        self.assertIsNone(self.page_of(first))

    def test_task_accepts_only_one_page(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 3)
        self.mount(job, issue_id, 1)
        # Moving the same task to another page leaves the first page empty.
        self.assertEqual(self.mount(job, issue_id, 2).status_code, 200)
        self.assertEqual(self.page_of(job)["page_no"], 2)
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                pages = db.list_pages(connection, issue_id)
            finally:
                connection.close()
        self.assertEqual([page["page_no"] for page in pages], [2])

    def test_task_can_move_between_issues(self):
        job = self.upload()
        self.set_lines(job)
        first = self.make_issue("A", 2)
        second = self.make_issue("B", 2)
        self.mount(job, first, 1)
        self.mount(job, second, 1)
        placement = self.page_of(job)
        self.assertEqual((placement["issue_id"], placement["page_no"]), (second, 1))

    def test_rejects_page_beyond_total(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 2)
        response = self.mount(job, issue_id, 3)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        self.assertIsNone(self.page_of(job))

    def test_rejects_non_positive_page(self):
        job = self.upload()
        issue_id = self.make_issue("A", 2)
        response = self.mount(job, issue_id, 0)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.page_of(job))

    def test_failed_swap_keeps_previous_state(self):
        holder = self.upload(filename="holder.csv")
        challenger = self.upload(filename="challenger.csv")
        self.set_lines(holder)
        self.set_lines(challenger)
        issue_id = self.make_issue("A", 2)
        other_issue = self.make_issue("B", 2)
        self.mount(holder, issue_id, 1)
        self.mount(challenger, other_issue, 2)
        # Invalid page: the challenger must stay where it was, holder untouched.
        response = self.mount(challenger, issue_id, 99)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.page_of(holder)["page_no"], 1)
        placement = self.page_of(challenger)
        self.assertEqual((placement["issue_id"], placement["page_no"]),
                         (other_issue, 2))

    def test_issue_form_placement(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 2)
        response = self.client.post(
            f"/issues/{issue_id}/pages",
            data={"job_id": str(job), "page_no": "2"},
            follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.page_of(job)["page_no"], 2)

    def test_delete_issue_unmounts_but_keeps_job(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 2)
        self.mount(job, issue_id, 1)
        self.client.post(f"/issues/{issue_id}/delete", follow_redirects=True)
        self.assertIsNone(self.page_of(job))
        self.assertEqual(self.client.get(f"/api/jobs/{job}").status_code, 200)

    def test_job_page_shows_placement(self):
        job = self.upload()
        self.set_lines(job)
        issue_id = self.make_issue("A", 2)
        self.mount(job, issue_id, 1)
        response = self.client.get(f"/jobs/{job}")
        self.assertIn(b"placement-card", response.data)
        self.assertIn(b"\xe7\xac\xac 1 \xe9\xa1\xb5", response.data)  # 第 1 页


class CompareTests(BaseCase):
    complete_issue = BaseCase.complete_issue

    def test_lists_prices_with_page_and_row(self):
        _, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        data = self.compare_json(a, b)
        sugar = self.column(data, "白砂糖", "一级", 0)
        self.assertEqual(sugar["prices"][0]["price"], "5.00")
        self.assertEqual(sugar["prices"][0]["sources"],
                         [{"page_no": 1, "row_no": 1}])
        self.assertEqual(self.column(data, "白砂糖", "一级", 1)["prices"][0]["price"],
                         "5.20")
        html = self.compare(a, b)
        self.assertIn("第 1 页 · 第 1 行", html)

    def test_absent_product_on_complete_issue_is_missing(self):
        _, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        data = self.compare_json(a, b)
        # 冰糖 is only in B; A is complete, so A says 未刊登 for it.
        self.assertEqual(self.column(data, "冰糖", "统货", 0)["status"], "missing")
        self.assertEqual(self.column(data, "冰糖", "统货", 0)["status_label"], "未刊登")
        self.assertEqual(self.column(data, "冰糖", "统货", 1)["status"], "found")
        # The same holds in the other direction for a product only in A.
        self.assertEqual(self.column(data, "红糖", "", 1)["status"], "found")

    def test_absent_product_with_missing_page_is_pending(self):
        _, a = self.complete_issue("A", total_pages=2)
        _, b = self.complete_issue("B", body=PAGE_2, total_pages=1)
        data = self.compare_json(a, b)
        column = self.column(data, "冰糖", "统货", 0)
        self.assertEqual(column["status"], "pending")
        self.assertEqual(column["status_label"], "待核")
        self.assertEqual(column["missing_pages"], [2])
        html = self.compare(a, b)
        self.assertIn("缺页：第 2 页", html)

    def test_absent_product_with_unfinished_page_is_pending(self):
        job = self.upload(filename="unfinished.csv", body=PAGE + "6,跨格,90,10,110,40\n")
        self.set_lines(job)
        issue_id = self.make_issue("A", 1)
        self.mount(job, issue_id, 1)
        _, b = self.complete_issue("B", body=PAGE_2)
        data = self.compare_json(issue_id, b)
        column = self.column(data, "冰糖", "统货", 0)
        self.assertEqual(column["status"], "pending")
        self.assertEqual([page["page_no"] for page in column["unfinished_pages"]],
                         [1])
        html = self.compare(issue_id, b)
        self.assertIn("未核完的页", html)

    def test_incomplete_issue_contributes_no_prices(self):
        job = self.upload(filename="unfinished.csv", body=PAGE + "6,跨格,90,10,110,40\n")
        self.set_lines(job)
        issue_id = self.make_issue("A", 1)
        self.mount(job, issue_id, 1)
        _, b = self.complete_issue("B", body=PAGE_2)
        data = self.compare_json(issue_id, b)
        # The unfinished page's 白砂糖 price must not be shown for A.
        column = self.column(data, "白砂糖", "一级", 0)
        self.assertEqual(column["prices"], [])
        self.assertEqual(column["status"], "pending")
        html = self.compare(issue_id, b)
        table = html.split('<table class="compare">', 1)[1]
        self.assertNotIn("5.00", table)

    def test_invalid_grid_issue_is_unfinished(self):
        job = self.upload(filename="bad.csv")
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                db.set_lines(connection, job, [0, 100], [0, 100])
            finally:
                connection.close()
        issue_id = self.make_issue("A", 1)
        self.mount(job, issue_id, 1)
        _, b = self.complete_issue("B", body=PAGE_2)
        data = self.compare_json(issue_id, b)
        self.assertEqual(data["issues"][0]["complete"], False)
        self.assertEqual(data["issues"][0]["unfinished_pages"][0]["page_no"], 1)
        self.assertIn("格线无效", data["issues"][0]["unfinished_pages"][0]["invalid"])
        html = self.compare(issue_id, b)
        self.assertIn("格线无效", html)

    def test_page_rows_follow_grid_row_numbers(self):
        _, a = self.complete_issue("A")
        # Two extra horizontal lines split the page into four table rows.
        _, b = self.complete_issue("B", body=PAGE_2,
                                   horizontal=[0, 100, 200, 300, 400])
        data = self.compare_json(a, b)
        sugar = self.column(data, "白砂糖", "一级", 0)
        self.assertEqual(sugar["prices"][0]["sources"][0]["row_no"], 1)
        red = self.column(data, "红糖", "", 0)
        self.assertEqual(red["prices"][0]["sources"][0]["row_no"], 2)
        self.assertEqual(self.column(data, "冰糖", "统货", 0)["status"], "missing")

    def test_comparison_is_symmetric(self):
        _, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        forward = self.compare_json(a, b)
        backward = self.compare_json(b, a)
        self.assertEqual(len(forward["rows"]), len(backward["rows"]))
        for entry in forward["rows"]:
            reverse = next(other for other in backward["rows"]
                           if other["name"] == entry["name"]
                           and other["spec"] == entry["spec"])
            self.assertEqual(
                [column["status"] for column in entry["columns"]],
                [column["status"] for column in reversed(reverse["columns"])])

    def test_compare_page_requires_two_issues(self):
        self.complete_issue("A")
        response = self.client.get("/compare?a=1")
        self.assertIn("请选择两期".encode(), response.data)


class CacheTests(BaseCase):
    def extract(self, job_id):
        with self.app.app_context():
            connection = db.connect(self.db_path)
            try:
                job = db.get_job(connection, job_id)
                return db.get_extract(connection, job_id, job["version"])
            finally:
                connection.close()

    def test_repeating_compare_reuses_cached_extract(self):
        job, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        self.compare(a, b)
        first = self.extract(job)
        self.assertIsNotNone(first)
        self.compare(a, b)
        self.assertEqual(self.extract(job)["job_version"], first["job_version"])

    def test_line_change_invalidates_cached_prices(self):
        job, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        self.compare(a, b)
        self.assertIsNotNone(self.extract(job))
        self.set_lines(job, [0, 60, 240, 300], [0, 100, 200, 300])
        self.assertIsNone(self.extract(job))
        # The new grid still exports; the comparison recomputes it from
        # scratch and reflects the new row numbers.
        data = self.compare_json(a, b)
        self.assertEqual(self.column(data, "白砂糖", "一级", 0)["prices"][0]
                         ["sources"][0]["row_no"], 1)
        self.assertIsNotNone(self.extract(job))

    def test_decision_change_invalidates_cached_prices(self):
        body = PAGE + "6,跨格,90,10,110,40\n"
        job = self.upload(body=body)
        state = self.set_lines(job)
        issue_id = self.make_issue("A", 1)
        self.mount(job, issue_id, 1)
        _, b = self.complete_issue("B", body=PAGE_2)
        pending_box = next(box for box in state["boxes"] if box["status"] == "pending")
        self.client.post(f"/api/jobs/{job}/decisions",
                         json={"box_id": pending_box["box_id"],
                               "kind": "cell",
                               "target_cell": pending_box["candidates"][0]["key"]})
        self.assertIsNone(self.extract(job))
        html = self.compare(issue_id, b)
        self.assertIn("5.00", html)
        # Now that the page is exportable it is cached; a new decision must
        # still evict the cached extract so the comparison re-reads the data.
        self.assertIsNotNone(self.extract(job))
        self.client.delete(f"/api/jobs/{job}/decisions/{pending_box['box_id']}")
        self.assertIsNone(self.extract(job))

    def test_page_swap_withdraws_old_prices(self):
        job, a = self.complete_issue("A")
        replacement = self.upload(body=PAGE_2, filename="replacement.csv")
        self.set_lines(replacement)
        response = self.mount(replacement, a, 1)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.page_of(job))
        # Compare A with a clean, empty-page issue so each column is unambiguous.
        other, other_issue = self.complete_issue("D", body=PAGE)
        data = self.compare_json(a, other_issue)
        sugar_a = self.column(data, "白砂糖", "一级", 0)
        self.assertEqual([price["price"] for price in sugar_a["prices"]], ["5.20"])
        self.assertEqual(sugar_a["status"], "found")

    def test_deleted_task_withdraws_prices(self):
        job, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        self.client.post(f"/jobs/{job}/delete", follow_redirects=True)
        data = self.compare_json(a, b)
        column = self.column(data, "白砂糖", "一级", 0)
        self.assertEqual(column["prices"], [])
        self.assertEqual(column["status"], "pending")  # page 1 is now missing
        self.assertEqual(column["missing_pages"], [1])

    def test_grid_change_withdraws_prices_until_rechecked(self):
        job, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        before = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.assertEqual(before["prices"][0]["price"], "5.00")
        # Split the page with an extra row line: the grid stays valid but no
        # text box moves, so prices stay; then break the grid and they vanish.
        self.set_lines(job, [0, 50, 100, 200])
        still_valid = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.assertEqual(still_valid["status"], "found")
        # A line across the text boxes makes every box straddle a cell.
        self.set_lines(job, [0, 25, 200])
        after = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.assertEqual(after["prices"], [], "stale prices must be withdrawn")
        self.assertEqual(after["status"], "pending")
        self.assertEqual([page["page_no"] for page in after["unfinished_pages"]],
                         [1])

    def test_reexport_after_restoring_lines_restores_prices(self):
        job, a = self.complete_issue("A")
        _, b = self.complete_issue("B", body=PAGE_2)
        original = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.set_lines(job, [0, 25, 200])
        withdrawn = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.assertEqual(withdrawn["prices"], [])
        self.set_lines(job, H)
        restored = self.column(self.compare_json(a, b), "白砂糖", "一级", 0)
        self.assertEqual(restored["prices"], original["prices"])


class CoverageTests(unittest.TestCase):
    def pages(self, entries, total):
        return layout.issue_page_coverage(entries, total)

    def test_missing_and_unfinished_are_reported(self):
        coverage = self.pages([
            {"page_no": 1, "job_id": 1, "grid_valid": True, "pending": 0},
            {"page_no": 3, "job_id": 3, "grid_valid": False, "pending": 2},
        ], 4)
        self.assertEqual(coverage["missing"], [2, 4])
        self.assertEqual([page["page_no"] for page in coverage["unfinished"]], [3])
        self.assertFalse(coverage["complete"])

    def test_complete_only_when_all_pages_exportable(self):
        coverage = self.pages([
            {"page_no": 1, "job_id": 1, "grid_valid": True, "pending": 0},
            {"page_no": 2, "job_id": 2, "grid_valid": True, "pending": 0},
        ], 2)
        self.assertTrue(coverage["complete"])


class CompareLogicTests(unittest.TestCase):
    def issue(self, issue_id, rows, complete=True, missing=None, unfinished=None):
        return {
            "issue_id": issue_id, "issue_no": str(issue_id),
            "total_pages": 1, "complete": complete, "rows": rows,
            "missing": missing or [],
            "unfinished": unfinished or [],
            "pages": {},
        }

    def test_missing_status_requires_complete_issue(self):
        a = self.issue(1, [{"name": "盐", "spec": "细", "price": "1",
                            "page_no": 1, "row_no": 1}])
        b = self.issue(2, [], complete=False, missing=[1])
        result = layout.compare_issues([a, b])
        self.assertEqual(result[0]["columns"][1]["status"], layout.PENDING)
        self.assertEqual(result[0]["columns"][1]["missing_pages"], [1])

    def test_same_price_merges_sources(self):
        a = self.issue(1, [
            {"name": "盐", "spec": "细", "price": "1", "page_no": 1, "row_no": 1},
            {"name": "盐", "spec": "细", "price": "1", "page_no": 2, "row_no": 3},
        ])
        result = layout.compare_issues([a])
        prices = result[0]["columns"][0]["prices"]
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0]["sources"],
                         [{"page_no": 1, "row_no": 1},
                          {"page_no": 2, "row_no": 3}])

    def test_name_and_spec_grouping_ignores_whitespace(self):
        a = self.issue(1, [{"name": "白 砂糖", "spec": "一级", "price": "5",
                            "page_no": 1, "row_no": 1}])
        result = layout.compare_issues([a])
        self.assertEqual(result[0]["name"], "白 砂糖")
        self.assertEqual(result[0]["spec"], "一级")


if __name__ == "__main__":
    unittest.main()
