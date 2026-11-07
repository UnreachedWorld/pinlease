# Tests for Pinlease's first slice (spec section 5, items 1-11).
# A fake host is a local git checkout. Nothing here installs or moves a real machine.
# The rules are in SPEC.md, in this folder.

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pinlease import (
    Cut,
    Refused,
    Store,
    Uncertain,
    main,
    ordinary_job_refusal,
)


def git(repo, *args):
    """Run one git command in the fake host and return its text."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_host(path):
    """Make a clean fake host with two commits. Returns (home id, other id)."""
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "pinlease-test@example.com")
    git(path, "config", "user.name", "Pinlease Test")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgsign", "false")
    (path / "readme.txt").write_text("home\n")
    git(path, "add", "readme.txt")
    git(path, "commit", "-m", "home")
    home = git(path, "rev-parse", "HEAD")
    (path / "readme.txt").write_text("other\n")
    git(path, "add", "readme.txt")
    git(path, "commit", "-m", "other")
    other = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "--detach", home)
    return home, other


def clock_at(start):
    """Return a clock the test can move by writing now[0]."""
    now = [start]

    def clock():
        return now[0]

    return now, clock


def new_store(tmp, host, clock, holder_alive=None, hooks=None, faults=None):
    """Open a store on a new lease folder for this fake host."""
    lease_dir = tmp / "lease"
    lease_dir.mkdir(exist_ok=True)
    store = Store(
        lease_dir=lease_dir,
        host_name="box",
        checkout=host,
        clock=clock,
        holder_alive=holder_alive,
        hooks=hooks,
        faults=faults,
    )
    return store


def test_borrow_records_lease_and_status(tmp_path):
    """Item 1: borrow puts the commit on the host and status shows the loan."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(1000)
    store = new_store(tmp_path, host, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=30 * 60000,
        why="link cases",
        borrower="ada",
    )
    assert git(host, "rev-parse", "HEAD") == other
    view = store.status()
    assert view["id"] == lease_id
    assert view["holder"] == "ada"
    assert view["commit"] == other
    assert view["why"] == "link cases"
    assert view["since_ms"] == 1000
    assert view["until_ms"] == 1000 + 30 * 60000
    assert view["overdue"] is False
    assert view["state"] == "HELD"


def test_a_worktree_checkout_leases_and_releases(tmp_path):
    """A linked git worktree can be leased and put back. The main checkout stays."""
    host = tmp_path / "host"
    home, other = make_host(host)
    linked = tmp_path / "linked"
    git(host, "worktree", "add", "--detach", str(linked), home)
    assert (linked / ".git").is_file()
    now, clock = clock_at(1000)
    store = new_store(tmp_path, linked, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="worktree",
        borrower="ada",
    )
    assert git(linked, "rev-parse", "HEAD") == other
    assert git(host, "rev-parse", "HEAD") == home
    store.release(lease_id, borrower="ada")
    assert git(linked, "rev-parse", "HEAD") == home
    assert git(host, "rev-parse", "HEAD") == home
    assert store.status()["state"] == "FREE"
    root = Path(__file__).resolve().parents[1]
    assert "A checkout can be a git worktree." in (root / "README.md").read_text()
    assert "A checkout may be a linked git worktree." in (root / "SPEC.md").read_text()


def test_release_restores_home_and_tells_next(tmp_path):
    """Item 2: release restores the home pin, frees the lock, and tells the waiter."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
    )
    seen = []
    started = threading.Event()

    def on_wait():
        seen.append(git(host, "rev-parse", "HEAD"))
        started.set()

    def waiter():
        store.borrow(
            host="box",
            commit=home,
            duration_ms=1000,
            why="second",
            borrower="bea",
            on_wait=on_wait,
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    assert started.wait(timeout=5)
    assert seen[-1] == other
    store.release(first, borrower="ada")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert git(host, "rev-parse", "HEAD") == home
    assert store.status()["state"] == "HELD"
    assert store.status()["holder"] == "bea"
    log = (tmp_path / "lease" / "lease.log").read_text()
    assert "bea" in log
    assert ordinary_job_refusal(tmp_path / "lease") is not None


def test_second_borrower_waits_until_release(tmp_path):
    """Item 3: the second borrower does not get the host before the first release."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    (host / "third.txt").write_text("third\n")
    git(host, "add", "third.txt")
    git(host, "commit", "-m", "third")
    third = git(host, "rev-parse", "HEAD")
    git(host, "checkout", "--detach", home)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
    )
    heads = []
    started = threading.Event()

    def on_wait():
        heads.append(git(host, "rev-parse", "HEAD"))
        started.set()

    result = {}

    def waiter():
        result["id"] = store.borrow(
            host="box",
            commit=third,
            duration_ms=1000,
            why="second",
            borrower="bea",
            on_wait=on_wait,
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    assert started.wait(timeout=5)
    assert all(item == other for item in heads)
    assert git(host, "rev-parse", "HEAD") == other
    store.release(first, borrower="ada")
    thread.join(timeout=5)
    assert result["id"] != first
    assert git(host, "rev-parse", "HEAD") == third


def test_lost_holder_stays_excluded_without_a_process(tmp_path):
    """Item 4: a dead borrower stays excluded after the lock holder is gone too."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock, holder_alive=lambda: True)
    store.borrow(
        host="box",
        commit=other,
        duration_ms=5000,
        why="loan",
        borrower="ada",
    )
    lost = Store(
        lease_dir=tmp_path / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
        holder_alive=lambda: False,
    )
    view = lost.status()
    assert view["holder"] == "UNKNOWN"
    assert view["state"] == "HELD"
    assert view["overdue"] is False
    assert git(host, "rev-parse", "HEAD") == other
    text = ordinary_job_refusal(tmp_path / "lease")
    assert text == "held by lease L1 (UNKNOWN, not overdue)"
    with pytest.raises(Refused):
        lost.borrow(
            host="box",
            commit=home,
            duration_ms=1000,
            why="next",
            borrower="bea",
            wait=False,
        )
    assert ordinary_job_refusal(tmp_path / "lease") == text
    assert git(host, "rev-parse", "HEAD") == other


def test_overdue_is_independent_of_unknown(tmp_path):
    """Item 5: overdue does not move the host, and status shows holder and overdue apart."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    alive = {"ok": True}
    store = new_store(tmp_path, host, clock, holder_alive=lambda: alive["ok"])
    store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    now[0] = 1000
    live = store.status()
    assert live["holder"] == "ada"
    assert live["overdue"] is True
    assert git(host, "rev-parse", "HEAD") == other
    now[0] = 100
    alive["ok"] = False
    lost = store.status()
    assert lost["holder"] == "UNKNOWN"
    assert lost["overdue"] is False
    now[0] = 5000
    both = store.status()
    assert both["holder"] == "UNKNOWN"
    assert both["overdue"] is True
    assert git(host, "rev-parse", "HEAD") == other


def test_human_release_names_the_human(tmp_path):
    """Item 6: a named human ends an UNKNOWN lease and the log names them."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock, holder_alive=lambda: True)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    lost = Store(
        lease_dir=tmp_path / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
        holder_alive=lambda: False,
    )
    with pytest.raises(Refused):
        lost.release(lease_id, by_human="Sam")
    lost.release(lease_id, by_human="Sam", reason="borrower gone")
    assert git(host, "rev-parse", "HEAD") == home
    assert lost.status()["state"] == "FREE"
    log = (tmp_path / "lease" / "lease.log").read_text()
    assert "Sam" in log
    assert "borrower gone" in log


def test_bad_commit_frees_and_leaves_the_host(tmp_path):
    """Item 7: a missing commit is refused, the host stays put, and the lock is free."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit="deadbeef",
            duration_ms=1000,
            why="no such commit",
            borrower="ada",
        )
    assert git(host, "rev-parse", "HEAD") == home
    assert store.status()["state"] == "FREE"
    assert ordinary_job_refusal(tmp_path / "lease") is None
    store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="later",
        borrower="ada",
    )
    assert git(host, "rev-parse", "HEAD") == other


def test_dirty_checkout_is_untouched(tmp_path):
    """Item 8: a dirty checkout is refused and the file bytes stay the same."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    before = (host / "readme.txt").read_bytes()
    (host / "readme.txt").write_bytes(before + b"dirty\n")
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="should not move",
            borrower="ada",
        )
    assert (host / "readme.txt").read_bytes() == before + b"dirty\n"
    assert git(host, "status", "--porcelain") != ""
    raw = (tmp_path / "lease" / "lease.json").read_text() if (tmp_path / "lease" / "lease.json").exists() else ""
    assert '"state": "HELD"' not in raw
    assert git(host, "rev-parse", "HEAD") == home


def test_cut_points_fail_closed(tmp_path):
    """Item 9: a crash at or after checkout stays held; only a pre-checkout failure frees."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)

    def crashed(hooks=None, faults=None):
        store = new_store(tmp_path, host, clock, hooks=hooks, faults=faults)
        with pytest.raises((Cut, Uncertain)):
            store.borrow(
                host="box",
                commit=other,
                duration_ms=1000,
                why="cut",
                borrower="ada",
            )
        again = Store(
            lease_dir=tmp_path / "lease",
            host_name="box",
            checkout=host,
            clock=clock,
            holder_alive=lambda: True,
        )
        view = again.status(check=True)
        assert view["state"] == "HELD"
        assert view["holder"] == "UNKNOWN"
        assert view["observed_commit"] == git(host, "rev-parse", "HEAD")
        with pytest.raises(Refused):
            again.borrow(
                host="box",
                commit=home,
                duration_ms=1000,
                why="blocked",
                borrower="bea",
                wait=False,
            )
        return again

    def _raise_cut():
        raise Cut("intent")

    crashed(hooks={"after_intent": _raise_cut})
    assert git(host, "rev-parse", "HEAD") == home

    # Fresh folder for the next cut, so the held lease is not the previous one.
    # The helper above reuses tmp/lease. Clear it by using separate folders below.


def test_each_cut_point(tmp_path):
    """Item 9, one folder each: intent, lost read-back, record not updated, restore not marked."""
    cases = []

    def run_case(name, hooks, faults, expect_commit):
        root = tmp_path / name
        root.mkdir()
        host = root / "host"
        home, other = make_host(host)
        now, clock = clock_at(0)
        store = Store(
            lease_dir=root / "lease",
            host_name="box",
            checkout=host,
            clock=clock,
            hooks=hooks,
            faults=faults,
        )
        (root / "lease").mkdir(exist_ok=True)
        store = Store(
            lease_dir=root / "lease",
            host_name="box",
            checkout=host,
            clock=clock,
            hooks=hooks,
            faults=faults,
        )
        with pytest.raises((Cut, Uncertain)):
            store.borrow(
                host="box",
                commit=other,
                duration_ms=1000,
                why=name,
                borrower="ada",
            )
        again = Store(
            lease_dir=root / "lease",
            host_name="box",
            checkout=host,
            clock=clock,
            holder_alive=lambda: True,
        )
        view = again.status(check=True)
        assert view["holder"] == "UNKNOWN"
        assert view["state"] == "HELD"
        assert view["observed_commit"] == expect_commit(home, other, host)
        assert ordinary_job_refusal(root / "lease") is not None
        cases.append(name)

    def boom():
        raise Cut("cut")

    run_case(
        "after-intent",
        {"after_intent": boom},
        None,
        lambda home, other, host: home,
    )
    run_case(
        "readback-fails",
        None,
        {"readback"},
        lambda home, other, host: other,
    )
    run_case(
        "record-not-updated",
        {"after_readback": boom},
        None,
        lambda home, other, host: other,
    )

    root = tmp_path / "restore"
    root.mkdir()
    host = root / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    (root / "lease").mkdir()
    store = Store(
        lease_dir=root / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
    )
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="restore cut",
        borrower="ada",
    )

    def boom_restore():
        raise Cut("restore")

    cutting = Store(
        lease_dir=root / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
        hooks={"after_restore_readback": boom_restore},
    )
    with pytest.raises(Cut):
        cutting.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == home
    again = Store(
        lease_dir=root / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
        holder_alive=lambda: True,
    )
    view = again.status(check=True)
    assert view["state"] == "HELD"
    assert view["holder"] == "UNKNOWN"
    assert view["observed_commit"] == home
    with pytest.raises(Refused):
        again.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="still held",
            borrower="bea",
            wait=False,
        )

    # A clean failure before checkout frees the lock and leaves the host.
    root = tmp_path / "before"
    root.mkdir()
    host = root / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    (root / "lease").mkdir()
    store = Store(
        lease_dir=root / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
        faults={"copy"},
    )
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="copy fails",
            borrower="ada",
        )
    assert git(host, "rev-parse", "HEAD") == home
    assert ordinary_job_refusal(root / "lease") is None
    assert cases == ["after-intent", "readback-fails", "record-not-updated"]


def test_non_lease_lock_waits_or_refuses(tmp_path):
    """Item 10: a non-lease flock makes --no-wait refuse and changes nothing."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    store = Store(
        lease_dir=lease_dir,
        host_name="box",
        checkout=host,
        clock=clock,
    )
    lock_path = lease_dir / "pinlease.lock"
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, os, sys\n"
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "sys.stdin.readline()\n"
            ),
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    line = holder.stdout.readline()
    assert line.strip() == "locked"
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="no wait",
            borrower="ada",
            wait=False,
        )
    assert git(host, "rev-parse", "HEAD") == home
    released = threading.Event()

    def on_wait():
        if not released.is_set():
            holder.stdin.write("go\n")
            holder.stdin.flush()
            released.set()

    store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="after lock",
        borrower="ada",
        on_wait=on_wait,
    )
    holder.wait(timeout=5)
    assert git(host, "rev-parse", "HEAD") == other


def test_planted_secret_is_not_stored(tmp_path):
    """Item 11: a planted marker is refused before the lease file or log is written."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="see PLANTED-SECRET-MARKER",
            borrower="ada",
        )
    raw_json = ""
    raw_log = ""
    if (tmp_path / "lease" / "lease.json").exists():
        raw_json = (tmp_path / "lease" / "lease.json").read_text()
    if (tmp_path / "lease" / "lease.log").exists():
        raw_log = (tmp_path / "lease" / "lease.log").read_text()
    assert "PLANTED-SECRET-MARKER" not in raw_json
    assert "PLANTED-SECRET-MARKER" not in raw_log
    assert git(host, "rev-parse", "HEAD") == home
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="fine",
            borrower="ada",
            settings_text="host token PLANTED-SECRET-MARKER",
        )
    assert "PLANTED-SECRET-MARKER" not in (tmp_path / "lease" / "lease.json").read_text() if (tmp_path / "lease" / "lease.json").exists() else True


def test_home_refused_while_held_and_release_uses_grant_pin(tmp_path):
    """Home changes nothing while a lease is held. Release restores the grant pin."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    store.set_home("box", home)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    with pytest.raises(Refused):
        store.set_home("box", other)
    store.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == home
    store.set_home("box", other)
    assert store.status()["state"] == "FREE"
    assert git(host, "rev-parse", "HEAD") == home


def test_repeat_release_does_not_move_the_host(tmp_path):
    """A second release of the same id does not check the home pin out again."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    store.release(lease_id, borrower="ada")
    git(host, "checkout", "--detach", other)
    store.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == other
    with pytest.raises(Refused):
        store.release("L9", borrower="ada")


def test_wrong_borrower_is_refused(tmp_path):
    """A borrower release must name the stored borrower. It is an audit check, not a login."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    with pytest.raises(Refused):
        store.release(lease_id, borrower="eve")
    assert git(host, "rev-parse", "HEAD") == other


def test_unknown_schema_stays_excluded_and_unmodified(tmp_path):
    """A schema this tool does not know is refused and the file bytes stay."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    body = (
        '{\n'
        '  "schema_version": 99,\n'
        '  "lease": {"id": "L1", "state": "HELD", "holder": "ada", "overdue": false}\n'
        '}\n'
    )
    path = lease_dir / "lease.json"
    path.write_text(body)
    store = Store(
        lease_dir=lease_dir,
        host_name="box",
        checkout=host,
        clock=clock,
    )
    with pytest.raises(Refused):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="no",
            borrower="bea",
            wait=False,
        )
    assert path.read_text() == body
    assert ordinary_job_refusal(lease_dir) == "held by lease L1 (ada, not overdue)"
    path.write_text("{not json")
    broken = path.read_text()
    with pytest.raises(Refused):
        store.status()
    assert path.read_text() == broken
    assert ordinary_job_refusal(lease_dir) == "lease record unreadable"


def test_stale_release_after_a_later_lease_is_refused(tmp_path):
    """Releasing an older id must not restore over the borrower who holds it now."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
    )
    store.release(first, borrower="ada")
    (host / "third.txt").write_text("third\n")
    git(host, "add", "third.txt")
    git(host, "commit", "-m", "third")
    third = git(host, "rev-parse", "HEAD")
    git(host, "checkout", "--detach", home)
    second = store.borrow(
        host="box",
        commit=third,
        duration_ms=1000,
        why="second",
        borrower="bea",
    )
    with pytest.raises(Refused):
        store.release(first, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == third
    assert second != first


def test_cli_exit_codes(tmp_path):
    """Exit 0 is success, 2 is a refusal, 3 is an uncertain move."""
    host = tmp_path / "host"
    home, other = make_host(host)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "lease_dir: %s\nhosts:\n  box:\n    checkout: %s\n" % (lease_dir, host)
    )
    code = main(
        [
            "borrow",
            "box",
            "--commit",
            other,
            "--for",
            "30m",
            "--why",
            "cli",
            "--borrower",
            "ada",
            "--settings",
            str(settings),
        ]
    )
    assert code == 0
    assert git(host, "rev-parse", "HEAD") == other
    status = main(["status", "box", "--settings", str(settings)])
    assert status == 0
    refused = main(
        [
            "borrow",
            "box",
            "--commit",
            home,
            "--for",
            "5m",
            "--why",
            "busy",
            "--borrower",
            "bea",
            "--settings",
            str(settings),
            "--no-wait",
        ]
    )
    assert refused == 2
    released = main(
        ["release", "L1", "--borrower", "ada", "--settings", str(settings)]
    )
    assert released == 0
    assert git(host, "rev-parse", "HEAD") == home


def test_unknown_state_is_not_treated_as_free(tmp_path):
    """A stored state other than HELD or RELEASED stays excluded and is not rewritten."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    path = tmp_path / "lease" / "lease.json"
    data = json.loads(path.read_text())
    data["lease"]["state"] = "MOVING"
    path.write_text(json.dumps(data))
    body = path.read_text()
    assert ordinary_job_refusal(tmp_path / "lease") == "held by lease L1 (ada, not overdue)"
    fresh = Store(
        lease_dir=tmp_path / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
    )
    with pytest.raises(Refused):
        fresh.borrow(
            host="box",
            commit=home,
            duration_ms=1000,
            why="should not replace",
            borrower="bea",
            wait=False,
        )
    assert path.read_text() == body
    assert json.loads(path.read_text())["lease"]["id"] == first
    assert git(host, "rev-parse", "HEAD") == other


def test_release_refuses_a_different_checkout(tmp_path):
    """Release must not restore a different checkout or clear the original lease."""
    first = tmp_path / "a"
    home, other = make_host(first)
    second = tmp_path / "b"
    shutil.copytree(first, second)
    now, clock = clock_at(0)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    store = Store(
        lease_dir=lease_dir,
        host_name="host-a",
        checkout=first,
        clock=clock,
    )
    lease_id = store.borrow(
        host="host-a",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    other_store = Store(
        lease_dir=lease_dir,
        host_name="host-b",
        checkout=second,
        clock=clock,
    )
    with pytest.raises(Refused):
        other_store.release(lease_id, borrower="ada")
    assert git(first, "rev-parse", "HEAD") == other
    assert git(second, "rev-parse", "HEAD") == home
    assert ordinary_job_refusal(lease_dir) is not None
    assert store.status()["state"] == "HELD"
    assert store.status()["id"] == lease_id


def test_git_env_cannot_redirect_the_checkout(tmp_path):
    """GIT_DIR and GIT_WORK_TREE must not make borrow move a different checkout."""
    first = tmp_path / "a"
    home, other = make_host(first)
    second = tmp_path / "b"
    shutil.copytree(first, second)
    (first / "readme.txt").write_text("dirty\n")
    now, clock = clock_at(0)
    store = Store(
        lease_dir=tmp_path / "lease",
        host_name="box",
        checkout=first,
        clock=clock,
    )
    saved = {}
    for key in ("GIT_DIR", "GIT_WORK_TREE"):
        saved[key] = os.environ.get(key)
        os.environ[key] = str(second / ".git") if key == "GIT_DIR" else str(second)
    try:
        with pytest.raises(Refused):
            store.borrow(
                host="box",
                commit=other,
                duration_ms=1000,
                why="redirect",
                borrower="ada",
            )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert git(first, "rev-parse", "HEAD") == home
    assert "dirty" in (first / "readme.txt").read_text()
    assert git(second, "rev-parse", "HEAD") == home


def test_dead_borrower_process_is_unknown(tmp_path):
    """The default path reports UNKNOWN after the borrower process is gone. P2."""
    host = tmp_path / "host"
    home, other = make_host(host)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from pinlease import Store\n"
                "store = Store(sys.argv[1], 'box', sys.argv[2])\n"
                "lease = store.borrow('box', sys.argv[3], 60000, 'loan', 'ada', wait=False)\n"
                "print(lease, flush=True)\n"
                "sys.stdin.read()\n"
            ),
            str(lease_dir),
            str(host),
            other,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline()
        assert line.strip() == "L1"
        child.terminate()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    view = Store(lease_dir=lease_dir, host_name="box", checkout=host).status()
    assert view["state"] == "HELD"
    assert view["holder"] == "UNKNOWN"
    assert git(host, "rev-parse", "HEAD") == other
    assert ordinary_job_refusal(lease_dir) is not None


def test_cli_status_shows_the_lease(tmp_path):
    """status prints the id, commit, reason, times and queue, and --check adds the read-back."""
    host = tmp_path / "host"
    home, other = make_host(host)
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir()
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "lease_dir: %s\nhosts:\n  box:\n    checkout: %s\n" % (lease_dir, host)
    )
    assert main(
        [
            "borrow",
            "box",
            "--commit",
            other,
            "--for",
            "30m",
            "--why",
            "link cases",
            "--borrower",
            "ada",
            "--settings",
            str(settings),
        ]
    ) == 0
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["status", "box", "--settings", str(settings), "--check"])
    text = out.getvalue()
    assert code == 0
    assert "L1" in text
    assert other in text
    assert "link cases" in text
    assert "since_ms" in text
    assert "until_ms" in text
    assert "queue" in text
    assert "observed_commit" in text


def test_empty_lease_payload_is_not_free(tmp_path):
    """An empty object or array is not a free lease. Null is the free sentinel."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
    )
    path = tmp_path / "lease" / "lease.json"
    for payload in ({}, []):
        data = json.loads(path.read_text())
        data["lease"] = payload
        path.write_text(json.dumps(data))
        body = path.read_text()
        assert ordinary_job_refusal(tmp_path / "lease") == "lease record unreadable"
        fresh = Store(
            lease_dir=tmp_path / "lease",
            host_name="box",
            checkout=host,
            clock=clock,
        )
        with pytest.raises(Refused):
            fresh.borrow(
                host="box",
                commit=home,
                duration_ms=1000,
                why="should not replace",
                borrower="bea",
                wait=False,
            )
        assert path.read_text() == body
        assert git(host, "rev-parse", "HEAD") == other
    data = json.loads(path.read_text())
    data["lease"] = None
    path.write_text(json.dumps(data))
    fresh = Store(
        lease_dir=tmp_path / "lease",
        host_name="box",
        checkout=host,
        clock=clock,
    )
    fresh.borrow(
        host="box",
        commit=home,
        duration_ms=1000,
        why="null is free",
        borrower="bea",
    )
    assert git(host, "rev-parse", "HEAD") == home


def _settings_file(tmp_path, host):
    """Write a one-host settings file and return its path."""
    lease_dir = tmp_path / "lease"
    lease_dir.mkdir(exist_ok=True)
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "lease_dir: %s\nhosts:\n  box:\n    checkout: %s\n" % (lease_dir, host)
    )
    return settings


def _cli_process(args):
    """Run the command line in a new process and return the finished result."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nfrom pinlease import main\nraise SystemExit(main(sys.argv[1:]))\n",
            *args,
        ],
        capture_output=True,
        text=True,
    )


def test_version_prints_the_release(capsys):
    """The version flag prints 0.1.0. It needs no settings file and moves nothing."""
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "0.1.0\n"
    assert captured.err == ""


def test_version_with_no_rules_file_is_a_refusal(tmp_path):
    """A missing rules file on --version is exit 2 and no traceback. 31 Oct late error."""
    from pathlib import Path

    script = tmp_path / "pinlease.py"
    script.write_text(Path(main.__code__.co_filename).read_text())
    result = subprocess.run(
        [sys.executable, str(script), "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "rules file could not be read\n"
    assert "Traceback" not in result.stderr


def test_cli_borrower_stays_named_after_the_command_exits(tmp_path):
    """A finished borrow command is not a lost borrower. P2."""
    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    borrowed = _cli_process(
        [
            "borrow",
            "box",
            "--commit",
            other,
            "--for",
            "30m",
            "--why",
            "loan",
            "--borrower",
            "ada",
            "--settings",
            str(settings),
        ]
    )
    assert borrowed.returncode == 0
    assert borrowed.stdout.strip() == "L1"
    assert "Traceback" not in borrowed.stderr
    status = _cli_process(["status", "box", "--settings", str(settings)])
    assert status.returncode == 0
    assert "holder ada" in status.stdout
    assert "holder UNKNOWN" not in status.stdout
    assert "state HELD" in status.stdout
    assert git(host, "rev-parse", "HEAD") == other
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["holder"] == "ada"
    assert record["lease"]["holder_pid"] is None
    assert record["lease"]["state"] == "HELD"
    log = (tmp_path / "lease" / "lease.log").read_text()
    assert "holder UNKNOWN" not in log
    assert ordinary_job_refusal(tmp_path / "lease") is not None


def test_foreign_pid_does_not_block_status_or_release(tmp_path):
    """A pid this process cannot signal becomes UNKNOWN and release still runs. P1, P2."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=60000,
        why="loan",
        borrower="ada",
    )
    path = tmp_path / "lease" / "lease.json"
    data = json.loads(path.read_text())
    data["lease"]["holder_pid"] = 1
    path.write_text(json.dumps(data))
    view = store.status()
    assert view["state"] == "HELD"
    assert view["holder"] == "UNKNOWN"
    assert view["id"] == lease_id
    assert git(host, "rev-parse", "HEAD") == other
    store.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == home
    again = store.borrow(
        host="box",
        commit=other,
        duration_ms=60000,
        why="loan",
        borrower="ada",
    )
    data = json.loads(path.read_text())
    data["lease"]["holder_pid"] = 1
    path.write_text(json.dumps(data))
    store.release(again, by_human="sam", reason="foreign pid")
    assert git(host, "rev-parse", "HEAD") == home
    settings = _settings_file(tmp_path, host)
    third = store.borrow(
        host="box",
        commit=other,
        duration_ms=60000,
        why="loan",
        borrower="ada",
    )
    data = json.loads(path.read_text())
    data["lease"]["holder_pid"] = 1
    path.write_text(json.dumps(data))
    status = _cli_process(["status", "box", "--settings", str(settings)])
    assert status.returncode == 0
    assert "Traceback" not in status.stderr
    assert "holder UNKNOWN" in status.stdout
    assert "state HELD" in status.stdout
    released = _cli_process(
        [
            "release",
            third,
            "--by-human",
            "sam",
            "--reason",
            "foreign pid",
            "--settings",
            str(settings),
        ]
    )
    assert released.returncode == 0
    assert "Traceback" not in released.stderr
    assert git(host, "rev-parse", "HEAD") == home


def test_borrow_without_a_borrower_is_refused(tmp_path):
    """A borrow with no borrower does not move the host or write a held lease. P6, P7."""
    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    import io
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(
            [
                "borrow",
                "box",
                "--commit",
                other,
                "--for",
                "30m",
                "--why",
                "loan",
                "--settings",
                str(settings),
            ]
        )
    assert code == 2
    assert "Traceback" not in err.getvalue()
    assert git(host, "rev-parse", "HEAD") == home
    record_path = tmp_path / "lease" / "lease.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        lease = record.get("lease")
        assert lease is None or lease.get("state") != "HELD"
    blank = io.StringIO()
    with contextlib.redirect_stderr(blank):
        blank_code = main(
            [
                "borrow",
                "box",
                "--commit",
                other,
                "--for",
                "30m",
                "--why",
                "loan",
                "--borrower",
                "",
                "--settings",
                str(settings),
            ]
        )
    assert blank_code == 2
    assert git(host, "rev-parse", "HEAD") == home


def test_bad_cli_args_exit_2_and_do_not_move(tmp_path):
    """Missing or bad arguments are a refusal, and a negative duration does not lend. P3."""
    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    import io
    import contextlib

    calls = [
        ["borrow", "box", "--commit", other, "--why", "loan", "--borrower", "ada", "--settings", str(settings)],
        ["borrow", "box", "--commit", other, "--for", "m", "--why", "loan", "--borrower", "ada", "--settings", str(settings)],
        ["borrow", "--settings", str(settings)],
        ["release", "--settings", str(settings)],
        ["borrow", "box", "--commit", "--for", "30m", "--why", "loan", "--borrower", "ada", "--settings", str(settings)],
        ["borrow", "box", "--commit", other, "--for", "-5m", "--why", "loan", "--borrower", "ada", "--settings", str(settings)],
    ]
    for args in calls:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(args)
        assert code == 2
        assert "Traceback" not in err.getvalue()
        assert err.getvalue().strip() != ""
        assert git(host, "rev-parse", "HEAD") == home
    record_path = tmp_path / "lease" / "lease.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        lease = record.get("lease")
        if isinstance(lease, dict) and lease.get("since_ms") is not None:
            assert lease["until_ms"] >= lease["since_ms"]
            assert lease.get("state") != "HELD"


def test_oserror_after_checkout_is_uncertain(tmp_path, monkeypatch):
    """An error after the checkout has moved is uncertain, and the lease stays held. P5."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    original = Store._hook

    def raise_os(self, name):
        if name == "after_readback":
            raise OSError("disk")
        return original(self, name)

    monkeypatch.setattr(Store, "_hook", raise_os)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(
            [
                "borrow",
                "box",
                "--commit",
                other,
                "--for",
                "30m",
                "--why",
                "loan",
                "--borrower",
                "ada",
                "--settings",
                str(settings),
            ]
        )
    assert code == 3
    assert "Traceback" not in err.getvalue()
    assert git(host, "rev-parse", "HEAD") == other
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["state"] == "HELD"
    assert record["lease"]["intent"] is not None
    assert ordinary_job_refusal(tmp_path / "lease") is not None
    status = io.StringIO()
    with contextlib.redirect_stdout(status):
        status_code = main(["status", "box", "--settings", str(settings)])
    assert status_code == 0
    assert "state HELD" in status.getvalue()
    assert "holder UNKNOWN" in status.getvalue()
    assert git(host, "rev-parse", "HEAD") == other

    cut_root = tmp_path / "cut"
    cut_root.mkdir()
    cut_host = cut_root / "host"
    cut_home, cut_other = make_host(cut_host)
    cut_settings = _settings_file(cut_root, cut_host)

    def raise_cut(self, name):
        if name == "after_readback":
            raise Cut("cut")
        return original(self, name)

    monkeypatch.setattr(Store, "_hook", raise_cut)
    cut_err = io.StringIO()
    with contextlib.redirect_stderr(cut_err):
        cut_code = main(
            [
                "borrow",
                "box",
                "--commit",
                cut_other,
                "--for",
                "30m",
                "--why",
                "loan",
                "--borrower",
                "ada",
                "--settings",
                str(cut_settings),
            ]
        )
    assert cut_code == 3
    assert "Traceback" not in cut_err.getvalue()
    assert git(cut_host, "rev-parse", "HEAD") == cut_other
    cut_record = json.loads((cut_root / "lease" / "lease.json").read_text())
    assert cut_record["lease"]["state"] == "HELD"
    assert cut_record["lease"]["intent"] is not None
    assert ordinary_job_refusal(cut_root / "lease") is not None
    missing = io.StringIO()
    with contextlib.redirect_stderr(missing):
        missing_code = main(
            [
                "borrow",
                "box",
                "--commit",
                home,
                "--for",
                "30m",
                "--why",
                "loan",
                "--borrower",
                "ada",
                "--settings",
                str(tmp_path / "missing-settings.yaml"),
            ]
        )
    assert missing_code == 2
    assert "Traceback" not in missing.getvalue()


def _borrow_args(commit, settings, span):
    """One borrow command for the late-error tests."""
    return [
        "borrow",
        "box",
        "--commit",
        commit,
        "--for",
        span,
        "--why",
        "loan",
        "--borrower",
        "ada",
        "--settings",
        str(settings),
    ]


def test_git_readback_after_checkout_is_uncertain(tmp_path, monkeypatch):
    """A git failure while reading back after checkout is uncertain. P5."""
    import io
    import contextlib
    import pinlease as pinlease_mod

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    real_run = pinlease_mod.subprocess.run

    def fail_readback(args, **kwargs):
        command = list(args)
        if (
            "rev-parse" in command
            and "HEAD" in command
            and "--verify" not in command
            and "checkout" in fail_readback.seen
        ):
            raise OSError("git spawn")
        if "checkout" in command:
            fail_readback.seen.add("checkout")
        return real_run(args, **kwargs)

    fail_readback.seen = set()
    monkeypatch.setattr(pinlease_mod.subprocess, "run", fail_readback)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(_borrow_args(other, settings, "30m"))
    assert code == 3
    assert "Traceback" not in err.getvalue()
    monkeypatch.setattr(pinlease_mod.subprocess, "run", real_run)
    assert git(host, "rev-parse", "HEAD") == other
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["state"] == "HELD"
    assert record["lease"]["intent"] is not None
    assert ordinary_job_refusal(tmp_path / "lease") is not None

    early = tmp_path / "early"
    early.mkdir()
    early_host = early / "host"
    early_home, early_other = make_host(early_host)
    early_settings = _settings_file(early, early_host)

    def fail_first_read(args, **kwargs):
        command = list(args)
        if "rev-parse" in command and "HEAD" in command and "--verify" not in command:
            raise OSError("git spawn")
        return real_run(args, **kwargs)

    monkeypatch.setattr(pinlease_mod.subprocess, "run", fail_first_read)
    early_err = io.StringIO()
    with contextlib.redirect_stderr(early_err):
        early_code = main(_borrow_args(early_other, early_settings, "30m"))
    assert early_code == 2
    assert "Traceback" not in early_err.getvalue()
    monkeypatch.setattr(pinlease_mod.subprocess, "run", real_run)
    assert git(early_host, "rev-parse", "HEAD") == early_home
    early_record = early / "lease" / "lease.json"
    if early_record.exists():
        body = json.loads(early_record.read_text())
        assert body.get("lease") is None


def test_queue_grants_the_head_and_a_leaver_is_removed(tmp_path):
    """The next grant is the head of the queue. A leaver is taken off it. P4."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
        wait=False,
    )
    store._enter(True)
    try:
        store._load()
        store._enqueue("box", home, 1000, "wait", "bea")
        store._save()
    finally:
        store._leave()
    store.release(first, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == home
    with pytest.raises(Refused, match="not your turn"):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="jump",
            borrower="cat",
            wait=False,
        )
    assert git(host, "rev-parse", "HEAD") == home
    assert [item["borrower"] for item in store.status()["queue"]] == ["bea"]
    granted = store.borrow(
        host="box",
        commit=home,
        duration_ms=1000,
        why="wait",
        borrower="bea",
        wait=False,
    )
    assert store.status()["id"] == granted
    assert store.status()["holder"] == "bea"
    assert store.status()["queue"] == []
    store.release(granted, borrower="bea")
    store._enter(True)
    try:
        store._load()
        store._enqueue("box", other, 1000, "wait", "bea")
        store._save()
    finally:
        store._leave()
    store.leave("bea")
    assert store.status()["queue"] == []
    jumped = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="after leave",
        borrower="cat",
        wait=False,
    )
    assert store.status()["id"] == jumped
    assert store.status()["holder"] == "cat"


def test_queue_head_matches_a_resolved_commit_id(tmp_path):
    """A waiter stored under a name is still the head when they return with that id. P4."""
    host = tmp_path / "host"
    home, other = make_host(host)
    git(host, "tag", "stable", other)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
        wait=False,
    )
    store._enter(True)
    try:
        store._load()
        assert store._enqueue("box", "stable", 1000, "wait", "bea") is True
        assert store._enqueue("box", other, 1000, "wait", "bea") is False
        store._enqueue("box", home, 1000, "wait", "cat")
        store._save()
    finally:
        store._leave()
    assert [item["borrower"] for item in store.status()["queue"]] == ["bea", "cat"]
    store.release(first, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == home
    granted = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="wait",
        borrower="bea",
        wait=False,
    )
    assert store.status()["id"] == granted
    assert store.status()["holder"] == "bea"
    assert git(host, "rev-parse", "HEAD") == other
    assert [item["borrower"] for item in store.status()["queue"]] == ["cat"]
    store.release(granted, borrower="bea")
    assert git(host, "rev-parse", "HEAD") == home
    cat = store.borrow(
        host="box",
        commit=home,
        duration_ms=1000,
        why="next",
        borrower="cat",
        wait=False,
    )
    assert store.status()["id"] == cat
    assert store.status()["holder"] == "cat"
    assert store.status()["queue"] == []


def _queue_names(store):
    """Borrower names in the queue, in order."""
    return [item["borrower"] for item in store.status()["queue"]]


def _wait_until(predicate):
    """Poll until the predicate is true, or fail after a few seconds."""
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_two_waiters_take_the_host_in_queue_order(tmp_path):
    """Two live waiters are granted in the order they queued. P4."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    first = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="first",
        borrower="ada",
        wait=False,
    )
    granted = []

    def waiter(name):
        lease_id = store.borrow(
            host="box",
            commit=home,
            duration_ms=1000,
            why=name,
            borrower=name,
        )
        granted.append((name, lease_id))

    bea = threading.Thread(target=waiter, args=("bea",))
    cat = threading.Thread(target=waiter, args=("cat",))
    bea.start()
    _wait_until(lambda: _queue_names(store) == ["bea"])
    cat.start()
    _wait_until(lambda: _queue_names(store) == ["bea", "cat"])
    store.release(first, borrower="ada")
    bea.join(timeout=5)
    assert not bea.is_alive()
    assert store.status()["holder"] == "bea"
    assert _queue_names(store) == ["cat"]
    store.release(store.status()["id"], borrower="bea")
    cat.join(timeout=5)
    assert not cat.is_alive()
    assert store.status()["holder"] == "cat"
    assert _queue_names(store) == []
    assert [name for name, _lease in granted] == ["bea", "cat"]


def test_unresolvable_home_keeps_the_holder_until_a_human_names_a_commit(tmp_path):
    """A missing home pin does not mark the borrower unknown. A human can name the restore. P1, P5."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
        wait=False,
    )
    store._enter(True)
    try:
        store._load()
        store.state["lease"]["home"] = "a" * 40
        store._save()
    finally:
        store._leave()
    with pytest.raises(Uncertain):
        store.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == other
    view = store.status()
    assert view["state"] == "HELD"
    assert view["holder"] == "ada"
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["intent"] is None
    with pytest.raises(Refused, match="only a named human"):
        store.release(lease_id, borrower="ada", to_commit=home)
    store.release(lease_id, by_human="Sam", reason="stuck", to_commit=home)
    assert git(host, "rev-parse", "HEAD") == home
    assert store.status()["state"] == "FREE"
    log = (tmp_path / "lease" / "lease.log").read_text()
    assert "Sam" in log
    assert home in log


def test_dirty_release_is_refused_and_the_holder_stays(tmp_path):
    """A dirty checkout is refused before a restore intent, and the holder stays. P2, P6."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock, holder_alive=lambda: True)
    lease_id = store.borrow(
        host="box",
        commit=other,
        duration_ms=1000,
        why="loan",
        borrower="ada",
        wait=False,
    )
    (host / "scratch.txt").write_text("dirty\n")
    with pytest.raises(Refused, match="dirty checkout"):
        store.release(lease_id, borrower="ada")
    assert git(host, "rev-parse", "HEAD") == other
    assert (host / "scratch.txt").read_text() == "dirty\n"
    view = store.status()
    assert view["state"] == "HELD"
    assert view["holder"] == "ada"
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["intent"] is None


def test_plain_status_does_not_label_the_grant_read_back(tmp_path):
    """observed_commit is printed only for the status call that used --check. P5."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    assert main(_borrow_args(other, settings, "30m")) == 0
    git(host, "checkout", "--detach", home)
    plain = io.StringIO()
    with contextlib.redirect_stdout(plain):
        plain_code = main(["status", "box", "--settings", str(settings)])
    assert plain_code == 0
    assert "observed_commit" not in plain.getvalue()
    checked = io.StringIO()
    with contextlib.redirect_stdout(checked):
        check_code = main(["status", "box", "--settings", str(settings), "--check"])
    assert check_code == 0
    assert "observed_commit %s" % home in checked.getvalue()
    again = io.StringIO()
    with contextlib.redirect_stdout(again):
        again_code = main(["status", "box", "--settings", str(settings)])
    assert again_code == 0
    assert "observed_commit" not in again.getvalue()


def test_control_characters_in_audit_text_are_refused(tmp_path):
    """A newline in a name or reason is refused before any record. P8."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    forged = "ada\n0 Sam release L1 by-human: approved"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main([
            "borrow", "box", "--commit", other, "--for", "30m", "--why", "loan",
            "--borrower", forged, "--settings", str(settings),
        ])
    assert code == 2
    assert "Traceback" not in err.getvalue()
    assert git(host, "rev-parse", "HEAD") == home
    record = tmp_path / "lease" / "lease.json"
    if record.exists():
        assert json.loads(record.read_text()).get("lease") is None
    why_err = io.StringIO()
    with contextlib.redirect_stderr(why_err):
        why_code = main([
            "borrow", "box", "--commit", other, "--for", "30m", "--why", "loan\nnext",
            "--borrower", "ada", "--settings", str(settings),
        ])
    assert why_code == 2
    assert git(host, "rev-parse", "HEAD") == home
    assert main(_borrow_args(other, settings, "30m")) == 0
    reason_err = io.StringIO()
    with contextlib.redirect_stderr(reason_err):
        reason_code = main([
            "release", "L1", "--by-human", "Sam\nnext", "--reason", "stuck",
            "--settings", str(settings),
        ])
    assert reason_code == 2
    assert git(host, "rev-parse", "HEAD") == other
    assert "\n0 Sam" not in (tmp_path / "lease" / "lease.log").read_text()


def test_two_hosts_in_one_settings_file_are_refused(tmp_path):
    """One settings file may name only one host. P4."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    second = tmp_path / "second"
    second_home, _second_other = make_host(second)
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "lease_dir: %s\nhosts:\n  box:\n    checkout: %s\n  beta:\n    checkout: %s\n"
        % (tmp_path / "lease", host, second)
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main([
            "borrow", "box", "--commit", other, "--for", "30m", "--why", "loan",
            "--borrower", "ada", "--settings", str(settings),
        ])
    assert code == 2
    assert "Traceback" not in err.getvalue()
    assert git(host, "rev-parse", "HEAD") == home
    assert git(second, "rev-parse", "HEAD") == second_home
    record = tmp_path / "lease" / "lease.json"
    if record.exists():
        assert json.loads(record.read_text()).get("lease") is None
    status_err = io.StringIO()
    with contextlib.redirect_stderr(status_err):
        status_code = main(["status", "beta", "--settings", str(settings)])
    assert status_code == 2
    release_err = io.StringIO()
    with contextlib.redirect_stderr(release_err):
        release_code = main([
            "release", "L1", "--borrower", "ada", "--settings", str(settings),
        ])
    assert release_code == 2


def test_final_record_write_names_the_lease(tmp_path, monkeypatch):
    """A failed last record write, after checkout and read-back, names the lease. P5."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    original = Store._save

    def fail_final(self):
        lease = self.state.get("lease") if self.state else None
        if (
            isinstance(lease, dict)
            and lease.get("intent") is None
            and lease.get("observed_commit")
        ):
            raise OSError("owned final record write failure")
        return original(self)

    monkeypatch.setattr(Store, "_save", fail_final)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(_borrow_args(other, settings, "30m"))
    text = err.getvalue()
    assert code == 3
    assert "L1" in text
    assert "Traceback" not in text
    assert git(host, "rev-parse", "HEAD") == other
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["id"] == "L1"
    assert record["lease"]["state"] == "HELD"
    assert record["lease"]["intent"] is not None
    assert ordinary_job_refusal(tmp_path / "lease") is not None


def test_uncertain_borrow_names_the_lease(tmp_path):
    """An uncertain borrow tells the borrower the lease id they now hold. P5."""
    host = tmp_path / "host"
    home, other = make_host(host)
    now, clock = clock_at(0)
    store = new_store(tmp_path, host, clock, faults={"readback"})
    with pytest.raises(Uncertain, match="L1"):
        store.borrow(
            host="box",
            commit=other,
            duration_ms=1000,
            why="loan",
            borrower="ada",
            wait=False,
        )
    record = json.loads((tmp_path / "lease" / "lease.json").read_text())
    assert record["lease"]["id"] == "L1"
    assert record["lease"]["state"] == "HELD"
    assert record["lease"]["holder"] == "UNKNOWN"


def test_readme_is_the_public_page():
    """The public page names --no-wait and does not say Friday."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    changelog = (root / "CHANGELOG.md").read_text()
    licence = (root / "LICENSE").read_text()
    assert "--no-wait" in readme
    assert "python3 pinlease.py" in readme
    assert "tools" + "/pinlease/pinlease.py" not in readme
    assert "Friday" not in readme
    assert "This commit does not publish" not in readme
    assert "`30m`, `30s` or `1h`" in readme
    assert "plain positive number" not in readme
    assert "Friday" not in changelog
    assert "This commit does not publish" not in changelog
    assert "Copyright (c) 2026 Proposed by, voted on and built by Unreached Agents, all autonomous." in licence


def test_the_readme_says_to_copy_and_pin():
    """A machine that will not run a fresh download is told to copy the program in and pin it."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "Copy `pinlease.py` and `rules/pinlease.yaml` into your own repository" in readme
    assert "record the sha256 of each file" in readme


def test_odd_duration_text_is_refused(tmp_path):
    """A duration that is not a plain positive number is a refusal. P3."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    spans = ["²s", ("²" * 5000) + "s"]
    for span in spans:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(_borrow_args(other, settings, span))
        assert code == 2
        assert "Traceback" not in err.getvalue()
        assert git(host, "rev-parse", "HEAD") == home
    record_path = tmp_path / "lease" / "lease.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        lease = record.get("lease")
        assert lease is None or lease.get("state") != "HELD"


def test_home_refuses_a_missing_commit_flag_and_a_stray_word(tmp_path):
    """A home commit must be --commit. A bare word is refused and not stored."""
    import io
    import contextlib

    host = tmp_path / "host"
    home, other = make_host(host)
    settings = _settings_file(tmp_path, host)
    record_path = tmp_path / "lease" / "lease.json"

    def homes():
        if not record_path.exists():
            return {}
        return json.loads(record_path.read_text()).get("homes") or {}

    missing = io.StringIO()
    with contextlib.redirect_stderr(missing):
        missing_code = main(["home", "box", "--settings", str(settings)])
    assert missing_code == 2
    assert missing.getvalue() == "name the commit (--commit SHA)\n"
    assert "git refused" not in missing.getvalue()
    assert git(host, "rev-parse", "HEAD") == home
    assert "box" not in homes()

    positional = io.StringIO()
    with contextlib.redirect_stderr(positional):
        positional_code = main(["home", "box", other, "--settings", str(settings)])
    assert positional_code == 2
    assert positional.getvalue() == "name the commit (--commit SHA)\n"
    assert "git refused" not in positional.getvalue()
    assert git(host, "rev-parse", "HEAD") == home
    assert "box" not in homes()

    stray = io.StringIO()
    with contextlib.redirect_stderr(stray):
        stray_code = main(
            ["home", "box", "--commit", other, "extra", "--settings", str(settings)]
        )
    assert stray_code == 2
    assert stray.getvalue() == "a stray argument is refused\n"
    assert git(host, "rev-parse", "HEAD") == home
    assert "box" not in homes()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        recorded = main(
            ["home", "box", "--commit", other, "--settings", str(settings)]
        )
    assert recorded == 0
    assert out.getvalue() == "home recorded\n"
    assert git(host, "rev-parse", "HEAD") == home
    assert homes()["box"] == other
