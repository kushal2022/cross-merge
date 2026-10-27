#!/usr/bin/env python3
"""Cross-merge UI: a local web front end for the Bitbucket cross-merge script.

Run:  python3 server.py [--port 8765] [--no-browser]
Then open http://127.0.0.1:8765

Python 3.8+ standard library only. Binds to 127.0.0.1 so the API key never
leaves this machine except in the Bitbucket PR request itself.
"""
import argparse
import datetime
import json
import os
import re
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "crossmerge_config.json")
LOG_DIR = os.path.join(ROOT, "logs")
INDEX_PATH = os.path.join(ROOT, "index.html")

JOBS = {}
RUN_LOCK = threading.Lock()  # one merge run at a time; they share working trees

BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/+\-]+(?<!\.lock)(?<!/)(?<!\.)$")
SPRINT_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


# ============================================================
# Job: collects events for one run and fans them out to SSE
# ============================================================
class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.events = []
        self.cond = threading.Condition()
        self.done = False
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%d-%m-%Y-%H-%M-%S")
        self.log_path = os.path.join(LOG_DIR, f"merge_crossmerge_{stamp}.log")
        self._log_file = open(self.log_path, "a", encoding="utf-8")

    def emit(self, type_, **data):
        ev = {"type": type_, **data}
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def log(self, msg, level="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_file.write(f"{ts} {msg}\n")
        self._log_file.flush()
        self.emit("log", ts=ts, msg=msg, level=level)

    def finish(self):
        self._log_file.close()
        with self.cond:
            self.done = True
            self.cond.notify_all()


# ============================================================
# Git helpers
# ============================================================
def git(cwd, *args, job=None, timeout=600):
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
    if job:
        job.log("$ git " + " ".join(args), "cmd")
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rc, out = p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        rc, out = 124, f"git {args[0]} timed out after {timeout}s"
    except FileNotFoundError:
        rc, out = 127, "git executable not found on PATH"
    if job and out:
        for line in out.splitlines():
            job.log("  " + line, "out")
    return rc, out


def repo_info(path, fetch=False):
    path = os.path.expanduser(path or "")
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "Folder not found"}
    rc, out = git(path, "rev-parse", "--is-inside-work-tree", timeout=20)
    if rc != 0 or out != "true":
        return {"ok": False, "error": "Not a git repository"}
    if fetch:
        rc, out = git(path, "fetch", "origin", "--prune")
        if rc != 0:
            return {"ok": False, "error": "git fetch failed: " + out[-300:]}
    _, branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=20)
    _, dirty = git(path, "status", "--porcelain", "--untracked-files=no", timeout=60)
    _, remote = git(path, "remote", "get-url", "origin", timeout=20)
    _, refs = git(path, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", timeout=60)
    branches = sorted({r[len("origin/"):] for r in refs.splitlines()
                       if r.startswith("origin/") and r != "origin/HEAD"})
    return {"ok": True, "branch": branch, "dirty": bool(dirty.strip()),
            "remote": remote, "branches": branches}


# ============================================================
# Bitbucket Server PR creation
# ============================================================
def create_pr(base_url, token, project, slug, src, merge_branch, target, reviewers, insecure):
    url = f"{base_url.rstrip('/')}/rest/api/latest/projects/{project}/repos/{slug}/pull-requests"
    ref = lambda name: {"id": f"refs/heads/{name}",
                        "repository": {"slug": slug, "project": {"key": project}}}
    body = {
        "title": f"Merge {src} into {merge_branch} (conflict resolution needed)",
        "description": f"Please resolve conflicts between {src} and {merge_branch} for {target} in {slug}.",
        "state": "OPEN", "open": True, "closed": False,
        "fromRef": ref(src), "toRef": ref(merge_branch),
        "reviewers": [{"user": {"name": r}} for r in reviewers],
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
            data = json.loads(r.read().decode() or "{}")
        links = data.get("links", {}).get("self", [])
        return True, (links[0].get("href") if links else None), f"PR #{data.get('id', '?')} created"
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            msg = "; ".join(x.get("message", "") for x in json.loads(raw).get("errors", []))
        except Exception:
            msg = raw[:300]
        return False, None, f"HTTP {e.code}: {msg}"
    except Exception as e:
        return False, None, f"Request failed: {e}"


# ============================================================
# Merge run
# ============================================================
def process_target(job, cfg, repo, path, src, t):
    target = t["branch"]
    merge_branch = f"Sprint{cfg['sprint']}-{src}-Crossmerge-to-{target}-{cfg['date']}"
    res = {"repo": repo["slug"], "group": repo.get("group", ""), "source": src,
           "target": target, "mergeBranch": merge_branch}

    def cleanup():
        git(path, "merge", "--abort")
        git(path, "checkout", target)
        git(path, "branch", "-D", merge_branch)

    def done(status, detail, **extra):
        res.update(status=status, detail=detail, **extra)
        job.emit("result", **res)

    job.log("")
    job.log(f"Processing: merge '{src}' -> '{target}'", "head")

    if git(path, "rev-parse", "--verify", "--quiet", f"origin/{target}")[0] != 0:
        return done("failed", f"origin/{target} does not exist")

    rc, out = git(path, "checkout", target, job=job)
    if rc != 0:
        return done("failed", "checkout failed: " + out[-200:])
    rc, out = git(path, "merge", "--ff-only", f"origin/{target}", job=job)
    if rc != 0:
        return done("failed", f"local {target} has diverged from origin/{target}; fix it manually")

    git(path, "branch", "-D", merge_branch)
    rc, out = git(path, "checkout", "-b", merge_branch, job=job)
    if rc != 0:
        return done("failed", "could not create merge branch: " + out[-200:])

    _, before = git(path, "rev-parse", "HEAD")
    job.log(f"Attempting to merge origin/{src} into {merge_branch}")
    rc, out = git(path, "merge", "--no-ff", "--no-edit", "-m", f"Merged {src} into {target}",
                  f"origin/{src}", job=job)
    _, after = git(path, "rev-parse", "HEAD")

    if rc == 0 and before == after:
        cleanup()
        return done("up_to_date", f"{target} already contains {src}")

    if rc == 0:
        if cfg["dryRun"]:
            cleanup()
            return done("clean", "Merges cleanly (dry run — nothing pushed)")
        git(path, "checkout", target, job=job)
        rc, out = git(path, "merge", "--ff-only", merge_branch, job=job)
        if rc != 0:
            cleanup()
            return done("failed", "could not fast-forward target: " + out[-200:])
        rc, out = git(path, "push", "origin", target, job=job)
        git(path, "branch", "-D", merge_branch, job=job)
        if rc != 0:
            git(path, "reset", "--hard", f"origin/{target}", job=job)
            return done("failed", "push rejected: " + out[-300:])
        job.log(f"{target} updated and pushed with {src} changes.", "ok")
        return done("merged", f"Merged and pushed to origin/{target}")

    # Conflict (or other merge failure)
    _, files = git(path, "diff", "--name-only", "--diff-filter=U")
    files = [f for f in files.splitlines() if f.strip()]
    git(path, "merge", "--abort", job=job)
    if not files:
        cleanup()
        return done("failed", "merge failed: " + out[-300:])
    job.log(f"Merge conflict detected in {len(files)} file(s).", "warn")

    if cfg["dryRun"]:
        cleanup()
        return done("conflict", "Would conflict (dry run — no branch pushed, no PR)", files=files)

    rc, out = git(path, "push", "-u", "origin", merge_branch, job=job)
    git(path, "checkout", target)
    git(path, "branch", "-D", merge_branch)
    if rc != 0:
        return done("failed", "could not push merge branch: " + out[-300:], files=files)

    reviewers = [r.strip() for r in re.split(r"[,;\s]+", t.get("reviewers", "")) if r.strip()]
    job.log(f"Creating PR {src} -> {merge_branch}; reviewers: {', '.join(reviewers) or '(none)'}")
    ok, href, msg = create_pr(cfg["baseUrl"], cfg["token"], repo["project"], repo["slug"],
                              src, merge_branch, target, reviewers, cfg["insecure"])
    job.log(msg, "ok" if ok else "err")
    if ok:
        return done("conflict_pr", msg, files=files, prUrl=href)
    return done("failed", f"Merge branch pushed but PR creation failed — {msg}", files=files)


def run_job(job, cfg):
    try:
        mode = "DRY RUN" if cfg["dryRun"] else "LIVE"
        job.log(f"=== Merge started at {datetime.datetime.now():%c} ({mode}) ===", "head")
        job.log(f"Sprint {cfg['sprint']} · source '{cfg['source']}' · log: {job.log_path}")
        src = cfg["source"]
        for repo in cfg["repos"]:
            path = os.path.expanduser(repo["path"])
            targets = [t for t in cfg["targets"] if repo["id"] in t.get("repos", [])]
            if not targets:
                continue
            job.log("")
            job.log(f"##### Repo {repo['slug']} [{repo.get('group', '')}] — {path}", "head")
            job.emit("repo", slug=repo["slug"])

            def fail_all(reason):
                job.log(reason, "err")
                for t in targets:
                    job.emit("result", repo=repo["slug"], group=repo.get("group", ""), source=src,
                             target=t["branch"], status="failed", detail=reason)

            info = repo_info(path)
            if not info["ok"]:
                fail_all(info["error"])
                continue
            if info["dirty"]:
                fail_all("Working tree has uncommitted changes — commit or stash first")
                continue
            orig = info["branch"]
            rc, out = git(path, "fetch", "origin", "--prune", job=job)
            if rc != 0:
                fail_all("git fetch failed")
                continue
            if git(path, "rev-parse", "--verify", "--quiet", f"origin/{src}")[0] != 0:
                fail_all(f"Source branch origin/{src} not found")
                continue
            for t in targets:
                try:
                    process_target(job, cfg, repo, path, src, t)
                except Exception as e:
                    job.emit("result", repo=repo["slug"], source=src, target=t["branch"],
                             status="failed", detail=f"Unexpected error: {e}")
            if orig and orig != "HEAD":
                git(path, "checkout", orig)
        job.log("")
        job.log(f"=== Merge finished at {datetime.datetime.now():%c} ===", "head")
    except Exception as e:
        job.log(f"Run aborted: {e}", "err")
    finally:
        job.emit("end", logPath=job.log_path)
        job.finish()
        RUN_LOCK.release()


def validate_run(p):
    errs = []
    if not SPRINT_RE.match(str(p.get("sprint", ""))):
        errs.append("Sprint number is required (letters, digits, . _ -)")
    if not BRANCH_RE.match(p.get("source", "")):
        errs.append("Source branch is missing or not a valid branch name")
    if not p.get("repos"):
        errs.append("Select at least one repository")
    if not p.get("targets"):
        errs.append("Add at least one target branch")
    for t in p.get("targets", []):
        if not BRANCH_RE.match(t.get("branch", "")):
            errs.append(f"Invalid target branch: '{t.get('branch', '')}'")
        elif t["branch"] == p.get("source"):
            errs.append(f"Target '{t['branch']}' is the same as the source")
    for r in p.get("repos", []):
        if not re.match(r"^[A-Za-z0-9._\-~+]+$", r.get("slug", "")):
            errs.append(f"Invalid repo slug: '{r.get('slug', '')}'")
        if not re.match(r"^[A-Za-z0-9_~+\-]+$", r.get("project", "")):
            errs.append(f"Invalid project key for {r.get('slug', '')}")
    if not p.get("dryRun"):
        if not p.get("token"):
            errs.append("Bitbucket API key is required for a live run")
        if not str(p.get("baseUrl", "")).startswith(("https://", "http://")):
            errs.append("Bitbucket base URL must start with https://")
    return errs


# ============================================================
# HTTP
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self):
        # Block other websites from driving this server through the browser.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/config":
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
            self._json(200, cfg)
        elif u.path == "/api/stream":
            job = JOBS.get(parse_qs(u.query).get("job", [""])[0])
            if not job:
                return self._json(404, {"error": "unknown job"})
            self._stream(job)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin request refused"})
        u = urlparse(self.path)
        try:
            p = self._body()
        except Exception:
            return self._json(400, {"error": "invalid JSON"})
        if u.path == "/api/config":
            p.pop("token", None)  # never persist the API key
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(p, f, indent=2)
            self._json(200, {"ok": True})
        elif u.path == "/api/repo-info":
            self._json(200, repo_info(p.get("path", ""), fetch=bool(p.get("fetch"))))
        elif u.path == "/api/run":
            errs = validate_run(p)
            if errs:
                return self._json(400, {"errors": errs})
            if not RUN_LOCK.acquire(blocking=False):
                return self._json(409, {"errors": ["A merge run is already in progress"]})
            cfg = {
                "sprint": str(p["sprint"]), "source": p["source"], "repos": p["repos"],
                "targets": p["targets"], "dryRun": bool(p.get("dryRun")),
                "baseUrl": p.get("baseUrl", ""), "token": p.get("token", ""),
                "insecure": bool(p.get("insecure")),
                "date": datetime.date.today().strftime("%Y%m%d"),
            }
            job = Job()
            JOBS[job.id] = job
            threading.Thread(target=run_job, args=(job, cfg), daemon=True).start()
            self._json(200, {"job": job.id, "logPath": job.log_path})
        else:
            self._json(404, {"error": "not found"})

    def _stream(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        i = 0
        try:
            while True:
                with job.cond:
                    if i >= len(job.events) and not job.done:
                        job.cond.wait(timeout=15)
                    batch = job.events[i:]
                    i += len(batch)
                    finished = job.done and i >= len(job.events)
                if batch:
                    for ev in batch:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                if finished:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description="Cross-merge UI server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Cross-merge UI running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
