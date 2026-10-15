#!/usr/bin/env python3
"""Research hub tool: catalog, search, checks and indexes.

Standard library only. Python 3.9+ on macOS or Linux (it uses fcntl locks;
on Windows run the hub inside WSL). Run from the hub folder:

    python3 tools/hub.py --help

The catalog JSON files are written only by this tool, never edited by hand
or by an AI assistant directly. Every write is validated, locked, atomic,
journalled in .hub/journal.jsonl and preceded by a backup in .hub/history/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent

RECORD_KINDS = ("result", "conjecture", "question", "obstruction", "correction")
STATUSES = ("reported", "conditional", "verified", "open", "refuted", "superseded")
EVIDENCE_LEVELS = ("imported", "draft-proof", "computation-reproduced",
                   "primary-checked", "internally-reviewed", "externally-reviewed")
NOVELTY = ("unassessed", "known", "subsumed", "candidate")
SOURCE_STATES = ("lead", "imported", "primary-checked")
RECORD_REQUIRED = ("id", "project", "kind", "title", "topics", "status", "evidence_level",
                   "novelty", "statement", "hypotheses", "limitations", "evidence",
                   "depends_on", "supersedes", "subsumed_by", "source_refs")
SOURCE_REQUIRED = ("id", "project", "citation", "topics", "check_state", "locator",
                   "statement", "use", "limitations", "evidence")
LIST_FIELDS = ("topics", "hypotheses", "evidence", "depends_on", "supersedes",
               "subsumed_by", "source_refs")
PLACEHOLDER = re.compile(r"PROJECT-NAME|DATE-topic|Short precise title|One precise claim, with "
                         r"assumptions|A\. Author, Title of paper|xxxx\.xxxxx|\[TODO\]")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
PREFIX = re.compile(r"^[A-Z]{2,5}$")
TEXT_SUFFIXES = {".md", ".txt", ".tex", ".bib", ".py", ".sage", ".spyx", ".m2", ".g",
                 ".gp", ".mag", ".magma", ".jl", ".sing", ".lean", ".v", ".org",
                 ".rst", ".csv", ".log", ".json", ".ipynb", ".html", ".mpl", ".mw"}
SKIP_DIRS = {".git", ".hub", "__pycache__", "node_modules", ".ipynb_checkpoints",
             ".venv", "venv", ".mypy_cache", ".DS_Store"}
MAX_TEXT_BYTES = 3_000_000
NOVELTY_WARNING = ("An empty or partial result does not establish novelty; "
                   "it reports only what this search covered.")


class HubError(Exception):
    pass


# ---------------------------------------------------------------- basics

def today() -> str:
    return dt.date.today().isoformat()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_hash(entry: dict) -> str:
    raw = json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
    return sha256_bytes(raw)[:12]


def dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@contextmanager
def hub_lock():
    """One short exclusive lock for every write. The kernel releases it if
    the process dies, so the lock file itself is never deleted."""
    lock_dir = HUB / ".hub"
    lock_dir.mkdir(exist_ok=True)
    with open(lock_dir / "lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def journal(action: str, **fields) -> None:
    line = dict(time=now_utc(), action=action, user=os.environ.get("USER", "unknown"), **fields)
    path = HUB / ".hub" / "journal.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def backup(path: Path) -> None:
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        rel = str(path.relative_to(HUB)).replace(os.sep, "__")
        dest = HUB / ".hub" / "history" / f"{stamp}-{rel}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def load_json(path: Path, default=None):
    if not path.exists():
        if default is not None:
            return default
        raise HubError(f"Missing file: {path.relative_to(HUB)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HubError(f"{path.relative_to(HUB)} is not valid JSON: {exc}") from exc


def config() -> dict:
    return load_json(HUB / "hub.json")


def save_config(cfg: dict) -> None:
    backup(HUB / "hub.json")
    atomic_write(HUB / "hub.json", dump(cfg))


def project_dir(slug: str) -> Path:
    return HUB / "projects" / slug


def require_project(cfg: dict, slug: str | None) -> str:
    if not slug:
        projects = list(cfg.get("projects", {}))
        if len(projects) == 1:
            return projects[0]
        raise HubError("Name a project with --project. Registered: " + ", ".join(projects))
    if slug not in cfg.get("projects", {}):
        raise HubError(f"Unknown project '{slug}'. Registered: " + ", ".join(cfg.get("projects", {})))
    return slug


def roots(cfg: dict) -> dict:
    found = {"hub": HUB}
    for alias, info in cfg.get("archives", {}).items():
        found[alias] = Path(info["path"])
    return found


# ---------------------------------------------------------------- catalog

class Catalog:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.records: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.where: dict[str, str] = {}
        self.duplicates: list[str] = []
        for slug in cfg.get("projects", {}):
            for kind, store in (("records", self.records), ("sources", self.sources)):
                path = project_dir(slug) / "catalog" / f"{kind}.json"
                data = load_json(path, default=[])
                if not isinstance(data, list):
                    raise HubError(f"{path.relative_to(HUB)} must be a JSON list")
                for entry in data:
                    eid = entry.get("id", "?")
                    if eid in self.records or eid in self.sources:
                        self.duplicates.append(eid)
                    store[eid] = entry
                    self.where[eid] = slug

    def get(self, eid: str) -> dict | None:
        return self.records.get(eid) or self.sources.get(eid)

    def incoming(self) -> dict[str, list[tuple[str, str]]]:
        rev: dict[str, list[tuple[str, str]]] = {}
        for rid, rec in self.records.items():
            for target in rec.get("depends_on", []):
                rev.setdefault(target, []).append((rid, "depends on it"))
            for target in rec.get("source_refs", []):
                rev.setdefault(target, []).append((rid, "cites it"))
            for target in rec.get("supersedes", []):
                rev.setdefault(target, []).append((rid, "CORRECTS/SUPERSEDES it"))
            for target in rec.get("subsumed_by", []):
                rev.setdefault(target, []).append((rid, "is subsumed by it"))
        return rev


def is_source(entry: dict) -> bool:
    return "citation" in entry


def structure_errors(cat: Catalog) -> list[str]:
    """Problems visible from the catalog alone (no file reads)."""
    errors = [f"Duplicate ID {eid}" for eid in cat.duplicates]
    projects = cat.cfg.get("projects", {})
    known_roots = set(roots(cat.cfg))
    for eid, entry in list(cat.records.items()) + list(cat.sources.items()):
        source = is_source(entry)
        required = SOURCE_REQUIRED if source else RECORD_REQUIRED
        missing = [f for f in required if f not in entry]
        if missing:
            errors.append(f"{eid}: missing fields {', '.join(missing)}")
            continue
        slug = entry["project"]
        if slug not in projects:
            errors.append(f"{eid}: unknown project '{slug}'")
        elif cat.where.get(eid) != slug:
            errors.append(f"{eid}: stored in {cat.where.get(eid)} but says project {slug}")
        else:
            prefix = projects[slug]["prefix"]
            letter = "L" if source else "R"
            if not re.fullmatch(rf"{prefix}-{letter}\d{{3,}}", eid):
                errors.append(f"{eid}: ID should look like {prefix}-{letter}001")
        for field in LIST_FIELDS:
            if field in entry and not isinstance(entry[field], list):
                errors.append(f"{eid}: {field} must be a list")
        for item in entry.get("evidence", []) or []:
            if not isinstance(item, dict) or not {"root", "path", "locator", "sha256"} <= set(item):
                errors.append(f"{eid}: each evidence item needs root, path, locator, sha256")
            elif item["root"] not in known_roots:
                errors.append(f"{eid}: evidence root '{item['root']}' is not 'hub' or a registered archive")
        text = json.dumps(entry, ensure_ascii=False)
        if PLACEHOLDER.search(text):
            errors.append(f"{eid}: still contains a template placeholder")
        if source:
            if entry["check_state"] not in SOURCE_STATES:
                errors.append(f"{eid}: check_state must be one of {', '.join(SOURCE_STATES)}")
            if entry["check_state"] == "primary-checked" and not entry["evidence"]:
                errors.append(f"{eid}: primary-checked needs a completed source-check file as evidence")
            continue
        for field, allowed in (("kind", RECORD_KINDS), ("status", STATUSES),
                               ("evidence_level", EVIDENCE_LEVELS), ("novelty", NOVELTY)):
            if entry[field] not in allowed:
                errors.append(f"{eid}: {field} '{entry[field]}' must be one of {', '.join(allowed)}")
        if entry["kind"] == "obstruction" and not entry.get("reopening_condition"):
            errors.append(f"{eid}: an obstruction needs reopening_condition (what new input would reopen it)")
        if entry["status"] == "verified":
            review = entry.get("review")
            if not isinstance(review, dict) or not all(review.get(k) for k in ("date", "reviewer", "scope", "artifact")):
                errors.append(f"{eid}: 'verified' needs a review object with date, reviewer, scope and artifact")
            if entry["evidence_level"] in ("imported", "draft-proof"):
                errors.append(f"{eid}: 'verified' is inconsistent with evidence_level '{entry['evidence_level']}'")
        for target in entry["depends_on"] + entry["supersedes"] + entry["subsumed_by"]:
            if target not in cat.records:
                errors.append(f"{eid}: refers to unknown record {target}")
        for target in entry["source_refs"]:
            if target not in cat.sources:
                errors.append(f"{eid}: cites unknown source {target}")
        if eid in entry["depends_on"] + entry["supersedes"] + entry["subsumed_by"]:
            errors.append(f"{eid}: refers to itself")
    errors.extend(cycle_errors(cat))
    return errors


def cycle_errors(cat: Catalog) -> list[str]:
    state: dict[str, int] = {}
    errors: list[str] = []

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in cat.records.get(node, {}).get("depends_on", []):
            if nxt not in cat.records:
                continue
            if state.get(nxt) == 1:
                errors.append("Dependency cycle: " + " -> ".join(path + [node, nxt]))
            elif nxt not in state:
                visit(nxt, path + [node])
        state[node] = 2

    for rid in cat.records:
        if rid not in state:
            visit(rid, [])
    return errors


def evidence_problems(cat: Catalog, ids) -> list[str]:
    problems = []
    rts = roots(cat.cfg)
    for eid in ids:
        entry = cat.get(eid) or {}
        items = list(entry.get("evidence", []) or [])
        review = entry.get("review")
        if isinstance(review, dict) and isinstance(review.get("artifact"), dict):
            items.append(review["artifact"])
        for item in items:
            if not isinstance(item, dict) or item.get("root") not in rts:
                continue
            path = rts[item["root"]] / item.get("path", "")
            if not path.is_file():
                problems.append(f"{eid}: evidence file missing: {item['root']}:{item.get('path')}")
            elif sha256_file(path) != item.get("sha256"):
                problems.append(f"{eid}: STALE evidence (file changed since it was recorded): "
                                f"{item['root']}:{item.get('path')}")
    return problems


def closure(cat: Catalog, slug: str) -> set[str]:
    """A project's entries plus everything they depend on, cite, or are corrected by."""
    ids = {eid for eid, where in cat.where.items() if where == slug}
    rev = cat.incoming()
    frontier = list(ids)
    while frontier:
        eid = frontier.pop()
        entry = cat.get(eid) or {}
        linked = list(entry.get("depends_on", [])) + list(entry.get("source_refs", []))
        linked += [src for src, rel in rev.get(eid, []) if rel.startswith("CORRECTS")]
        for nxt in linked:
            if nxt not in ids and cat.get(nxt):
                ids.add(nxt)
                frontier.append(nxt)
    return ids


# ---------------------------------------------------------------- commands

def cmd_context(args) -> None:
    cfg = config()
    slug = require_project(cfg, args.project)
    info = cfg["projects"][slug]
    cat = Catalog(cfg)
    pdir = f"projects/{slug}"
    if args.role == "helper":
        print(f"Helper route for project '{slug}' ({info['title']}):")
        print("  1. Your role file and the brief you were given (one bounded question).")
        print(f"  2. {pdir}/AGENTS.md: only its 'Mathematical conventions' section.")
        print("  3. Only the files, record IDs and sources named in the brief.")
        print("Do not read conductor guides, investigation history or the full catalog.")
        print(f"Search: python3 tools/hub.py search \"terms\" --project {slug} [--archives]")
    else:
        print(f"Conductor route for project '{slug}' ({info['title']}):")
        for step, path in enumerate(["AGENTS.md", "WORKFLOW.md", "PROOF_SWARM.md (for proof work)",
                                     f"{pdir}/AGENTS.md", f"{pdir}/START_HERE.md",
                                     f"{pdir}/TOPICS.md (the relevant row only)"], 1):
            print(f"  {step}. {path}")
    archives = {a: i["path"] for a, i in cfg.get("archives", {}).items() if slug in i.get("projects", [])}
    print("Read-only archives: " + (", ".join(f"{a} = {p}" for a, p in archives.items()) or "none registered"))
    recs = [r for r in cat.records.values() if r.get("project") == slug]
    counts: dict[str, int] = {}
    for rec in recs:
        counts[rec.get("status", "?")] = counts.get(rec.get("status", "?"), 0) + 1
    srcs = [s for s in cat.sources.values() if s.get("project") == slug]
    print(f"Catalog: {len(recs)} records ("
          + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none") + f"), {len(srcs)} sources")
    if args.role != "helper":
        notes = sorted((project_dir(slug) / "investigations").glob("*/NOTE.md"))
        open_notes = []
        for note in notes:
            head = note.read_text(encoding="utf-8", errors="replace")[:600].lower()
            if "status: closed" not in head:
                open_notes.append(str(note.relative_to(HUB)))
        print("Investigations not marked closed: " + (", ".join(open_notes[-8:]) or "none"))


def cmd_add_project(args) -> None:
    if not SLUG.match(args.slug):
        raise HubError("Project name must be lowercase letters, digits and hyphens, e.g. hodge-fourfolds")
    prefix = args.prefix.upper()
    if not PREFIX.match(prefix):
        raise HubError("Prefix must be 2 to 5 capital letters, e.g. HF")
    with hub_lock():
        cfg = config()
        projects = cfg.setdefault("projects", {})
        if args.slug in projects:
            raise HubError(f"Project {args.slug} already exists")
        if any(p["prefix"] == prefix for p in projects.values()):
            raise HubError(f"Prefix {prefix} is already used")
        pdir = project_dir(args.slug)
        if pdir.exists():
            raise HubError(f"{pdir.relative_to(HUB)} already exists")
        values = {
            "TITLE": args.title, "SLUG": args.slug, "PREFIX": prefix, "DATE": today(),
            "DESCRIPTION": args.description or "(Describe the main question here.)",
            "CONVENTIONS": args.conventions or "- (List the conventions every argument must state: "
                           "field and characteristic, category of objects, smooth/projective, etc.)",
            "OWNER": cfg.get("owner", "the hub owner"),
        }
        for template in sorted((HUB / "templates" / "project").glob("*")):
            text = template.read_text(encoding="utf-8")
            for key, value in values.items():
                text = text.replace("{{" + key + "}}", value)
            atomic_write(pdir / template.name, text)
        atomic_write(pdir / "catalog" / "records.json", "[]\n")
        atomic_write(pdir / "catalog" / "sources.json", "[]\n")
        (pdir / "investigations").mkdir(parents=True, exist_ok=True)
        (pdir / "indexes").mkdir(parents=True, exist_ok=True)
        projects[args.slug] = {"title": args.title, "prefix": prefix, "state": "active",
                               "created": today(), "description": args.description or ""}
        save_config(cfg)
        update_project_table(cfg)
        journal("add-project", project=args.slug, prefix=prefix)
    render_project(Catalog(config()), args.slug)
    print(f"Created project '{args.slug}' (IDs {prefix}-R001..., sources {prefix}-L001...) in projects/{args.slug}/")


def update_project_table(cfg: dict) -> None:
    path = HUB / "START_HERE.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- projects:start -->", "<!-- projects:end -->"
    if start not in text or end not in text:
        return
    rows = ["| Project | Guide | State |", "|---|---|---|"]
    for slug, info in cfg.get("projects", {}).items():
        rows.append(f"| {info['title']} | [projects/{slug}/START_HERE.md](projects/{slug}/START_HERE.md) | {info.get('state', 'active')} |")
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    atomic_write(path, before + start + "\n" + "\n".join(rows) + "\n" + end + after)


def cmd_add_archive(args) -> None:
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        raise HubError(f"{path} is not a folder")
    alias_path = path
    alias = args.alias or re.sub(r"[^a-z0-9-]+", "-", path.name.lower()).strip("-")[:30] or "archive"
    if not SLUG.match(alias) or alias in ("hub", "library"):
        raise HubError("Give an --alias of lowercase letters, digits and hyphens (not 'hub' or 'library')")
    with hub_lock():
        cfg = config()
        for slug in args.project:
            require_project(cfg, slug)
        archives = cfg.setdefault("archives", {})
        if alias in archives and Path(archives[alias]["path"]) != alias_path:
            raise HubError(f"Alias {alias} already points to {archives[alias]['path']}")
        entry = archives.setdefault(alias, {"path": str(alias_path), "projects": [], "added": today()})
        for slug in args.project:
            if slug not in entry["projects"]:
                entry["projects"].append(slug)
        save_config(cfg)
        protect_in_claude_settings(alias_path)
        journal("add-archive", alias=alias, path=str(alias_path), projects=args.project)
    print(f"Registered read-only archive '{alias}' = {alias_path} for {', '.join(args.project)}")
    print(f"Search it with: python3 tools/hub.py search \"terms\" --project {args.project[0]} --archives")


def protect_in_claude_settings(path: Path) -> None:
    """Let Claude Code read an outside folder and forbid its edit tools there."""
    settings_path = HUB / ".claude" / "settings.json"
    if not settings_path.parent.exists():
        return
    settings = load_json(settings_path, default={})
    perms = settings.setdefault("permissions", {})
    if HUB not in path.parents and path != HUB:
        dirs = perms.setdefault("additionalDirectories", [])
        if str(path) not in dirs:
            dirs.append(str(path))
    rule = f"Edit(//{str(path).lstrip('/')}/**)"
    deny = perms.setdefault("deny", [])
    if rule not in deny:
        deny.append(rule)
    atomic_write(settings_path, dump(settings))


def load_ids() -> dict:
    return load_json(HUB / ".hub" / "ids.json", default={"counters": {}, "reserved": {}})


def next_id(ids: dict, cat: Catalog, prefix: str, letter: str) -> str:
    key = f"{prefix}-{letter}"
    used = [int(m.group(1)) for eid in list(cat.records) + list(cat.sources) + list(ids["reserved"])
            for m in [re.fullmatch(rf"{re.escape(key)}(\d+)", eid)] if m]
    number = max([ids["counters"].get(key, 0)] + used) + 1
    ids["counters"][key] = number
    return f"{key}{number:03d}"


def cmd_reserve(args) -> None:
    with hub_lock():
        cfg = config()
        slug = require_project(cfg, args.project)
        ids = load_ids()
        new = next_id(ids, Catalog(cfg), cfg["projects"][slug]["prefix"], "L" if args.kind == "L" else "R")
        ids["reserved"][new] = {"project": slug, "by": args.owner or os.environ.get("USER", "unknown"),
                                "date": today(), "purpose": args.purpose or ""}
        atomic_write(HUB / ".hub" / "ids.json", dump(ids))
        journal("reserve", id=new, project=slug)
    print(new)


def parse_put_file(path: Path) -> tuple[list[dict], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError(f"Cannot read {path}: {exc}") from exc
    if isinstance(data, dict) and "entries" in data:
        return list(data["entries"]), dict(data.get("expect", {}))
    if isinstance(data, dict):
        return [data], {}
    if isinstance(data, list):
        return data, {}
    raise HubError("The file must hold one entry, a list of entries, or {\"entries\": [...], \"expect\": {...}}")


def cmd_put(args) -> None:
    entries, expect = parse_put_file(Path(args.file).expanduser().resolve())
    for item in args.expect or []:
        if "=" not in item:
            raise HubError("--expect takes ID=HASH (the hash printed by 'show ID')")
        key, value = item.split("=", 1)
        expect[key] = value
    with hub_lock():
        cfg = config()
        cat = Catalog(cfg)
        before = set(structure_errors(cat))
        ids = load_ids()
        rts = roots(cfg)
        touched: list[str] = []
        files_changed: set[tuple[str, str]] = set()
        for raw in entries:
            entry = dict(raw)
            source = is_source(entry)
            slug = require_project(cfg, entry.get("project") or args.project)
            entry["project"] = slug
            eid = entry.get("id", "auto")
            allocated = eid in ("auto", "", None)
            if allocated:
                eid = next_id(ids, cat, cfg["projects"][slug]["prefix"], "L" if source else "R")
                entry["id"] = eid
            existing = cat.get(eid)
            if existing is not None:
                if expect.get(eid) != entry_hash(existing):
                    raise HubError(
                        f"{eid} already exists. To change it, run 'python3 tools/hub.py show {eid}', read the "
                        f"current version, then pass --expect {eid}={entry_hash(existing)} . This protects "
                        "changes made by someone else since you last looked.")
                if existing.get("project") != slug and not args.allow_transfer:
                    raise HubError(f"{eid} belongs to {existing.get('project')}; moving it needs --allow-transfer")
                entry.setdefault("created", existing.get("created", today()))
            elif not allocated:
                if ids["reserved"].get(eid, {}).get("project", slug) != slug:
                    raise HubError(f"{eid} was reserved for project {ids['reserved'][eid]['project']}")
                if eid not in ids["reserved"]:
                    raise HubError(f"{eid} is new but was not reserved. Use \"id\": \"auto\" or "
                                   f"'python3 tools/hub.py reserve --project {slug}' first.")
                ids["reserved"].pop(eid, None)
            if existing is None:
                entry.setdefault("created", today())
            entry["updated"] = today()
            for item in entry.get("evidence", []) or []:
                fill_hash(item, rts)
            review = entry.get("review")
            if isinstance(review, dict) and isinstance(review.get("artifact"), dict):
                fill_hash(review["artifact"], rts)
            if not source:
                for field in ("topics", "hypotheses", "evidence", "depends_on", "supersedes",
                              "subsumed_by", "source_refs"):
                    entry.setdefault(field, [])
                entry.setdefault("novelty", "unassessed")
            store = cat.sources if source else cat.records
            if existing is not None and cat.where.get(eid) != slug:
                files_changed.add((cat.where[eid], "sources" if source else "records"))
            store[eid] = entry
            cat.where[eid] = slug
            files_changed.add((slug, "sources" if source else "records"))
            touched.append(eid)
        after = structure_errors(cat)
        new_errors = [e for e in after if e not in before]
        problems = new_errors + evidence_problems(cat, touched)
        if problems:
            raise HubError("Nothing was written. Fix these first:\n  " + "\n  ".join(problems))
        for slug, kind in sorted(files_changed):
            data = [e for e in (cat.sources if kind == "sources" else cat.records).values()
                    if e.get("project") == slug]
            data.sort(key=lambda e: e["id"])
            path = project_dir(slug) / "catalog" / f"{kind}.json"
            old_hash = sha256_file(path) if path.exists() else None
            backup(path)
            text = dump(data)
            atomic_write(path, text)
            journal("put", file=str(path.relative_to(HUB)), before=old_hash,
                    after=sha256_bytes(text.encode()), ids=touched)
        atomic_write(HUB / ".hub" / "ids.json", dump(ids))
    for eid in touched:
        print(f"Saved {eid}  (hash {entry_hash(cat.get(eid))})")
    print("Run 'python3 tools/hub.py render' to refresh the readable indexes.")


def fill_hash(item: dict, rts: dict) -> None:
    if isinstance(item, dict) and item.get("sha256") in ("auto", "", None) and item.get("root") in rts:
        path = rts[item["root"]] / item.get("path", "")
        if not path.is_file():
            raise HubError(f"Evidence file not found: {item['root']}:{item.get('path')}")
        item["sha256"] = sha256_file(path)


def cmd_show(args) -> None:
    cat = Catalog(config())
    entry = cat.get(args.id)
    if entry is None:
        raise HubError(f"No entry {args.id}. Try: python3 tools/hub.py search \"{args.id}\"")
    rev = cat.incoming()
    corrections = [src for src, rel in rev.get(args.id, []) if rel.startswith("CORRECTS")]
    if corrections:
        print(f"!! {args.id} is corrected or superseded by: {', '.join(corrections)} (read those too)")
    print(dump(entry), end="")
    print(f"hash: {entry_hash(entry)}   (pass --expect {args.id}={entry_hash(entry)} to update it with put)")
    links = rev.get(args.id, [])
    if links:
        print("Referenced by:")
        for src, rel in links:
            other = cat.get(src) or {}
            print(f"  {src} {rel}  [{other.get('status', '')}] {other.get('title', '')}")


def cmd_impact(args) -> None:
    cat = Catalog(config())
    if cat.get(args.id) is None:
        raise HubError(f"No entry {args.id}")
    rev = cat.incoming()
    seen = {args.id}
    queue = [(args.id, 0)]
    rows = []
    while queue:
        node, depth = queue.pop(0)
        neighbours = list(rev.get(node, []))
        rec = cat.records.get(node, {})
        if rec.get("kind") == "correction":
            neighbours += [(t, "is corrected by it") for t in rec.get("supersedes", [])]
        for other, rel in neighbours:
            if other in seen:
                continue
            seen.add(other)
            entry = cat.get(other) or {}
            rows.append(f"{'  ' * depth}- {other} {rel.replace(' it', ' ' + node)}  "
                        f"[{entry.get('status', entry.get('check_state', ''))}] {entry.get('title', entry.get('citation', ''))}")
            queue.append((other, depth + 1))
    if rows:
        print(f"Entries to review if {args.id} changes:")
        print("\n".join(rows))
    else:
        print(f"No recorded entry depends on {args.id}.")
    print("The recorded graph is incomplete: also search for related claims by topic.")


def cmd_check(args) -> None:
    cfg = config()
    cat = Catalog(cfg)
    errors = structure_errors(cat)
    warnings: list[str] = []
    if args.project:
        slug = require_project(cfg, args.project)
        scope = closure(cat, slug)
        label = f"project {slug} and its dependencies"
    else:
        scope = set(cat.records) | set(cat.sources)
        label = "the whole hub"
    errors += evidence_problems(cat, sorted(scope))
    for slug in cfg.get("projects", {}):
        for name in ("AGENTS.md", "START_HERE.md", "TOPICS.md", "CHANGELOG.md"):
            if not (project_dir(slug) / name).exists():
                warnings.append(f"projects/{slug}/{name} is missing")
    for alias, info in cfg.get("archives", {}).items():
        if not Path(info["path"]).is_dir():
            errors.append(f"Archive '{alias}' not found at {info['path']} (moved? update hub.json via add-archive)")
    rev = cat.incoming()
    for rid, rec in cat.records.items():
        if rec.get("status") == "superseded" and not any(r.startswith("CORRECTS") for _, r in rev.get(rid, [])):
            warnings.append(f"{rid}: marked superseded but no correction lists it in 'supersedes'")
    ids = load_ids()
    stale = [k for k, v in ids["reserved"].items() if v.get("date", today()) < (dt.date.today() - dt.timedelta(days=14)).isoformat()]
    if stale:
        warnings.append("Reserved but unused for over 14 days: " + ", ".join(stale))
    warnings += role_sync_warnings()
    for line in errors:
        print("ERROR   " + line)
    for line in warnings:
        print("warning " + line)
    print(f"Checked {label}: {len(errors)} errors, {len(warnings)} warnings. "
          "(Catalog integrity only; this does not check any mathematics.)")
    if errors:
        sys.exit(1)


def md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_project(cat: Catalog, slug: str) -> Path:
    recs = sorted((r for r in cat.records.values() if r.get("project") == slug), key=lambda r: r["id"])
    srcs = sorted((s for s in cat.sources.values() if s.get("project") == slug), key=lambda s: s["id"])
    rev = cat.incoming()
    title = cat.cfg["projects"][slug]["title"]
    lines = [f"# Catalog: {title}", "",
             f"Generated by `python3 tools/hub.py render` on {today()}. Do not edit: the JSON files in "
             "`catalog/` are the source of truth. A label here is a pointer to evidence, not a certificate.", ""]
    groups = [("Verified results", lambda r: r["status"] == "verified"),
              ("Reported or conditional (not independently reviewed)",
               lambda r: r["status"] in ("reported", "conditional") and r["kind"] not in ("question", "conjecture")),
              ("Open questions and conjectures", lambda r: r["status"] == "open" or r["kind"] in ("question", "conjecture")),
              ("Obstructions and failed approaches", lambda r: r["kind"] == "obstruction"),
              ("Corrections", lambda r: r["kind"] == "correction"),
              ("Refuted or superseded", lambda r: r["status"] in ("refuted", "superseded"))]
    placed: set[str] = set()
    for heading, test in groups:
        rows = [r for r in recs if r["id"] not in placed and test(r)]
        if not rows:
            continue
        lines += [f"## {heading}", "", "| ID | Kind | Title | Status | Evidence | Novelty | Notes |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            placed.add(r["id"])
            notes = []
            fixes = [s for s, rel in rev.get(r["id"], []) if rel.startswith("CORRECTS")]
            if fixes:
                notes.append("corrected by " + ", ".join(fixes))
            if r.get("reopening_condition"):
                notes.append("reopen if: " + r["reopening_condition"])
            if r.get("supersedes"):
                notes.append("supersedes " + ", ".join(r["supersedes"]))
            lines.append(f"| {r['id']} | {r['kind']} | {md(r['title'])} | {r['status']} | "
                         f"{r['evidence_level']} | {r['novelty']} | {md('; '.join(notes))} |")
        lines.append("")
    if srcs:
        lines += ["## Literature", "", "| ID | Citation | Locator | Check | Use |", "|---|---|---|---|---|"]
        for s in srcs:
            lines.append(f"| {s['id']} | {md(s['citation'])} | {md(s['locator'])} | {s['check_state']} | {md(s['use'])} |")
        lines.append("")
    if not recs and not srcs:
        lines += ["The catalog is empty. Ask your assistant to import results from your notes "
                  "(see 'Import earlier work' in PROMPTS.md).", ""]
    path = project_dir(slug) / "indexes" / "CATALOG.md"
    atomic_write(path, "\n".join(lines))
    return path


def cmd_render(args) -> None:
    cfg = config()
    cat = Catalog(cfg)
    slugs = [require_project(cfg, args.project)] if args.project else list(cfg.get("projects", {}))
    for slug in slugs:
        print(f"Wrote {render_project(cat, slug).relative_to(HUB)}")
    if not args.project:
        lines = ["# Hub overview", "", f"Generated on {today()} by `python3 tools/hub.py render`.", "",
                 "| Project | Records | Verified | Open | Sources |", "|---|---|---|---|---|"]
        for slug, info in cfg.get("projects", {}).items():
            recs = [r for r in cat.records.values() if r.get("project") == slug]
            lines.append(f"| [{md(info['title'])}](../projects/{slug}/indexes/CATALOG.md) | {len(recs)} | "
                         f"{sum(r.get('status') == 'verified' for r in recs)} | "
                         f"{sum(r.get('status') == 'open' for r in recs)} | "
                         f"{sum(s.get('project') == slug for s in cat.sources.values())} |")
        atomic_write(HUB / "indexes" / "OVERVIEW.md", "\n".join(lines) + "\n")
        print("Wrote indexes/OVERVIEW.md")


def cmd_new_note(args) -> None:
    cfg = config()
    slug = require_project(cfg, args.project)
    topic = re.sub(r"[^a-z0-9]+", "-", args.topic.lower()).strip("-")[:50] or "investigation"
    folder = project_dir(slug) / "investigations" / f"{today()}-{topic}"
    note = folder / "NOTE.md"
    if note.exists():
        raise HubError(f"{note.relative_to(HUB)} already exists; continue that NOTE instead")
    text = (HUB / "templates" / "investigation.md").read_text(encoding="utf-8")
    for key, value in {"TITLE": args.topic, "DATE": today(), "PROJECT": slug,
                       "OWNER": args.owner or cfg.get("owner", "")}.items():
        text = text.replace("{{" + key + "}}", value)
    atomic_write(note, text)
    print(note.relative_to(HUB))


# ---------------------------------------------------------------- search

def text_of(path: Path, allow_cache_write: bool = True) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return pdf_text(path, allow_cache_write)
        if suffix not in TEXT_SUFFIXES or path.stat().st_size > MAX_TEXT_BYTES:
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".ipynb":
            try:
                cells = json.loads(raw).get("cells", [])
                return "\n".join("".join(c.get("source", [])) for c in cells)
            except (json.JSONDecodeError, AttributeError):
                return raw
        return raw
    except OSError:
        return None


def pdf_text(path: Path, allow_cache_write: bool) -> str | None:
    tool = shutil.which("pdftotext")
    key = sha256_bytes(f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}".encode())[:24]
    cache = HUB / ".hub" / "cache" / "pdftext" / f"{key}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    if not tool:
        return None
    try:
        out = subprocess.run([tool, "-q", "-layout", str(path), "-"], capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = out.stdout.decode("utf-8", errors="replace")
    if allow_cache_write:
        try:
            atomic_write(cache, text)
        except OSError:
            pass
    return text


def walk_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".tmp-"))
        current = Path(dirpath)
        for name in sorted(filenames):
            yield current / name


def match_text(text: str, terms: list[str]) -> tuple[int, list[tuple[int, str]]] | None:
    low = text.lower()
    if not all(t in low for t in terms):
        return None
    score = sum(low.count(t) for t in terms)
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        lowline = line.lower()
        found = sum(t in lowline for t in terms)
        if found:
            hits.append((found, number, line.strip()))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return score, [(n, l[:160]) for _, n, l in hits[:3]]


def cmd_search(args) -> None:
    cfg = config()
    cat = Catalog(cfg)
    terms = [t.lower() for t in shlex.split(args.query) if t.strip()]
    if not terms:
        raise HubError("Give some search terms")
    slug = require_project(cfg, args.project) if args.project else None
    rev = cat.incoming()
    entry_ids = closure(cat, slug) if slug else set(cat.records) | set(cat.sources)
    results = []
    for eid in sorted(entry_ids):
        entry = cat.get(eid)
        m = match_text(json.dumps(entry, ensure_ascii=False), terms)
        if m:
            fixes = [s for s, rel in rev.get(eid, []) if rel.startswith("CORRECTS")]
            label = f"[{entry.get('status', entry.get('check_state', ''))}] {entry.get('title', entry.get('citation', ''))}"
            if fixes:
                label += f"  !! corrected by {', '.join(fixes)}"
            if entry.get("project") != slug and slug:
                label += f"  (from project {entry.get('project')})"
            results.append((0, -m[0], eid, label, []))
    base = project_dir(slug) if slug else HUB / "projects"
    counted = {"documents": 0, "archive files": 0, "PDFs without text": 0}
    if base.is_dir():
        for path in walk_files(base):
            rel = path.relative_to(HUB)
            if "catalog" in rel.parts or "indexes" in rel.parts:
                continue
            text = text_of(path, not args.no_cache)
            if text is None:
                continue
            counted["documents"] += 1
            m = match_text(text, terms)
            if m:
                results.append((1, -m[0], str(rel), "", m[1]))
    if args.archives:
        targets = [("library", HUB / "library")]
        targets += [(a, Path(i["path"])) for a, i in cfg.get("archives", {}).items()
                    if not slug or slug in i.get("projects", [])]
        for alias, base in targets:
            if not base.is_dir():
                continue
            for path in walk_files(base):
                text = text_of(path, not args.no_cache)
                if text is None:
                    if path.suffix.lower() == ".pdf":
                        counted["PDFs without text"] += 1
                    continue
                counted["archive files"] += 1
                m = match_text(text, terms)
                if m:
                    results.append((2, -m[0], f"{alias}:{path.relative_to(base)}", "", m[1]))
    results.sort()
    shown = results[: args.limit]
    section_names = {0: "Catalog entries", 1: "Hub documents", 2: "Archives and library"}
    last = None
    for section, _, name, label, hits in shown:
        if section != last:
            print(f"== {section_names[section]}")
            last = section
        print(f"{name}  {label}".rstrip())
        for number, line in hits:
            print(f"    {number}: {line}")
    if not shown:
        print("No matches.")
    if len(results) > len(shown):
        print(f"... {len(results) - len(shown)} more matches (use --limit).")
    scope = f"project {slug} (with its dependencies and corrections)" if slug else "all projects"
    summary = f"Searched {scope}: {len(entry_ids)} catalog entries, {counted['documents']} hub documents"
    if args.archives:
        summary += f", {counted['archive files']} archive/library files"
        if counted["PDFs without text"]:
            summary += (f", {counted['PDFs without text']} PDFs skipped "
                        "(no text layer, or pdftotext is not installed)")
    else:
        summary += "; archives not searched (add --archives)"
    print(summary + ".")
    print(NOVELTY_WARNING)


# ---------------------------------------------------------------- computations

def cmd_package(args) -> None:
    cfg = config()
    slug = require_project(cfg, args.project)
    spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
    dest = (HUB / args.dest).resolve()
    allowed = (project_dir(slug) / "investigations").resolve()
    if allowed not in dest.parents:
        raise HubError(f"--dest must be inside projects/{slug}/investigations/")
    if dest.exists():
        raise HubError(f"{args.dest} already exists; packages are never overwritten")
    for field in ("command", "environment", "domain", "exhaustiveness", "files"):
        if not spec.get(field):
            raise HubError(f"The spec needs '{field}'")
    roles = {f.get("role") for f in spec["files"]}
    if not {"source", "input", "output"} <= roles:
        raise HubError("Keep the exact source code, inputs and outputs (roles source, input, output)")
    manifest = {k: spec.get(k) for k in ("command", "environment", "domain", "exhaustiveness", "seed", "notes")}
    manifest.update(created=now_utc(), files=[],
                    caution="Retained bytes only. This package does not certify the computation or any theorem.")
    staged = []
    for item in spec["files"]:
        src = Path(item["from"]).expanduser().resolve()
        name = item.get("path") or src.name
        if ".." in Path(name).parts or Path(name).is_absolute():
            raise HubError(f"Bad package path {name}")
        raw = src.read_bytes()
        staged.append((name, raw))
        manifest["files"].append({"path": name, "role": item["role"], "sha256": sha256_bytes(raw), "bytes": len(raw)})
    with hub_lock():
        dest.mkdir(parents=True)
        for name, raw in staged:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        atomic_write(dest / "manifest.json", dump(manifest))
        journal("package", dest=str(dest.relative_to(HUB)), files=len(staged))
    print(f"Packaged {len(staged)} files in {dest.relative_to(HUB)}; register manifest.json as evidence.")


# ---------------------------------------------------------------- roles and environment

def parse_role(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise HubError(f"{path} has no frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, m.group(2).strip() + "\n"


def codex_role_text(meta: dict, body: str) -> str:
    if "'''" in body:
        raise HubError("Role bodies may not contain three single quotes")
    name = meta["name"].replace("-", "_")
    description = meta["description"].replace("\\", "\\\\").replace('"', '\\"')
    return (f'# Generated from .claude/agents/{meta["name"]}.md by hub.py sync-roles. Edit that file instead.\n'
            f'name = "{name}"\ndescription = "{description}"\n'
            f"developer_instructions = '''\n{body}'''\n")


def role_sync_warnings() -> list[str]:
    claude_dir, codex_dir = HUB / ".claude" / "agents", HUB / ".codex" / "agents"
    if not claude_dir.is_dir() or not codex_dir.is_dir():
        return []
    out = []
    for path in sorted(claude_dir.glob("*.md")):
        try:
            meta, body = parse_role(path)
            target = codex_dir / (meta["name"].replace("-", "_") + ".toml")
            if not target.exists() or target.read_text(encoding="utf-8") != codex_role_text(meta, body):
                out.append(f"Codex role for {path.name} is out of date: run python3 tools/hub.py sync-roles")
        except HubError as exc:
            out.append(str(exc))
    return out


def cmd_sync_roles(args) -> None:
    claude_dir, codex_dir = HUB / ".claude" / "agents", HUB / ".codex" / "agents"
    codex_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(claude_dir.glob("*.md")):
        meta, body = parse_role(path)
        target = codex_dir / (meta["name"].replace("-", "_") + ".toml")
        atomic_write(target, codex_role_text(meta, body))
        print(f"Wrote {target.relative_to(HUB)}")


def version_of(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20)
        return (out.stdout or out.stderr).decode(errors="replace").strip().splitlines()[0]
    except (subprocess.TimeoutExpired, OSError, IndexError):
        return "installed (version unknown)"


def cmd_doctor(args) -> None:
    ok = sys.version_info >= (3, 9)
    print(f"{'ok  ' if ok else 'FIX '} Python {sys.version.split()[0]} (3.9 or newer needed)")
    for label, cmd, advice in [
        ("Claude Code", ["claude", "--version"], "curl -fsSL https://claude.ai/install.sh | bash"),
        ("Codex", ["codex", "--version"], "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
        ("pdftotext (PDF search)", ["pdftotext", "-v"], "macOS: brew install poppler; Debian/Ubuntu: sudo apt install poppler-utils"),
        ("git (version history)", ["git", "--version"], "macOS: xcode-select --install"),
        ("latexmk (papers)", ["latexmk", "-v"], "install a TeX distribution such as MacTeX or TeX Live"),
    ]:
        found = version_of(cmd)
        print(f"{'ok  ' if found else 'opt '} {label}: {found or 'not found. Install with: ' + advice}")
    try:
        cfg = config()
        print(f"ok   hub.json: {len(cfg.get('projects', {}))} projects, {len(cfg.get('archives', {}))} archives")
    except HubError as exc:
        print(f"FIX  {exc}")
    for line in role_sync_warnings():
        print("FIX  " + line)


def cmd_hash(args) -> None:
    for name in args.paths:
        print(f"{sha256_file(Path(name))}  {name}")


# ---------------------------------------------------------------- CLI

def main(argv=None) -> None:
    global HUB
    parser = argparse.ArgumentParser(prog="hub.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--hub-dir", help="hub folder (default: the folder containing tools/)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("context", help="print what a session should read first")
    p.add_argument("--project")
    p.add_argument("--role", choices=["conductor", "helper"], default="conductor")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("search", help="search catalog, notes and (with --archives) old work")
    p.add_argument("query")
    p.add_argument("--project")
    p.add_argument("--archives", action="store_true", help="also search registered archives and library/")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--no-cache", action="store_true", help="never write the PDF text cache (read-only helpers)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="show one record or source, its hash and what refers to it")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("impact", help="list entries that depend on an entry")
    p.add_argument("id")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("reserve", help="reserve the next record (R) or literature (L) ID")
    p.add_argument("--project")
    p.add_argument("--kind", choices=["R", "L"], default="R")
    p.add_argument("--owner")
    p.add_argument("--purpose")
    p.set_defaults(func=cmd_reserve)

    p = sub.add_parser("put", help="add or update catalog entries from a JSON file (validated)")
    p.add_argument("file")
    p.add_argument("--project")
    p.add_argument("--expect", action="append", metavar="ID=HASH")
    p.add_argument("--allow-transfer", action="store_true")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("check", help="validate the catalog and evidence hashes")
    p.add_argument("--project")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("render", help="rebuild the readable indexes")
    p.add_argument("--project")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("new-note", help="start an investigation NOTE")
    p.add_argument("topic")
    p.add_argument("--project")
    p.add_argument("--owner")
    p.set_defaults(func=cmd_new_note)

    p = sub.add_parser("add-project", help="create a new research project")
    p.add_argument("slug")
    p.add_argument("--title", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--description")
    p.add_argument("--conventions")
    p.set_defaults(func=cmd_add_project)

    p = sub.add_parser("add-archive", help="register a folder of earlier work as a read-only archive")
    p.add_argument("path")
    p.add_argument("--project", action="append", required=True)
    p.add_argument("--alias")
    p.set_defaults(func=cmd_add_archive)

    p = sub.add_parser("package", help="retain the exact code, inputs and outputs of a computation")
    p.add_argument("spec")
    p.add_argument("--project")
    p.add_argument("--dest", required=True)
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("hash", help="print SHA-256 hashes of files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_hash)

    p = sub.add_parser("sync-roles", help="regenerate .codex/agents from .claude/agents")
    p.set_defaults(func=cmd_sync_roles)

    p = sub.add_parser("doctor", help="check the installation")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if args.hub_dir:
        HUB = Path(args.hub_dir).expanduser().resolve()
    try:
        args.func(args)
    except HubError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
