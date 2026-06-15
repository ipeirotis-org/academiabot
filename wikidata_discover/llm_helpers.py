import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from rich.console import Console
import hashlib
from pathlib import Path

from wikidata_discover.config import (
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
    LLM_MODEL, ANTHROPIC_MODEL, GEMINI_MODEL,
    require_key,
)

console = Console()
logger = logging.getLogger(__name__)

# ─────────────────────────  LLM PROMPTS  ─────────────────────────
# Phase 2: the extraction prompt is parameterized by hierarchy level so the
# same providers can extract schools (level 1), departments (level 2), or
# programs/labs/centers (level 3+) depending on which entity we are expanding.
SYSTEM_EXTRACT_TEMPLATE = (
    "You are an education data analyst. Given the name of an academic entity and "
    "(optionally) its website URL, return a JSON key `units` whose value is an "
    "array of objects, each describing a direct sub-unit of that entity. "
    "Scope: {scope}. "
    "Each object must include: name, unit_type, city, state, website. Use null if "
    "a value is unknown. The unit_type should be one of school, college, faculty, "
    "division, campus, department, program, lab, center, institute, or unit. "
    "Only list direct children of the entity, not every descendant. "
    "For cross-listed or joint units, set is_joint to true and list the other "
    "direct parent unit names in parent_names. If there are no additional "
    "parents, set is_joint to false and parent_names to an empty array. "
    "Set evidence to a short source-backed phrase explaining the parentage, or "
    "null if unknown. "
    "Provide also a URL as a reference so that someone can validate the "
    "information. The key for the reference URL should be 'reference'. "
    "You should double check that reference URL exists and contains the "
    "supporting information for the existence of the units."
)

SYSTEM_EXTRACT = SYSTEM_EXTRACT_TEMPLATE.format(
    scope=(
        "top-level academic or administrative units under a university, such as "
        "schools, colleges, faculties, divisions, or campuses. Do not list "
        "departments or research centers."
    )
)


def _extract_system_prompt(level: int = 1) -> str:
    """Return the extraction system prompt scoped to the given hierarchy level."""
    if level <= 1:
        return SYSTEM_EXTRACT
    if level == 2:
        scope = (
            "departments and similar direct academic sub-units under a school, "
            "college, faculty, division, or campus. Do not list programs, labs, "
            "or centers unless they are direct peers of departments."
        )
    else:
        scope = (
            "direct programs, labs, centers, institutes, and other direct "
            "sub-units under a department or academic unit."
        )
    return SYSTEM_EXTRACT_TEMPLATE.format(scope=scope)


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

JUDGE_PROMPT_TEMPLATE = (
    "You are evaluating academic units for an academic entity. Given the name of UNIVERSITY and a union of sub-unit names "
    "proposed by multiple automated extraction systems, filter to only those that are real, direct sub-units of UNIVERSITY.\n\n"
    "SCOPE_NOTE"
    "Proposed units:\nUNITS_LIST\n\n"
    "Return a JSON object with a 'keep' key containing an array of unit names you confirm as real direct sub-units. "
    "Do not invent or add units not in the list above."
)


def _judge_scope_note(level: int = 1) -> str:
    """Return a level-specific scope clause for the judge prompt."""
    if level <= 1:
        scope = (
            "Keep only genuine top-level academic units such as schools, colleges, "
            "faculties, campuses, or major divisions. Remove departments within a "
            "school, research centers, labs, and programs."
        )
    elif level == 2:
        scope = (
            "Keep only genuine direct departments or department-like academic units "
            "under the given parent. Remove university-wide schools, standalone "
            "centers, labs, and degree programs unless they are direct department peers."
        )
    else:
        scope = (
            "Keep only genuine direct programs, labs, centers, institutes, and other "
            "named sub-units under the given parent. Remove unrelated units and "
            "broader parents."
        )
    return scope + "\n\n"


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
                    "website":   {"type": ["string", "null"]},
                    "is_joint":  {"type": "boolean"},
                    "parent_names": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "evidence":  {"type": ["string", "null"]}
                },
                "required": [
                    "name",
                    "unit_type",
                    "city",
                    "state",
                    "website",
                    "is_joint",
                    "parent_names",
                    "evidence"
                ],
                "additionalProperties": False
            }
        },
        "reference": {"type": "string"}
    },
    "required": ["units", "reference"],
    "additionalProperties": False
}

JUDGE_KEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["keep"],
    "additionalProperties": False
}

_EXTRACT_MAX_RETRIES = 2
_CACHE_DIR = Path(__file__).parent / "results" / "cache"

# ─────────────────────────  LAZY CLIENTS  ─────────────────────────

_openai_client = None
_anthropic_client = None
_gemini_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=require_key("OPENAI_API_KEY", OPENAI_API_KEY))
    return _openai_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=require_key("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
        )
    return _anthropic_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=require_key("GOOGLE_API_KEY", GOOGLE_API_KEY))
    return _gemini_client

# ─────────────────────────  NAME MATCHING  ─────────────────────────


def _names_match(a: str, b: str) -> bool:
    """Lightweight fuzzy name match for deduplicating ensemble outputs."""
    from rapidfuzz import fuzz
    na = re.sub(r"[^a-z0-9 ]", "", a.lower().strip())
    nb = re.sub(r"[^a-z0-9 ]", "", b.lower().strip())
    return na == nb or fuzz.token_sort_ratio(na, nb) >= 88


def _cache_key(
    univ_label: str,
    provider: str,
    model: str,
    level: int = 1,
    parent_context: str = "",
) -> str:
    """Cache key includes provider and hierarchy level to avoid collisions."""
    return hashlib.sha256(
        f"{provider}|{univ_label}|{model}|level:{level}|parent:{parent_context}".encode()
    ).hexdigest()


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
    (_CACHE_DIR / f"{key}.json").write_text(json.dumps(units, indent=2))


def _parse_json_text(text: str) -> Any:
    """Parse JSON text, raising on invalid JSON or empty input."""
    if not text:
        raise ValueError("Empty response text")
    return json.loads(text)


def _normalize_units(payload: Any) -> List[Dict[str, Any]]:
    """Extract and normalize units from provider response payload.

    Expects payload to be a dict with 'units' key containing a list.
    Returns normalized list or raises ValueError if structure is invalid.
    Phase 2 fields (is_joint, parent_names, evidence) are defaulted when absent
    so downstream recursive discovery can rely on them.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Expected dict payload, got {type(payload).__name__}")

    units = payload.get("units")
    if not isinstance(units, list):
        raise ValueError(f"Expected 'units' to be a list, got {type(units).__name__ if units else 'None'}")

    # Normalize entries: wrap bare strings into dicts, validate all items are dicts
    result = []
    for itm in units:
        if isinstance(itm, str):
            unit = {"name": itm}
        elif isinstance(itm, dict):
            unit = itm
        else:
            raise ValueError(f"Invalid unit entry: expected string or dict, got {type(itm).__name__}: {itm}")
        unit.setdefault("is_joint", False)
        unit.setdefault("parent_names", [])
        unit.setdefault("evidence", None)
        result.append(unit)
    return result


class LLMHelper:
    """Multi-provider LLM extraction and matching helper."""

    @staticmethod
    def extract_divisions(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract top-level academic/administrative units for a university.

        Deprecated: use extract_divisions_best_available() for multi-provider support.
        Falls back to OpenAI-only extraction.
        """
        try:
            return LLMHelper.extract_divisions_openai(univ_label, website)
        except Exception as e:
            logger.error("extract_divisions (OpenAI fallback) failed: %s", e)
            return []

    @staticmethod
    def extract_divisions_openai(
        univ_label: str,
        website: str,
        level: int = 1,
        parent_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Extract divisions using OpenAI API."""
        model = LLM_MODEL
        key = _cache_key(univ_label, "openai", model, level, parent_context)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_openai: cache hit for %s", univ_label)
            return cached

        client = _get_openai_client()
        system_prompt = _extract_system_prompt(level)

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            try:
                resp = client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"Entity: {univ_label}\n"
                                f"Parent context: {parent_context or 'none'}\n"
                                f"Website: {website or 'unknown'}"
                            ),
                        },
                    ],
                    tools=[{"type": "web_search_preview"}],
                    text={"format": {"type": "json_schema", "name": "university_units", "schema": UNIVERSITY_UNITS_SCHEMA}},
                    store=False
                )

                raw_text = resp.output_text if resp.output else None
                if not raw_text:
                    logger.warning(
                        "extract_divisions_openai attempt %d/%d: empty response for %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label,
                    )
                    continue

                try:
                    payload = _parse_json_text(raw_text)
                except json.JSONDecodeError as exc:
                    logger.error(
                        "extract_divisions_openai attempt %d/%d: JSON parse error for %s: %s\nRaw response: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc, raw_text[:500],
                    )
                    continue

                try:
                    result = _normalize_units(payload)
                except ValueError as exc:
                    logger.error(
                        "extract_divisions_openai attempt %d/%d: payload normalization error for %s: %s\nParsed payload: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc, json.dumps(payload)[:500],
                    )
                    continue

                _save_cache(key, result)
                return result

            except Exception as e:
                logger.error(
                    "extract_divisions_openai attempt %d/%d: API error for %s: %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label, e
                )
                continue

        logger.error("extract_divisions_openai failed for %s after %d attempts", univ_label, _EXTRACT_MAX_RETRIES)
        return []

    @staticmethod
    def extract_divisions_anthropic(
        univ_label: str,
        website: str,
        level: int = 1,
        parent_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Extract divisions using Anthropic Claude API."""
        model = ANTHROPIC_MODEL
        key = _cache_key(univ_label, "anthropic", model, level, parent_context)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_anthropic: cache hit for %s", univ_label)
            return cached

        client = _get_anthropic_client()
        system_prompt = _extract_system_prompt(level)

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Entity: {univ_label}\n"
                                f"Parent context: {parent_context or 'none'}\n"
                                f"Website: {website or 'unknown'}\n\n"
                                "Search the web for this entity's direct sub-units, "
                                "then respond with ONLY a JSON object in this exact format with no other text:\n"
                                '{"units": [{"name": "...", "unit_type": "school", "city": "...", "state": "...", "website": "...", "is_joint": false, "parent_names": [], "evidence": null}], "reference": "url"}'
                            )
                        }
                    ]
                )

                raw_text = resp.content[0].text if resp.content else None
                if not raw_text:
                    logger.warning(
                        "extract_divisions_anthropic attempt %d/%d: empty response for %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label,
                    )
                    continue

                # Try to extract JSON from response (may be wrapped in markdown)
                match = re.search(r'\{[\s\S]*\}', raw_text)
                json_text = match.group(0) if match else raw_text

                try:
                    payload = _parse_json_text(json_text)
                except json.JSONDecodeError as exc:
                    logger.error(
                        "extract_divisions_anthropic attempt %d/%d: JSON parse error for %s: %s\nRaw response: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc, raw_text[:500],
                    )
                    continue

                try:
                    result = _normalize_units(payload)
                except ValueError as exc:
                    logger.error(
                        "extract_divisions_anthropic attempt %d/%d: payload normalization error for %s: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc
                    )
                    continue

                _save_cache(key, result)
                return result

            except Exception as e:
                logger.error(
                    "extract_divisions_anthropic attempt %d/%d: API error for %s: %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label, e
                )
                continue

        logger.error("extract_divisions_anthropic failed for %s after %d attempts", univ_label, _EXTRACT_MAX_RETRIES)
        return []

    @staticmethod
    def extract_divisions_gemini(
        univ_label: str,
        website: str,
        level: int = 1,
        parent_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Extract divisions using Google Gemini API."""
        model = GEMINI_MODEL
        key = _cache_key(univ_label, "gemini", model, level, parent_context)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_gemini: cache hit for %s", univ_label)
            return cached

        client = _get_gemini_client()
        system_prompt = _extract_system_prompt(level)

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            try:
                from google.genai import types as genai_types

                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        genai_types.Content(
                            parts=[
                                genai_types.Part.from_text(
                                    f"System: {system_prompt}\n\n"
                                    f"Entity: {univ_label}\n"
                                    f"Parent context: {parent_context or 'none'}\n"
                                    f"Website: {website or 'unknown'}"
                                )
                            ]
                        )
                    ],
                    generation_config=genai_types.GenerationConfig(
                        temperature=0.7,
                        max_output_tokens=2048,
                    ),
                )

                raw_text = resp.text if resp.text else None
                if not raw_text:
                    logger.warning(
                        "extract_divisions_gemini attempt %d/%d: empty response for %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label,
                    )
                    continue

                # Try to extract JSON from response (may be wrapped in markdown)
                match = re.search(r'\{[\s\S]*\}', raw_text)
                json_text = match.group(0) if match else raw_text

                try:
                    payload = _parse_json_text(json_text)
                except json.JSONDecodeError as exc:
                    logger.error(
                        "extract_divisions_gemini attempt %d/%d: JSON parse error for %s: %s\nRaw response: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc, raw_text[:500],
                    )
                    continue

                try:
                    result = _normalize_units(payload)
                except ValueError as exc:
                    logger.error(
                        "extract_divisions_gemini attempt %d/%d: payload normalization error for %s: %s",
                        attempt, _EXTRACT_MAX_RETRIES, univ_label, exc
                    )
                    continue

                _save_cache(key, result)
                return result

            except Exception as e:
                logger.error(
                    "extract_divisions_gemini attempt %d/%d: API error for %s: %s",
                    attempt, _EXTRACT_MAX_RETRIES, univ_label, e
                )
                continue

        logger.error("extract_divisions_gemini failed for %s after %d attempts", univ_label, _EXTRACT_MAX_RETRIES)
        return []

    @staticmethod
    def extract_divisions_best_available(
        univ_label: str,
        website: str,
        level: int = 1,
        parent_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Extract divisions using the best available provider.

        Tries providers in order: OpenAI, Anthropic, Gemini.
        Falls back to next provider if current one fails or is not configured.
        Raises ValueError if no providers are available.

        level and parent_context control which hierarchy level is extracted
        (1=schools, 2=departments, 3+=programs/labs/centers).
        """
        providers = [
            ("openai", LLMHelper.extract_divisions_openai),
            ("anthropic", LLMHelper.extract_divisions_anthropic),
            ("gemini", LLMHelper.extract_divisions_gemini),
        ]

        for provider_name, extractor in providers:
            try:
                logger.debug("Trying %s for extraction...", provider_name)
                result = extractor(univ_label, website, level, parent_context)
                if result:  # Successfully extracted non-empty list
                    logger.info("extract_divisions_best_available: %s returned %d units", provider_name, len(result))
                    return result
                else:
                    logger.debug("extract_divisions_best_available: %s returned empty list", provider_name)
            except ValueError as e:
                # Provider not configured (missing key)
                logger.debug("extract_divisions_best_available: %s not available (%s)", provider_name, e)
                continue
            except Exception as e:
                logger.warning("extract_divisions_best_available: %s raised error (%s), trying next", provider_name, e)
                continue

        logger.error("extract_divisions_best_available: all providers failed or unavailable for %s", univ_label)
        raise ValueError(
            f"No LLM providers available for extraction. "
            f"Please configure at least one of: OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY. "
            f"Entity: {univ_label}"
        )

    @staticmethod
    def extract_divisions_ensemble(
        univ_label: str,
        website: str,
        level: int = 1,
        parent_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Extract divisions using ensemble: generate from OpenAI + Anthropic, judge with Gemini.

        Returns union of kept names from judge, or empty list if any step fails.
        """
        try:
            openai_units = LLMHelper.extract_divisions_openai(univ_label, website, level, parent_context)
            openai_names = [u.get("name") or u.get("unit") for u in openai_units if u.get("name") or u.get("unit")]
        except Exception as e:
            logger.error("ensemble: OpenAI extraction failed: %s", e)
            openai_names = []

        try:
            anthropic_units = LLMHelper.extract_divisions_anthropic(univ_label, website, level, parent_context)
            anthropic_names = [u.get("name") or u.get("unit") for u in anthropic_units if u.get("name") or u.get("unit")]
        except Exception as e:
            logger.error("ensemble: Anthropic extraction failed: %s", e)
            anthropic_names = []

        if not openai_names and not anthropic_names:
            logger.error("ensemble: both generators failed for %s", univ_label)
            return []

        # Union of both extractions
        union = _union_names(openai_names, anthropic_names)
        if not union:
            logger.warning("ensemble: union is empty after merging for %s", univ_label)
            return []

        # Judge the union with Gemini
        try:
            kept = LLMHelper.judge_union(univ_label, union, "gemini", level)
        except Exception as e:
            logger.error("ensemble: judge failed for %s: %s", univ_label, e)
            kept = union  # Fall back to union if judge fails

        # Deduplicate and restrict to original union
        result = []
        seen = set()
        for name in kept:
            if name not in seen and any(_names_match(name, u) for u in union):
                seen.add(name)
                result.append({"name": name})

        return result

    @staticmethod
    def judge_union(univ_label: str, candidates: List[str], judge_provider: str, level: int = 1) -> List[str]:
        """Use a judge provider to filter candidates to real direct sub-units.

        Returns list of approved unit names. level scopes the judgement to the
        appropriate hierarchy tier (1=schools, 2=departments, 3+=programs/labs).
        """
        if not candidates:
            return []

        candidates_list = "\n".join(f"- {c}" for c in candidates)
        prompt = (
            JUDGE_PROMPT_TEMPLATE
            .replace("UNIVERSITY", univ_label)
            .replace("SCOPE_NOTE", _judge_scope_note(level))
            .replace("UNITS_LIST", candidates_list)
        )

        if judge_provider == "openai":
            try:
                client = _get_openai_client()
                resp = client.responses.create(
                    model=LLM_MODEL,
                    input=[{"role": "user", "content": prompt}],
                    text={"format": {"type": "json_schema", "name": "judge_keep", "schema": JUDGE_KEEP_SCHEMA}},
                    max_output_tokens=1024,
                    store=False
                )
                raw_text = resp.output_text if resp.output else None
            except Exception as e:
                logger.error("judge_union (OpenAI) failed: %s", e)
                raise

        elif judge_provider == "anthropic":
            try:
                client = _get_anthropic_client()
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw_text = resp.content[0].text if resp.content else None
                # Extract JSON if wrapped in markdown
                match = re.search(r'\{[\s\S]*\}', raw_text) if raw_text else None
                raw_text = match.group(0) if match else raw_text
            except Exception as e:
                logger.error("judge_union (Anthropic) failed: %s", e)
                raise

        elif judge_provider == "gemini":
            try:
                client = _get_gemini_client()
                from google.genai import types as genai_types
                resp = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[genai_types.Content(parts=[genai_types.Part.from_text(prompt)])],
                    generation_config=genai_types.GenerationConfig(max_output_tokens=1024),
                )
                raw_text = resp.text if resp.text else None
                # Extract JSON if wrapped in markdown
                match = re.search(r'\{[\s\S]*\}', raw_text) if raw_text else None
                raw_text = match.group(0) if match else raw_text
            except Exception as e:
                logger.error("judge_union (Gemini) failed: %s", e)
                raise
        else:
            raise ValueError(f"Unknown judge provider: {judge_provider}")

        try:
            payload = _parse_json_text(raw_text)
            kept = payload.get("keep", [])
            if not isinstance(kept, list):
                raise ValueError(f"judge_union: 'keep' field is not a list, got {type(kept).__name__}")
            # Validate each element is a string
            for i, item in enumerate(kept):
                if not isinstance(item, str):
                    raise ValueError(f"judge_union: 'keep[{i}]' is not a string, got {type(item).__name__}: {item}")
            return kept
        except Exception as e:
            logger.error("judge_union: failed to parse judge response: %s", e)
            raise

    @staticmethod
    def choose_match(candidate: str, univ_label: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Return (qid,label) if LLM says the candidate matches one of the children, else None.

        Uses best available provider for matching.
        """
        if not children:
            return None

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

        # Try providers in order
        providers = [
            ("openai", _get_openai_client, LLM_MODEL),
            ("anthropic", _get_anthropic_client, ANTHROPIC_MODEL),
            ("gemini", _get_gemini_client, GEMINI_MODEL),
        ]

        for provider_name, get_client, model in providers:
            try:
                if provider_name == "openai":
                    client = get_client()
                    resp = client.responses.create(
                        model=model,
                        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                        max_output_tokens=16,
                    )
                    answer = (resp.output_text or "").strip()

                elif provider_name == "anthropic":
                    client = get_client()
                    resp = client.messages.create(
                        model=model,
                        max_tokens=16,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    answer = (resp.content[0].text if resp.content else "").strip()

                elif provider_name == "gemini":
                    client = get_client()
                    from google.genai import types as genai_types
                    resp = client.models.generate_content(
                        model=model,
                        contents=[genai_types.Content(parts=[genai_types.Part.from_text(prompt)])],
                    )
                    answer = (resp.text or "").strip()

                if not answer:
                    logger.debug("choose_match (%s): empty response for candidate '%s'", provider_name, candidate)
                    continue

                if answer.upper() == "NONE":
                    logger.debug("choose_match (%s): returned NONE for candidate '%s'", provider_name, candidate)
                    return None

                # Parse answer (may be QID or ORPHAN:QID)
                token = answer.split()[0]
                for qid, label in children:
                    if qid == token:
                        return (qid, label)

                logger.debug("choose_match (%s): answer '%s' did not match any child QID", provider_name, answer)
                continue

            except ValueError as e:
                # Provider not configured
                logger.debug("choose_match: %s not available (%s)", provider_name, e)
                continue
            except Exception as e:
                logger.warning("choose_match: %s failed (%s), trying next provider", provider_name, e)
                continue

        logger.warning("choose_match: all providers failed for candidate '%s'", candidate)
        return None


def _union_names(names_a: List[str], names_b: List[str]) -> List[str]:
    """Compute union of names, deduplicating fuzzy matches."""
    result = []
    for name in names_a + names_b:
        if not any(_names_match(name, r) for r in result):
            result.append(name)
    return result
