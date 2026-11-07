# Pinlease lends a git checkout at one commit and puts it back on release.
# The rules are in SPEC.md, in this folder.
# This slice keeps the lease record, borrow, release, status and the home pin.
# The shared server lock is a later step. A fake local checkout stands in for the host.

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


class Refused(Exception):
    """The move was not started, or it was rolled back. The lock is free."""


class Uncertain(Exception):
    """The host may have moved. The lease stays held."""


class Cut(Exception):
    """A test stopped the process after a durable write. Nothing is rolled back."""


_GATES = {}
_GATES_LOCK = threading.Lock()
_RECORD_PID = object()


class _Gate:
    """The in-process lock and the flock fd shared by every store on one folder."""

    def __init__(self):
        """Make one gate. P4: writers take turns."""
        self.thread = threading.Lock()
        self.fd = None
        self.held = False


def _gate_for(path):
    """Return the gate for a lease folder. P4."""
    key = str(Path(path).resolve())
    with _GATES_LOCK:
        if key not in _GATES:
            _GATES[key] = _Gate()
        return _GATES[key]


def load_rules(path=None):
    """Read Pinlease's numbers from rules/pinlease.yaml. P7 markers live there too."""
    if path is None:
        path = Path(__file__).resolve().parent / "rules" / "pinlease.yaml"
    try:
        text = Path(path).read_text()
    except OSError:
        raise Refused("rules file could not be read")
    return parse_yaml(text)


def parse_yaml(text):
    """Read the small yaml this tool writes by hand. No outside parser."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, stripped))
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise Refused("rules file could not be read")
    return value


def _parse_block(lines, index, indent):
    """Read one yaml map or list at this indent."""
    if index < len(lines) and lines[index][1].startswith("- "):
        items = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
            items.append(_scalar(lines[index][1][2:]))
            index += 1
        return items, index
    obj = {}
    while index < len(lines) and lines[index][0] == indent:
        line = lines[index][1]
        key, _, val = line.partition(":")
        index += 1
        if val.strip() == "":
            child, index = _parse_block(lines, index, indent + 2)
            obj[key.strip()] = child
        else:
            obj[key.strip()] = _scalar(val.strip())
    return obj, index


def _scalar(text):
    """Turn one yaml value into text or a whole number."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    if text.isdigit():
        return int(text)
    return text


def held_text(lease):
    """The refusal line for a lease that is not released. P4."""
    if lease.get("overdue"):
        flag = "overdue"
    else:
        flag = "not overdue"
    return "held by lease %s (%s, %s)" % (lease.get("id"), lease.get("holder"), flag)


def ordinary_job_refusal(lease_dir):
    """What a later ordinary heavy job would read. It does not take the server lock. P4."""
    path = Path(lease_dir) / "lease.json"
    if not path.exists():
        return None
    raw = path.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "lease record unreadable"
    if "lease" not in data or data.get("lease") is None:
        return None
    lease = data.get("lease")
    if not isinstance(lease, dict) or len(lease) == 0:
        return "lease record unreadable"
    if lease.get("state") == "RELEASED":
        return None
    return held_text(lease)


def duration_to_ms(text, rules):
    """Turn a positive 30m, 30s or 1h into milliseconds using the rule file. P3."""
    if not isinstance(text, str) or len(text) < 2:
        raise Refused("duration must be a positive number of h, m or s")
    unit = text[-1]
    number = text[:-1]
    if unit == "h":
        scale = rules["hour_ms"]
    elif unit == "m":
        scale = rules["minute_ms"]
    elif unit == "s":
        scale = rules["second_ms"]
    else:
        raise Refused("duration must be a positive number of h, m or s")
    if not number.isascii() or not number.isdigit():
        raise Refused("duration must be a positive number of h, m or s")
    try:
        count = int(number)
    except ValueError:
        raise Refused("duration must be a positive number of h, m or s")
    if count <= 0:
        raise Refused("duration must be a positive number of h, m or s")
    return count * scale


class Store:
    """The lease folder for one host: record, log, and the checkout it lends."""

    def __init__(self, lease_dir, host_name, checkout, clock=None, holder_alive=None, hooks=None, faults=None, rules=None):
        """Open a lease folder. Unfinished intent is handled on the next locked read. P5."""
        self.lease_dir = Path(lease_dir)
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        self.host_name = host_name
        self.checkout = Path(checkout)
        self.rules = rules if rules is not None else load_rules()
        if clock is None:
            second_ms = self.rules["second_ms"]
            self.clock = lambda: int(time.time() * second_ms)
        else:
            self.clock = clock
        self.holder_alive = holder_alive
        self.hooks = hooks if hooks is not None else {}
        self.faults = faults if faults is not None else set()
        self._gate = _gate_for(self.lease_dir)
        self._lock_path = self.lease_dir / "pinlease.lock"
        self.state = None

    def borrow(self, host, commit, duration_ms, why, borrower, wait=True, on_wait=None, settings_text=None, holder_pid=_RECORD_PID):
        """Wait if needed, record the lease, then put that commit on the host. P4, P5, P6."""
        self._screen(host, commit, why, borrower, settings_text)
        self._one_line(why, borrower)
        if borrower is None or str(borrower).strip() == "":
            raise Refused("borrower is required")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise Refused("duration must be positive")
        while True:
            entered = self._enter(False)
            if not entered:
                if not wait:
                    raise Refused("lock held by a non-lease job")
                if on_wait is not None:
                    on_wait()
                self._sleep_poll()
                continue
            grant = False
            try:
                self._load()
                self._guard_record()
                self._recover()
                if self._is_held():
                    if not wait:
                        raise Refused(held_text(self.state["lease"]))
                    if self._enqueue(host, commit, duration_ms, why, borrower):
                        self._save()
                        self._log("pinlease", "queued %s" % borrower)
                elif not self._queue_allows(borrower, commit):
                    if not wait:
                        raise Refused("not your turn; waiting %s" % self.state["queue"][0]["borrower"])
                    if self._enqueue(host, commit, duration_ms, why, borrower):
                        self._save()
                        self._log("pinlease", "queued %s" % borrower)
                else:
                    lease_id = self._grant(host, commit, duration_ms, why, borrower, holder_pid)
                    grant = True
            finally:
                self._leave()
            if grant:
                return lease_id
            if on_wait is not None:
                on_wait()
            self._sleep_poll()

    def release(self, lease_id, borrower=None, by_human=None, reason=None, to_commit=None):
        """Give the host back, or record that a named human did. P1, P5."""
        self._screen(lease_id, borrower, by_human, reason, to_commit)
        self._one_line(borrower, by_human, reason)
        self._enter(True)
        try:
            self._load()
            self._guard_record()
            self._recover()
            self._release_locked(lease_id, borrower, by_human, reason, to_commit)
        finally:
            self._leave()

    def status(self, host=None, check=False):
        """Report holder, commit, times, overdue, and the queue. P2, P3, P5."""
        self._enter(True)
        try:
            self._load()
            self._guard_record()
            self._recover()
            if check and self._is_held():
                found = self._readback()
                self.state["lease"]["observed_commit"] = found
                self._save()
                self._log("pinlease", "check %s" % found)
            return self._view()
        finally:
            self._leave()

    def set_home(self, host, commit):
        """Record the home pin. Refused while a lease is held. Does not check it out."""
        self._screen(host, commit)
        self._enter(True)
        try:
            self._load()
            self._guard_record()
            self._recover()
            if self._is_held():
                raise Refused("home stays as it was when the lease was granted")
            full = self._resolve(commit)
            self.state["homes"][host] = full
            self._save()
            self._log("pinlease", "home %s" % full)
        finally:
            self._leave()

    def _grant(self, host, commit, duration_ms, why, borrower, holder_pid):
        """Write intent, then copy and check out. A late error names the lease. P5, P6."""
        if holder_pid is _RECORD_PID:
            stored_pid = os.getpid()
        else:
            stored_pid = holder_pid
        if host != self.host_name:
            raise Refused("unknown host")
        if self._dirty():
            raise Refused("dirty checkout")
        original = self._git("rev-parse", "HEAD")
        if self.host_name not in self.state["homes"]:
            self.state["homes"][self.host_name] = original
        home = self.state["homes"][self.host_name]
        now = self.clock()
        lease_id = "L%s" % self.state["next_id"]
        self.state["next_id"] = self.state["next_id"] + 1
        self.state["lease"] = {
            "id": lease_id,
            "host": self.host_name,
            "borrower": borrower,
            "why": why,
            "commit": commit,
            "home": home,
            "since_ms": now,
            "until_ms": now + duration_ms,
            "state": "HELD",
            "holder": borrower,
            "holder_pid": stored_pid,
            "checkout": str(self.checkout.resolve()),
            "overdue": False,
            "intent": {
                "step": "intent",
                "kind": "borrow",
                "original": original,
                "target": commit,
            },
            "observed_commit": None,
        }
        self._drop_queue(borrower, commit)
        self._save()
        self._log(borrower, "intent %s" % lease_id)
        self._hook("after_intent")
        try:
            target = self._resolve(commit)
        except Refused:
            self.state["lease"] = None
            self._save()
            self._log("pinlease", "cleared %s" % lease_id)
            raise
        self.state["lease"]["commit"] = target
        self.state["lease"]["intent"]["target"] = target
        self._save()
        try:
            self._checkout(target)
            found = self._readback()
        except Uncertain as caught:
            self.state["lease"]["holder"] = "UNKNOWN"
            self._save()
            self._log("pinlease", "uncertain %s" % lease_id)
            raise Uncertain("%s; lease %s" % (caught, lease_id))
        if found != target:
            self.state["lease"]["holder"] = "UNKNOWN"
            self._save()
            self._log("pinlease", "uncertain %s" % lease_id)
            raise Uncertain("read-back did not match the commit; lease %s" % lease_id)
        try:
            self._hook("after_readback")
            self.state["lease"]["intent"] = None
            self.state["lease"]["holder"] = borrower
            self.state["lease"]["observed_commit"] = found
            self._save()
            self._log(borrower, "borrow %s" % lease_id)
        except Cut as caught:
            raise Cut("%s; lease %s" % (caught, lease_id))
        except Exception as caught:
            raise Uncertain("%s; lease %s" % (caught, lease_id))
        return lease_id

    def _release_locked(self, lease_id, borrower, by_human, reason, to_commit=None):
        """Restore the home pin and only then mark the lease released. P1, P5."""
        lease = self.state.get("lease")
        if self._is_held() and lease["id"] != lease_id:
            raise Refused("stale lease")
        if not self._is_held():
            if lease_id in self.state["released"]:
                return
            raise Refused("unknown lease")
        if by_human:
            if not reason:
                raise Refused("a human release needs a name and a reason")
            who = by_human
            detail = reason
        else:
            if borrower != lease["borrower"]:
                raise Refused("borrower does not match the lease")
            who = borrower
            detail = ""
        if self._dirty():
            raise Refused("dirty checkout; clean it and release again")
        original = self._git("rev-parse", "HEAD")
        if to_commit:
            if not by_human:
                raise Refused("only a named human can choose the restore commit")
            target = self._resolve(to_commit)
            lease["home"] = target
            self._save()
            self._log(who, "restore target %s %s" % (lease_id, target))
        else:
            target = lease["home"]
        try:
            resolved = self._resolve(target)
        except Refused as caught:
            raise Uncertain(str(caught))
        lease["intent"] = {
            "step": "intent",
            "kind": "restore",
            "original": original,
            "target": resolved,
        }
        self._save()
        self._log(who, "release intent %s" % lease_id)
        self._hook("after_intent")
        try:
            self._checkout(resolved)
            found = self._readback()
            if found != resolved:
                raise Uncertain("read-back did not match the home pin")
            self._hook("after_restore_readback")
        except Uncertain:
            lease["holder"] = "UNKNOWN"
            self._save()
            self._log("pinlease", "uncertain %s" % lease_id)
            raise
        self.state["released"].append(lease_id)
        self.state["lease"] = None
        self._save()
        self._log(who, "release %s %s" % (lease_id, detail))
        for item in self.state["queue"]:
            self._log("pinlease", "told %s" % item["borrower"])

    def leave(self, borrower):
        """Take one waiter off the queue. They do not get the host. P4."""
        self._screen(borrower)
        self._one_line(borrower)
        if borrower is None or str(borrower).strip() == "":
            raise Refused("borrower is required")
        self._enter(True)
        try:
            self._load()
            self._guard_record()
            kept = []
            removed = False
            for item in self.state["queue"]:
                if item.get("borrower") == borrower:
                    removed = True
                    continue
                kept.append(item)
            if not removed:
                raise Refused("not waiting")
            self.state["queue"] = kept
            self._save()
            self._log(borrower, "left the queue")
        finally:
            self._leave()

    def _same_commit(self, left, right):
        """True when both texts resolve to one commit id. P4."""
        return self._resolve(left) == self._resolve(right)

    def _queue_allows(self, borrower, commit):
        """True when this borrower is the head for that commit, or the queue is empty. P4."""
        queue = self.state.get("queue") or []
        if not queue:
            return True
        head = queue[0]
        if head.get("borrower") != borrower:
            return False
        return self._same_commit(head.get("commit"), commit)

    def _enqueue(self, host, commit, duration_ms, why, borrower):
        """Remember one waiter. The same waiter is not added twice. P4."""
        for item in self.state["queue"]:
            if item["borrower"] == borrower and self._same_commit(item["commit"], commit):
                return False
        self.state["queue"].append(
            {
                "host": host,
                "commit": commit,
                "duration_ms": duration_ms,
                "why": why,
                "borrower": borrower,
            }
        )
        return True

    def _drop_queue(self, borrower, commit):
        """Remove a waiter who is now being granted."""
        kept = []
        for item in self.state["queue"]:
            if item["borrower"] == borrower and self._same_commit(item["commit"], commit):
                continue
            kept.append(item)
        self.state["queue"] = kept

    def _is_held(self):
        """True when the record is a known live loan. P4."""
        lease = self.state.get("lease")
        if not lease:
            return False
        return lease.get("state") == "HELD"

    def _guard_record(self):
        """Refuse an unknown lease, or one for another checkout, without writing. P4, P5."""
        lease = self.state.get("lease")
        if lease is None:
            return
        if not isinstance(lease, dict) or len(lease) == 0:
            raise Refused("unknown lease state")
        state = lease.get("state")
        if state == "RELEASED":
            return
        if state != "HELD":
            raise Refused("unknown lease state")
        for key in ("id", "holder", "home", "commit", "until_ms", "host", "checkout"):
            if lease.get(key) is None:
                raise Refused("unknown lease state")
        if lease.get("host") != self.host_name:
            raise Refused("lease is for another checkout")
        saved = Path(lease.get("checkout")).resolve()
        if saved != self.checkout.resolve():
            raise Refused("lease is for another checkout")

    def _holder_gone(self, lease):
        """True when a stored pid is dead or not ours. A missing pid is no signal. P2."""
        if self.holder_alive is not None:
            return not self.holder_alive()
        pid = lease.get("holder_pid")
        if pid is None:
            return False
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        return False

    def _view(self):
        """The status map a caller prints. P2, P3."""
        if not self._is_held():
            return {
                "state": "FREE",
                "id": None,
                "holder": None,
                "commit": None,
                "why": None,
                "since_ms": None,
                "until_ms": None,
                "overdue": False,
                "observed_commit": None,
                "queue": list(self.state.get("queue") or []),
            }
        lease = self.state["lease"]
        return {
            "state": "HELD",
            "id": lease["id"],
            "holder": lease["holder"],
            "commit": lease["commit"],
            "why": lease["why"],
            "since_ms": lease["since_ms"],
            "until_ms": lease["until_ms"],
            "overdue": lease["overdue"],
            "observed_commit": lease.get("observed_commit"),
            "queue": list(self.state.get("queue") or []),
        }

    def _empty(self):
        """A new lease file. The schema number comes from the rule file."""
        return {
            "schema_version": self.rules["schema_version"],
            "next_id": 1,
            "homes": {},
            "lease": None,
            "queue": [],
            "released": [],
        }

    def _load(self):
        """Read the record. A bad or unknown file is left untouched. P4, P5."""
        path = self.lease_dir / "lease.json"
        if not path.exists():
            self.state = self._empty()
            return
        raw = path.read_text()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.state = None
            raise Refused("lease record unreadable")
        if data.get("schema_version") != self.rules["schema_version"]:
            self.state = None
            raise Refused("unknown schema")
        data.setdefault("homes", {})
        data.setdefault("queue", [])
        data.setdefault("released", [])
        data.setdefault("next_id", 1)
        data.setdefault("lease", None)
        self.state = data

    def _recover(self):
        """A restart with intent still open, or a dead holder, stays excluded. P2, P3, P5."""
        if not self._is_held():
            return
        lease = self.state["lease"]
        changed = False
        if lease.get("intent"):
            if lease.get("holder") != "UNKNOWN":
                lease["holder"] = "UNKNOWN"
                changed = True
                self._log("pinlease", "holder UNKNOWN %s" % lease["id"])
        elif self._holder_gone(lease):
            if lease.get("holder") != "UNKNOWN":
                lease["holder"] = "UNKNOWN"
                changed = True
                self._log("pinlease", "holder UNKNOWN %s" % lease["id"])
        overdue = self.clock() >= lease["until_ms"]
        if overdue != bool(lease.get("overdue")):
            lease["overdue"] = overdue
            changed = True
            if overdue:
                self._log("pinlease", "overdue %s" % lease["id"])
                for item in self.state["queue"]:
                    self._log("pinlease", "told %s" % item["borrower"])
            else:
                self._log("pinlease", "not overdue %s" % lease["id"])
        if changed:
            self._save()

    def _save(self):
        """Write the record and sync it before any later host move. P4, P5."""
        if self.state is None:
            raise Refused("lease record unreadable")
        path = self.lease_dir / "lease.json"
        temp = self.lease_dir / "lease.json.tmp"
        payload = json.dumps(self.state, indent=2, sort_keys=True) + "\n"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        dir_fd = os.open(self.lease_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _log(self, who, action):
        """Append one line: time, who, what. P8."""
        line = "%s %s %s\n" % (self.clock(), who, action)
        self._screen(line)
        path = self.lease_dir / "lease.log"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _one_line(self, *parts):
        """Refuse a control character in a name or reason. P8."""
        for part in parts:
            if part is None:
                continue
            for ch in str(part):
                if ord(ch) < 32 or ord(ch) == 127:
                    raise Refused("name and reason must be one line")

    def _screen(self, *parts):
        """Refuse if a planted marker is present. Do not write it down. P7."""
        markers = self.rules["secret_markers"]
        for part in parts:
            if part is None:
                continue
            text = str(part)
            for marker in markers:
                if marker and marker in text:
                    raise Refused("refused: secret marker")

    def _hook(self, name):
        """Run one injected cut point. A cut leaves the record as it stands. P5."""
        fn = self.hooks.get(name)
        if fn is not None:
            fn()

    def _sleep_poll(self):
        """Wait one poll interval from the rule file. P4."""
        time.sleep(self.rules["wait_poll_ms"] / self.rules["second_ms"])

    def _enter(self, blocking):
        """Take the in-process lock and the flock. P4."""
        gate = self._gate
        gate.thread.acquire()
        if gate.fd is None:
            gate.fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        flags = fcntl.LOCK_EX
        if not blocking:
            flags = flags | fcntl.LOCK_NB
        try:
            fcntl.flock(gate.fd, flags)
        except BlockingIOError:
            gate.thread.release()
            return False
        gate.held = True
        return True

    def _leave(self):
        """Drop the flock and the in-process lock. P4."""
        gate = self._gate
        if gate.held:
            fcntl.flock(gate.fd, fcntl.LOCK_UN)
            gate.held = False
        gate.thread.release()

    def _git_env(self):
        """Environment for git. Inherited directory overrides are dropped. P5, P6."""
        env = {}
        for key, value in os.environ.items():
            if key.startswith("GIT_") and not key.startswith("GIT_SSH"):
                continue
            env[key] = value
        git_dir = str((self.checkout / ".git").resolve())
        work = str(self.checkout.resolve())
        env["GIT_DIR"] = git_dir
        env["GIT_WORK_TREE"] = work
        return env, git_dir, work

    def _git(self, *args, uncertain=False):
        """Run one git command in the configured checkout. No stash and no reset. P6."""
        env, git_dir, work = self._git_env()
        try:
            result = subprocess.run(
                ["git", "--git-dir", git_dir, "--work-tree", work, *args],
                cwd=work,
                capture_output=True,
                text=True,
                env=env,
            )
        except OSError:
            if uncertain:
                raise Uncertain("git could not finish the move")
            raise Refused("git refused the move")
        if result.returncode != 0:
            if uncertain:
                raise Uncertain("git could not finish the move")
            raise Refused("git refused the move")
        return result.stdout.strip()

    def _dirty(self):
        """True when the checkout has anything other than commits. P6."""
        text = self._git("status", "--porcelain")
        return text != ""

    def _resolve(self, commit):
        """Check the commit exists and return its id. A copy fault fails here. P5."""
        if "copy" in self.faults:
            raise Refused("copy failed")
        return self._git("rev-parse", "--verify", "%s^{commit}" % commit)

    def _checkout(self, commit):
        """Check out that commit. A fault can fail before or after the move. P5, P6."""
        if "checkout" in self.faults:
            raise Uncertain("checkout failed")
        self._git("checkout", "--detach", commit, uncertain=True)
        if "checkout_moved" in self.faults:
            raise Uncertain("checkout failed")

    def _readback(self):
        """Read the checkout's commit. A fault means the answer was lost. P5."""
        if "readback" in self.faults:
            raise Uncertain("read-back failed")
        return self._git("rev-parse", "HEAD", uncertain=True)


def _settings(path):
    """Read the host map. It holds checkout paths, never a secret. P7."""
    try:
        text = Path(path).read_text()
    except OSError:
        raise Refused("settings file is unreadable")
    data = parse_yaml(text)
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict) or len(hosts) != 1:
        raise Refused("settings must name one host")
    return data


def _store_from_settings(settings, host, clock=None):
    """Build a store for one named host in the settings file."""
    data = _settings(settings)
    hosts = data["hosts"]
    if host not in hosts:
        raise Refused("unknown host")
    checkout = hosts[host]["checkout"]
    return Store(
        lease_dir=data["lease_dir"],
        host_name=host,
        checkout=checkout,
        clock=clock,
    )


def _take(args, name):
    """Return the value after a flag and remove both. A bare flag is refused."""
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args) or str(args[index + 1]).startswith("--"):
        raise Refused("%s needs a value" % name)
    value = args[index + 1]
    del args[index : index + 2]
    return value


def main(argv=None):
    """The command line. 0 is done, 2 is refused, 3 is uncertain. P5."""
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    try:
        if argv == ["--version"]:
            rules = load_rules()
            sys.stdout.write("%s\n" % rules["version"])
            return 0
        return _main(argv)
    except Refused as caught:
        sys.stderr.write("%s\n" % caught)
        return 2
    except Uncertain as caught:
        sys.stderr.write("%s\n" % caught)
        return 3
    except Cut as caught:
        sys.stderr.write("%s\n" % caught)
        return 3
    except Exception as caught:
        sys.stderr.write("%s\n" % caught)
        return 3


def _main(argv):
    """Run one pinlease command from already split arguments."""
    if not argv:
        raise Refused("usage: pinlease borrow|release|status|home|leave")
    command = argv[0]
    rest = argv[1:]
    settings = _take(rest, "--settings")
    if settings is None:
        settings = os.environ.get("PINLEASE_SETTINGS")
    if settings is None:
        raise Refused("settings file is required")
    if command == "borrow":
        if not rest or str(rest[0]).startswith("-"):
            raise Refused("name the host")
        host = rest[0]
        commit = _take(rest, "--commit")
        span = _take(rest, "--for")
        why = _take(rest, "--why")
        borrower = _take(rest, "--borrower")
        no_wait = "--no-wait" in rest
        if borrower is None or str(borrower).strip() == "":
            raise Refused("borrower is required")
        if span is None:
            raise Refused("duration is required")
        rules = load_rules()
        duration_ms = duration_to_ms(span, rules)
        store = _store_from_settings(settings, host)
        lease_id = store.borrow(
            host=host,
            commit=commit,
            duration_ms=duration_ms,
            why=why,
            borrower=borrower,
            wait=not no_wait,
            holder_pid=None,
        )
        sys.stdout.write("%s\n" % lease_id)
        return 0
    if command == "release":
        if not rest or str(rest[0]).startswith("-"):
            raise Refused("name the lease")
        lease_id = rest[0]
        borrower = _take(rest, "--borrower")
        by_human = _take(rest, "--by-human")
        reason = _take(rest, "--reason")
        to_commit = _take(rest, "--to")
        data = _settings(settings)
        host = _only_host(data)
        store = _store_from_settings(settings, host)
        store.release(
            lease_id,
            borrower=borrower,
            by_human=by_human,
            reason=reason,
            to_commit=to_commit,
        )
        sys.stdout.write("released %s\n" % lease_id)
        return 0
    if command == "status":
        host = None
        check = "--check" in rest
        names = [item for item in rest if not item.startswith("--")]
        if names:
            host = names[0]
        else:
            host = _only_host(_settings(settings))
        store = _store_from_settings(settings, host)
        view = store.status(host=host, check=check)
        sys.stdout.write(_status_text(view, show_observed=check))
        return 0
    if command == "leave":
        borrower = _take(rest, "--borrower")
        if borrower is None or str(borrower).strip() == "":
            raise Refused("borrower is required")
        names = [item for item in rest if not str(item).startswith("-")]
        if names:
            host = names[0]
        else:
            host = _only_host(_settings(settings))
        store = _store_from_settings(settings, host)
        store.leave(borrower)
        sys.stdout.write("left\n")
        return 0
    if command == "home":
        if not rest or str(rest[0]).startswith("-"):
            raise Refused("name the host")
        host = rest.pop(0)
        commit = _take(rest, "--commit")
        if commit is None:
            raise Refused("name the commit (--commit SHA)")
        for word in rest:
            if not str(word).startswith("-"):
                raise Refused("a stray argument is refused")
        store = _store_from_settings(settings, host)
        store.set_home(host, commit)
        sys.stdout.write("home recorded\n")
        return 0
    raise Refused("unknown command")


def _status_text(view, show_observed=False):
    """Print the fields a handover needs. The read-back label is only for --check. P2, P5."""
    lines = []
    lines.append("state %s" % view["state"])
    lines.append("id %s" % view["id"])
    lines.append("holder %s" % view["holder"])
    lines.append("commit %s" % view["commit"])
    lines.append("why %s" % view["why"])
    lines.append("since_ms %s" % view["since_ms"])
    lines.append("until_ms %s" % view["until_ms"])
    lines.append("overdue %s" % view["overdue"])
    lines.append("queue %s" % len(view["queue"]))
    for item in view["queue"]:
        lines.append("waiting %s" % item.get("borrower"))
    if show_observed and view.get("observed_commit"):
        lines.append("observed_commit %s" % view["observed_commit"])
    return "\n".join(lines) + "\n"


def _only_host(data):
    """Use the single host in a settings file when a command omits the name."""
    names = list(data["hosts"].keys())
    if len(names) != 1:
        raise Refused("name the host")
    return names[0]


if __name__ == "__main__":
    sys.exit(main())
