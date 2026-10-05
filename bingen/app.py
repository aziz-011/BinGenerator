"""BinGenerator - local web UI to tune STM32 firmware parameters and build a .bin.

Run:  python -m bingen            (from the BinGenerator folder)
"""

import io
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import builder, scanner

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
PROJECTS = os.path.join(DATA, "projects")
WORK = os.path.join(DATA, "work")
OUTPUT = os.path.join(BASE, "output")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SETTINGS_FILE = os.path.join(DATA, "settings.json")

JOBS = {}
BUILD_LOCKS = {}
_TOOLCHAINS = None


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------

def _slug(name):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "project"
    return s[:60]


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def settings():
    return _load_json(SETTINGS_FILE, {"toolchain": "", "extra_ide_roots": [], "jobs": 0})


def toolchains(refresh=False):
    global _TOOLCHAINS
    if _TOOLCHAINS is None or refresh:
        _TOOLCHAINS = builder.find_toolchains(settings().get("extra_ide_roots", []))
    return _TOOLCHAINS


def proj_dir(slug):
    return os.path.join(PROJECTS, slug)


def load_project(slug):
    p = _load_json(os.path.join(proj_dir(slug), "project.json"), None)
    if not p:
        raise KeyError("Unknown project '%s'" % slug)
    return p


def project_root(p):
    if p["source_type"] == "folder":
        return p["source_path"]
    return os.path.join(proj_dir(p["slug"]), "src", p.get("root_rel", ""))


def load_profile(slug):
    return _load_json(os.path.join(proj_dir(slug), "profile.json"),
                      {"params": [], "presets": {}, "last_values": {}, "build": {}})


def save_profile(slug, prof):
    _save_json(os.path.join(proj_dir(slug), "profile.json"), prof)


def list_projects():
    out = []
    if os.path.isdir(PROJECTS):
        for slug in sorted(os.listdir(PROJECTS)):
            p = _load_json(os.path.join(PROJECTS, slug, "project.json"), None)
            if p:
                root = project_root(p)
                p["available"] = os.path.isdir(root)
                p["root"] = root
                out.append(p)
    return out


def _unique_slug(name):
    base = _slug(name)
    slug, i = base, 2
    while os.path.exists(proj_dir(slug)):
        slug = "%s_%d" % (base, i); i += 1
    return slug


def import_zip(data, filename, slug=None):
    zf = zipfile.ZipFile(io.BytesIO(data))
    new = slug is None
    if new:
        slug = _unique_slug(os.path.splitext(os.path.basename(filename))[0])
    pdir = proj_dir(slug)
    src = os.path.join(pdir, "src")
    tmp = src + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    root_abs = os.path.abspath(tmp)
    for member in zf.infolist():
        target = os.path.abspath(os.path.join(tmp, member.filename))
        if not (target == root_abs or target.startswith(root_abs + os.sep)):
            raise ValueError("Unsafe path in zip: %s" % member.filename)
    zf.extractall(tmp)
    root = builder.find_project_root(tmp)
    if not root:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("No STM32CubeIDE project (.cproject) found in the zip")
    shutil.rmtree(src, ignore_errors=True)
    os.replace(tmp, src)
    root_rel = os.path.relpath(root, tmp)
    root_rel = "" if root_rel == "." else root_rel
    info = builder.project_info(os.path.join(src, root_rel))
    p = load_project(slug) if not new else {"slug": slug, "created": time.strftime("%Y-%m-%d %H:%M")}
    p.update({"name": info["name"], "source_type": "zip", "source_path": filename,
              "root_rel": root_rel, "updated": time.strftime("%Y-%m-%d %H:%M")})
    _save_json(os.path.join(pdir, "project.json"), p)
    return p


def import_folder(path, slug=None):
    path = os.path.abspath(path.strip().strip('"'))
    if not os.path.isdir(path):
        raise ValueError("Folder not found: %s" % path)
    root = builder.find_project_root(path)
    if not root:
        raise ValueError("No STM32CubeIDE project (.cproject) found under %s" % path)
    info = builder.project_info(root)
    new = slug is None
    if new:
        slug = _unique_slug(info["name"])
    p = load_project(slug) if not new else {"slug": slug, "created": time.strftime("%Y-%m-%d %H:%M")}
    p.update({"name": info["name"], "source_type": "folder", "source_path": root,
              "updated": time.strftime("%Y-%m-%d %H:%M")})
    _save_json(os.path.join(proj_dir(slug), "project.json"), p)
    return p


def scan(slug):
    p = load_project(slug)
    root = project_root(p)
    params = scanner.scan_project(root)
    return {"project": p, "info": builder.project_info(root), "params": params}


def file_context(slug, rel, line, radius=8):
    root = project_root(load_project(slug))
    path = os.path.abspath(os.path.join(root, rel))
    if not path.startswith(os.path.abspath(root) + os.sep):
        raise ValueError("bad path")
    text, _ = scanner.read_text(path)
    lines = text.split("\n")
    a, b = max(0, line - 1 - radius), min(len(lines), line + radius)
    return {"file": rel, "start": a + 1, "lines": [l.rstrip("\r") for l in lines[a:b]], "line": line}


def outputs(slug):
    d = os.path.join(OUTPUT, slug)
    res = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d), key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True):
            if f.endswith(".bin"):
                m = _load_json(os.path.join(d, f[:-4] + ".json"), {})
                res.append({"name": f, "size": os.path.getsize(os.path.join(d, f)),
                            "built_at": m.get("built_at", ""), "label": m.get("label", ""),
                            "changes": m.get("changes", {})})
    return res


def start_build(slug, body):
    p = load_project(slug)
    root = project_root(p)
    if not os.path.isdir(root):
        raise ValueError("Project source not found: %s" % root)
    lock = BUILD_LOCKS.setdefault(slug, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ValueError("A build is already running for this project")

    values = body.get("values", {})        # id -> {value, enabled}
    prof = load_profile(slug)
    prof["last_values"] = values
    prof["build"] = {k: body.get(k) for k in ("config", "mode", "toolchain", "label") if body.get(k) is not None}
    save_profile(slug, prof)

    # current values in source, to record what actually changed
    current = {q["id"]: q for q in scanner.scan_project(root)}
    changes_by_file, changed = {}, {}
    for pid, v in values.items():
        q = current.get(pid)
        if not q:
            continue
        changes_by_file.setdefault(q["file"], {})[pid] = v
        if (v.get("value") is not None and str(v["value"]).strip() != q["value"]) or \
                (v.get("enabled") is not None and bool(v["enabled"]) != q["enabled"]):
            changed[q["name"]] = {"from": q["value"] if q["enabled"] else "(disabled)",
                                  "to": v.get("value") if v.get("enabled", True) else "(disabled)"}

    info = builder.project_info(root)
    tcs = toolchains()
    config = body.get("config") or (info["prebuilt_makefiles"] or info["configs"] or ["Debug"])[0]
    tc = builder.pick_toolchain(tcs, builder.makefile_gcc_version(root, config),
                                body.get("toolchain") or settings().get("toolchain", ""))
    if not tc:
        lock.release()
        raise ValueError("No STM32CubeIDE GNU toolchain found (looked in C:\\ST)")
    label = _slug(body.get("label") or "custom")
    out_name = "%s_%s_%s" % (_slug(info["artifact"]), label, time.strftime("%Y%m%d-%H%M%S"))

    job = builder.BuildJob(uuid.uuid4().hex[:10])
    JOBS[job.id] = job

    def run():
        try:
            job.result = builder.build(job, root, os.path.join(WORK, slug), changes_by_file, {
                "config": config, "mode": body.get("mode", "auto"), "toolchain": tc,
                "jobs": settings().get("jobs") or 0, "clean": bool(body.get("clean")),
                "out_dir": os.path.join(OUTPUT, slug), "out_name": out_name,
                "manifest": {"project": info["name"], "label": body.get("label", ""),
                             "changes": changed, "values": values},
            })
            job.state = "ok"
        except Exception as exc:  # noqa: BLE001 - report everything to the UI
            job.log("")
            job.log("BUILD FAILED: %s" % exc)
            if not isinstance(exc, RuntimeError):
                job.log(traceback.format_exc())
            job.state = "failed"
        finally:
            lock.release()

    threading.Thread(target=run, daemon=True).start()
    return {"job": job.id, "changed": changed, "toolchain": tc["id"], "config": config}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BinGenerator/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _route(self, method):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [unquote(x) for x in url.path.strip("/").split("/") if x]
        # guard against cross-site requests to this local server
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and not re.match(r"^http://(127\.0\.0\.1|localhost):\d+$", origin):
                return self._send(403, {"error": "forbidden origin"})
        try:
            if method == "GET" and not parts:
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if parts[0] == "download" and len(parts) == 3:
                path = os.path.join(OUTPUT, _slug(parts[1]), os.path.basename(parts[2]))
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), "application/octet-stream",
                                      {"Content-Disposition": 'attachment; filename="%s"' % os.path.basename(path)})
            if parts[0] != "api":
                return self._send(404, {"error": "not found"})
            r = parts[1:]
            if r == ["projects"] and method == "GET":
                return self._send(200, list_projects())
            if r == ["toolchains"]:
                return self._send(200, {"toolchains": toolchains(q.get("refresh") == "1"), "settings": settings()})
            if r == ["settings"] and method == "PUT":
                s = settings(); s.update(self._json()); _save_json(SETTINGS_FILE, s)
                return self._send(200, s)
            if r == ["import", "zip"] and method == "POST":
                p = import_zip(self._body(), q.get("filename", "project.zip"), q.get("slug") or None)
                return self._send(200, p)
            if r == ["import", "folder"] and method == "POST":
                b = self._json()
                return self._send(200, import_folder(b["path"], b.get("slug") or None))
            if r and r[0] == "jobs" and len(r) == 2:
                job = JOBS.get(r[1])
                if not job:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, job.snapshot(int(q.get("since", 0))))
            if len(r) >= 2 and r[0] == "projects":
                slug = _slug(r[1])
                sub = r[2] if len(r) > 2 else ""
                if sub == "scan":
                    return self._send(200, scan(slug))
                if sub == "profile" and method == "GET":
                    return self._send(200, load_profile(slug))
                if sub == "profile" and method == "PUT":
                    prof = self._json(); save_profile(slug, prof)
                    return self._send(200, prof)
                if sub == "file":
                    return self._send(200, file_context(slug, q["path"], int(q.get("line", 1))))
                if sub == "build" and method == "POST":
                    return self._send(200, start_build(slug, self._json()))
                if sub == "outputs":
                    return self._send(200, outputs(slug))
                if sub == "open-output" and method == "POST":
                    d = os.path.join(OUTPUT, slug); os.makedirs(d, exist_ok=True); os.startfile(d)
                    return self._send(200, {"ok": True})
                if sub == "delete-output" and method == "POST":
                    name = os.path.basename(self._json()["name"])
                    for ext in (".bin", ".json"):
                        f = os.path.join(OUTPUT, slug, name[:-4] + ext)
                        if os.path.isfile(f):
                            os.remove(f)
                    return self._send(200, {"ok": True})
                if sub == "remove" and method == "POST":
                    shutil.rmtree(proj_dir(slug), ignore_errors=True)
                    shutil.rmtree(os.path.join(WORK, slug), ignore_errors=True)
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except KeyError as exc:
            return self._send(404, {"error": str(exc).strip("'\"")})
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": str(exc)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")


def main(port=8765, open_browser=True):
    os.makedirs(PROJECTS, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit("No free port")
    url = "http://127.0.0.1:%d/" % port
    print("BinGenerator running at %s  (close this window to stop)" % url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
