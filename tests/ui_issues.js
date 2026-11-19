// Headless check of the issue/placement/comparison workflow.  Requires a live
// server: node tests/ui_issues.js <baseUrl>
const { JSDOM } = require("jsdom");

const base = process.argv[2] || "http://127.0.0.1:5057";

const PAGE = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,面碱,10,210,90,240
7,袋装,110,210,190,240
8,2.10,210,210,290,240
`;
// Same products as PAGE except 面碱 is absent and 冰糖 is new.
const PAGE_2 = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.20,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,冰糖,10,210,90,240
7,统货,110,210,190,240
8,6.60,210,210,290,240
`;
// A page with only the two shared products, used to fill a missing page.
const FILLER = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
`;

function assert(condition, message) {
  if (!condition) throw new Error("FAIL: " + message);
  console.log("ok - " + message);
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function upload(name, body) {
  const form = new FormData();
  form.append("file", new Blob([body], { type: "text/csv" }), name);
  const response = await fetch(`${base}/upload`, {
    method: "POST", body: form, redirect: "manual",
  });
  return response.headers.get("location").split("/").pop();
}

async function makeIssue(issueNo, totalPages) {
  const body = new URLSearchParams({ issue_no: issueNo, total_pages: String(totalPages) });
  const response = await fetch(`${base}/issues`, {
    method: "POST", body, redirect: "manual",
  });
  assert(response.status === 302, `issue ${issueNo} registered`);
  const list = await fetch(`${base}/`).then((r) => r.text());
  const match = new RegExp(`/issues/(\\d+)">${issueNo}<`).exec(list);
  assert(match, `issue ${issueNo} listed`);
  return Number(match[1]);
}

async function resolveAll(jobId, horizontal = [0, 100, 200]) {
  await fetch(`${base}/api/jobs/${jobId}/lines`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      horizontal, vertical: [0, 100, 200, 300],
    }),
  });
  const state = await fetch(`${base}/api/jobs/${jobId}`).then((r) => r.json());
  for (const box of state.boxes.filter((b) => b.status === "pending" || b.status === "stale")) {
    await fetch(`${base}/api/jobs/${jobId}/decisions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        box_id: box.box_id,
        kind: box.candidates.length ? "cell" : "exclude",
        target_cell: box.candidates.length ? box.candidates[0].key : undefined,
      }),
    });
  }
}

async function place(jobId, issueId, pageNo) {
  return fetch(`${base}/api/jobs/${jobId}/placement`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ issue_id: issueId, page_no: pageNo }),
  });
}

async function open(path) {
  const page = await fetch(`${base}${path}`).then((r) => r.text());
  const dom = new JSDOM(page, {
    url: `${base}${path}`, runScripts: "dangerously",
    resources: "usable", pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = (url, options) => fetch(new URL(url, base), options);
  window.alert = (message) => { throw new Error("UI alert: " + message); };
  await new Promise((resolve) => window.addEventListener("load", resolve));
  await wait(200);
  return window;
}

(async () => {
  const jobA = await upload("a.csv", PAGE);
  const jobB = await upload("b.csv", PAGE_2);
  await resolveAll(jobA, [0, 100, 200, 300]);
  await resolveAll(jobB, [0, 100, 200, 300]);

  const issueA = await makeIssue("1998-01-01", 1);
  const issueB = await makeIssue("1998-01-02", 2);

  // The job page offers the placement form and the issue page lists tasks.
  {
    const window = await open(`/jobs/${jobA}`);
    const doc = window.document;
    assert(doc.getElementById("placement-card"), "job page has placement card");
    const select = doc.querySelector('#placement-card select[name=issue_id]');
    assert(select && select.options.length === 2, "issue choices are listed");
    select.value = String(issueA);
    doc.querySelector('#placement-card input[name=page_no]').value = "1";
    doc.querySelector('#placement-card form').dispatchEvent(
      new window.Event("submit", { bubbles: true, cancelable: true }));
    await wait(400);
    const placement = await fetch(`${base}/api/jobs/${jobA}`).then((r) => r.json());
    const page = await fetch(`${base}/issues/${issueA}`).then((r) => r.text());
    assert(page.includes("a.csv"), "form submission mounts the task on the page");
    assert(placement.ok, "task still readable after form placement");
  }

  // A page already holding a task is a swap: the old task is withdrawn.
  {
    assert((await place(jobB, issueB, 1)).status === 200, "job B placed on page 1");
    const swapped = await place(jobA, issueB, 1);
    assert(swapped.status === 200, "page 1 accepts only one task (swap)");
    const data = await swapped.json();
    assert(data.displaced && data.displaced.job_id === Number(jobB),
      "displaced task reported");
    const stateA = await fetch(`${base}/api/jobs/${jobA}`).then((r) => r.json());
    assert(stateA.ok, "task A still exists after moving");
  }

  // Out-of-range pages are refused, and the previous placement is kept.
  {
    const refused = await place(jobA, issueB, 9);
    assert(refused.status === 409, "page 9 refused on a 2-page issue");
    const detail = await refused.json();
    assert(detail.ok === false && detail.error.includes("超出"), "reason is Chinese");
    const pages = await fetch(`${base}/issues/${issueB}`).then((r) => r.text());
    assert(pages.includes("b.csv"), "displaced task stays withdrawn");
  }

  // Missing page 2 keeps an absent product at 待核 instead of 未刊登.
  {
    assert((await place(jobA, issueA, 1)).status === 200, "task A back on issue A");
    assert((await place(jobB, issueB, 2)).status === 200, "task B moved to page 2");
    const data = await fetch(
      `${base}/api/compare?a=${issueA}&b=${issueB}`).then((r) => r.json());
    assert(data.ok, "compare API answers");
    const sugar = data.rows.find((row) => row.name === "白砂糖");
    assert(sugar, "shared product is grouped by name and spec");
    assert(sugar.columns[0].prices[0].price === "5.00", "A price from page 1");
    assert(sugar.columns[1].prices[0].sources[0].page_no === 2,
      "B price carries its page");
    const ice = data.rows.find((row) => row.name === "冰糖");
    // A is complete, so its absence of 冰糖 is a firm 未刊登 even though B
    // is still missing a page.
    assert(ice.columns[0].status === "missing", "complete side marks 未刊登");
    assert(ice.columns[1].status === "found", "B lists 冰糖");
    // A's own product 面碱 has no place on B; B is incomplete, so 待核.
    const soda = data.rows.find((row) => row.name === "面碱");
    assert(soda.columns[1].status === "pending", "absent product on B is 待核");
    assert(soda.columns[1].missing_pages.includes(1), "B missing page is listed");
  }

  // Filling the missing page turns the same product into 未刊登.
  {
    const filler = await upload("filler.csv", FILLER);
    await resolveAll(filler, [0, 100, 200, 300]);
    assert((await place(filler, issueB, 1)).status === 200, "page 1 filled");
    const data = await fetch(
      `${base}/api/compare?a=${issueA}&b=${issueB}`).then((r) => r.json());
    const soda = data.rows.find((row) => row.name === "面碱");
    assert(soda.columns[1].status === "missing", "complete issue marks product 未刊登");
    assert(soda.columns[1].status_label === "未刊登", "label is 未刊登");
    const ice = data.rows.find((row) => row.name === "冰糖");
    assert(ice.columns[0].status === "missing", "A also marks 冰糖 未刊登");
  }

  // The compare page renders both columns with page/row attribution.
  {
    const window = await open(`/compare?a=${issueA}&b=${issueB}`);
    const doc = window.document;
    const table = doc.querySelector("table.compare");
    assert(table, "comparison table rendered");
    const headers = Array.from(table.querySelectorAll("thead th"))
      .map((node) => node.textContent.trim());
    assert(headers.includes("1998-01-01"), "left issue header");
    assert(headers.includes("1998-01-02"), "right issue header");
    assert(table.textContent.includes("第"), "prices carry page numbers");
    const badges = Array.from(table.querySelectorAll(".badge"))
      .map((node) => node.textContent.trim());
    assert(badges.length >= 1, "status badges rendered");
  }

  console.log("done");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
