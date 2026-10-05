"""Prepare a build copy of an STM32CubeIDE project, apply parameter values,
compile it with the GNU tools bundled in STM32CubeIDE and collect the .bin."""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import scanner

COPY_EXCLUDES = {".git", ".metadata", ".settings_bak"}
BUILD_ARTIFACT_EXT = (".o", ".d", ".su", ".cyclo", ".elf", ".bin", ".hex", ".map", ".list")


# --------------------------------------------------------------------------
# Toolchain discovery
# --------------------------------------------------------------------------

def find_toolchains(extra_roots=()):
    """Return list of {ide, version, gcc_bin, make_bin, cubeidec}."""
    roots = list(extra_roots) + [r"C:\ST", r"C:\Program Files\STMicroelectronics",
                                 os.path.expanduser(r"~\AppData\Local\Programs")]
    found = []
    for root in roots:
        for ide in glob.glob(os.path.join(root, "STM32CubeIDE*", "STM32CubeIDE")) + \
                glob.glob(os.path.join(root, "STM32CubeIDE*")):
            plugins = os.path.join(ide, "plugins")
            if not os.path.isdir(plugins):
                continue
            makes = sorted(glob.glob(os.path.join(plugins, "*externaltools.make.win32*", "tools", "bin")))
            cubeidec = os.path.join(ide, "stm32cubeidec.exe")
            for gcc in sorted(glob.glob(os.path.join(plugins, "*gnu-tools-for-stm32.*.win32*", "tools", "bin"))):
                m = re.search(r"gnu-tools-for-stm32\.([\d.]+rel\d+)", gcc)
                found.append({
                    "id": "%s | GCC %s" % (os.path.basename(os.path.dirname(ide)) or ide, m.group(1) if m else "?"),
                    "ide": ide,
                    "gcc_version": m.group(1) if m else "",
                    "gcc_bin": gcc,
                    "make_bin": makes[-1] if makes else "",
                    "cubeidec": cubeidec if os.path.isfile(cubeidec) else "",
                })
    # de-dup
    seen, out = set(), []
    for t in found:
        if t["gcc_bin"] not in seen:
            seen.add(t["gcc_bin"]); out.append(t)
    return out


def pick_toolchain(toolchains, wanted_version="", preferred_id=""):
    if preferred_id:
        for t in toolchains:
            if t["id"] == preferred_id:
                return t
    if wanted_version:
        for t in toolchains:
            if t["gcc_version"] == wanted_version:
                return t
    return toolchains[-1] if toolchains else None


# --------------------------------------------------------------------------
# Project helpers
# --------------------------------------------------------------------------

def find_project_root(path):
    """Directory containing .cproject (searching downwards)."""
    if os.path.isfile(os.path.join(path, ".cproject")):
        return path
    best = None
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in (".git", "Drivers", "Middlewares")]
        if ".cproject" in filenames:
            if best is None or dirpath.count(os.sep) < best.count(os.sep):
                best = dirpath
    return best


def project_info(root):
    info = {"name": os.path.basename(root), "configs": [], "artifact": ""}
    try:
        with open(os.path.join(root, ".project"), encoding="utf-8", errors="replace") as fh:
            m = re.search(r"<name>([^<]+)</name>", fh.read())
            if m:
                info["name"] = m.group(1).strip()
    except OSError:
        pass
    try:
        with open(os.path.join(root, ".cproject"), encoding="utf-8", errors="replace") as fh:
            cp = fh.read()
        names = re.findall(r'<configuration [^>]*?name="([^"]+)"', cp)
        info["configs"] = list(dict.fromkeys(names))
        m = re.search(r'artifactName="([^"]+)"', cp)
        if m and "${" not in m.group(1):
            info["artifact"] = m.group(1)
    except OSError:
        pass
    if not info["artifact"]:
        info["artifact"] = info["name"]
    info["prebuilt_makefiles"] = [c for c in info["configs"]
                                  if os.path.isfile(os.path.join(root, c, "makefile"))]
    return info


def makefile_gcc_version(root, config):
    try:
        with open(os.path.join(root, config, "makefile"), encoding="utf-8", errors="replace") as fh:
            m = re.search(r"GNU Tools for STM32 \(([\d.]+rel\d+)\)", fh.read(2000))
            return m.group(1) if m else ""
    except OSError:
        return ""


def _file_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_if_changed(path, data):
    if _file_bytes(path) == data:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return True


def sync_tree(src, dst, configs, log):
    """Mirror src -> dst. Build output folders are not copied, except the
    makefiles inside them. Only files whose content differs are written, so
    make only recompiles what actually changed."""
    config_dirs = set(configs)
    wanted = set()
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        top = rel.split(os.sep)[0] if rel != "." else ""
        dirnames[:] = [d for d in dirnames if d not in COPY_EXCLUDES]
        for f in filenames:
            if top in config_dirs and not (f == "makefile" or f.endswith((".mk", ".list"))):
                continue
            s = os.path.join(dirpath, f)
            relf = os.path.normpath(os.path.join(rel, f))
            wanted.add(relf)
            d = os.path.join(dst, relf)
            data = _file_bytes(s)
            if data is not None:
                _write_if_changed(d, data)
    # remove files that disappeared from the source (but never build outputs)
    removed = 0
    for dirpath, dirnames, filenames in os.walk(dst):
        rel = os.path.relpath(dirpath, dst)
        top = rel.split(os.sep)[0] if rel != "." else ""
        if top in config_dirs or top == ".metadata":
            continue
        for f in filenames:
            relf = os.path.normpath(os.path.join(rel, f))
            if relf not in wanted:
                os.remove(os.path.join(dirpath, f)); removed += 1
    if removed:
        log("Removed %d file(s) no longer in the source project" % removed)


ABS_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^"\s\']*')


def relocate_makefiles(work, config, log):
    """CubeIDE writes absolute paths of the machine that generated the
    makefiles. Rewrite every absolute path that points inside the project to
    a path relative to the build folder ("../...")."""
    cfg_dir = os.path.join(work, config)
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(cfg_dir) for f in fs
             if f == "makefile" or f.endswith((".mk", ".list"))]
    # learn the old project root(s) from absolute paths found in the files
    roots = {}
    for path in files:
        text = _file_bytes(path).decode("utf-8", "replace")
        for ap in set(ABS_PATH_RE.findall(text)):
            norm = ap.replace("\\", "/").rstrip("/")
            parts = norm.split("/")
            for i in range(len(parts) - 1, 0, -1):
                suffix = "/".join(parts[i:])
                if os.path.exists(os.path.join(work, suffix)):
                    old_root = "/".join(parts[:i])
                    roots[old_root] = roots.get(old_root, 0) + 1
                    break
    if not roots:
        return
    changed = 0
    for path in files:
        text = _file_bytes(path).decode("utf-8", "replace")
        new = text
        for old_root in sorted(roots, key=len, reverse=True):
            variants = {old_root, old_root.replace("/", "\\")}
            for v in variants:
                pat = re.compile(re.escape(v) + r'(?P<rest>(?:[\\/][^"\s\']*)?)(?![^\\/"\s\'])', re.IGNORECASE)
                new = pat.sub(lambda m: ".." + m.group("rest").replace("\\", "/"), new)
        if new != text:
            _write_if_changed(path, new.encode("utf-8")); changed += 1
    if changed:
        log("Rewrote absolute paths in %d makefile(s): %s -> project-relative" % (changed, ", ".join(roots)))


def dedupe_cproject(work, log):
    """Some .cproject files contain the same configuration name twice; the
    headless builder then builds both and the second may fail. Keep the first."""
    path = os.path.join(work, ".cproject")
    text = _file_bytes(path)
    if text is None:
        return
    text = text.decode("utf-8", "replace")
    blocks = list(re.finditer(r"\s*<cconfiguration .*?</cconfiguration>", text, re.S))
    seen, drop = set(), []
    for b in blocks:
        m = re.search(r'<configuration [^>]*?name="([^"]+)"', b.group(0))
        name = m.group(1) if m else None
        if name in seen:
            drop.append(b)
        seen.add(name)
    if drop:
        for b in reversed(drop):
            text = text[:b.start()] + text[b.end():]
        _write_if_changed(path, text.encode("utf-8"))
        log("Removed %d duplicate configuration(s) from .cproject (build copy only)" % len(drop))


# --------------------------------------------------------------------------
# Build job
# --------------------------------------------------------------------------

class BuildJob:
    def __init__(self, job_id):
        self.id = job_id
        self.lines = []
        self.state = "running"   # running | ok | failed
        self.result = None
        self.started = time.time()
        self.lock = threading.Lock()

    def log(self, msg):
        with self.lock:
            for line in str(msg).splitlines() or [""]:
                self.lines.append(line)

    def snapshot(self, since=0):
        with self.lock:
            return {"id": self.id, "state": self.state, "lines": self.lines[since:],
                    "next": len(self.lines), "result": self.result,
                    "elapsed": round(time.time() - self.started, 1)}


def _run(cmd, cwd, env, job, timeout=1800):
    job.log("> " + " ".join('"%s"' % c if " " in c else c for c in cmd))
    # Windows does not look up executables in the child's PATH
    exe = shutil.which(cmd[0], path=env.get("PATH"))
    if not exe:
        raise RuntimeError("'%s' not found (toolchain PATH: %s)" % (cmd[0], env.get("PATH", "")[:300]))
    cmd = [exe] + list(cmd[1:])
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + timeout
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        # compiler command lines are huge; shorten them for the UI
        if line.startswith("arm-none-eabi-gcc ") and len(line) > 220:
            m = re.match(r'arm-none-eabi-gcc\s+"?([^"\s]+)', line)
            line = "  CC  " + (m.group(1) if m else line[:200])
        job.log(line)
        if time.time() > deadline:
            proc.kill()
            job.log("!! timeout")
            break
    return proc.wait()


def build(job, src_root, work_root, changes_by_file, opts):
    """opts: config, mode ('auto'|'make'|'headless'), toolchain, jobs, clean,
    out_dir, out_name, manifest"""
    log = job.log
    info = project_info(src_root)
    config = opts.get("config") or (info["prebuilt_makefiles"] or info["configs"] or ["Debug"])[0]
    tc = opts["toolchain"]
    log("Project %s  |  configuration %s  |  %s" % (info["name"], config, tc["id"]))

    os.makedirs(work_root, exist_ok=True)
    log("Syncing source -> build copy ...")
    sync_tree(src_root, work_root, info["configs"] or [config], log)

    # apply parameter values
    total_errors = []
    for relfile, changes in changes_by_file.items():
        path = os.path.join(work_root, relfile)
        text, enc = scanner.read_text(path)
        new, applied, errors = scanner.apply_changes(text, relfile, changes)
        total_errors += errors
        if new != text:
            with open(path, "wb") as fh:
                fh.write(new.encode(enc, "replace"))
        log("Applied %d value(s) in %s" % (len(applied), relfile))
    for e in total_errors:
        log("!! " + e)
    if total_errors:
        raise RuntimeError("Some parameters could not be applied (see log)")

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(p for p in (tc["gcc_bin"], tc["make_bin"]) if p) + os.pathsep + env.get("PATH", "")

    mode = opts.get("mode", "auto")
    has_makefile = os.path.isfile(os.path.join(work_root, config, "makefile"))
    if mode == "auto":
        mode = "make" if has_makefile else "headless"

    if mode == "headless":
        if not tc.get("cubeidec"):
            raise RuntimeError("stm32cubeidec.exe not found - cannot regenerate makefiles")
        dedupe_cproject(work_root, log)
        ws = os.path.join(os.path.dirname(work_root), "_workspace_" + os.path.basename(work_root))
        os.makedirs(ws, exist_ok=True)
        log("Generating makefiles with STM32CubeIDE headless build (this takes ~1-2 min) ...")
        rc = _run([tc["cubeidec"], "--launcher.suppressErrors", "-nosplash",
                   "-application", "org.eclipse.cdt.managedbuilder.core.headlessbuild",
                   "-data", ws, "-import", work_root, "-build", "%s/%s" % (info["name"], config)],
                  work_root, env, job)
        log("Headless build exited with %s" % rc)
        if not os.path.isfile(os.path.join(work_root, config, "makefile")):
            raise RuntimeError("Headless build did not produce %s/makefile" % config)
        # make sure the final binary comes from our own make run below
    else:
        relocate_makefiles(work_root, config, log)

    build_dir = os.path.join(work_root, config)
    if opts.get("clean"):
        _run(["make", "clean"], build_dir, env, job)
    rc = _run(["make", "-j%d" % int(opts.get("jobs") or os.cpu_count() or 4), "all"], build_dir, env, job)
    if rc != 0:
        raise RuntimeError("make failed with exit code %s" % rc)

    bins = sorted(glob.glob(os.path.join(build_dir, "*.bin")), key=os.path.getmtime)
    if not bins:
        elfs = sorted(glob.glob(os.path.join(build_dir, "*.elf")), key=os.path.getmtime)
        if not elfs:
            raise RuntimeError("Build finished but no .elf/.bin found in %s" % build_dir)
        binpath = elfs[-1][:-4] + ".bin"
        _run(["arm-none-eabi-objcopy", "-O", "binary", elfs[-1], binpath], build_dir, env, job)
        bins = [binpath]
    binpath = bins[-1]

    size_line = ""
    elf = binpath[:-4] + ".elf"
    if os.path.isfile(elf):
        try:
            out = subprocess.run([shutil.which("arm-none-eabi-size", path=env["PATH"]) or "arm-none-eabi-size", elf], cwd=build_dir, env=env, capture_output=True,
                                 text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            size_line = out.strip()
            log(size_line)
        except OSError:
            pass

    os.makedirs(opts["out_dir"], exist_ok=True)
    out_bin = os.path.join(opts["out_dir"], opts["out_name"] + ".bin")
    shutil.copyfile(binpath, out_bin)
    data = _file_bytes(out_bin)
    manifest = dict(opts.get("manifest") or {})
    manifest.update({
        "bin": os.path.basename(out_bin),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "configuration": config,
        "toolchain": tc["id"],
        "size": size_line,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    with open(out_bin[:-4] + ".json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    log("")
    log("OK  ->  %s  (%d bytes)" % (out_bin, len(data)))
    return {"bin": out_bin, "name": os.path.basename(out_bin), "size": len(data), "sha256": manifest["sha256"]}
