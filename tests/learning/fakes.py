"""A fake embedding provider with two meanings (plants, volcanoes), so search by meaning can be tested offline:
"vegetation" lands next to "photosynthesis" without any network."""

import re

PLANTS = {
    "plant",
    "plants",
    "photosynthesis",
    "chlorophyll",
    "leaf",
    "leaves",
    "green",
    "sunlight",
    "sugar",
    "food",
    "vegetation",
    "feed",
}
VOLCANOES = {"volcano", "volcanoes", "magma", "lava", "eruption", "erupt", "erupts", "molten"}


def vector(text: str) -> list[float]:
    words = re.findall(r"[a-z]+", text.lower())
    return [float(sum(w in group for w in words)) for group in (PLANTS, VOLCANOES)]


class FakeEmbedder:
    """Two meanings: plants and volcanoes. Text about neither is a zero vector, similar to nothing."""

    model = "fake:concepts"
    dimensions = 2

    def __init__(self):
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.fail = False

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("provider down")
        self.documents += texts
        return [vector(t) for t in texts]

    def embed_query(self, text):
        if self.fail:
            raise RuntimeError("provider down")
        self.queries.append(text)
        return vector(text)
