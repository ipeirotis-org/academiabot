import hashlib
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI
from wikidata_discover.config import OPENAI_API_KEY, LLM_MODEL
from rich.console import Console

client = OpenAI(api_key=OPENAI_API_KEY)
console = Console()
logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / "results" / "cache"


def _cache_key(univ_label: str, model: str) -> str:
    raw = f"{univ_label}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_cache(key: str) -> Optional[List[Dict[str, Any]]]:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _save_cache(key: str, units: List[Dict[str, Any]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(units, indent=2))

# ─────────────────────────  LLM PROMPTS  ─────────────────────────
SYSTEM_EXTRACT = (
    "You are an education data analyst. Given the name of a university and (optionally) its website URL, return a JSON "
    "key `units` whose value is an *array* of objects, each describing a *top-level* "
    "academic or administrative unit (school, college, faculty, division, or campus). "
    "Each object *must* include: name, unit_type, city, state, website. Use null if a "
    "value is unknown. Do not list departments or research centers."
    "Provide also a URL as a reference so that someone can validate the information. The key for the reference URL should be 'reference'."
    "You should double check that reference URL exists and contains the supporting information for the existence of the units."
)

MATCH_TEMPLATE = (
    "You are assisting with entity alignment to Wikidata. Below is the name of a "
    "candidate academic unit *CANDIDATE* from UNIVERSITY, followed by a numbered "
    "list of existing descendant units from Wikidata, each labelled `[n] QID -- LABEL`.\n\n"
    "If the candidate is equivalent to a listed unit **that already has** a parent-"
    "link to UNIVERSITY, reply with that QID.\n"
    "If it matches a listed unit but that unit is **missing** the parent link, "
    "reply `ORPHAN:QID`.\n"
    "If none match, reply `NONE`.\n"
    "*Return that single token only -- no explanation.*"
)

UNIVERSITY_UNITS_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "unit_type": {"type": "string"},
                    "city":      {"type": "string"},
                    "state":     {"type": "string"},
                    "website":   {"type": ["string", "null"]}
                },
                "required": ["name", "unit_type", "city", "state", "website"],
                "additionalProperties": False
            }
        },
        "reference": {"type": "string"}
    },
    "required": ["units", "reference"],
    "additionalProperties": False
}

_EXTRACT_MAX_RETRIES = 2


class LLMHelper:
    @staticmethod
    def extract_divisions(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract top-level academic/administrative units for a university.

        Results are cached in results/cache/ keyed by (univ_label, LLM_MODEL)
        to avoid redundant API calls during iteration.
        """
        cache_key = _cache_key(univ_label, LLM_MODEL)
        cached = _load_cache(cache_key)
        if cached is not None:
            logger.info("extract_divisions: cache hit for %s", univ_label)
            return cached

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            resp = client.responses.create(
                model=LLM_MODEL,
                input=[
                    {"role": "system", "content": SYSTEM_EXTRACT},
                    {"role": "user", "content": f"{univ_label} -- {website}"}
                ],
                tools=[{"type": "web_search_preview"}],
                reasoning={"effort": "high"},
                text={"format": {"type": "json_schema", "name": "university_units", "schema": UNIVERSITY_UNITS_SCHEMA}},
                store=False
            )

            raw_text = resp.output_text if resp.output else None
            if not raw_text:
                logger.warning(
                    "extract_divisions attempt %d/%d: empty response for %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label,
                )
                continue

            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                logger.error(
                    "extract_divisions attempt %d/%d: JSON parse error for %s: %s\nRaw response: %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label, exc, raw_text[:500],
                )
                continue

            units = payload.get("units")
            if not isinstance(units, list):
                logger.error(
                    "extract_divisions attempt %d/%d: 'units' key missing or not a list for %s\nParsed payload: %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label, json.dumps(payload)[:500],
                )
                continue

            # Normalize entries: wrap bare strings into dicts
            result = [{"name": itm} if isinstance(itm, str) else itm for itm in units]
            _save_cache(cache_key, result)
            logger.info("extract_divisions: cached %d units for %s", len(result), univ_label)
            return result

        console.print(f"[red]Failed to extract divisions for {univ_label} after {_EXTRACT_MAX_RETRIES} attempts.[/red]")
        return []

    @staticmethod
    def choose_match(candidate: str, univ_label: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Return (qid,label) if GPT says the candidate matches one of the children, else None."""

        # Compose numbered list for prompt
        listing_lines = [
            f"[{i+1}] {qid} -- {label}" for i, (qid, label) in enumerate(children)
        ]
        listing = "\n".join(listing_lines)

        prompt = (
            MATCH_TEMPLATE.replace("CANDIDATE", candidate).replace(
                "UNIVERSITY", univ_label
            )
            + "\n\nExisting units:\n"
            + listing
        )

        try:
            resp = client.responses.create(
                model=LLM_MODEL,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                max_output_tokens=16,
            )
        except Exception:
            logger.exception("choose_match LLM call failed for candidate '%s'", candidate)
            return None

        answer = (resp.output_text or "").strip()

        if not answer:
            return None

        if answer.upper() == "NONE":
            return None
        # look up answer among children
        answer = answer.split()[0]  # just in case extra text
        for qid, label in children:
            if qid == answer:
                return qid, label
        return None
