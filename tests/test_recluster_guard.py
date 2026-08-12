#!/usr/bin/env python3
"""
Guard test for scripts/backfill_embeddings.py --recluster.

--recluster resets every dedup field on every embedded lead. Leads a human
already resolved in the dashboard (dup_review "merged" / "separate") must be
held back from that reset, and --force-all must still be able to discard them.
This breaks silently — a regression looks like a normal run, and the only
symptom is resolved pairs quietly re-appearing as "pending" days later.

Runs offline against a throwaway SQLite DB with fake embeddings; no Voyage API
key needed.

Usage:  python tests/test_recluster_guard.py
"""
import os, sys, json, shutil, sqlite3, tempfile, importlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from database.models import init_database

# Fake embeddings: A/A2/A3 are near-duplicates, B and C are far from everything.
VEC = {
    "A":  [1.0, 0.0, 0.0, 0.0],
    "A2": [0.995, 0.1, 0.0, 0.0],   # cos(A, A2) ~= 0.995 -> auto-merge
    "A3": [0.99, 0.14, 0.0, 0.0],   # also merges into A
    "B":  [0.0, 1.0, 0.0, 0.0],
    "C":  [0.0, 0.0, 1.0, 0.0],
}

# id, title, vec, is_dup, group, known_ref, susp_of, susp_score, dup_review
#
# #3 and #4 are the human decisions under test. #4 is deliberately merged into
# #6 (not #1) because #6 is itself re-clustered into #1's group — that is the
# case where preserving the row verbatim is not enough and the stored
# dedup_group_id has to follow its canonical.
ROWS = [
    (1, "Alpha canonical",    "A",  None, None, None,            None, None, None),
    (2, "Alpha restated",     "A2", None, None, None,            None, None, None),
    (3, "Beta kept separate", "B",  None, "3",  None,            1,    0.84, "separate"),
    (4, "Alpha manual merge", "A3", 1,    "6",  "Alpha sibling", 6,    0.90, "merged"),
    (5, "Gamma unique",       "C",  None, None, None,            None, None, None),
    (6, "Alpha sibling",      "A2", None, "6",  None,            None, None, None),
]

TMP = tempfile.mkdtemp(prefix="recluster_guard_")
_case = [0]
DB = None


def build_db():
    """Fresh DB per case — SQLAlchemy keeps the previous file handle open."""
    global DB
    _case[0] += 1
    DB = os.path.join(TMP, f"case_{_case[0]}.db")
    init_database(f"sqlite:///{DB}")
    c = sqlite3.connect(DB)
    c.execute("DELETE FROM leads")
    for (i, title, v, isdup, gid, ref, so, ss, dr) in ROWS:
        c.execute(
            "INSERT INTO leads (id,title,company,israeli_law_basis,embedding,"
            "is_duplicate_of_known,dedup_group_id,known_case_ref,suspected_dup_of,"
            "suspected_dup_score,dup_review) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (i, title, "Co", "basis", json.dumps(VEC[v]), isdup, gid, ref, so, ss, dr),
        )
    c.commit()
    c.close()


def snapshot():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT id,dedup_group_id,is_duplicate_of_known,known_case_ref,"
        "suspected_dup_of,suspected_dup_score,dup_review FROM leads ORDER BY id"
    ).fetchall()
    c.close()
    # id -> (group, is_dup, known_ref, susp_of, susp_score, dup_review)
    return {r[0]: r[1:] for r in rows}


class _FakeDedup:
    """Stands in for SemanticDeduplicator: no network, all leads pre-embedded."""
    enabled = True

    def __init__(self, *a, **k):
        pass

    def compute_embeddings_batch(self, texts):
        return [[] for _ in texts]


def run(argv):
    """Invoke backfill_embeddings.main() against the throwaway DB."""
    import analysis.dedup as dedup_mod
    dedup_mod.SemanticDeduplicator = _FakeDedup

    bf = importlib.import_module("scripts.backfill_embeddings")
    importlib.reload(bf)
    bf.SemanticDeduplicator = _FakeDedup
    bf.DATABASE_URL = f"sqlite:///{DB}"
    bf.SLEEP_BETWEEN_BATCHES = 0

    # Silence the script's own output (including argparse's usage dump on the
    # rejection case) so only PASS/FAIL lines show.
    old_argv, old_out, old_err = sys.argv, sys.stdout, sys.stderr
    sys.argv = ["backfill_embeddings.py"] + argv
    sink = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = sink
    try:
        bf.main()
    finally:
        sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
        sink.close()


FAILURES = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main():
    print("\n### --recluster --dry-run writes nothing")
    build_db()
    before = snapshot()
    run(["--recluster", "--dry-run"])
    check("DB byte-identical after dry run", snapshot() == before)

    print("\n### --recluster preserves manual decisions")
    build_db()
    run(["--recluster"])
    a = snapshot()
    check("#3 keep-separate intact (dup_review='separate', own group, ref kept)",
          a[3][5] == "separate" and a[3][0] == "3" and a[3][3] == 1)
    check("#4 manual merge intact (dup_review='merged', is_duplicate=1)",
          a[4][5] == "merged" and a[4][1] == 1)
    check("#4 re-pointed to canonical #6's new group",
          a[4][0] == a[6][0])
    check("#2 still auto-merged into #1 by the recluster",
          a[2][1] == 1 and a[2][0] == "1")
    check("#5 stayed canonical", a[5][0] == "5" and not a[5][1])

    print("\n### --force-all discards manual decisions")
    build_db()
    run(["--recluster", "--force-all"])
    a = snapshot()
    check("#3 manual 'separate' cleared", a[3][5] is None)
    check("#4 manual 'merged' re-derived by clustering",
          a[4][5] is None and a[4][3] is None)

    print("\n### --force-all without --recluster is rejected")
    build_db()
    try:
        run(["--force-all"])
        check("rejected", False)
    except SystemExit as e:
        check("rejected with argparse exit code 2", e.code == 2)

    print("\n### incremental run (no --recluster) leaves decisions alone")
    build_db()
    c = sqlite3.connect(DB)
    c.execute("UPDATE leads SET dedup_group_id=NULL WHERE id=5")
    c.commit()
    c.close()
    run([])
    a = snapshot()
    check("#3/#4 untouched by incremental run",
          a[3][5] == "separate" and a[4][5] == "merged")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)  # open handles on Windows
    sys.exit(code)
