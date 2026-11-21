"""Stage 3: Intelligent chunking.

Haiku identifies the major contract sections ("Payment Terms", "Termination",
"Liability"...) and returns a start marker for each. We split the text at the
markers. If Haiku fails or returns garbage, we fall back to a ~1000-token
sentence-boundary splitter so the pipeline degrades instead of failing.
"""

import logging
import re
from typing import List, Optional, Tuple

from src.config import PipelineConfig
from src.llm_utils import UsageTracker, call_claude, parse_json
from src.models import Chunk, ExtractedText

logger = logging.getLogger(__name__)

SEGMENTATION_PROMPT = """Identify the major sections of this contract (e.g., 'Payment Terms', 'Termination', 'Liability').
Return section boundaries as a JSON array. Each element must have:
- "title": short section name
- "start_marker": the EXACT first 40-80 characters of the section, copied verbatim from the text

Sections must be listed in document order and must not overlap.
Return ONLY valid JSON, no markdown.

Example:
[{{"title": "Payment Terms", "start_marker": "3. Payment. Client shall pay all undisputed invoices within"}}]

Contract text:
{text}"""

_PAGE_RE = re.compile(r"\[\[PAGE (\d+)\]\]")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _page_at(text: str, offset: int, first_page: int = 1) -> int:
    """Page number in effect at `offset`: the last [[PAGE n]] marker starting at or before it.

    (A marker that *starts* at `offset` counts, so a chunk beginning exactly at
    "[[PAGE 3]]" is on page 3, not page 2.)
    """
    page = first_page
    for m in _PAGE_RE.finditer(text):
        if m.start() > offset:
            break
        page = int(m.group(1))
    return page


def split_sentence_spans(text: str, max_tokens: int) -> List[Tuple[int, int]]:
    """Greedy sentence packing up to max_tokens per piece.

    Returns (start, end) offsets into `text` so callers can map every piece
    back to its exact position (and hence its page) without re-searching.
    """
    bounds = [0] + [m.end() for m in re.finditer(r"(?<=[.;!?])\s+", text)] + [len(text)]
    spans: List[Tuple[int, int]] = []
    start = None
    for a, b in zip(bounds, bounds[1:]):
        if a == b:
            continue
        if start is not None and estimate_tokens(text[start:b].strip()) > max_tokens:
            spans.append((start, a))
            start = a
        elif start is None:
            start = a
    if start is not None and text[start:].strip():
        spans.append((start, len(text)))
    # Trim surrounding whitespace so offsets point at real content.
    out = []
    for a, b in spans:
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        out.append((a + lead, a + len(seg.rstrip())))
    return [(a, b) for a, b in out if b > a]


def split_sentences(text: str, max_tokens: int) -> List[str]:
    """Greedy sentence packing up to max_tokens per piece (text only)."""
    return [text[a:b] for a, b in split_sentence_spans(text, max_tokens)]


class ContractChunker:
    def __init__(self, client, config: PipelineConfig = None, tracker: Optional[UsageTracker] = None):
        self.client = client
        self.config = config or PipelineConfig()
        self.tracker = tracker or UsageTracker()
        self.used_fallback = False

    def _segment_with_haiku(self, text: str) -> List[dict]:
        prompt = SEGMENTATION_PROMPT.format(text=text[: self.config.max_segmentation_chars])
        raw = call_claude(
            self.client, self.config.anthropic.segmentation_model, prompt,
            self.config.anthropic.segmentation_max_tokens, self.tracker,
            self.config.anthropic.max_retries,
        )
        sections = parse_json(raw)
        if not isinstance(sections, list) or not sections:
            raise ValueError("segmentation returned no sections")
        return sections

    def chunk(self, extracted: ExtractedText) -> List[Chunk]:
        text = extracted.full_text
        try:
            sections = self._segment_with_haiku(text)
            chunks = self._split_at_markers(text, sections)
            if not chunks:
                raise ValueError("no markers could be located in text")
        except Exception as e:
            logger.warning(f"Haiku segmentation failed ({e}); using sentence-boundary fallback")
            self.used_fallback = True
            chunks = self._fallback(text)
        return self._enforce_size(chunks)

    # -- helpers -------------------------------------------------------------
    def _split_at_markers(self, text: str, sections: List[dict]) -> List[Chunk]:
        located = []
        cursor = 0
        for s in sections:
            marker = (s.get("start_marker") or "").strip()
            if not marker:
                continue
            idx = text.find(marker, cursor)
            if idx == -1:
                logger.debug(f"Marker not found for section {s.get('title')}")
                continue
            located.append((idx, s.get("title", "Untitled")))
            cursor = idx + 1
        if not located:
            return []
        # Preamble before the first section is kept so no text is lost.
        if located[0][0] > 0:
            located.insert(0, (0, "Preamble"))
        chunks = []
        for i, (start, title) in enumerate(located):
            end = located[i + 1][0] if i + 1 < len(located) else len(text)
            body = text[start:end].strip()
            if not body:
                continue
            chunks.append(Chunk(
                chunk_id=len(chunks), title=title, text=body,
                start_page=_page_at(text, start), end_page=_page_at(text, max(end - 1, start)),
            ))
        return chunks

    def _fallback(self, text: str) -> List[Chunk]:
        chunks = []
        for a, b in split_sentence_spans(text, self.config.max_chunk_tokens):
            chunks.append(Chunk(
                chunk_id=len(chunks), title=f"Segment {len(chunks) + 1}", text=text[a:b],
                start_page=_page_at(text, a), end_page=_page_at(text, b - 1),
            ))
        return chunks

    def _enforce_size(self, chunks: List[Chunk]) -> List[Chunk]:
        """Oversized sections are sub-split on sentence boundaries."""
        limit = self.config.max_chunk_tokens * 4  # sections may be larger than the fallback size
        out: List[Chunk] = []
        for c in chunks:
            if estimate_tokens(c.text) <= limit:
                out.append(c.model_copy(update={"chunk_id": len(out)}))
                continue
            for j, (a, b) in enumerate(split_sentence_spans(c.text, limit)):
                out.append(c.model_copy(update={
                    "chunk_id": len(out), "title": f"{c.title} (part {j + 1})", "text": c.text[a:b],
                    # page markers inside the parent chunk locate each part
                    "start_page": _page_at(c.text, a, c.start_page),
                    "end_page": _page_at(c.text, b - 1, c.start_page),
                }))
        return out
