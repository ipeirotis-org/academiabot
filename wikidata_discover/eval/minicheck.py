"""
MiniCheck, the cheap yes-or-no checker of the week 6 verifier (Shuo; see SPECS.md).

Model: lytang/MiniCheck-Flan-T5-Large (Tang, Laban and Durrett, EMNLP 2024), run on the CPU.
It reads a document and a claim and gives the probability that the document supports the
claim. As in the MiniCheck package, a long document is split into pieces of about
CHUNK_WORDS words, each piece is scored, and the highest probability counts. 0.5 or more
counts as supported.

It needs two extra packages that the main requirements leave out (about 1 GB):
    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install transformers sentencepiece
"""

from typing import List, Optional

MODEL = "lytang/MiniCheck-Flan-T5-Large"
CHUNK_WORDS = 400
CHUNK_OVERLAP_WORDS = 50
MAX_TOKENS = 2048
NO_ID, YES_ID = 3, 209   # the decoder tokens "0" (no support) and "1" (support), as in the MiniCheck package


def word_chunks(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> List[str]:
    """Overlapping pieces of `size` words. An empty text gives one empty piece. Pure."""
    words = (text or "").split()
    if len(words) <= size:
        return [" ".join(words)]
    step = size - overlap
    out = []
    for start in range(0, len(words), step):
        out.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return out


class MiniCheck:
    """Loads the model once. score(document, claim) gives the highest support probability
    over the document's pieces."""

    def __init__(self, model: str = MODEL, batch_size: int = 8, cache_dir: Optional[str] = None):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model, cache_dir=cache_dir)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model, cache_dir=cache_dir)
        self.model.eval()
        self.batch_size = batch_size

    def chunk_probs(self, chunks: List[str], claim: str) -> List[float]:
        torch = self.torch
        eos = self.tokenizer.eos_token
        probs: List[float] = []
        for i in range(0, len(chunks), self.batch_size):
            batch = ["predict: " + eos.join([c, claim]) for c in chunks[i:i + self.batch_size]]
            enc = self.tokenizer(batch, max_length=MAX_TOKENS, truncation=True, padding=True, return_tensors="pt")
            with torch.no_grad():
                dec = torch.zeros((enc["input_ids"].size(0), 1), dtype=torch.long)
                logits = self.model(input_ids=enc["input_ids"], attention_mask=enc["attention_mask"],
                                    decoder_input_ids=dec).logits.squeeze(1)
                p = torch.softmax(logits[:, torch.tensor([NO_ID, YES_ID])], dim=-1)[:, 1]
            probs.extend(float(x) for x in p)
        return probs

    def score(self, document: str, claim: str) -> float:
        return max(self.chunk_probs(word_chunks(document), claim))
