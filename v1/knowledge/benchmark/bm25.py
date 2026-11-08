"""Okapi BM25, dependency-free, for lexical retrieval benchmarking.

The M2-003 benchmark compares chunking strategies with the retriever held
fixed. BM25 needs no model, so it runs in CI and on any laptop; the same gold
set can be re-run with dense embeddings through the Model Gateway on the DGX.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_TOK = re.compile(r"[a-z0-9]+")
_STOP = set("""a an and are as at be by for from has have in is it its of on or that the to was were with which
this these those what how why when where who whom does do did can will shall into than then there their they them
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOK.findall(text.lower()) if t not in _STOP and len(t) > 1]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lens = [sum(c.values()) for c in self.docs]
        self.avg = sum(self.lens) / max(len(self.lens), 1)
        df: Counter = Counter()
        for c in self.docs:
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        q = tokenize(query)
        scores: list[tuple[int, float]] = []
        for i, c in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = c.get(t)
                if not f:
                    continue
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: (-x[1], x[0]))
        return scores[:k]
