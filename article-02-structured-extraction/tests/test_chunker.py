import json

from src.chunker import ContractChunker, _page_at, estimate_tokens, split_sentence_spans, split_sentences
from src.models import ExtractedText, PageText
from tests.conftest import FakeAnthropic, LIAB_TEXT, PAY_TEXT, TERM_TEXT


def extracted(*texts):
    pages = [PageText(page_number=i + 1, text=t, method="PyPDF2") for i, t in enumerate(texts)]
    return ExtractedText(pages=pages, extraction_method="PyPDF2", confidence_score=1.0)


def test_splits_at_haiku_markers_and_tracks_pages(config):
    ext = extracted(PAY_TEXT, TERM_TEXT, LIAB_TEXT)
    sections = [
        {"title": "Payment Terms", "start_marker": "Payment. Client shall pay"},
        {"title": "Termination", "start_marker": "Termination. Either party"},
        {"title": "Liability", "start_marker": "Liability. Liability is capped"},
    ]
    client = FakeAnthropic(lambda m, p: json.dumps(sections))
    chunker = ContractChunker(client, config)
    chunks = chunker.chunk(ext)
    titles = [c.title for c in chunks]
    assert titles == ["Preamble", "Payment Terms", "Termination", "Liability"]
    pay = next(c for c in chunks if c.title == "Payment Terms")
    assert pay.start_page == 1 and "30 days" in pay.text
    assert next(c for c in chunks if c.title == "Liability").start_page == 3
    assert client.calls[0]["model"] == config.anthropic.segmentation_model  # Haiku
    assert not chunker.used_fallback


def test_no_text_lost_between_sections(config):
    ext = extracted(PAY_TEXT, TERM_TEXT)
    client = FakeAnthropic(lambda m, p: json.dumps(
        [{"title": "Termination", "start_marker": "Termination. Either party"}]))
    chunks = ContractChunker(client, config).chunk(ext)
    assert chunks[0].title == "Preamble" and "Payment" in chunks[0].text


def test_invalid_json_triggers_fallback(config):
    client = FakeAnthropic(lambda m, p: "sorry, no JSON")
    chunker = ContractChunker(client, config)
    chunks = chunker.chunk(extracted(PAY_TEXT, TERM_TEXT))
    assert chunker.used_fallback and chunks
    assert all(c.title.startswith("Segment") for c in chunks)


def test_unlocatable_markers_trigger_fallback(config):
    client = FakeAnthropic(lambda m, p: json.dumps([{"title": "X", "start_marker": "does not exist anywhere"}]))
    chunker = ContractChunker(client, config)
    chunker.chunk(extracted(PAY_TEXT))
    assert chunker.used_fallback


def test_api_error_triggers_fallback(config):
    def boom(m, p):
        raise RuntimeError("down")
    chunker = ContractChunker(FakeAnthropic(boom), config)
    assert chunker.chunk(extracted(PAY_TEXT))
    assert chunker.used_fallback


def test_sentence_splitter_respects_token_limit():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    pieces = split_sentences(text, 100)
    assert len(pieces) > 1
    assert all(estimate_tokens(p) <= 110 for p in pieces)


def test_oversized_section_is_subsplit(config):
    config.max_chunk_tokens = 50  # limit 200 tokens = 800 chars per section
    big = " ".join(f"Clause sentence {i} text." for i in range(200))
    client = FakeAnthropic(lambda m, p: json.dumps([{"title": "Big", "start_marker": "Clause sentence 0"}]))
    chunks = ContractChunker(client, config).chunk(extracted(big))
    assert len(chunks) > 1 and any("part 2" in c.title for c in chunks)


def test_page_at_counts_marker_starting_at_offset():
    text = "[[PAGE 1]]\nabc.\n\n[[PAGE 2]]\nxyz."
    assert _page_at(text, text.index("[[PAGE 2]]")) == 2
    assert _page_at(text, text.index("[[PAGE 2]]") - 1) == 1
    assert _page_at(text, 0) == 1


def test_split_spans_are_exact_offsets_and_lose_no_content():
    text = "First one here.\n\nSecond is here!  Third; fourth?\nFifth."
    spans = split_sentence_spans(text, 5)
    assert len(spans) > 1
    assert all(text[a:b] == text[a:b].strip() and text[a:b] for a, b in spans)
    # every non-whitespace char is covered exactly once, in order
    assert " ".join(text[a:b] for a, b in spans).split() == text.split()
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


def test_fallback_chunks_report_correct_pages(config):
    """Repeated boilerplate across pages used to make the fallback mis-locate chunks."""
    config.max_chunk_tokens = 40
    s = "Sentence number one here is long enough.\n\nSecond sentence is also present."
    ext = extracted(*([" ".join([s] * 3)] * 3))
    chunker = ContractChunker(FakeAnthropic(lambda m, p: "not json"), config)
    chunks = chunker.chunk(ext)
    assert chunker.used_fallback
    for c in chunks:
        on_pages = [int(n) for n in __import__("re").findall(r"\[\[PAGE (\d+)\]\]", c.text)]
        if c.text.startswith("[[PAGE"):
            assert c.start_page == on_pages[0]
        if on_pages:
            assert c.end_page == on_pages[-1]
    assert chunks[0].start_page == 1 and chunks[-1].end_page == 3
    assert sorted({c.start_page for c in chunks}) == [1, 2, 3]


def test_subsplit_parts_get_their_own_pages(config):
    config.max_chunk_tokens = 25  # sections > 100 tokens are sub-split
    body = " ".join(f"Clause sentence {i} text." for i in range(25))
    ext = extracted(body, body, body)
    client = FakeAnthropic(lambda m, p: json.dumps([{"title": "All", "start_marker": "Clause sentence 0"}]))
    chunks = ContractChunker(client, config).chunk(ext)
    assert len(chunks) > 3
    assert chunks[0].start_page == 1 and chunks[-1].start_page == 3 and chunks[-1].end_page == 3
