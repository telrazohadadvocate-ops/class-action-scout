"""
Semantic deduplication using Voyage AI embeddings.

Prevents the same lawsuit from appearing multiple times under different
wording (e.g. "Dove Sensitive Body Wash" vs "Unilever Dove hypoallergenic").
"""
import math
import logging
import unicodedata

try:
    import numpy as _np
except ImportError:
    _np = None

logger = logging.getLogger("scout.dedup")


# ── Exact-title rule ──────────────────────────────────────────────────────
# The embedding text is "company | title | israeli_law_basis", so two leads
# with a byte-identical title and the same company still score below the
# auto-merge floor when their law-basis text differs — the noise sits in a
# field that has nothing to do with whether it is the same lawsuit. Those
# pairs were filling the manual review queue. Identical title + identical
# company is decided on the text itself, before any embedding score.
#
# Normalization keeps Unicode letters and digits only (so Hebrew and English
# both work) and drops case, punctuation, symbols, combining marks and all
# whitespace.

MIN_TITLE_KEY_LEN = 8   # normalized chars; below this a title is too generic
                        # to auto-merge on (e.g. "עדכון" / "Settlement") and we
                        # fall through to the embedding tiers instead.


def normalize_for_match(text: str) -> str:
    """Lowercase, strip punctuation/symbols/marks/whitespace, keep letters+digits."""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKC", text).casefold():
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            out.append(ch)
    return "".join(out)


def title_match_key(title: str, company: str):
    """
    Key identifying "same headline, same company", or None when the title is
    missing or too short to be decisive on its own.

    Compares the FULL title — never a truncated display string.
    """
    t = normalize_for_match(title)
    if len(t) < MIN_TITLE_KEY_LEN:
        return None
    return (t, normalize_for_match(company))


class SemanticDeduplicator:
    def __init__(self, api_key: str, threshold: float = 0.85):
        self.threshold = threshold
        self._client = None

        if not api_key:
            logger.info("Semantic dedup disabled — no VOYAGE_API_KEY")
            return
        try:
            import voyageai
            self._client = voyageai.Client(api_key=api_key)
            logger.info(f"Semantic deduplicator ready (threshold={threshold})")
        except ImportError:
            logger.warning("voyageai package not installed — semantic dedup disabled")
        except Exception as e:
            logger.warning(f"Voyage AI init failed: {e} — semantic dedup disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def compute_embedding(self, company: str, title: str, israeli_law_basis: str) -> list:
        if not self._client:
            return []
        text = f"{company} | {title} | {israeli_law_basis}"
        result = self._client.embed([text], model="voyage-3")
        return result.embeddings[0]

    def compute_embeddings_batch(self, texts: list) -> list:
        if not self._client or not texts:
            return [[] for _ in texts]
        result = self._client.embed(texts, model="voyage-3")
        return result.embeddings

    @staticmethod
    def cosine_similarity(a: list, b: list) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def find_duplicate(self, new_embedding: list, existing_leads_with_embeddings: list):
        """
        Return (lead, score) of the most similar existing lead if score >= threshold,
        else (None, 0.0).
        """
        if not new_embedding:
            return None, 0.0
        best_lead = None
        best_score = 0.0
        for lead, emb in existing_leads_with_embeddings:
            score = self.cosine_similarity(new_embedding, emb)
            if score > best_score:
                best_score = score
                best_lead = lead
        if best_score >= self.threshold:
            return best_lead, best_score
        return None, 0.0

    # ── Vectorized path (numpy) ───────────────────────────────────────────
    # find_duplicate is O(n) pure Python per call; over thousands of stored
    # embeddings that is far too slow. build_index precomputes a matrix once,
    # and find_duplicate_indexed does a single vectorized matvec per query.

    def build_index(self, leads_with_embs: list):
        """Return (leads, matrix, norms) for fast similarity, or None if numpy
        is unavailable / the input is empty."""
        if _np is None or not leads_with_embs:
            return None
        leads = [l for l, _ in leads_with_embs]
        mat = _np.asarray([e for _, e in leads_with_embs], dtype=_np.float32)
        norms = _np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1e-12
        return (leads, mat, norms)

    def find_duplicate_indexed(self, query_emb: list, index):
        """Vectorized nearest-neighbour against a prebuilt index. Returns
        (lead, score) if the best cosine >= threshold, else (None, 0.0)."""
        if index is None or not query_emb:
            return None, 0.0
        leads, mat, norms = index
        q = _np.asarray(query_emb, dtype=_np.float32)
        qn = float(_np.linalg.norm(q))
        if qn == 0:
            return None, 0.0
        sims = mat.dot(q) / (norms * qn)
        i = int(sims.argmax())
        best = float(sims[i])
        if best >= self.threshold:
            return leads[i], best
        return None, 0.0
