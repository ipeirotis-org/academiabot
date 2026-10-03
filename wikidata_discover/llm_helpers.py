import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from rich.console import Console
import hashlib
from pathlib import Path

from wikidata_discover import config
from wikidata_discover.config import require_key
# Model names and API keys are read from config at call time (config.LLM_MODEL,
# config.OPENAI_API_KEY etc.), never captured at import, so that the CLI --llm
# override, Secret Manager loading in batch.py, and tests can change them.

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

JUDGE_PROMPT_TEMPLATE = (
    "You are evaluating academic units for a university. Given the name of UNIVERSITY and a union of school/college/division names "
    "proposed by multiple automated extraction systems, filter to only those that are real, top-level academic units of UNIVERSITY.\n\n"
    "Proposed units:\nUNITS_LIST\n\n"
    "Return a JSON object with a 'keep' key containing an array of unit names you confirm as real top-level units. "
    "Do not invent or add units not in the list above."
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
_gemini_client = None
LLM_TIMEOUT_S = 180  # one LLM request; the SDK defaults (10 minutes) are too long for a timed slice
_MIN_TIME_FOR_LLM_CALL_S = 30  # below this much time before config.DEADLINE, no LLM call is started


class LLMUnavailable(RuntimeError):
    """Raised by choose_match when no provider produced a usable answer (every one
    failed, was not configured, or replied with nothing). It is not a NONE: the
    caller leaves the candidate unresolved instead of calling it missing."""


class LLMDeadline(RuntimeError):
    """Raised when a decision could not be asked for because the process deadline
    is too close. The attempt fails and is retried; it is never an answer."""


def llm_timeout() -> float:
    """Timeout for one LLM request: LLM_TIMEOUT_S, or less when config.DEADLINE is
    closer (never below 5 seconds)."""
    left = config.seconds_left()
    return LLM_TIMEOUT_S if left is None else max(5.0, min(LLM_TIMEOUT_S, left))


LLM_MAX_RETRIES = 2   # the OpenAI and Anthropic SDK default


def llm_retries() -> int:
    """How many times the OpenAI or Anthropic SDK may retry one request. The SDK
    applies the timeout to each try, so the tries together must still fit before
    config.DEADLINE: the default 2 when there is room, fewer when there is not."""
    left = config.seconds_left()
    if left is None:
        return LLM_MAX_RETRIES
    return max(0, min(LLM_MAX_RETRIES, int(left // llm_timeout()) - 1))


def _client_options() -> dict:
    """with_options() arguments for one OpenAI or Anthropic request: a timeout and a
    retry count that together end before the deadline."""
    return {"timeout": llm_timeout(), "max_retries": llm_retries()}


def _gemini_http_options():
    """Per-request Gemini timeout, capped at the deadline like the other providers."""
    from google.genai import types as genai_types
    return genai_types.HttpOptions(timeout=int(llm_timeout() * 1000))


def enough_time_for_llm_call() -> bool:
    """False once config.DEADLINE is too close to start another LLM request."""
    left = config.seconds_left()
    return left is None or left >= _MIN_TIME_FOR_LLM_CALL_S


def reset_clients() -> None:
    """Forget the cached provider clients so the next call builds them from the
    current keys in config (used after a key is loaded or rotated)."""
    global _openai_client, _anthropic_client, _gemini_client
    _openai_client = _anthropic_client = _gemini_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=require_key("OPENAI_API_KEY", config.OPENAI_API_KEY), timeout=LLM_TIMEOUT_S)
    return _openai_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=require_key("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY), timeout=LLM_TIMEOUT_S
        )
    return _anthropic_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        from google.genai import types as genai_types
        _gemini_client = genai.Client(api_key=require_key("GOOGLE_API_KEY", config.GOOGLE_API_KEY),
                                      http_options=genai_types.HttpOptions(timeout=int(LLM_TIMEOUT_S * 1000)))
    return _gemini_client

# ─────────────────────────  NAME MATCHING  ─────────────────────────


def _names_match(a: str, b: str) -> bool:
    """Lightweight fuzzy name match for deduplicating ensemble outputs."""
    from rapidfuzz import fuzz
    na = re.sub(r"[^a-z0-9 ]", "", a.lower().strip())
    nb = re.sub(r"[^a-z0-9 ]", "", b.lower().strip())
    return na == nb or fuzz.token_sort_ratio(na, nb) >= 88


def _prompt_hash(*parts: Any) -> str:
    """Short hash of the prompt text and schema a call uses, so a changed prompt
    never reads an answer produced by the old one."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


EXTRACT_PROMPT_HASH = _prompt_hash(SYSTEM_EXTRACT, UNIVERSITY_UNITS_SCHEMA)


def _cache_key(univ_label: str, provider: str, model: str, purpose: str = "extract",
               prompt_hash: str = EXTRACT_PROMPT_HASH, extra: str = "") -> str:
    """Cache key: provider, model, purpose, university, the hash of the prompt and
    schema (so a new prompt gets a new file), and `extra` for anything else the
    request depends on (the website, so two institutions with one name differ)."""
    return hashlib.sha256(f"{provider}|{purpose}|{prompt_hash}|{univ_label}|{model}|{extra}".encode()).hexdigest()


# Cache files read or written since the last reset. A batch runner uses this to
# upload exactly the cache entries a run depended on, including old ones it reused.
cache_paths_touched: set = set()

# The provider whose answer the last extract_divisions_best_available() call used
# (None before any call, or when every provider failed). Recorded per university.
last_extraction_provider = None


def available_providers() -> dict:
    """Which providers have a key right now, in call order. Recorded with every run so
    a replay with a different set of keys cannot pass for the same configuration."""
    return {"openai": bool(config.OPENAI_API_KEY), "anthropic": bool(config.ANTHROPIC_API_KEY),
            "gemini": bool(config.GOOGLE_API_KEY)}


def _load_cache(key: str) -> Optional[List[Dict[str, Any]]]:
    path = _CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            cache_paths_touched.add(path)
            return data
        except Exception:
            pass
    return None


def _save_cache(key: str, units: List[Dict[str, Any]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(units, indent=2))
    cache_paths_touched.add(path)


def _parse_json_text(text: str) -> Any:
    """Parse JSON text, raising on invalid JSON or empty input."""
    if not text:
        raise ValueError("Empty response text")
    return json.loads(text)


def _normalize_units(payload: Any) -> List[Dict[str, Any]]:
    """Extract and normalize units from provider response payload.

    Expects payload to be a dict with 'units' key containing a list.
    Returns normalized list or raises ValueError if structure is invalid.
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
            result.append({"name": itm})
        elif isinstance(itm, dict):
            result.append(itm)
        else:
            raise ValueError(f"Invalid unit entry: expected string or dict, got {type(itm).__name__}: {itm}")
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
    def extract_divisions_openai(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using OpenAI API."""
        model = config.LLM_MODEL
        key = _cache_key(univ_label, "openai", model, extra=website or "")
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_openai: cache hit for %s", univ_label)
            return cached

        client = _get_openai_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_openai: deadline too close, not calling for %s", univ_label)
                break
            try:
                resp = client.with_options(**_client_options()).responses.create(
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
    def extract_divisions_anthropic(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using Anthropic Claude API."""
        model = config.ANTHROPIC_MODEL
        key = _cache_key(univ_label, "anthropic", model, extra=website or "")
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_anthropic: cache hit for %s", univ_label)
            return cached

        client = _get_anthropic_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_anthropic: deadline too close, not calling for %s", univ_label)
                break
            try:
                resp = client.with_options(**_client_options()).messages.create(
                    model=model,
                    max_tokens=2048,
                    system=SYSTEM_EXTRACT,
                    messages=[
                        {"role": "user", "content": f"{univ_label} -- {website}"}
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
    def extract_divisions_gemini(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using Google Gemini API."""
        model = config.GEMINI_MODEL
        key = _cache_key(univ_label, "gemini", model, extra=website or "")
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_gemini: cache hit for %s", univ_label)
            return cached

        client = _get_gemini_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_gemini: deadline too close, not calling for %s", univ_label)
                break
            try:
                from google.genai import types as genai_types

                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        genai_types.Content(
                            parts=[
                                genai_types.Part.from_text(text=f"System: {SYSTEM_EXTRACT}\n\nInput: {univ_label} -- {website}")
                            ]
                        )
                    ],
                    config=genai_types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=2048,
                        http_options=_gemini_http_options(),
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
    def extract_divisions_best_available(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using the best available provider.

        Tries providers in order: OpenAI, Anthropic, Gemini.
        Falls back to next provider if current one fails or is not configured.
        Raises ValueError if no providers are available.
        """
        global last_extraction_provider
        last_extraction_provider = None
        providers = [
            ("openai", LLMHelper.extract_divisions_openai),
            ("anthropic", LLMHelper.extract_divisions_anthropic),
            ("gemini", LLMHelper.extract_divisions_gemini),
        ]

        for provider_name, extractor in providers:
            try:
                logger.debug("Trying %s for extraction...", provider_name)
                result = extractor(univ_label, website)
                if result:  # Successfully extracted non-empty list
                    logger.info("extract_divisions_best_available: %s returned %d units", provider_name, len(result))
                    last_extraction_provider = provider_name
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

        logger.error("extract_divisions_best_available: all providers failed, unavailable, or empty for %s", univ_label)
        raise ValueError(
            f"No LLM provider returned any units for {univ_label!r}: each one was not "
            f"configured, failed, or answered with an empty list (an empty list usually "
            f"means the item is not a university). Configure at least one of "
            f"OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY if none is set."
        )

    @staticmethod
    def extract_divisions_ensemble(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract divisions using ensemble: generate from OpenAI + Anthropic, judge with Gemini.

        Returns union of kept names from judge, or empty list if any step fails.
        """
        try:
            openai_units = LLMHelper.extract_divisions_openai(univ_label, website)
            openai_names = [u.get("name") or u.get("unit") for u in openai_units if u.get("name") or u.get("unit")]
        except Exception as e:
            logger.error("ensemble: OpenAI extraction failed: %s", e)
            openai_names = []

        try:
            anthropic_units = LLMHelper.extract_divisions_anthropic(univ_label, website)
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
            kept = LLMHelper.judge_union(univ_label, union, "gemini")
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
    def judge_union(univ_label: str, candidates: List[str], judge_provider: str) -> List[str]:
        """Use a judge provider to filter candidates to real top-level units.

        Returns list of approved unit names.
        """
        if not candidates:
            return []

        candidates_list = "\n".join(f"- {c}" for c in candidates)
        prompt = JUDGE_PROMPT_TEMPLATE.replace("UNIVERSITY", univ_label).replace("UNITS_LIST", candidates_list)

        if judge_provider == "openai":
            try:
                client = _get_openai_client()
                resp = client.responses.create(
                    model=config.LLM_MODEL,
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
                    model=config.ANTHROPIC_MODEL,
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
                    model=config.GEMINI_MODEL,
                    contents=[genai_types.Content(parts=[genai_types.Part.from_text(text=prompt)])],
                    config=genai_types.GenerateContentConfig(max_output_tokens=1024, http_options=_gemini_http_options()),
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
            ("openai", _get_openai_client, config.LLM_MODEL),
            ("anthropic", _get_anthropic_client, config.ANTHROPIC_MODEL),
            ("gemini", _get_gemini_client, config.GEMINI_MODEL),
        ]

        # A match decision is cached on the full prompt (candidate, university, and
        # the exact list of choices), the models in use, and which providers are
        # configured, so a retry of the same university repeats no paid call and
        # cannot flip an earlier decision. A decision that a fallback provider made
        # while the preferred one was failing is not reused once the preferred one
        # is configured again: it is asked afresh and the new answer replaces it.
        configured = [n for n, on in available_providers().items() if on]
        match_key = _cache_key(univ_label, "match", "|".join(m for _, _, m in providers),
                               purpose="match", prompt_hash=_prompt_hash(prompt), extra=",".join(configured))
        cached = _load_cache(match_key)
        fallback_cached = None
        if isinstance(cached, dict) and "answer" in cached:
            if not configured or cached.get("provider") == configured[0]:
                return parse_match_answer(cached["answer"], children)
            # A fallback provider decided while the preferred one was failing. Ask
            # the preferred one again; if it still fails, the cached decision stands
            # rather than paying a fallback provider a second time for a new one.
            fallback_cached = cached
            logger.info("choose_match: cached answer came from %s, not the preferred %s; asking it again",
                        cached.get("provider"), configured[0])

        deadline_hit = False
        for provider_name, get_client, model in providers:
            if fallback_cached is not None and provider_name != configured[0]:
                logger.info("choose_match: %s still gives no answer; keeping the cached %s decision",
                            configured[0], fallback_cached.get("provider"))
                return parse_match_answer(fallback_cached["answer"], children)
            if not enough_time_for_llm_call():
                logger.warning("choose_match: deadline too close, not calling for candidate '%s'", candidate)
                deadline_hit = True
                break
            try:
                if provider_name == "openai":
                    client = get_client().with_options(**_client_options())
                    resp = client.responses.create(
                        model=model,
                        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                        max_output_tokens=16,
                    )
                    answer = (resp.output_text or "").strip()

                elif provider_name == "anthropic":
                    client = get_client().with_options(**_client_options())
                    resp = client.messages.create(
                        model=model,
                        max_tokens=16,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    answer = (resp.content[0].text if resp.content else "").strip()

                elif provider_name == "gemini":
                    client = get_client()
                    from google.genai import types as genai_types
                    # One token is wanted. Gemini counts its thinking against
                    # max_output_tokens and answers with nothing when thinking uses
                    # it all, so thinking is off for this classification.
                    resp = client.models.generate_content(
                        model=model,
                        contents=[genai_types.Content(parts=[genai_types.Part.from_text(text=prompt)])],
                        config=genai_types.GenerateContentConfig(
                            max_output_tokens=32, http_options=_gemini_http_options(),
                            thinking_config=genai_types.ThinkingConfig(thinking_budget=0)),
                    )
                    answer = (resp.text or "").strip()

                if not answer:
                    logger.debug("choose_match (%s): empty response for candidate '%s'", provider_name, candidate)
                    continue

                if answer.upper() == "NONE":
                    logger.debug("choose_match (%s): returned NONE for candidate '%s'", provider_name, candidate)
                    _save_cache(match_key, {"answer": "NONE", "provider": provider_name})
                    return None

                parsed = parse_match_answer(answer, children)
                if parsed is not None:
                    _save_cache(match_key, {"answer": answer, "provider": provider_name})
                    return parsed

                logger.debug("choose_match (%s): answer '%s' did not match any child QID", provider_name, answer)
                continue

            except ValueError as e:
                # Provider not configured
                logger.debug("choose_match: %s not available (%s)", provider_name, e)
                continue
            except Exception as e:
                logger.warning("choose_match: %s failed (%s), trying next provider", provider_name, e)
                continue

        if deadline_hit:
            # Not an answer: nobody was asked. Returning None here would make the
            # candidate "missing" and export a possible duplicate. Fail the attempt
            # instead; the university is retried in a later slice.
            raise LLMDeadline(f"no time left to match candidate {candidate!r} before the deadline")
        # Not an answer either: every provider failed or said nothing. None would make
        # the candidate "missing" and export a possible duplicate.
        raise LLMUnavailable(f"no provider could judge candidate {candidate!r}")


def parse_match_answer(answer: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """Turn a choose_match reply into (qid, label), ("ORPHAN:" + qid, label), or None.

    The model replies with a bare QID, ORPHAN:QID, or NONE. The QID must be one of the
    offered children; anything else is treated as no match. Pure function, unit tested.
    """
    token = (answer or "").strip().split()[0] if (answer or "").strip() else ""
    if not token or token.upper() == "NONE":
        return None
    orphan = False
    if token.upper().startswith("ORPHAN:"):
        orphan = True
        token = token.split(":", 1)[1].strip()
    token = token.upper()
    for qid, label in children:
        if qid.upper() == token:
            return (f"ORPHAN:{qid}", label) if orphan else (qid, label)
    return None


def _union_names(names_a: List[str], names_b: List[str]) -> List[str]:
    """Compute union of names, deduplicating fuzzy matches."""
    result = []
    for name in names_a + names_b:
        if not any(_names_match(name, r) for r in result):
            result.append(name)
    return result
