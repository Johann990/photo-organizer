"""
Tests for the `audit` DB-quality checker (photo_organizer.auditor).

`audit` is a read-only consistency pass over the decision ledger — distinct
from `validate` (pre-flight, samples disk) and `reconcile` (conservation
proof, asks "did any file vanish"). `audit` asks "is the DB's own data
self-consistent and were past bugs (event-name residue) actually cleaned up".

Each check gets a dirty-DB case (one violating row -> nonzero count, correct
severity) plus one shared clean-DB case asserting all checks are zero.
"""

from __future__ import annotations

from photo_organizer.auditor import (
    ERROR,
    WARN,
    audit,
)
from photo_organizer.db import Database


def _add_file(db, path, *, file_type="CAMERA_JPEG", status="scanned", **extra) -> int:
    cols = ["path", "filename", "extension", "file_type", "status"]
    vals = [str(path), str(path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1], "jpg", file_type, status]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    placeholders = ",".join("?" * len(cols))
    cur = db.conn.execute(
        f"INSERT INTO files ({','.join(cols)}) VALUES ({placeholders})", vals
    )
    return cur.lastrowid


def _add_op(db, file_id, op_type, status, *, source="/src", target="/dst"):
    cur = db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?,?,?,?,?)",
        (file_id, op_type, source, target, status),
    )
    return cur.lastrowid


def _add_dup(db, a, b, dup_type, *, keep_file_id=None, status="pending", hamming=0):
    db.conn.execute(
        "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, hamming_distance, "
        "keep_file_id, status) VALUES (?,?,?,?,?,?)",
        (a, b, dup_type, hamming, keep_file_id, status),
    )


def _build_clean_db(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        # one healthy file with a healthy MOVE op, properly resolved dup pair
        a = _add_file(db, tmp_path / "Masters" / "2020" / "a.jpg", status="done",
                      date_confidence="HIGH")
        b = _add_file(db, tmp_path / "src" / "b.jpg", status="done",
                      date_confidence="HIGH")
        _add_op(db, a, "MOVE", "done", target=str(tmp_path / "Masters" / "2020" / "a.jpg"))
        _add_op(db, b, "STAGE_DELETE", "done")
        _add_dup(db, a, b, "EXACT", keep_file_id=a, status="resolved")
        db.commit()
    return db_path


def test_clean_db_has_no_findings(tmp_path):
    db_path = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        report = audit(db)
    assert report.exit_ok
    for check in report.checks:
        assert check.count == 0, f"{check.name} unexpectedly found {check.count}"


def test_orphan_operation_file_id_is_error(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        # A real orphan can only arise from an edit that bypassed FK
        # enforcement — e.g. a one-off raw sqlite3.connect() fix script
        # (this project's own diagnostic scripts this session did exactly
        # that, never going through the Database wrapper). Simulate it the
        # same way: FK off for the violating insert only.
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (999, 'MOVE', '/x', 'done')"
        )
        db.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")
        report = audit(db)
    check = report.find("orphan_operations")
    assert check.severity == ERROR
    assert check.count == 1
    assert not report.exit_ok


def test_dangling_duplicate_reference_is_error(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        a = _add_file(db, tmp_path / "a.jpg")
        db.commit()  # PRAGMA foreign_keys is a no-op inside an open transaction
        # file_id_b points nowhere — same FK-bypass scenario as above
        db.conn.execute("PRAGMA foreign_keys=OFF")
        _add_dup(db, a, 999, "EXACT")
        db.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")
        report = audit(db)
    check = report.find("dangling_duplicates")
    assert check.severity == ERROR
    assert check.count == 1
    assert not report.exit_ok


def test_keep_all_reviewed_decision_is_not_flagged(tmp_path):
    """'reviewed' + keep_file_id IS NULL is the DESIGNED encoding for a
    'keep all' decision on a NEAR cluster (reviewer._record_decision:
    keeper=None records keep_file_id=NULL so plan stages nothing) — it must
    NOT be treated as a corrupted/incomplete review."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        a = _add_file(db, tmp_path / "a.jpg")
        b = _add_file(db, tmp_path / "b.jpg")
        _add_dup(db, a, b, "NEAR", keep_file_id=None, status="reviewed")
        db.commit()
        report = audit(db)
    assert report.exit_ok
    # the two duplicate-bookkeeping checks must see nothing wrong here — only
    # date_confidence is unset in this fixture (irrelevant to this scenario)
    assert report.find("self_referential_duplicate").count == 0
    assert report.find("dangling_duplicates").count == 0


def test_self_referential_duplicate_is_error(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        a = _add_file(db, tmp_path / "a.jpg")
        _add_dup(db, a, a, "EXACT")  # file_id_a == file_id_b — both satisfy the FK fine
        db.commit()
        report = audit(db)
    check = report.find("self_referential_duplicate")
    assert check.severity == ERROR
    assert check.count == 1
    assert not report.exit_ok


def test_duplicate_current_path_is_error(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        same = str(tmp_path / "Masters" / "2020" / "a.jpg")
        _add_file(db, same, status="done")
        # second row claims the exact same current path (UNIQUE constraint
        # is on `path`, so simulate via direct insert bypassing the helper's
        # default path — use a second physically-identical insert through a
        # different file_id by inserting raw SQL with INSERT OR IGNORE off
        # is not possible since path is UNIQUE; instead this check targets
        # operations.target_path collisions between two DIFFERENT done files,
        # which the schema does NOT forbid at the operations level.
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status) "
            "VALUES (?,?,?,?,?)",
            (str(tmp_path / "Masters" / "2020" / "a (2).jpg"), "a (2).jpg", "jpg",
             "CAMERA_JPEG", "done"),
        )
        fid2 = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _add_op(db, fid2, "MOVE", "done", target=same)
        fid1 = db.conn.execute(
            "SELECT file_id FROM files WHERE path=?", (same,)
        ).fetchone()[0]
        _add_op(db, fid1, "MOVE", "done", target=same)
        db.commit()
        report = audit(db)
    check = report.find("target_path_collision")
    assert check.severity == ERROR
    assert check.count >= 1
    assert not report.exit_ok


def test_event_name_residue_in_done_op_is_error(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        target = str(tmp_path / "Masters" / "2020" / "2020-01-12 20200112三貂嶺_小蜂" / "a.jpg")
        fid = _add_file(db, target, status="done")
        _add_op(db, fid, "MOVE", "done", target=target)
        db.commit()
        report = audit(db)
    check = report.find("event_name_residue_done")
    assert check.severity == ERROR
    assert check.count == 1
    assert not report.exit_ok


def test_event_name_residue_in_pending_op_is_warn(tmp_path):
    """Leftover '(Nd)' day-count token leaking into the label itself — the
    other bug fixed in _sanitize_event, still in scope here (unlike the
    AMBIGUOUS-separator date-RANGE tail-day residue, which needs EXIF date
    context this DB-only label scan doesn't have; see
    test_event_name_residue_pending_ignores_date_range_tail below)."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        target = str(tmp_path / "Masters" / "2020" / "2020-01-30(4d) 4d_台南移地訓練" / "a.jpg")
        fid = _add_file(db, tmp_path / "src" / "a.jpg", status="confirmed")
        _add_op(db, fid, "MOVE", "confirmed", target=target)
        db.commit()
        report = audit(db)
    check = report.find("event_name_residue_pending")
    assert check.severity == WARN
    assert check.count == 1
    # a WARN-only finding must not fail the gate
    assert report.exit_ok


def test_event_name_residue_pending_ignores_date_range_tail(tmp_path):
    """_sanitize_event's '~'/'&' date-RANGE separators (e.g. "20080614~23
    NICE 出差") now strip unconditionally at the source, so a FRESH plan
    never produces that residue. But '_'/'-' on a compact date stay
    deliberately ambiguous without EXIF date corroboration (collides with a
    real short numeric label like "101_煙火" — Taipei 101) — this DB-only
    label scan has no date context to supply, so a leftover "19_清水溪" style
    tail (from "20020413_19 清水溪") must NOT be flagged here. Flagging it
    would be a false positive against _sanitize_event's intentional,
    by-design conservatism, not a real bug."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        target = str(tmp_path / "Masters" / "2002" / "2002-04-13(7d) 19_清水溪" / "a.jpg")
        fid = _add_file(db, tmp_path / "src" / "a.jpg", status="confirmed")
        _add_op(db, fid, "MOVE", "confirmed", target=target)
        db.commit()
        report = audit(db)
    check = report.find("event_name_residue_pending")
    assert check.count == 0


def test_event_name_residue_pending_does_not_flag_legit_cjk_number(tmp_path):
    """'11月團集會' is a legitimate CJK name, not date residue — must not
    false-positive (mirrors test_sanitize_event's
    test_leading_date_keeps_legit_cjk_number_name)."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        target = str(tmp_path / "Masters" / "2020" / "2020-11-15 11月團集會" / "a.jpg")
        fid = _add_file(db, tmp_path / "src" / "a.jpg", status="confirmed")
        _add_op(db, fid, "MOVE", "confirmed", target=target)
        db.commit()
        report = audit(db)
    check = report.find("event_name_residue_pending")
    assert check.count == 0


def test_cross_base_empty_twin_is_warn(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        populated = tmp_path / "Masters" / "2020" / "2020-07-11(2d) 貢寮浮潛" / "a.jpg"
        fid = _add_file(db, populated, status="done")
        _add_op(db, fid, "MOVE", "done", target=str(populated))
        # Others/ twin of the same event folder name, but with zero files —
        # represented as a stray done-op'd file whose dirname differs only by
        # the Masters/Others base segment, then immediately staged out. We
        # model "empty" by simply asserting no files row exists there at all;
        # the check must discover the empty twin by walking target_path dirs
        # in operations, not by listing disk. So instead exercise the case
        # via duplicates that left one side with zero surviving MOVE targets.
        loser_dir = tmp_path / "Others" / "2020" / "2020-07-11(2d) 貢寮浮潛"
        loser = loser_dir / "a.jpg"
        fid2 = _add_file(db, tmp_path / "src2" / "a.jpg", status="done")
        _add_op(db, fid2, "STAGE_DELETE", "done", target=str(loser))
        db.commit()
        report = audit(db)
    check = report.find("cross_base_empty_twin")
    assert check.severity == WARN
    assert check.count == 1
    assert report.exit_ok


def test_low_confidence_date_summary_is_warn(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, tmp_path / "a.jpg", date_confidence="LOW")
        db.commit()
        report = audit(db)
    check = report.find("low_confidence_dates")
    assert check.severity == WARN
    assert check.count == 1
    assert report.exit_ok


def test_audit_is_idempotent_in_run_log(tmp_path):
    """Re-running audit must not accumulate duplicate run_log rows from a
    prior pass — it clears its own phase='audit' entries first."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (999, 'MOVE', '/x', 'done')"
        )
        db.commit()
        audit(db)
        audit(db)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE phase='audit'"
        ).fetchone()[0]
    # exactly one logged finding for the single orphan op, not two
    assert n == 1
