import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from rich.console import Console
import hashlib
import time
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
_CACHE_DIR = config.LLM_CACHE_DIR   # ACADEMIABOT_CACHE_DIR overrides it

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

# Run log ids (wikidata_discover/run_log.py) of the llm_calls rows behind the last
# extraction that returned units, and behind the last choose_match() decision. The
# discovery step copies them into each candidates row. None and [] when no run log
# is active.
last_extraction_call_id = None
last_match_call_ids: list = []

# Which extraction entry point is running, recorded in each call's request_config.
_extraction_mode = "single"


# ─────────────────────────  RUN LOG RECORDING  ─────────────────────────

def _record(**fields) -> Optional[str]:
    """Add one llm_calls row to the active run log. Returns its id, or None when no
    run log is active. Never raises: a logging problem must not stop a run."""
    from wikidata_discover import run_log
    log = run_log.current()
    if log is None:
        return None
    try:
        return log.record_llm_call(**fields)
    except Exception as e:  # noqa: BLE001
        logger.warning("run log: could not record an LLM call (%s: %s)", type(e).__name__, e)
        return None


def _raw_dump(resp: Any, text: Optional[str]) -> Optional[str]:
    """The full raw response as JSON text when the SDK object can give it (it keeps
    the web search calls and sources), else the plain response text."""
    try:
        dumped = resp.model_dump_json(indent=2)
        if isinstance(dumped, str):
            return dumped
    except Exception:  # noqa: BLE001
        pass
    return text


def _usage(resp: Any) -> Tuple[Optional[int], Optional[int]]:
    """(input tokens, output tokens) from an OpenAI, Anthropic, or Gemini response."""
    def as_int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None
    usage = getattr(resp, "usage", None)
    if usage is not None:
        return as_int(getattr(usage, "input_tokens", None)), as_int(getattr(usage, "output_tokens", None))
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        return as_int(getattr(meta, "prompt_token_count", None)), as_int(getattr(meta, "candidates_token_count", None))
    return None, None


def _record_response(call: dict, resp: Any, text: Optional[str], attempt: int, t0: float) -> Optional[str]:
    """Record a call that returned. `call` holds provider, model, purpose, prompt,
    request_config, cache_key, prompt_hash. Done before the answer is parsed."""
    tokens_in, tokens_out = _usage(resp)
    return _record(**call, raw_response=_raw_dump(resp, text), attempt=attempt,
                   tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=int((time.time() - t0) * 1000))


def _record_error(call: dict, exc: Exception, attempt: int, t0: float) -> Optional[str]:
    return _record(**call, attempt=attempt, error=f"{type(exc).__name__}: {str(exc)[:500]}",
                   latency_ms=int((time.time() - t0) * 1000))


def _record_cache_hit(call: dict, key: str) -> Optional[str]:
    """Record an answer read from the cache. It points to the original call's prompt
    and raw response when the cache file has a .meta.json record of them."""
    return _record(**call, cache_hit=True, cached_from=_load_cache_meta(key))


def _meta_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.meta.json"


def _save_cache_meta(key: str, call_id: Optional[str]) -> None:
    """Next to a cache file, keep which call produced it and where its prompt and raw
    response are, so a later cache hit can point to them."""
    from wikidata_discover import run_log
    log = run_log.current()
    if not call_id or log is None:
        return
    meta = {"llm_call_id": call_id, "run_id": log.run_id, **log.text_paths.get(call_id, {})}
    path = _meta_path(key)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2))
    cache_paths_touched.add(path)


def _load_cache_meta(key: str) -> Optional[dict]:
    path = _meta_path(key)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    cache_paths_touched.add(path)
    return meta if isinstance(meta, dict) else None


def _extract_call(provider: str, model: str, key: str, prompt: dict, **request) -> dict:
    """The fixed fields of one extraction call's llm_calls row."""
    return {"provider": provider, "model": model, "purpose": "extract", "prompt": prompt,
            "cache_key": key, "prompt_hash": EXTRACT_PROMPT_HASH,
            "request_config": {"extraction_mode": _extraction_mode, "sample": None, **request}}


def chosen(items: List[Any]) -> List[Any]:
    """The (provider name, ...) entries to try: all of them, or only config.ONLY_PROVIDER."""
    return [i for i in items if config.ONLY_PROVIDER in (None, i[0])]


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

    # Normalize entries: wrap bare strings into dicts, validate all items are dicts.
    # The answer's one reference URL is kept on each unit as its source.
    reference = payload.get("reference") if isinstance(payload.get("reference"), str) else None
    result = []
    for itm in units:
        if isinstance(itm, str):
            itm = {"name": itm}
        elif not isinstance(itm, dict):
            raise ValueError(f"Invalid unit entry: expected string or dict, got {type(itm).__name__}: {itm}")
        if reference:
            itm = {**itm, "reference": itm.get("reference") or reference}
        result.append(itm)
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
        global last_extraction_call_id
        last_extraction_call_id = None
        model = config.LLM_MODEL
        key = _cache_key(univ_label, "openai", model, extra=website or "")
        messages = [
            {"role": "system", "content": SYSTEM_EXTRACT},
            {"role": "user", "content": f"{univ_label} -- {website}"}
        ]
        call = _extract_call("openai", model, key, {"input": messages, "schema": UNIVERSITY_UNITS_SCHEMA},
                             tools=["web_search_preview"], store=False)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_openai: cache hit for %s", univ_label)
            last_extraction_call_id = _record_cache_hit(call, key)
            return cached

        client = _get_openai_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_openai: deadline too close, not calling for %s", univ_label)
                break
            call_id, t0 = None, time.time()
            try:
                resp = client.with_options(**_client_options()).responses.create(
                    model=model,
                    input=messages,
                    tools=[{"type": "web_search_preview"}],
                    text={"format": {"type": "json_schema", "name": "university_units", "schema": UNIVERSITY_UNITS_SCHEMA}},
                    store=False
                )

                raw_text = resp.output_text if resp.output else None
                call_id = _record_response(call, resp, raw_text, attempt, t0)
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
                _save_cache_meta(key, call_id)
                last_extraction_call_id = call_id
                return result

            except Exception as e:
                if call_id is None:
                    _record_error(call, e, attempt, t0)
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
        global last_extraction_call_id
        last_extraction_call_id = None
        model = config.ANTHROPIC_MODEL
        key = _cache_key(univ_label, "anthropic", model, extra=website or "")
        messages = [{"role": "user", "content": f"{univ_label} -- {website}"}]
        call = _extract_call("anthropic", model, key, {"system": SYSTEM_EXTRACT, "messages": messages},
                             tools=[], max_tokens=2048)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_anthropic: cache hit for %s", univ_label)
            last_extraction_call_id = _record_cache_hit(call, key)
            return cached

        client = _get_anthropic_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_anthropic: deadline too close, not calling for %s", univ_label)
                break
            call_id, t0 = None, time.time()
            try:
                resp = client.with_options(**_client_options()).messages.create(
                    model=model,
                    max_tokens=2048,
                    system=SYSTEM_EXTRACT,
                    messages=messages
                )

                raw_text = resp.content[0].text if resp.content else None
                call_id = _record_response(call, resp, raw_text, attempt, t0)
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
                _save_cache_meta(key, call_id)
                last_extraction_call_id = call_id
                return result

            except Exception as e:
                if call_id is None:
                    _record_error(call, e, attempt, t0)
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
        global last_extraction_call_id
        last_extraction_call_id = None
        model = config.GEMINI_MODEL
        key = _cache_key(univ_label, "gemini", model, extra=website or "")
        prompt_text = f"System: {SYSTEM_EXTRACT}\n\nInput: {univ_label} -- {website}"
        call = _extract_call("gemini", model, key, {"text": prompt_text},
                             tools=[], temperature=0.7, max_output_tokens=2048)
        cached = _load_cache(key)
        if cached is not None:
            logger.info("extract_divisions_gemini: cache hit for %s", univ_label)
            last_extraction_call_id = _record_cache_hit(call, key)
            return cached

        client = _get_gemini_client()

        for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
            if not enough_time_for_llm_call():
                logger.warning("extract_divisions_gemini: deadline too close, not calling for %s", univ_label)
                break
            call_id, t0 = None, time.time()
            try:
                from google.genai import types as genai_types

                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        genai_types.Content(
                            parts=[
                                genai_types.Part.from_text(text=prompt_text)
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
                call_id = _record_response(call, resp, raw_text, attempt, t0)
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
                _save_cache_meta(key, call_id)
                last_extraction_call_id = call_id
                return result

            except Exception as e:
                if call_id is None:
                    _record_error(call, e, attempt, t0)
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
        global last_extraction_provider, last_extraction_call_id, _extraction_mode
        last_extraction_provider = last_extraction_call_id = None
        _extraction_mode = f"only_{config.ONLY_PROVIDER}" if config.ONLY_PROVIDER else "best_available"
        try:
            return LLMHelper._best_available(univ_label, website)
        finally:
            _extraction_mode = "single"

    @staticmethod
    def _best_available(univ_label: str, website: str) -> List[Dict[str, Any]]:
        global last_extraction_provider
        providers = chosen([
            ("openai", LLMHelper.extract_divisions_openai),
            ("anthropic", LLMHelper.extract_divisions_anthropic),
            ("gemini", LLMHelper.extract_divisions_gemini),
        ])

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
        if config.ONLY_PROVIDER:
            raise ValueError(f"{config.ONLY_PROVIDER} (run alone with --provider) returned no units for "
                             f"{univ_label!r}: it was not configured, failed, or answered with an empty list.")
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
        global _extraction_mode
        _extraction_mode = "ensemble"
        try:
            return LLMHelper._ensemble(univ_label, website)
        finally:
            _extraction_mode = "single"

    @staticmethod
    def _ensemble(univ_label: str, website: str) -> List[Dict[str, Any]]:
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
        model = {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL,
                 "gemini": config.GEMINI_MODEL}.get(judge_provider)
        call = {"provider": judge_provider, "model": model, "purpose": "judge",
                "prompt": {"text": prompt, "candidates": candidates},
                "prompt_hash": _prompt_hash(prompt, JUDGE_KEEP_SCHEMA),
                "request_config": {"max_output_tokens": 1024, "tools": []}}
        t0 = time.time()

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
                _record_response(call, resp, raw_text, 1, t0)
            except Exception as e:
                _record_error(call, e, 1, t0)
                logger.error("judge_union (OpenAI) failed: %s", e)
                raise

        elif judge_provider == "anthropic":
            recorded = None
            try:
                client = _get_anthropic_client()
                resp = client.messages.create(
                    model=config.ANTHROPIC_MODEL,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw_text = resp.content[0].text if resp.content else None
                recorded = _record_response(call, resp, raw_text, 1, t0)
                # Extract JSON if wrapped in markdown
                match = re.search(r'\{[\s\S]*\}', raw_text) if raw_text else None
                raw_text = match.group(0) if match else raw_text
            except Exception as e:
                if recorded is None:
                    _record_error(call, e, 1, t0)
                logger.error("judge_union (Anthropic) failed: %s", e)
                raise

        elif judge_provider == "gemini":
            recorded = None
            try:
                client = _get_gemini_client()
                from google.genai import types as genai_types
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=[genai_types.Content(parts=[genai_types.Part.from_text(text=prompt)])],
                    config=genai_types.GenerateContentConfig(max_output_tokens=1024, http_options=_gemini_http_options()),
                )
                raw_text = resp.text if resp.text else None
                recorded = _record_response(call, resp, raw_text, 1, t0)
                # Extract JSON if wrapped in markdown
                match = re.search(r'\{[\s\S]*\}', raw_text) if raw_text else None
                raw_text = match.group(0) if match else raw_text
            except Exception as e:
                if recorded is None:
                    _record_error(call, e, 1, t0)
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

        Uses best available provider for matching. Every call, and every decision read
        from the cache, is recorded in the run log with the full list of choices shown;
        their ids are left in last_match_call_ids.
        """
        global last_match_call_ids
        last_match_call_ids = []
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

        # Try providers in order (only the one given with --provider, if any)
        providers = chosen([
            ("openai", _get_openai_client, config.LLM_MODEL),
            ("anthropic", _get_anthropic_client, config.ANTHROPIC_MODEL),
            ("gemini", _get_gemini_client, config.GEMINI_MODEL),
        ])

        # A match decision is cached on the full prompt (candidate, university, and
        # the exact list of choices), the models in use, and which providers are
        # configured, so a retry of the same university repeats no paid call and
        # cannot flip an earlier decision. A decision that a fallback provider made
        # while the preferred one was failing is not reused once the preferred one
        # is configured again: it is asked afresh and the new answer replaces it.
        configured = [n for n, on in available_providers().items() if on and config.ONLY_PROVIDER in (None, n)]
        match_hash = _prompt_hash(prompt)
        match_key = _cache_key(univ_label, "match", "|".join(m for _, _, m in providers),
                               purpose="match", prompt_hash=match_hash, extra=",".join(configured))
        # What the run log keeps as this call's prompt: the exact text, and the same
        # choices as a list, so a reviewer sees every Wikidata item the model chose from.
        logged_prompt = {"text": prompt, "candidate": candidate, "university": univ_label,
                         "choices": [{"n": i + 1, "qid": q, "label": l} for i, (q, l) in enumerate(children)]}

        def call_fields(provider_name, model, **request):
            return {"provider": provider_name, "model": model, "purpose": "match", "prompt": logged_prompt,
                    "cache_key": match_key, "prompt_hash": match_hash,
                    "request_config": {"configured_providers": configured, "tools": [], **request}}

        cached = _load_cache(match_key)
        fallback_cached = None
        if isinstance(cached, dict) and "answer" in cached:
            cached_model = dict((n, m) for n, _, m in providers).get(cached.get("provider"))
            cached_call = call_fields(cached.get("provider"), cached_model)
            if not configured or cached.get("provider") == configured[0]:
                last_match_call_ids.append(_record_cache_hit(cached_call, match_key))
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
                last_match_call_ids.append(_record_cache_hit(cached_call, match_key))
                return parse_match_answer(fallback_cached["answer"], children)
            if not enough_time_for_llm_call():
                logger.warning("choose_match: deadline too close, not calling for candidate '%s'", candidate)
                deadline_hit = True
                break
            call, call_id, t0 = None, None, time.time()
            try:
                if provider_name == "openai":
                    client = get_client().with_options(**_client_options())
                    call, t0 = call_fields(provider_name, model, max_output_tokens=16), time.time()
                    resp = client.responses.create(
                        model=model,
                        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                        max_output_tokens=16,
                    )
                    answer = (resp.output_text or "").strip()

                elif provider_name == "anthropic":
                    client = get_client().with_options(**_client_options())
                    call, t0 = call_fields(provider_name, model, max_tokens=16), time.time()
                    resp = client.messages.create(
                        model=model,
                        max_tokens=16,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    answer = (resp.content[0].text if resp.content else "").strip()

                elif provider_name == "gemini":
                    client = get_client()
                    call, t0 = call_fields(provider_name, model, max_output_tokens=32, thinking_budget=0), time.time()
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

                call_id = _record_response(call, resp, answer, 1, t0)
                last_match_call_ids.append(call_id)
                if not answer:
                    logger.debug("choose_match (%s): empty response for candidate '%s'", provider_name, candidate)
                    continue

                if answer.upper() == "NONE":
                    logger.debug("choose_match (%s): returned NONE for candidate '%s'", provider_name, candidate)
                    _save_cache(match_key, {"answer": "NONE", "provider": provider_name})
                    _save_cache_meta(match_key, call_id)
                    return None

                parsed = parse_match_answer(answer, children)
                if parsed is not None:
                    _save_cache(match_key, {"answer": answer, "provider": provider_name})
                    _save_cache_meta(match_key, call_id)
                    return parsed

                logger.debug("choose_match (%s): answer '%s' did not match any child QID", provider_name, answer)
                continue

            except ValueError as e:
                # Provider not configured (no call was made), or a call that failed
                if call is not None and call_id is None:
                    last_match_call_ids.append(_record_error(call, e, 1, t0))
                logger.debug("choose_match: %s not available (%s)", provider_name, e)
                continue
            except Exception as e:
                if call is not None and call_id is None:
                    last_match_call_ids.append(_record_error(call, e, 1, t0))
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


    @staticmethod
    def extract_subunits(unit: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The direct sub-units of one unit (a university or a school), each with a name,
        a type, a website, one source URL, and a country with the page or address that
        shows it. Tries the providers in order, or only config.ONLY_PROVIDER, and returns
        the first answer with units. Each provider searches the web while it answers.

        unit: {"qid", "label", "parent_label", "website", "country"} from Wikidata.
        Sets last_subunit_provider and last_subunit_call_ids. Raises ValueError when no
        provider answered with units."""
        global last_subunit_provider, last_subunit_call_ids
        last_subunit_provider, last_subunit_call_ids = None, []
        mode = f"only_{config.ONLY_PROVIDER}" if config.ONLY_PROVIDER else "best_available"
        # Anthropic first here: on Stern it found all 10 units of Shuo's ground truth in
        # 55 s, and OpenAI found 5 (week 4 experiments). discover keeps its own order.
        for provider in chosen([(p,) for p in SUBUNIT_PROVIDER_ORDER]):
            try:
                units, call_ids = _subunits_from(provider[0], unit, mode)
            except ValueError as e:      # no key for this provider
                logger.debug("extract_subunits: %s not available (%s)", provider[0], e)
                continue
            if units:
                last_subunit_provider, last_subunit_call_ids = provider[0], call_ids
                return units
        who = f"{config.ONLY_PROVIDER} (run alone with --provider)" if config.ONLY_PROVIDER else "No provider"
        raise ValueError(f"{who} returned no sub-units for {unit.get('label')!r} ({unit.get('qid')}).")

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


# ─────────────────────────  SUB-UNITS (week 4)  ─────────────────────────

SUBUNIT_TYPES = ("school", "department", "program", "center", "campus", "office", "other")
# Panos's scope rule (2026-10-29): offices are out; centers and "other" need a person.
SUBUNIT_IN_SCOPE = {"school": "yes", "department": "yes", "program": "yes", "campus": "yes",
                    "office": "no", "center": "check", "other": "check"}

SYSTEM_SUBUNITS = (
    "You list the organizational sub-units of one academic unit, for a database of university "
    "structures. The input gives the unit, the university it belongs to, its website, and the "
    "country Wikidata has for it.\n"
    "List the direct sub-units only, one level down. For a university: its schools, colleges, "
    "faculties, and campuses. For a school or college: its departments, the degree programs that "
    "are not part of a listed department, its research centers, its campuses, and its offices.\n"
    "Do not list courses, majors, or concentrations inside a department. Do not invent units.\n"
    "Search the web to find and check each unit. For each unit give:\n"
    "- name: the name the unit uses for itself.\n"
    "- unit_type: one of school, department, program, center, campus, office, other.\n"
    "- website: the unit's own website, or null.\n"
    "- source_url: one page, on an official site of the university when possible, that names this "
    "unit as part of the parent unit. Give the exact URL you saw. Use null if you found none.\n"
    "- country: the country where the unit is, in English. Do not copy the parent's country: a "
    "unit can be in another country (NYU Abu Dhabi is in the United Arab Emirates).\n"
    "- country_source: the URL of a page, or the street address, that shows that country.\n"
    "Reply with one JSON object and nothing else: {\"units\": [ ... ]}."
)

SUBUNITS_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "unit_type": {"type": "string", "enum": list(SUBUNIT_TYPES)},
                    "website": {"type": ["string", "null"]},
                    "source_url": {"type": ["string", "null"]},
                    "country": {"type": ["string", "null"]},
                    "country_source": {"type": ["string", "null"]},
                },
                "required": ["name", "unit_type", "website", "source_url", "country", "country_source"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["units"],
    "additionalProperties": False,
}

SUBUNIT_PROMPT_HASH = _prompt_hash(SYSTEM_SUBUNITS, SUBUNITS_SCHEMA)
SUBUNIT_PROVIDER_ORDER = ("anthropic", "openai", "gemini")
SUBUNIT_MAX_SEARCHES = 5         # web searches per request (Anthropic max_uses, OpenAI max_tool_calls)
# The basic Anthropic search tool. The newer web_search_20260209 filters results by running
# code: on Stern it made 46 code-execution steps for 3 searches (552k input tokens, 808 s).
ANTHROPIC_SEARCH_TOOL = "web_search_20250305"


def subunit_tools(provider: str) -> list:
    """The search tool each provider gets, with the current search limit (Gemini has none)."""
    return {"openai": [{"type": "web_search_preview"}],
            "anthropic": [{"type": ANTHROPIC_SEARCH_TOOL, "name": "web_search", "max_uses": SUBUNIT_MAX_SEARCHES}],
            "gemini": [{"google_search": {}}]}[provider]
SUBUNIT_MAX_TOKENS = 16000
SUBUNIT_TIMEOUT_S = 600          # one request; a web search over a whole school takes minutes
_MAX_CONTINUATIONS = 3           # Anthropic pause_turn resumptions per request

last_subunit_provider: Optional[str] = None
last_subunit_call_ids: list = []


def subunit_input(unit: Dict[str, Any]) -> str:
    """The user message of a sub-unit request. Pure."""
    return (f"Unit: {unit.get('label')} ({unit.get('qid')})\n"
            f"Part of: {unit.get('parent_label') or 'none (this unit is the university)'}\n"
            f"Website: {unit.get('website') or 'unknown'}\n"
            f"Country on Wikidata (a first guess only): {unit.get('country') or 'unknown'}")


def json_with_units(text: str) -> Any:
    """The last JSON object in `text` that has a "units" key. The model may write a
    sentence or a code fence around it. Raises ValueError when there is none. Pure."""
    if not text:
        raise ValueError("empty response text")
    decoder, found = json.JSONDecoder(), None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "units" in obj:
            found = obj
    if found is None:
        raise ValueError("no JSON object with a 'units' key in the response")
    return found


def _http_url(value: Any) -> Optional[str]:
    v = value.strip() if isinstance(value, str) else ""
    return v if re.match(r"^https?://\S+$", v, re.I) else None


def normalize_subunits(payload: Any) -> List[Dict[str, Any]]:
    """Clean one answer: a known type ("other" otherwise), http(s) URLs only, one row
    per name, the in_scope value of Panos's rule, and a no_url flag. Pure."""
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        raise ValueError("expected an object with a 'units' list")
    out, seen = [], set()
    for u in payload["units"]:
        if not isinstance(u, dict) or not str(u.get("name") or "").strip():
            continue
        name = " ".join(str(u["name"]).split())
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        t = str(u.get("unit_type") or "").strip().lower()
        t = t if t in SUBUNIT_TYPES else "other"
        source = _http_url(u.get("source_url"))
        country_source = u.get("country_source")
        country_source = " ".join(country_source.split()) if isinstance(country_source, str) and country_source.strip() else None
        country = u.get("country")
        out.append({"name": name, "unit_type": t, "in_scope": SUBUNIT_IN_SCOPE[t],
                    "website": _http_url(u.get("website")), "source_url": source,
                    "country": country.strip() if isinstance(country, str) and country.strip() else None,
                    "country_source": country_source, "no_url": source is None})
    return out


def _subunit_timeout() -> float:
    left = config.seconds_left()
    return SUBUNIT_TIMEOUT_S if left is None else max(5.0, min(SUBUNIT_TIMEOUT_S, left))


def _subunit_requests(provider: str, model: str, user: str):
    """Send one sub-unit request. Yields (response, text) for every API response, so
    each is recorded before the next; the text of the last one holds the answer."""
    if provider == "openai":
        client = _get_openai_client().with_options(timeout=_subunit_timeout(), max_retries=0)
        resp = client.responses.create(
            model=model, input=[{"role": "system", "content": SYSTEM_SUBUNITS}, {"role": "user", "content": user}],
            # Without a forced search, gpt-4o answered Stern from memory (no search call).
            tools=subunit_tools("openai"), tool_choice={"type": "web_search_preview"},
            max_tool_calls=SUBUNIT_MAX_SEARCHES, max_output_tokens=SUBUNIT_MAX_TOKENS, store=False,
            text={"format": {"type": "json_schema", "name": "subunits", "schema": SUBUNITS_SCHEMA}})
        yield resp, (resp.output_text if resp.output else None)
    elif provider == "anthropic":
        # Streamed, so a long search never sits silent until the read timeout, and no SDK
        # retry: the attempt loop in _subunits_from retries and records every attempt.
        client = _get_anthropic_client().with_options(timeout=_subunit_timeout(), max_retries=0)
        messages = [{"role": "user", "content": user}]
        for _ in range(_MAX_CONTINUATIONS + 1):
            t0 = time.time()
            with client.messages.stream(model=model, max_tokens=SUBUNIT_MAX_TOKENS, system=SYSTEM_SUBUNITS,
                                        tools=subunit_tools("anthropic"), messages=messages) as stream:
                for event in stream:
                    block = getattr(event, "content_block", None)
                    if getattr(event, "type", None) == "content_block_start" and \
                            getattr(block, "type", None) == "server_tool_use":
                        logger.info("extract_subunits anthropic: web search started (%.0f s)", time.time() - t0)
                resp = stream.get_final_message()
            searches = getattr(getattr(getattr(resp, "usage", None), "server_tool_use", None), "web_search_requests", None)
            logger.info("extract_subunits anthropic: done in %.0f s, stop %s, %s searches, %s output tokens",
                        time.time() - t0, resp.stop_reason, searches, getattr(getattr(resp, "usage", None), "output_tokens", None))
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            yield resp, (text or None)
            if resp.stop_reason != "pause_turn":
                return
            # The server stopped its search loop early: send the turn back and it resumes.
            messages = messages + [{"role": "assistant", "content": resp.content}]
    elif provider == "gemini":
        from google.genai import types as genai_types
        client = _get_gemini_client()
        resp = client.models.generate_content(
            model=model, contents=f"{SYSTEM_SUBUNITS}\n\n{user}",
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                max_output_tokens=SUBUNIT_MAX_TOKENS,
                http_options=genai_types.HttpOptions(timeout=int(_subunit_timeout() * 1000))))
        yield resp, (resp.text or None)
    else:
        raise ValueError(f"unknown provider {provider!r}")


def _subunits_from(provider: str, unit: Dict[str, Any], mode: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One provider's sub-units for one unit, from the cache or a fresh call, and the
    llm_calls ids behind them. Raises ValueError when the provider has no key."""
    model = {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL}[provider]
    user = subunit_input(unit)
    request = {"tools": subunit_tools(provider), "max_tokens": SUBUNIT_MAX_TOKENS,
               "max_searches": SUBUNIT_MAX_SEARCHES if provider != "gemini" else None,
               "search_forced": provider == "openai"}
    # The key holds the whole request: unit, input, tools, and limits, so a run with
    # search never reads an answer made without it.
    key = _cache_key(unit.get("label") or "", provider, model, purpose="subunits", prompt_hash=SUBUNIT_PROMPT_HASH,
                     extra=json.dumps({"qid": unit.get("qid"), "input": user, **request}, sort_keys=True))
    call = {"provider": provider, "model": model, "purpose": "extract",
            "prompt": {"system": SYSTEM_SUBUNITS, "user": user, "schema": SUBUNITS_SCHEMA},
            "cache_key": key, "prompt_hash": SUBUNIT_PROMPT_HASH,
            "request_config": {"step": "subunits", "extraction_mode": mode, "sample": None, **request}}
    cached = _load_cache(key)
    if isinstance(cached, list):
        return cached, [i for i in [_record_cache_hit(call, key)] if i]
    if provider == "openai":
        require_key("OPENAI_API_KEY", config.OPENAI_API_KEY)
    elif provider == "anthropic":
        require_key("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY)
    else:
        require_key("GOOGLE_API_KEY", config.GOOGLE_API_KEY)

    for attempt in range(1, _EXTRACT_MAX_RETRIES + 1):
        if not enough_time_for_llm_call():
            logger.warning("extract_subunits: deadline too close, not calling %s for %s", provider, unit.get("label"))
            break
        call_ids, text, t0 = [], None, time.time()
        try:
            for n, (resp, text) in enumerate(_subunit_requests(provider, model, user)):
                part = call if n == 0 else {**call, "request_config": {**call["request_config"], "continuation": n}}
                call_ids.append(_record_response(part, resp, text, attempt, t0))
                t0 = time.time()
        except Exception as e:  # noqa: BLE001 - recorded, then the next attempt
            call_ids.append(_record_error(call, e, attempt, t0))
            logger.error("extract_subunits %s attempt %d/%d: API error for %s: %s",
                         provider, attempt, _EXTRACT_MAX_RETRIES, unit.get("label"), e)
            continue
        try:
            units = normalize_subunits(json_with_units(text or ""))
        except ValueError as e:
            logger.error("extract_subunits %s attempt %d/%d: no usable answer for %s: %s",
                         provider, attempt, _EXTRACT_MAX_RETRIES, unit.get("label"), e)
            continue
        _save_cache(key, units)
        _save_cache_meta(key, call_ids[-1])
        return units, [i for i in call_ids if i]
    return [], []
