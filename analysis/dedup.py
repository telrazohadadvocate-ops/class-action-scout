"""
Semantic deduplication using Voyage AI embeddings.

Prevents the same lawsuit from appearing multiple times under different
wording (e.g. "Dove Sensitive Body Wash" vs "Unilever Dove hypoallergenic").
"""
import math
import logging

logger = logging.getLogger("scout.dedup")


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
