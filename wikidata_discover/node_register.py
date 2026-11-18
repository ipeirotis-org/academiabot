"""Node id register (Anya, week 5): one stable id per unit of a university's hierarchy.

The register for a university lives at gs://academiabot/hierarchy/<QID>/ids.json, outside
the run folders, because ids must survive across runs. It maps each node id to what was last
seen for that unit: QID, names (every name and alias ever seen, so a renamed unit still
matches its old name), parents, normalized website, and source URLs.

Ids are never derived from fields that can change. The university itself is always "root";
every other unit gets "n00001", "n00002", ... in the order it is first seen.

A unit of this run gets an existing id by these rules, in this order (AGENTS.md, "Hierarchy
file"). An entry is used by at most one unit per run.

1. Same QID.
2. Same name or alias under the same parent, when that name is unique on both sides (one
   register entry and one unit of this run with that name under that parent). When it is
   not unique, the source URL or website must also match, and exactly one entry must fit.
3. A unit without a QID that was renamed: the same source URL or website under the same
   parent, and exactly one entry fits.
4. A unit without a QID that moved to another parent: same name or alias AND same website
   or source URL, and exactly one entry in the whole register fits.
5. A name-only match under another parent is not a move (two schools can each have a
   "Department of Economics"): the unit gets a new id, and the possible old ids are flagged
   for a person.

Whenever more than one entry fits, the unit gets a new id and a flag. Ids are never guessed.

Merges (week 6): an unused entry with the same QID as a unit of this run, or the unit's own
earlier entry when the unit now took another id by QID, is recorded in "merged" as old id ->
surviving id. A merged entry is never matched again.

Storage: load() returns the register and its bucket generation; save() writes with an
if_generation_match precondition, so two runs never overwrite each other silently. On a
conflict the caller reloads and assigns again. When the bucket cannot be read or written,
RegisterUnavailable is raised and the run assigns no ids and writes no hierarchy file.
"""
import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT_ID = "root"
REGISTER_NAME = "hierarchy/{qid}/ids.json"
CROSS_REGISTER_NAME = "hierarchy/cross_register.json"


class RegisterUnavailable(RuntimeError):
    """The bucket register could not be read or written. No ids may be assigned."""


class RegisterConflict(RuntimeError):
    """Another run wrote the register after this run loaded it. Reload and assign again."""


# ── normalizing (pure) ───────────────────────────────────────────────────

def norm_name(name: Optional[str]) -> str:
    """Lowercase, "&" as "and", every run of other characters as one space. Pure."""
    s = (name or "").lower().replace("&", " and ")
    return " ".join(re.findall(r"\w+", s))


def norm_url(url: Optional[str]) -> str:
    """Host without "www." plus path without a trailing slash; no scheme, query, or
    fragment. "" for anything that is not an http(s) URL. Pure."""
    if not url or not re.match(r"https?://", url.strip(), re.I):
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/(index\.html?)?$", "", parts.path or "")
    return host + path.lower()


def empty_register(university_qid: str) -> dict:
    return {"university_qid": university_qid, "next_seq": 1, "entries": {}, "merged": {}}


def _unit_names(u: dict) -> set:
    return {n for n in [norm_name(u.get("name"))] + [norm_name(a) for a in u.get("aliases") or []] if n}


def _unit_urls(u: dict, shared: frozenset = frozenset()) -> set:
    return {x for x in (norm_url(u.get("website")), norm_url(u.get("source_url"))) if x and x not in shared}


def _entry_urls(e: dict, shared: frozenset = frozenset()) -> set:
    return {x for x in [e.get("website") or ""] + list(e.get("source_urls") or []) if x and x not in shared}


def shared_urls(units: List[dict]) -> frozenset:
    """URLs that more than one unit of the run gives (a list page that names many units,
    such as allnyu.nyu.edu for every NYU school). They say nothing about which unit is
    which, so the URL rules ignore them. Pure."""
    seen: Dict[str, set] = {}
    for u in units:
        for x in _unit_urls(u):
            seen.setdefault(x, set()).add(u["key"])
    return frozenset(x for x, keys in seen.items() if len(keys) > 1)


# ── assignment (pure) ────────────────────────────────────────────────────

def assign_ids(register: dict, units: List[dict], run_id: str) -> Tuple[Dict[str, str], Dict[str, List[str]], dict]:
    """Give every unit of this run a node id. Pure: the register passed in is not changed.

    units: in parent-first order, each {"key", "qid", "name", "aliases", "parent_keys",
    "website", "source_url", "is_root"}. parent_keys are keys of earlier units, or a
    reference outside this tree ("Q2034790/root"), which is kept as it is.

    Returns (ids: key -> node id, flags: key -> [text for a person], new register)."""
    reg = copy.deepcopy(register)
    entries: Dict[str, dict] = reg["entries"]
    ids: Dict[str, str] = {}
    flags: Dict[str, List[str]] = {}
    used: set = set()
    how: Dict[str, str] = {}
    shared = shared_urls(units)

    def parent_ids(u: dict) -> List[str]:
        return [ids.get(k, k) for k in u.get("parent_keys") or []]

    def primary_parent(u: dict) -> Optional[str]:
        p = parent_ids(u)
        return p[0] if p else None

    def free(eid: str, u: dict) -> bool:
        e = entries[eid]
        # An entry that already has a different QID is another unit; a merged entry is retired.
        return (eid not in used and eid not in reg.get("merged", {})
                and not (u.get("qid") and e.get("qid") and e["qid"] != u["qid"]))

    def take(u: dict, eid: str, rule: str) -> None:
        ids[u["key"]] = eid
        used.add(eid)
        how[u["key"]] = rule

    # Pass 1: the root and every unit whose QID the register knows. A joint unit can be in
    # the tree twice, under two parents, with one QID (the Furman Center: Wagner and Law):
    # then the parent decides. Entries with the same QID AND the same parent are one unit:
    # the oldest id survives and the others are recorded as merged into it.
    merged = reg.setdefault("merged", {})
    for u in units:
        if u.get("is_root"):
            take(u, ROOT_ID, "root")
            continue
        if u.get("qid"):
            fits = [eid for eid, e in entries.items()
                    if e.get("qid") == u["qid"] and eid not in used and eid not in merged]
            if len(fits) > 1:
                p = parent_ids(u)[0] if parent_ids(u) else None
                same_parent = [eid for eid in fits if p in (entries[eid].get("parents") or [])]
                if same_parent:
                    fits = sorted(same_parent)
                    for old in fits[1:]:
                        merged[old] = fits[0]
                        flags.setdefault(u["key"], []).append(
                            f"register: {old} merged into {fits[0]} (same QID and same parent)")
                    fits = fits[:1]
            if len(fits) == 1:
                take(u, fits[0], "qid")
            elif len(fits) > 1:
                flags.setdefault(u["key"], []).append(
                    f"register: several entries have QID {u['qid']}: {', '.join(sorted(fits))}")

    # How many units of this run carry each (parent, name) pair: the "unique on both sides" rule.
    # Parents with a QID-matched id are known here; the others resolve in order below.
    def run_name_count(parent: Optional[str], name: str) -> int:
        return sum(1 for v in units if not v.get("is_root") and primary_parent(v) == parent and name in _unit_names(v))

    # Pass 2: every other unit, parents first.
    for u in units:
        if u["key"] in ids or any(f.startswith("register: several entries have QID") for f in flags.get(u["key"], [])):
            continue
        parent, names, urls = primary_parent(u), _unit_names(u), _unit_urls(u, shared)
        under = [eid for eid in entries if free(eid, u) and parent in (entries[eid].get("parents") or [])]

        # Rule 2: same name or alias under the same parent.
        by_name = [eid for eid in under if names & set(entries[eid].get("names") or [])]
        if by_name:
            unique_here = all(run_name_count(parent, n) == 1 for n in names & {x for e in by_name for x in entries[e]["names"]})
            if len(by_name) == 1 and unique_here:
                take(u, by_name[0], "name under the same parent")
                continue
            with_url = [eid for eid in by_name if urls & _entry_urls(entries[eid], shared)]
            if len(with_url) == 1:
                take(u, with_url[0], "name and URL under the same parent")
                continue
            flags.setdefault(u["key"], []).append(
                "register: the name fits several entries under this parent and the URL does not "
                f"decide: {', '.join(sorted(by_name))}; new id given")
            _new(reg, u, ids, used, how)
            continue
        if not u.get("qid") and urls:
            # Rule 3: renamed, same URL under the same parent.
            by_url = [eid for eid in under if urls & _entry_urls(entries[eid], shared)]
            if len(by_url) == 1:
                take(u, by_url[0], "URL under the same parent (renamed)")
                continue
            if len(by_url) > 1:
                flags.setdefault(u["key"], []).append(
                    f"register: the URL fits several entries under this parent: {', '.join(sorted(by_url))}; new id given")
                _new(reg, u, ids, used, how)
                continue
            # Rule 4: moved, name and URL anywhere in the register.
            moved = [eid for eid in entries if free(eid, u) and names & set(entries[eid].get("names") or [])
                     and urls & _entry_urls(entries[eid], shared)]
            if len(moved) == 1:
                take(u, moved[0], "name and URL under another parent (moved)")
                continue
            if len(moved) > 1:
                flags.setdefault(u["key"], []).append(
                    f"register: name and URL fit several entries: {', '.join(sorted(moved))}; new id given")
                _new(reg, u, ids, used, how)
                continue
        # Rule 5: a name-only match elsewhere is not a move.
        elsewhere = sorted(eid for eid in entries if free(eid, u) and names & set(entries[eid].get("names") or []))
        if elsewhere:
            flags.setdefault(u["key"], []).append(
                f"register: same name as {', '.join(elsewhere)} under another parent, without a matching URL; "
                "new id given. A person decides whether it is the same unit")
        _new(reg, u, ids, used, how)

    for u in units:
        if u["key"] not in ids:          # a unit whose QID fits several entries
            _new(reg, u, ids, used, how)

    # Merges (week 6). An entry this run did not use is the same unit as a unit of this run
    # when it has the same QID, or when the unit took its id by QID and the entry has the
    # unit's name under the same parent and no QID (the unit's own id from before it was
    # aligned). The old id then points to the surviving id, so checks and reviews still resolve.
    merged = reg.setdefault("merged", {})
    for u in units:
        if u.get("is_root"):
            continue
        uid, names, parent = ids[u["key"]], _unit_names(u), primary_parent(u)
        for eid, e in entries.items():
            if eid in used or eid in merged or eid == uid:
                continue
            # With two units of this run on one QID (a joint unit), only the same parent decides.
            joint = u.get("qid") and sum(1 for v in units if v.get("qid") == u["qid"]) > 1
            same_qid = (u.get("qid") and e.get("qid") == u["qid"]
                        and (not joint or parent in (e.get("parents") or [])))
            own_old = (how.get(u["key"]) == "qid" and not e.get("qid") and parent in (e.get("parents") or [])
                       and names & set(e.get("names") or []))
            if same_qid or own_old:
                merged[eid] = uid
                flags.setdefault(u["key"], []).append(f"register: {eid} merged into {uid} (same unit)")
    for u in units:
        _update_entry(reg, ids[u["key"]], u, parent_ids(u), run_id, how.get(u["key"], "new"))
    return ids, flags, reg


def _new(reg: dict, u: dict, ids: dict, used: set, how: dict) -> None:
    eid = f"n{reg['next_seq']:05d}"
    reg["next_seq"] += 1
    reg["entries"][eid] = {"first_seen_run": None}
    ids[u["key"]] = eid
    used.add(eid)
    how[u["key"]] = "new"


def _update_entry(reg: dict, eid: str, u: dict, parents: List[str], run_id: str, rule: str) -> None:
    e = reg["entries"].setdefault(eid, {})
    if u.get("qid"):
        e["qid"] = u["qid"]
    else:
        e.setdefault("qid", None)
    e["names"] = sorted(set(e.get("names") or []) | _unit_names(u))
    e["name"] = u.get("name")
    e["parents"] = parents
    w = norm_url(u.get("website"))
    if w:
        e["website"] = w
    else:
        e.setdefault("website", "")
    s = norm_url(u.get("source_url"))
    e["source_urls"] = sorted(set(e.get("source_urls") or []) | ({s} if s else set()))
    if not e.get("first_seen_run"):
        e["first_seen_run"] = run_id
    e["last_seen_run"] = run_id
    e["last_match_rule"] = rule


# ── storage ──────────────────────────────────────────────────────────────

class RegisterStore:
    """The register in the bucket, with a generation-match precondition on every write,
    and a read-only local copy under results/hierarchy/<QID>/ids.json."""

    def __init__(self, bucket, local_dir: Path):
        self.bucket = bucket
        self.local_dir = Path(local_dir)

    def _blob(self, name: str):
        return self.bucket.blob(name)

    def load_json(self, name: str, default: dict) -> Tuple[dict, int]:
        """(content, generation); generation 0 when the object does not exist yet."""
        try:
            blob = self._blob(name)
            if not blob.exists():
                return copy.deepcopy(default), 0
            blob.reload()
            return json.loads(blob.download_as_bytes(if_generation_match=blob.generation)), int(blob.generation)
        except Exception as e:  # noqa: BLE001 - any read problem means no ids may be assigned
            raise RegisterUnavailable(f"cannot read gs://{getattr(self.bucket, 'name', '?')}/{name}: "
                                      f"{type(e).__name__}: {str(e)[:200]}") from e

    def load(self, university_qid: str) -> Tuple[dict, int]:
        return self.load_json(REGISTER_NAME.format(qid=university_qid), empty_register(university_qid))

    def save(self, register: dict, generation: int) -> int:
        """Write the register only if the bucket still holds `generation` (0: must not exist).
        Returns the new generation. Raises RegisterConflict or RegisterUnavailable."""
        name = REGISTER_NAME.format(qid=register["university_qid"])
        text = json.dumps(register, indent=2, sort_keys=True, ensure_ascii=False)
        try:
            blob = self._blob(name)
            blob.upload_from_string(text, content_type="application/json", if_generation_match=generation)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PreconditionFailed" or getattr(e, "code", None) == 412:
                raise RegisterConflict(f"{name} changed since generation {generation}") from e
            raise RegisterUnavailable(f"cannot write {name}: {type(e).__name__}: {str(e)[:200]}") from e
        local = self.local_dir / register["university_qid"] / "ids.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text)
        return int(getattr(blob, "generation", None) or generation + 1)


MAX_CONFLICT_RETRIES = 5


def assign_and_save(store: RegisterStore, university_qid: str, units: List[dict], run_id: str):
    """Load, assign, save; on a conflict, reload and assign again (up to 5 times).
    Returns (ids, flags, register, loaded_register, loaded_generation, saved_generation)."""
    for _ in range(MAX_CONFLICT_RETRIES):
        loaded, gen = store.load(university_qid)
        ids, flags, reg = assign_ids(loaded, units, run_id)
        try:
            new_gen = store.save(reg, gen)
        except RegisterConflict:
            continue
        return ids, flags, reg, loaded, gen, new_gen
    raise RegisterUnavailable(f"register for {university_qid}: {MAX_CONFLICT_RETRIES} write conflicts in a row")
