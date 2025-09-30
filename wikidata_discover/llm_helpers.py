import json
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI
from config import OPENAI_API_KEY, LLM_MODEL
from rich.console import Console

client = OpenAI(api_key=OPENAI_API_KEY)
console = Console()

# ─────────────────────────  LLM PROMPTS  ─────────────────────────
SYSTEM_EXTRACT = (
    "You are an education‑data analyst. Given the name of a university and (optionally) its website URL, return a JSON "
    "key `units` whose value is an *array* of objects, each describing a *top‑level* "
    "academic or administrative unit (school, college, faculty, division, or campus). "
    "Each object *must* include: name, unit_type, city, state, website. Use null if a "
    "value is unknown. Do not list departments or research centers."
    "Provide also a URL as a reference so that someone can validate the information. The key for the reference URL should be 'reference'."
    "You should double check that reference URL exists and contains the supporting information for the existence of the units."
)

MATCH_TEMPLATE = (
    "You are assisting with entity alignment to Wikidata. Below is the name of a "
    "candidate academic unit *CANDIDATE* from UNIVERSITY, followed by a numbered "
    "list of existing descendant units from Wikidata, each labelled `[n] QID — LABEL`.\n\n"
    "If the candidate is equivalent to a listed unit **that already has** a parent-"
    "link to UNIVERSITY, reply with that QID.\n"
    "If it matches a listed unit but that unit is **missing** the parent link, "
    "reply `ORPHAN:QID`.\n"
    "If none match, reply `NONE`.\n"
    "*Return that single token only — no explanation.*"
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

class LLMHelper:
    @staticmethod
    def extract_divisions(univ_label: str, website: str) -> List[Dict[str, Any]]:
        """Extract top-level academic/administrative units for a university."""
        messages = [
            {"role": "developer", "content": [{"type": "input_text", "text": SYSTEM_EXTRACT}]},
            {"role": "user",      "content": [{"type": "input_text", "text": f"{univ_label} -- {website}"}]}
        ]

        resp = client.responses.create(
            model=LLM_MODEL,
            input=messages,
            text={"format": {"type": "json_schema", "name": "university_units", "strict": True, "schema": UNIVERSITY_UNITS_SCHEMA}},
            reasoning={"effort": "high"},
            tools=[],
            store=False
        )

        payload = resp.choices[0].message.content or {}
        units = payload.get("units")
        if not isinstance(units, list):
            console.print("[red]LLM JSON did not contain expected `units` list.[/red]")
            return []

        # Normalize entries: wrap bare strings into dicts
        return [ {"name": itm} if isinstance(itm, str) else itm for itm in units ]

    @staticmethod
    def choose_match(candidate: str, univ_label: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Return (qid,label) if GPT says the candidate matches one of the children, else None."""

        # Compose numbered list for prompt
        listing_lines = [
            f"[{i+1}] {qid} — {label}" for i, (qid, label) in enumerate(children)
        ]
        listing = "\n".join(listing_lines)

        prompt = (
            MATCH_TEMPLATE.replace("CANDIDATE", candidate).replace(
                "UNIVERSITY", univ_label
            )
            + "\n\nExisting units:\n"
            + listing
        )

        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=6,
        )
        answer = resp.choices[0].message.content.strip()
        if answer.upper() == "NONE":
            return None
        # look up answer among children
        answer = answer.split()[0]  # just in case extra text
        for qid, label in children:
            if qid == answer:
                return qid, label
        return None
