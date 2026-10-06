"""End-to-end checks against a real uvicorn server.

Run:  python tests/e2e_test.py
Exits non-zero on the first failed assertion, printing a traceback.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback

import httpx
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_DIR = "/tmp/blind_test_data"
DB_PATH = os.path.join(DB_DIR, "blindreview.db")
BASE = "http://127.0.0.1:8000"


def start_server() -> uvicorn.Server:
    os.environ["DB_PATH"] = DB_PATH
    # Import after env is set so the module picks the test path.
    import importlib

    import app.db
    importlib.reload(app.db)
    import app.main
    importlib.reload(app.main)

    config = uvicorn.Config(
        app.main.app, host="127.0.0.1", port=8000, log_level="error"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    return server


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


PASS = 0


def check(name: str, cond: bool, detail="") -> None:
    global PASS
    assert cond, f"{name} failed: {detail}"
    PASS += 1
    print(f"  ok - {name}")


def main() -> None:
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    server = start_server()

    # --------------------------------------------------------------- #
    print("1) create batch")
    payload = {
        "categories": ["cat", "dog", "bird"],
        "samples": [
            {"code": "S01", "true_label": "cat"},
            {"code": "S02", "true_label": "dog"},
            {"code": "S03", "true_label": "bird"},
            {"code": "S04", "true_label": "cat"},
        ],
    }
    r = httpx.post(f"{BASE}/batches", json=payload, timeout=5)
    check("create 201", r.status_code == 201, r.text)
    created = r.json()
    bid = created["batch_id"]
    t1, t2, ta = created["reviewer1_token"], created["reviewer2_token"], created["arbiter_token"]
    check("three distinct tokens", len({t1, t2, ta}) == 3)
    check("stage accepting", created["stage"] == "accepting_submissions")

    print("2) pre-seal public view is blind")
    r = httpx.get(f"{BASE}/batches/{bid}", timeout=5)
    body = r.json()
    check("public 200", r.status_code == 200)
    check("only codes", body["sample_codes"] == ["S01", "S02", "S03", "S04"])
    check("samples hidden", body["samples"] is None)
    check("counts hidden", body["reviewer1_correct"] is None)
    check("no truth leaked", "true_label" not in r.text)

    print("3) bad input rejected")
    r = httpx.get(f"{BASE}/batches/deadbeef")
    check("unknown batch 404", r.status_code == 404)
    r = httpx.post(f"{BASE}/batches", json={"categories": ["a", "a"],
                                            "samples": [{"code": "x", "true_label": "a"}]})
    check("dup categories 422", r.status_code == 422, r.text)
    r = httpx.post(f"{BASE}/batches", json={"categories": ["a", "b"],
                                            "samples": [{"code": "x", "true_label": "a"},
                                                        {"code": "x", "true_label": "b"}]})
    check("dup codes 422", r.status_code == 422)

    print("4) auth")
    r = httpx.get(f"{BASE}/batches/{bid}/submission")
    check("no token 401", r.status_code == 401)
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth("garbage"))
    check("bad token 403", r.status_code == 403)

    other = httpx.post(f"{BASE}/batches", json={
        "categories": ["x", "y"],
        "samples": [{"code": "X1", "true_label": "x"}]}).json()
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth(other["reviewer1_token"]))
    check("cross-batch token 403", r.status_code == 403)

    print("5) reviewer sees only own state before submitting")
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth(t1))
    check("r1 status 200", r.status_code == 200)
    check("r1 not submitted", r.json()["submitted"] is False)
    check("r1 sees no labels", r.json()["classifications"] is None)
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth(ta))
    check("arbiter cannot read submission", r.status_code == 403)

    print("6) invalid submissions rejected")
    r = httpx.post(f"{BASE}/batches/{bid}/submission",
                   headers=auth(ta), json={"classifications": {"S01": "cat"}})
    check("arbiter cannot submit 403", r.status_code == 403)
    r = httpx.post(f"{BASE}/batches/{bid}/submission",
                   headers=auth(t1), json={"classifications": {"S01": "cat", "S02": "dog"}})
    check("incomplete cover 422", r.status_code == 422, r.text)
    r = httpx.post(f"{BASE}/batches/{bid}/submission",
                   headers=auth(t1),
                   json={"classifications": {"S01": "fish", "S02": "dog",
                                             "S03": "bird", "S04": "cat"}})
    check("out-of-set label 422", r.status_code == 422)

    print("7) reviewer1 submits: S01 cat, S02 dog, S03 bird, S04 dog(disagree)")
    sub1 = {"S01": "cat", "S02": "dog", "S03": "bird", "S04": "dog"}
    r = httpx.post(f"{BASE}/batches/{bid}/submission", headers=auth(t1),
                   json={"classifications": sub1})
    check("submit1 201", r.status_code == 201, r.text)
    check("still accepting", r.json()["stage"] == "accepting_submissions")

    print("8) submit is immutable")
    r = httpx.post(f"{BASE}/batches/{bid}/submission", headers=auth(t1),
                   json={"classifications": {k: "bird" for k in sub1}})
    check("resubmit 409", r.status_code == 409)
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth(t1))
    labels = r.json()["classifications"]
    check("judgement not rewritten", labels == sub1, json.dumps(labels))
    r = httpx.get(f"{BASE}/batches/{bid}/submission", headers=auth(t2))
    check("r2 cannot see r1 labels", r.json()["classifications"] is None)
    check("public still blind", "true_label" not in httpx.get(f"{BASE}/batches/{bid}").text)

    print("9) arbiter blind until both submissions in")
    r = httpx.get(f"{BASE}/batches/{bid}/disputes", headers=auth(ta))
    check("disputes early 409", r.status_code == 409)

    print("10) reviewer2 submits with two disagreements (S03, S04)")
    sub2 = {"S01": "cat", "S02": "dog", "S03": "cat", "S04": "cat"}
    r = httpx.post(f"{BASE}/batches/{bid}/submission", headers=auth(t2),
                   json={"classifications": sub2})
    check("submit2 201", r.status_code == 201, r.text)
    check("awaiting arbitration", r.json()["stage"] == "awaiting_arbitration")
    check("not sealed yet", r.json()["sealed_now"] is False)

    print("11) arbiter sees only disputes, no truth")
    r = httpx.get(f"{BASE}/batches/{bid}/disputes", headers=auth(ta))
    check("disputes 200", r.status_code == 200, r.text)
    dbody = r.json()
    codes = [d["code"] for d in dbody["disputes"]]
    check("only disputed codes", codes == ["S03", "S04"], str(codes))
    check("both labels shown",
          dbody["disputes"][0] == {"code": "S03", "reviewer1_label": "bird",
                                   "reviewer2_label": "cat", "chosen_label": None})
    check("no truth for arbiter", "true_label" not in r.text)
    check("remaining 2", dbody["remaining"] == 2)
    r = httpx.get(f"{BASE}/batches/{bid}/disputes", headers=auth(t1))
    check("reviewer cannot list disputes", r.status_code == 403)

    print("12) adjudication constraints")
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "S01", "label": "cat"})
    check("agreed sample locked 422", r.status_code == 422)
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "S03", "label": "dog"})
    check("choice must be reviewer label 422", r.status_code == 422)
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(t1),
                   json={"code": "S03", "label": "bird"})
    check("reviewer cannot adjudicate 403", r.status_code == 403)
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "NOPE", "label": "cat"})
    check("unknown sample 404", r.status_code == 404)

    print("13) decide S03, still sealed? no")
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "S03", "label": "bird"})
    check("adjudicate S03 201", r.status_code == 201, r.text)
    check("remaining 1", r.json()["remaining"] == 1)
    check("not sealed mid-way", r.json()["sealed_now"] is False)

    print("14) repeated adjudication cannot overwrite")
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "S03", "label": "cat"})
    check("readjudicate 409", r.status_code == 409)
    d = [x for x in httpx.get(f"{BASE}/batches/{bid}/disputes", headers=auth(ta)).json()["disputes"]
         if x["code"] == "S03"][0]
    check("choice preserved", d["chosen_label"] == "bird")
    check("still blind to public",
          httpx.get(f"{BASE}/batches/{bid}").json()["samples"] is None)

    print("15) final decision seals atomically")
    r = httpx.post(f"{BASE}/batches/{bid}/adjudications", headers=auth(ta),
                   json={"code": "S04", "label": "cat"})
    check("adjudicate S04 201", r.status_code == 201)
    check("sealed now", r.json()["sealed_now"] is True)
    check("stage sealed", r.json()["stage"] == "sealed")

    print("16) unblinded result")
    body = httpx.get(f"{BASE}/batches/{bid}").json()
    check("stage sealed", body["stage"] == "sealed")
    by_code = {s["code"]: s for s in body["samples"]}
    check("S01 agreement cat", by_code["S01"]["final_label"] == "cat"
          and by_code["S01"]["source"] == "agreement" and by_code["S01"]["correct"])
    check("S03 adjudication bird (arbiter chose r1; truth bird)",
          by_code["S03"]["final_label"] == "bird" and by_code["S03"]["source"] == "adjudication"
          and by_code["S03"]["correct"])
    check("S04 adjudication cat equals truth",
          by_code["S04"]["final_label"] == "cat" and by_code["S04"]["correct"])
    # r1: S01 cat ok, S02 dog ok, S03 bird ok, S04 dog wrong -> 3
    # r2: S01 cat ok, S02 dog ok, S03 cat wrong, S04 cat ok -> 3
    check("r1 correct 3", body["reviewer1_correct"] == 3, str(body["reviewer1_correct"]))
    check("r2 correct 3", body["reviewer2_correct"] == 3, str(body["reviewer2_correct"]))
    # Both reviewer labels exposed after sealing.
    check("both labels present", by_code["S03"]["reviewer1_label"] == "bird"
          and by_code["S03"]["reviewer2_label"] == "cat")

    print("17) everything closed after sealing")
    for headers, path, doc in [
        (auth(t1), "submission", {"classifications": sub1}),
        (auth(ta), "adjudications", {"code": "S03", "label": "cat"}),
    ]:
        r = httpx.post(f"{BASE}/batches/{bid}/{path}", headers=headers, json=doc)
        check(f"{path} closed 409", r.status_code == 409, r.text)
    r = httpx.get(f"{BASE}/batches/{bid}/disputes", headers=auth(ta))
    check("disputes closed after seal 409", r.status_code == 409)

    print("18) no-dispute batch seals on second submission")
    c = httpx.post(f"{BASE}/batches", json={
        "categories": ["a", "b"],
        "samples": [{"code": "A", "true_label": "a"}, {"code": "B", "true_label": "b"}]}).json()
    b2, u1, u2 = c["batch_id"], c["reviewer1_token"], c["reviewer2_token"]
    doc = {"classifications": {"A": "a", "B": "b"}}
    httpx.post(f"{BASE}/batches/{b2}/submission", headers=auth(u1), json=doc)
    r = httpx.post(f"{BASE}/batches/{b2}/submission", headers=auth(u2), json=doc)
    check("auto sealed", r.json()["sealed_now"] is True and r.json()["stage"] == "sealed", r.text)
    body = httpx.get(f"{BASE}/batches/{b2}").json()
    check("auto-seal results", body["reviewer1_correct"] == 2
          and body["reviewer2_correct"] == 2)
    check("arbiter never has a window",
          httpx.get(f"{BASE}/batches/{b2}/disputes",
                    headers=auth(c["arbiter_token"])).status_code == 409)

    print("19) concurrent duplicate submissions: exactly one wins")
    c = httpx.post(f"{BASE}/batches", json={
        "categories": ["a", "b"],
        "samples": [{"code": "A", "true_label": "a"}]}).json()
    b3, w1 = c["batch_id"], c["reviewer1_token"]
    results = []

    def fire():
        results.append(httpx.post(f"{BASE}/batches/{b3}/submission",
                                  headers=auth(w1),
                                  json={"classifications": {"A": "a"}},
                                  timeout=10).status_code)

    threads = [threading.Thread(target=fire) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("exactly one 201 among races", sorted(results).count(201) == 1, str(results))
    check("rest are 409", sorted(results).count(409) == 7, str(results))

    print("20) concurrent last-adjudication races: one seal, one result")
    c = httpx.post(f"{BASE}/batches", json={
        "categories": ["a", "b"],
        "samples": [{"code": "A", "true_label": "a"}]}).json()
    b4, v1, v2, va = c["batch_id"], c["reviewer1_token"], c["reviewer2_token"], c["arbiter_token"]
    httpx.post(f"{BASE}/batches/{b4}/submission", headers=auth(v1),
               json={"classifications": {"A": "a"}})
    httpx.post(f"{BASE}/batches/{b4}/submission", headers=auth(v2),
               json={"classifications": {"A": "b"}})
    outcomes = []

    def fire_adj(label):
        outcomes.append(httpx.post(f"{BASE}/batches/{b4}/adjudications",
                                   headers=auth(va), json={"code": "A", "label": label},
                                   timeout=10).status_code)

    threads = [threading.Thread(target=fire_adj, args=(l,))
               for l in ["a", "b"] * 4]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("one adjudication inserted", sorted(outcomes).count(201) == 1, str(outcomes))
    body = httpx.get(f"{BASE}/batches/{b4}").json()
    check("single deterministic result", body["stage"] == "sealed"
          and len(body["samples"]) == 1 and body["samples"][0]["final_label"] in {"a", "b"})

    print("21) restart persistence")
    server.should_exit = True
    time.sleep(1.5)
    server2 = start_server()
    body = httpx.get(f"{BASE}/batches/{bid}").json()
    check("old batch survives restart", body["stage"] == "sealed"
          and body["reviewer1_correct"] == 3 and len(body["samples"]) == 4)
    r = httpx.post(f"{BASE}/batches/{bid}/submission", headers=auth(t1),
                   json={"classifications": sub1})
    check("still immutable after restart", r.status_code == 409)
    body3 = httpx.get(f"{BASE}/batches/{b3}").json()
    check("accepting batch survives", body3["stage"] == "accepting_submissions"
          and body3["sample_codes"] == ["A"])
    server2.should_exit = True
    time.sleep(1)

    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
