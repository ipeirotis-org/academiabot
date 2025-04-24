import json
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI
from config import OPENAI_API_KEY, LLM_MODEL
from rich.console import Console

client = OpenAI(api_key=OPENAI_API_KEY)
console = Console()

# ─────────────────────────  LLM PROMPTS  ─────────────────────────
SYSTEM_EXTRACT = (
    "You are an education‑data analyst. Given the name of a university, return a JSON "
    "key `units` whose value is an *array* of objects, each describing a *top‑level* "
    "academic or administrative unit (school, college, faculty, division, or campus). "
    "Each object *must* include: name, unit_type, city, state, website. Use null if a "
    "value is unknown. Do not list departments or research centers."
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



class LLMHelper:
    
    @staticmethod
    def extract_divisions(univ_label: str) -> List[Dict[str, Any]]:
        """Call GPT to get a list of divisions; returns a list even on malformed JSON."""
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_EXTRACT},
                {"role": "user", "content": univ_label},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            console.print(f"[red]LLM JSON parse error: {e}\nRaw content:{raw}[/red]")
            return []
    
        # Accept both bare list or wrapped under common keys
        if isinstance(payload, list):
            units = payload
        elif isinstance(payload, dict):
            for key in ("units", "divisions", "schools"):
                if key in payload and isinstance(payload[key], list):
                    units = payload[key]
                    break
            else:
                console.print("[red]LLM JSON did not contain expected `units` list.[/red]")
                return []
        else:
            return []
    
        # Ensure list of dicts
        clean: List[Dict[str, Any]] = []
        for itm in units:
            if isinstance(itm, str):
                clean.append({"name": itm})
            elif isinstance(itm, dict):
                clean.append(itm)
        return clean
    
    @staticmethod
    def choose_match(candidate: str, univ_label: str, children: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Return (qid,label) if GPT says the candidate matches one of the children, else None."""
    
        # Compose numbered list for prompt
        listing_lines = [f"[{i+1}] {qid} — {label}" for i, (qid, label) in enumerate(children)]
        listing = "\n".join(listing_lines)
    
        prompt = (
            MATCH_TEMPLATE
            .replace("CANDIDATE", candidate)
            .replace("UNIVERSITY", univ_label)
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
