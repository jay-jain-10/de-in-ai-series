"""Golden-dataset tests for routing decisions (the article's 'testable logic')."""

import pytest

from src.router import KeywordAspectRetriever, ModelRouter, contains_keyword

SIMPLE = "This product is great. 5 stars."
COMPLEX = "The battery life is good but the software is buggy and the customer service refused to help when it broke."
LAWSUIT = "I will file a lawsuit over this."
LONG = " ".join(["word"] * 320)

GOLDEN_CLASSIFY = [
    (SIMPLE, "simple"),
    (LAWSUIT, "complex"),                       # keyword overrides short length
    ("This is explicit content.", "complex"),
    (LONG, "complex"),                          # > 300 words
    (" ".join(["fine"] * 99), "simple"),
]


@pytest.fixture
def router(config):
    return ModelRouter(config)


@pytest.mark.parametrize("text,expected", GOLDEN_CLASSIFY)
def test_pattern1_classification(router, text, expected):
    assert router.classify_complexity(text) == expected


def test_midlength_uses_score_threshold(router):
    mid = " ".join(["ok"] * 150)  # between 100 and 300 words, no keywords
    assert router.classify_complexity(mid) == "simple"  # score = 0.25 + 0.2 = 0.45 <= 0.6


def test_complexity_score_bounds_and_ordering(router):
    assert 0 <= router.complexity_score(SIMPLE) < router.complexity_score(LAWSUIT) <= 1
    assert router.complexity_score(LONG) == pytest.approx(0.5 + 0.2)  # 320 words, one 320-word sentence


def test_sentiment_simple_goes_to_haiku(router):
    d = router.decide("sentiment", SIMPLE)
    assert d.model == "haiku"
    assert "router_complexity_score=" in d.reason and "used_haiku_because <= 0.6" in d.reason


def test_sentiment_complex_goes_to_sonnet(router):
    text = LONG + " lawsuit discrimination"
    d = router.decide("sentiment", text)
    assert d.signals["complexity_score"] > 0.6
    assert d.model == "sonnet"


def test_aspect_high_similarity_uses_default_rag_model(router):
    d = router.decide("aspect_extraction", "battery and charging are great")  # 2 matches -> 1.0
    assert d.model == "haiku_with_rag" and d.signals["top_similarity"] >= 0.8


def test_aspect_low_similarity_escalates(router):
    d = router.decide("aspect_extraction", "meh")
    assert d.model == "sonnet_with_rag" and d.signals["top_similarity"] == 0.0


def test_aspect_threshold_boundary(config):
    class Fixed:
        def __init__(self, s): self.s = s
        def search(self, text, k=3): return [("battery", self.s)]
    assert ModelRouter(config, Fixed(0.8)).decide("aspect_extraction", "x").model == "haiku_with_rag"
    assert ModelRouter(config, Fixed(0.79)).decide("aspect_extraction", "x").model == "sonnet_with_rag"


@pytest.mark.parametrize("text,model", [
    ("I will find you and kill you", "opus"),
    ("This reviewer is racist", "opus"),
    ("Works fine, shipping was quick", "haiku"),
])
def test_toxicity_keyword_urgency(router, text, model):
    assert router.decide("toxicity_detection", text).model == model


def test_keyword_match_is_word_prefix_not_substring(router):
    assert contains_keyword("skilled worker", ["kill"]) == []
    assert contains_keyword("they will kill it", ["kill"]) == ["kill"]


def test_retriever_ranks_by_matches():
    r = KeywordAspectRetriever()
    hits = r.search("battery charging and price", k=2)
    assert hits[0][0] == "battery" and hits[0][1] == 1.0
    assert len(hits) == 2


def test_unknown_router_type_raises(config):
    config.tasks["sentiment"].router = "magic"
    with pytest.raises(ValueError):
        ModelRouter(config).decide("sentiment", "x")
