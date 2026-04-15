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
_gemini_configured = False


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
    global _gemini_configured
    from google import genai
    if not _gemini_configured:
        genai.configure(api_key=require_key("GOOGLE_API_KEY", GOOGLE_API_KEY))
        _gemini_configured = True
    return genai

# ─────────────────────────  NAME MATCHING  ─────────────────────────

def _names_match(a: str, b: str) -> bool:
    """Lightweight fuzzy name match for deduplicating ensemble outputs."""
    from rapidfuzz import fuzz
    na = re.sub(r"[^a-z0-9 ]", "", a.lower().strip())
    nb = re.sub(r"[^a-z0-9 ]", "", b.lower().strip())
    return na == nb or fuzz.token_sort_ratio(na, nb) >= 88

# ─────────────────────────  CACHE HELPERS  ─────────────────────────


def _cache_key(univ_label: str, model: str) -> str:
    return hashlib.sha256(f"{univ_label}|{model}".encode()).hexdigest()


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

# ─────────────────────────  JSON PARSING HELPERS  ─────────────────────────


def _parse_json_text(text: str) -> Optional[dict]:
    """Try to parse JSON from a text string, handling markdown code fences."""
    if not text:
        return None
    # Strip markdown fences
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Try to find JSON object containing "units" key
    match = re.search(r'\{[^{}]*"units"[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Wider search for any JSON object
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _normalize_units(payload: dict, source: str = "") -> List[Dict[str, Any]]:
    """Normalize units list from a parsed JSON payload."""
    units = payload.get("units")
    if not isinstance(units, list):
        logger.error("'units' key missing or not a list in payload from %s: %s", source, str(payload)[:500])
        return []
    return [{"name": itm} if isinstance(itm, str) else itm for itm in units]

# ─────────────────────────  PROVIDER IMPLEMENTATIONS  ─────────────────────────


def _extract_openai(univ_label: str, website: str, model: str) -> List[Dict[str, Any]]:
    """Extract divisions using OpenAI Responses API."""
    client = _get_openai_client()
    for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": SYSTEM_EXTRACT},
                {"role": "user", "content": f"{univ_label} -- {website}"}
            ],
            tools=[{"type": "web_search_preview"}],
            text={"format": {"type": "json_schema", "name": "university_units", "schema": UNIVERSITY_UNITS_SCHEMA}},
            store=False
        )

        raw_text = resp.output_text if resp.output else None
        if not raw_text:
            logger.warning(
                "_extract_openai attempt %d/%d: empty response for %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label,
            )
            continue

        payload = _parse_json_text(raw_text)
        if payload is None:
            logger.error(
                "_extract_openai attempt %d/%d: JSON parse error for %s\nRaw: %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label, raw_text[:500],
            )
            continue

        result = _normalize_units(payload, source=f"openai/{model}")
        if result or "units" in payload:
            return result

    console.print(f"[red]OpenAI failed to extract divisions for {univ_label} after {_EXTRACT_MAX_RETRIES} attempts.[/red]")
    return []


def _extract_anthropic(univ_label: str, website: str, model: str) -> List[Dict[str, Any]]:
    """Extract divisions using Anthropic API with web search tool."""
    client = _get_anthropic_client()
    for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_EXTRACT,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"University: {univ_label}\n"
                            f"Website: {website or 'unknown'}\n\n"
                            "Search the web for this university's top-level schools and colleges, "
                            "then respond with ONLY a JSON object in this exact format with no other text:\n"
                            '{"units": [{"name": "...", "unit_type": "school", "city": "...", "state": "...", "website": "..."}], "reference": "url"}'
                        )
                    }
                ]
            )
        except Exception as exc:
            logger.error("_extract_anthropic attempt %d/%d error: %s", attempt, _EXTRACT_MAX_RETRIES, exc)
            continue

        # Find last text block in resp.content
        raw_text = None
        for block in reversed(resp.content):
            if hasattr(block, "text"):
                raw_text = block.text
                break

        if not raw_text:
            logger.warning(
                "_extract_anthropic attempt %d/%d: no text block for %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label,
            )
            continue

        payload = _parse_json_text(raw_text)
        if payload is None:
            logger.error(
                "_extract_anthropic attempt %d/%d: JSON parse error for %s\nRaw: %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label, raw_text[:500],
            )
            continue

        result = _normalize_units(payload, source=f"anthropic/{model}")
        if result or "units" in payload:
            return result

    console.print(f"[red]Anthropic failed to extract divisions for {univ_label} after {_EXTRACT_MAX_RETRIES} attempts.[/red]")
    return []


def _extract_gemini(univ_label: str, website: str, model: str) -> List[Dict[str, Any]]:
    """Extract divisions using Gemini API with Google Search grounding."""
    from google import genai as google_genai
    from google.genai import types as genai_types
    client = google_genai.Client(api_key=require_key("GOOGLE_API_KEY", GOOGLE_API_KEY))
    for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
        try:
            prompt = (
                f"{SYSTEM_EXTRACT}\n\n"
                f"University: {univ_label}\n"
                f"Website: {website or 'unknown'}\n\n"
                "Search for this university's top-level schools and colleges, then respond with "
                "ONLY a JSON object and no other text, in this exact format:\n"
                '{"units": [{"name": "...", "unit_type": "school", "city": "...", "state": "...", "website": "..."}], "reference": "url"}'
            )
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                ),
            )
        except Exception as exc:
            logger.error("_extract_gemini attempt %d/%d error: %s", attempt, _EXTRACT_MAX_RETRIES, exc)
            continue

        raw_text = getattr(response, "text", None)
        if not raw_text:
            logger.warning(
                "_extract_gemini attempt %d/%d: empty response for %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label,
            )
            continue

        payload = _parse_json_text(raw_text)
        if payload is None:
            logger.error(
                "_extract_gemini attempt %d/%d: JSON parse error for %s\nRaw: %s",
                attempt, _EXTRACT_MAX_RETRIES, univ_label, raw_text[:500],
            )
            continue

        result = _normalize_units(payload, source=f"gemini/{model}")
        if result or "units" in payload:
            return result

    console.print(f"[red]Gemini failed to extract divisions for {univ_label} after {_EXTRACT_MAX_RETRIES} attempts.[/red]")
    return []

# ─────────────────────────  JUDGE  ─────────────────────────

_JUDGE_SYSTEM = (
    "You are an expert on university organizational structure. "
    "Given the name of a university and a list of proposed top-level academic units, "
    "return a JSON object with key 'keep' whose value is an array of the names from the "
    "input list that are genuine top-level academic units. "
    "A genuine top-level unit is a school, college, faculty, or major division that reports "
    "directly to the university president or provost and grants its own degrees. "
    "Only keep an item if you are confident it belongs -- remove anything that is a department "
    "within a school, a research center, a continuing education arm, an honors program, "
    "a graduate school umbrella, or an administrative unit. "
    "Do NOT use web search - reason from your knowledge."
)

def _judge_union_openai(univ_label: str, union_names: List[str], model: str) -> List[str]:
    client = _get_openai_client()
    user_content = (
        f"University: {univ_label}\n\n"
        f"Proposed units:\n" + "\n".join(f"- {n}" for n in union_names)
    )
    try:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user_content}
            ],
            text={"format": {"type": "json_schema", "name": "judge_keep", "schema": JUDGE_KEEP_SCHEMA}},
            store=False
        )
        raw_text = resp.output_text if resp.output else None
        if not raw_text:
            return union_names
        payload = _parse_json_text(raw_text)
        if payload and isinstance(payload.get("keep"), list):
            return payload["keep"]
    except Exception as exc:
        logger.error("_judge_union_openai error: %s", exc)
    return union_names


def _judge_union_anthropic(univ_label: str, union_names: List[str], model: str) -> List[str]:
    client = _get_anthropic_client()
    user_content = (
        f"University: {univ_label}\n\n"
        f"Proposed units:\n" + "\n".join(f"- {n}" for n in union_names) +
        "\n\nReturn JSON: {\"keep\": [\"...\", ...]}"
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[
                {"role": "user", "content": f"{_JUDGE_SYSTEM}\n\n{user_content}"}
            ]
        )
        raw_text = None
        for block in reversed(resp.content):
            if hasattr(block, "text"):
                raw_text = block.text
                break
        if not raw_text:
            return union_names
        payload = _parse_json_text(raw_text)
        if payload and isinstance(payload.get("keep"), list):
            return payload["keep"]
    except Exception as exc:
        logger.error("_judge_union_anthropic error: %s", exc)
    return union_names


def _judge_union_gemini(univ_label: str, union_names: List[str], model: str) -> List[str]:
    from google import genai as google_genai
    from google.genai import types as genai_types
    client = google_genai.Client(api_key=require_key("GOOGLE_API_KEY", GOOGLE_API_KEY))
    user_content = (
        f"University: {univ_label}\n\n"
        f"Proposed units:\n" + "\n".join(f"- {n}" for n in union_names) +
        "\n\nReturn JSON: {\"keep\": [\"...\", ...]}"
    )
    try:
        prompt = f"{_JUDGE_SYSTEM}\n\n{user_content}"
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        raw_text = getattr(response, "text", None)
        if not raw_text:
            return union_names
        payload = _parse_json_text(raw_text)
        if payload and isinstance(payload.get("keep"), list):
            return payload["keep"]
    except Exception as exc:
        logger.error("_judge_union_gemini error: %s", exc)
    return union_names

# ─────────────────────────  PUBLIC API  ─────────────────────────

class LLMHelper:
    @staticmethod
    def extract_divisions(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract top-level academic/administrative units using the default OpenAI model."""
        return LLMHelper.extract_divisions_openai(univ_label, website)

    @staticmethod
    def extract_divisions_best_available(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """
        Use the ensemble pipeline when OpenAI, Gemini, and Anthropic keys are all
        available. Otherwise fall back to whichever single provider has a key,
        preferring OpenAI -> Anthropic -> Gemini. Raises ValueError if no key is set.
        """
        has_openai = bool(OPENAI_API_KEY)
        has_anthropic = bool(ANTHROPIC_API_KEY)
        has_gemini = bool(GOOGLE_API_KEY)

        if has_openai and has_anthropic and has_gemini:
            return LLMHelper.extract_divisions_ensemble(univ_label, website)
        if has_openai:
            logger.info("extract_divisions_best_available: using OpenAI only (ensemble needs all three keys)")
            return LLMHelper.extract_divisions_openai(univ_label, website)
        if has_anthropic:
            logger.info("extract_divisions_best_available: using Anthropic only")
            return LLMHelper.extract_divisions_anthropic(univ_label, website)
        if has_gemini:
            logger.info("extract_divisions_best_available: using Gemini only")
            return LLMHelper.extract_divisions_gemini(univ_label, website)
        raise ValueError(
            "No LLM provider key set. Set OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY."
        )

    @staticmethod
    def extract_divisions_ensemble(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """
        Best-performing pipeline: OpenAI + Gemini generate, Anthropic judges.
        Returns a deduplicated, judge-filtered list of unit dicts.
        Requires OPENAI_API_KEY, GOOGLE_API_KEY, and ANTHROPIC_API_KEY.
        """
        openai_divs = LLMHelper.extract_divisions_openai(univ_label, website)

        # Use Gemini as second generator if available, fall back to Anthropic, else OpenAI only
        second_divs: List[Dict[str, Any]] = []
        second_label = "none"
        if GOOGLE_API_KEY:
            second_divs = LLMHelper.extract_divisions_gemini(univ_label, website)
            second_label = "gemini"
        elif ANTHROPIC_API_KEY:
            second_divs = LLMHelper.extract_divisions_anthropic(univ_label, website)
            second_label = "anthropic"

        if not second_divs:
            logger.warning("extract_divisions_ensemble: no second generator available, returning OpenAI only for %s", univ_label)
            return openai_divs

        # Deduplicated union of names from both generators
        seen: List[str] = []
        for div in openai_divs + second_divs:
            name = div.get("name") or div.get("unit")
            if name and not any(_names_match(name, s) for s in seen):
                seen.append(name)

        logger.info(
            "extract_divisions_ensemble: %d openai + %d %s -> %d union candidates for %s",
            len(openai_divs), len(second_divs), second_label, len(seen), univ_label,
        )

        # Use Anthropic as judge if available, else Gemini, else return union unfiltered
        if ANTHROPIC_API_KEY:
            kept_names = LLMHelper.judge_union(univ_label, seen, provider="anthropic")
        elif GOOGLE_API_KEY:
            kept_names = LLMHelper.judge_union(univ_label, seen, provider="gemini")
        else:
            logger.warning("extract_divisions_ensemble: no judge available, returning union unfiltered for %s", univ_label)
            kept_names = seen

        # Rebuild full dicts for kept names, preserving metadata from generators
        name_to_div: dict = {}
        for div in openai_divs + second_divs:
            name = div.get("name") or div.get("unit")
            if name and name not in name_to_div:
                name_to_div[name] = div

        result = []
        for name in kept_names:
            match = next((d for d in name_to_div.values() if _names_match(name, d.get("name") or d.get("unit", ""))), None)
            result.append(match if match else {"name": name})

        logger.info(
            "extract_divisions_ensemble: Anthropic judge kept %d/%d for %s",
            len(result), len(seen), univ_label,
        )
        return result

    @staticmethod
    def extract_divisions_openai(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using OpenAI (default model from config)."""
        model = LLM_MODEL
        key = _cache_key(univ_label, model)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_openai: cache hit for %s", univ_label)
            return cached
        result = _extract_openai(univ_label, website, model)
        if result:
            _save_cache(key, result)
        return result or []

    @staticmethod
    def extract_divisions_anthropic(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using Anthropic Claude."""
        model = ANTHROPIC_MODEL
        key = _cache_key(univ_label, model)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_anthropic: cache hit for %s", univ_label)
            return cached
        result = _extract_anthropic(univ_label, website, model)
        if result:
            _save_cache(key, result)
        return result or []

    @staticmethod
    def extract_divisions_gemini(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using Google Gemini."""
        model = GEMINI_MODEL
        key = _cache_key(univ_label, model)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_gemini: cache hit for %s", univ_label)
            return cached
        result = _extract_gemini(univ_label, website, model)
        if result:
            _save_cache(key, result)
        return result or []

    @staticmethod
    def judge_union(univ_label: str, union_names: List[str], provider: str) -> List[str]:
        """
        Given a university name and a deduplicated list of proposed unit names,
        filter to those that are genuine top-level academic units.

        provider must be one of: 'openai', 'anthropic', 'gemini'
        """
        if not union_names:
            return []
        if provider == "openai":
            return _judge_union_openai(univ_label, union_names, LLM_MODEL)
        elif provider == "anthropic":
            return _judge_union_anthropic(univ_label, union_names, ANTHROPIC_MODEL)
        elif provider == "gemini":
            return _judge_union_gemini(univ_label, union_names, GEMINI_MODEL)
        raise ValueError(f"Unknown provider for judge_union: {provider!r}. Must be 'openai', 'anthropic', or 'gemini'.")

    @staticmethod
    def choose_match(candidate: str, univ_label: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Return (qid,label) if LLM says the candidate matches one of the children, else None."""
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
            client = _get_openai_client()
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
        answer = answer.split()[0]
        for qid, label in children:
            if qid == answer:
                return qid, label
        return None
