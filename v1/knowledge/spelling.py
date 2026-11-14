"""Spelling correction against the curriculum's own vocabulary.

Students misspell subject words ("metosis", "photosinthesis"). Search on the
misspelt word finds nothing relevant, and the answer then wrongly reports the
topic as missing. Before answering, each longer word of the question that never
occurs in the active index is compared with the words that do:

- one match (within one edit, or two for words over seven letters, among words
  used often enough to be real curriculum terms): the question is answered
  with the corrected word, and the answer says so ("Showing results for
  'photosynthesis'");
- two or more equally close matches ("metosis": mitosis and meiosis are each one
  letter away): NEXA asks which was meant instead of guessing;
- no match: the question is left as it is.

The vocabulary comes from the index, so it is the curriculum's words in the
curriculum's spelling, and it is cached per index.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from . import config

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*[A-Za-z]|[A-Za-z]")
_CACHE: dict[str, tuple[float, Counter]] = {}
CACHE_TTL_S = 600


@dataclass
class Check:
    corrected: Optional[str] = None                     # the question with corrections applied
    corrections: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)

    def payload(self) -> dict:
        return {"corrections": self.corrections, "ambiguous": self.ambiguous}


def clear_cache() -> None:
    _CACHE.clear()


def vocabulary(texts: list[str]) -> Counter:
    return Counter(w.lower() for t in texts for w in _WORD.findall(t or "") if len(w) >= 3)


async def _vocab(index_mgr) -> Optional[Counter]:
    rec = await index_mgr.active()
    store = index_mgr.store
    if rec is None or not hasattr(store, "all_texts"):
        return None
    hit = _CACHE.get(rec.index_id)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        v = vocabulary(await store.all_texts(rec.index_id))
    except NotImplementedError:
        return None
    _CACHE[rec.index_id] = (time.time(), v)
    return v


def distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein (adjacent swaps count as one), stopping past limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            best = min(best, cur[j])
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[len(b)]


def _known(w: str, vocab: Counter) -> bool:
    return any(vocab.get(x, 0) for x in (w, w[:-1] if w.endswith("s") else w + "s", w[:-2] if w.endswith("es") else w))


def _same_family(a: str, b: str) -> bool:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= min(len(a), len(b)) - 3


def suggest(word: str, vocab: Counter) -> list[str]:
    """Best candidates for a word the curriculum never uses, best first; more
    than one means they are equally good."""
    w = word.lower()
    limit = 1 if len(w) <= 7 else 2
    scored = []
    for c, n in vocab.items():
        if n < config.SPELL_MIN_FREQ or abs(len(c) - len(w)) > limit:
            continue
        d = distance(w, c, limit)
        if d <= limit:
            scored.append((d, -n, c))
    if not scored:
        return []
    scored.sort()
    best_d = scored[0][0]
    out: list[str] = []
    for d, n, c in scored:
        if d != best_d:
            break
        # forms of one word (gene/genes, homeostasis/homeostatic) are not a real
        # choice: keep the more frequent form
        if any(_same_family(c, o) for o in out):
            continue
        out.append(c)
    return out[:3]


async def check(index_mgr, question: str) -> Check:
    out = Check()
    if not config.SPELLING:
        return out
    vocab = await _vocab(index_mgr)
    if not vocab:
        return out
    fixed = question
    for w in dict.fromkeys(_WORD.findall(question)):
        lw = w.lower()
        if len(lw) < config.SPELL_MIN_LEN or not lw.isalpha() or _known(lw, vocab):
            continue
        cands = suggest(lw, vocab)
        if len(cands) == 1:
            out.corrections[w] = cands[0]
            fixed = re.sub(rf"\b{re.escape(w)}\b", cands[0], fixed)
        elif len(cands) > 1:
            out.ambiguous[w] = cands
    if out.corrections:
        out.corrected = fixed
    return out


def clarification_text(chk: Check) -> str:
    parts = []
    for w, cands in chk.ambiguous.items():
        opts = " or ".join(f"'{c}'" for c in cands)
        parts.append(f"I could not find '{w}' in your curriculum materials. Did you mean {opts}?")
    return " ".join(parts) + " Please ask again with the word you meant."


def correction_line(chk: Check) -> str:
    fixes = ", ".join(f"'{c}' (you typed '{w}')" for w, c in chk.corrections.items())
    return f"Showing results for {fixes}."
