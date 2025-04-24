
import requests
from typing import List, Tuple
from config import USER_AGENT

def quick_wd_search(label: str) -> List[Tuple[str, str]]:
    url = (
        "https://www.wikidata.org/w/api.php?"
        "action=wbsearchentities&format=json&language=en&limit=10&search="
        + requests.utils.quote(label)
    )
    hits = requests.get(url, headers={"User-Agent": USER_AGENT}).json().get("search", [])
    return [(h["id"], h["label"]) for h in hits]
