"""Find tunable parameters in C sources and rewrite their values.

Two kinds of parameters are detected:
  * ``define``   -- ``#define NAME VALUE`` (also ``// #define NAME VALUE``,
                    which is reported as disabled so it can be toggled on)
  * ``variable`` -- file-scope scalar initialisations such as
                    ``static uint32_t foo = 10;`` or ``const char *url = "...";``

Every parameter carries the exact span of its value inside its line, so the
same scanner is used both to list parameters and to patch them.
"""

import os
import re

SOURCE_EXT = (".c", ".h")
DEFAULT_EXCLUDES = ("Drivers", "Middlewares", "Debug", "Release", ".git",
                    ".settings", ".metadata", "build")

SCALAR_TYPES = (
    r"(?:unsigned\s+|signed\s+)?(?:long\s+long|long|short|int|char)(?:\s+int)?"
    r"|u?int(?:8|16|32|64)_t|u?int_(?:least|fast)(?:8|16|32|64)_t"
    r"|float|double|bool|_Bool|size_t|BaseType_t|UBaseType_t|TickType_t"
    r"|uint|uchar|ulong|byte"
)

DEFINE_RE = re.compile(r"^(?P<lead>\s*)(?P<comment>//+\s*)?#\s*define\s+(?P<name>[A-Za-z_]\w*)(?P<rest>.*)$")
VAR_RE = re.compile(
    r"^\s*(?P<quals>(?:(?:static|const|volatile|extern|register)\s+)*)"
    r"(?P<type>" + SCALAR_TYPES + r")\s*(?P<quals2>(?:const\s+|volatile\s+)*)"
    r"(?P<decls>[^;{}()]*?(?:\[[^\]]*\][^;{}]*?)?)\s*;"
)
NUM_RE = re.compile(
    r"^(?P<pre>\(\s*)?(?P<num>[-+]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))"
    r"(?P<suf>[uUlLfF]*)(?P<post>\s*\))?$"
)


def _mask(text):
    """Return (no_comments, no_comments_no_strings), same length as text."""
    out_c, out_cs = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank = " " * (j - i)
            out_c.append(blank); out_cs.append(blank)
            i = j
        elif ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank = re.sub(r"[^\n]", " ", text[i:j])
            out_c.append(blank); out_cs.append(blank)
            i = j
        elif ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out_c.append(text[i:j])
            out_cs.append(ch + " " * max(0, j - i - 2) + (ch if j - i >= 2 else ""))
            i = j
        else:
            out_c.append(ch); out_cs.append(ch)
            i += 1
    return "".join(out_c), "".join(out_cs)


def _split_comment(s):
    """Split 'value  // comment' -> (value, comment) honouring strings."""
    in_str = None
    i = 0
    while i < len(s):
        ch = s[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif s.startswith("//", i) or s.startswith("/*", i):
            return s[:i], s[i:]
        i += 1
    return s, ""


def _clean_comment(c):
    c = c.strip()
    if "/**<" in c[1:]:
        c = c[c.index("/**<", 1):]
    c = re.sub(r"^/\*+<?|\*+/$|^//+<?", "", c).strip()
    c = re.sub(r"\*/\s*$", "", c).strip()
    return c


def classify(value):
    v = value.strip()
    if v == "":
        return "flag"
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', v):
        return "string"
    if re.fullmatch(r"'(?:[^'\\]|\\.)'", v):
        return "char"
    m = NUM_RE.match(v)
    if m:
        num = m.group("num").lower()
        if num.lstrip("+-").startswith(("0x", "0b")):
            return "hex"
        if "f" in m.group("suf").lower() or "." in num or "e" in num:
            return "float"
        return "int"
    if v in ("true", "false", "TRUE", "FALSE"):
        return "bool"
    return "expr"


def format_value(original, new, vtype):
    """Render a user-entered value back into C, keeping the original style
    (parentheses, U/f suffixes, quotes)."""
    new = str(new).strip()
    if vtype == "string":
        if not (new.startswith('"') and new.endswith('"') and len(new) >= 2):
            new = '"' + new.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return new
    if vtype in ("int", "float", "hex"):
        m = NUM_RE.match(original.strip())
        if not NUM_RE.match(new):
            raise ValueError("'%s' is not a number" % new)
        nm = NUM_RE.match(new)
        if m and not nm.group("suf") and not nm.group("pre"):
            num = nm.group("num")
            suf = m.group("suf")
            if "f" in suf.lower() and not re.search(r"[.eExX]", num):
                num += ".0"
            return (m.group("pre") or "") + num + suf + (m.group("post") or "")
        return new
    return new


def _iter_files(root, excludes):
    ex = {e.lower() for e in excludes}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.replace("\\", "/").split("/")
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in ex and not d.startswith("."))
        if parts and parts[0].lower() in ex:
            continue
        for f in sorted(filenames):
            if f.lower().endswith(SOURCE_EXT):
                yield os.path.join(dirpath, f)


def read_text(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


def scan_text(text, relfile):
    """Return list of parameter dicts found in one file."""
    params = []
    masked_c, masked_cs = _mask(text)
    raw_lines = text.split("\n")
    c_lines = masked_c.split("\n")
    cs_lines = masked_cs.split("\n")
    depth = 0
    in_block_comment_prev = False
    prev_comment = ""
    seen = {}

    def add(p):
        key = p["name"]
        seen[key] = seen.get(key, 0) + 1
        p["occurrence"] = seen[key]
        p["id"] = "%s::%s" % (relfile, key) + ("#%d" % seen[key] if seen[key] > 1 else "")
        params.append(p)

    for idx, raw in enumerate(raw_lines):
        cline = c_lines[idx] if idx < len(c_lines) else ""
        csline = cs_lines[idx] if idx < len(cs_lines) else ""
        line_depth = depth
        depth += csline.count("{") - csline.count("}")
        if depth < 0:
            depth = 0
        # A line fully inside a block comment masks to blanks, but so does a
        # `//` comment line. Distinguish: `//#define` must start the raw line.
        stripped = raw.strip()

        m = DEFINE_RE.match(raw.rstrip("\r"))
        if m:
            commented = bool(m.group("comment"))
            active = "#" in cline and not commented
            inside_block = (not commented) and not active
            rest = m.group("rest")
            if not inside_block and not rest.startswith("(") and not raw.rstrip().endswith("\\"):
                value_part, comment = _split_comment(rest)
                value = value_part.strip()
                # value span in the raw line
                rest_start = m.start("rest")
                vstart = rest_start + (len(value_part) - len(value_part.lstrip()))
                vend = vstart + len(value)
                desc = _clean_comment(comment) or prev_comment
                vtype = classify(value)
                # skip include guards and empty commented junk
                if not (vtype == "flag" and (m.group("name").endswith("_H") or m.group("name").endswith("_H_")
                                             or m.group("name").startswith("__"))):
                    add({
                        "kind": "define",
                        "file": relfile,
                        "line": idx + 1,
                        "name": m.group("name"),
                        "value": value,
                        "type": vtype,
                        "enabled": not commented,
                        "description": desc,
                        "_span": (vstart, vend),
                        "_comment_span": (m.start("comment"), m.end("comment")) if commented else None,
                        "_lead": m.end("lead"),
                    })
            prev_comment = ""
            continue

        if line_depth == 0 and not stripped.startswith("#") and "=" in cline:
            vm = VAR_RE.match(cline)
            if vm and "(" not in vm.group("decls"):
                quals = (vm.group("quals") + vm.group("quals2")).split()
                if "extern" not in quals:
                    decls = vm.group("decls")
                    base = vm.start("decls")
                    _, trailing = _split_comment(raw[vm.end():])
                    desc = _clean_comment(trailing) or prev_comment
                    # split declarators at top-level commas
                    pos = 0
                    for part in _split_top(decls):
                        dm = re.match(r"\s*(\*?\s*)(?P<name>[A-Za-z_]\w*)\s*(?P<arr>\[[^\]]*\])?\s*=\s*(?P<val>.+?)\s*$", part)
                        if dm:
                            val = dm.group("val")
                            vtype = classify(val)
                            if vtype not in ("expr", "flag"):
                                vstart = base + pos + dm.start("val")
                                add({
                                    "kind": "variable",
                                    "file": relfile,
                                    "line": idx + 1,
                                    "name": dm.group("name"),
                                    "value": raw[vstart:vstart + len(val)],
                                    "type": vtype,
                                    "ctype": " ".join(quals + [vm.group("type")]),
                                    "enabled": True,
                                    "description": desc,
                                    "_span": (vstart, vstart + len(val)),
                                })
                        pos += len(part) + 1

        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            c = _clean_comment(stripped.lstrip("*").strip() if stripped.startswith("*") else stripped)
            prev_comment = c if c and len(c) < 160 else prev_comment
        elif stripped:
            prev_comment = ""
    return params


def _split_top(s):
    parts, depth, cur, q = [], 0, [], None
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def scan_project(root, excludes=DEFAULT_EXCLUDES):
    result = []
    for path in _iter_files(root, excludes):
        rel = os.path.relpath(path, root).replace("\\", "/")
        text, _ = read_text(path)
        for p in scan_text(text, rel):
            result.append(public(p))
    return result


def public(p):
    return {k: v for k, v in p.items() if not k.startswith("_")}


def apply_changes(text, relfile, changes):
    """changes: {param_id: {"value": str|None, "enabled": bool|None}}.
    Returns (new_text, applied_ids, errors)."""
    params = {p["id"]: p for p in scan_text(text, relfile)}
    lines = text.split("\n")
    applied, errors = [], []
    # apply from the right so spans on the same line stay valid
    todo = []
    for pid, ch in changes.items():
        p = params.get(pid)
        if not p:
            errors.append("%s: not found in %s any more" % (pid, relfile))
            continue
        todo.append((p["line"] - 1, p["_span"][0], p, ch))
    todo.sort(key=lambda t: (t[0], t[1]), reverse=True)
    for li, _, p, ch in todo:
        line = lines[li]
        try:
            if ch.get("value") is not None and p["type"] != "flag":
                newv = format_value(p["value"], ch["value"], p["type"])
                s, e = p["_span"]
                if newv != p["value"]:
                    line = line[:s] + newv + line[e:]
            if p["kind"] == "define" and ch.get("enabled") is not None:
                want = bool(ch["enabled"])
                if want and not p["enabled"] and p["_comment_span"]:
                    s, e = p["_comment_span"]
                    line = line[:s] + line[e:]
                elif not want and p["enabled"]:
                    lead = p["_lead"]
                    line = line[:lead] + "// " + line[lead:]
        except ValueError as exc:
            errors.append("%s: %s" % (p["name"], exc))
            continue
        lines[li] = line
        applied.append(p["id"])
    return "\n".join(lines), applied, errors
