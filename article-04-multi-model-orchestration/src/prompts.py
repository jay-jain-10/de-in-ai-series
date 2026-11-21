"""Prompt templates for the three review tasks."""

SENTIMENT_PROMPT = """You are a product review sentiment classifier.
Return ONLY a JSON object: {{"sentiment": "positive|negative|neutral", "confidence": 0.0-1.0, "reasoning": "<max 100 words>"}}

Review: {text}"""

ASPECT_PROMPT = """You extract product aspects (features) mentioned in a review.
Sentiment context from the upstream model: {sentiment_context}
Known aspects found by vector search (extract ONLY aspects from this list): {known_aspects}
Return ONLY a JSON object: {{"aspects": [..], "unsafe_language": true|false, "confidence": 0.0-1.0, "reasoning": "<max 100 words>"}}

Review: {text}"""

TOXICITY_PROMPT = """You are a safety classifier detecting toxic or abusive content in product reviews.
Return ONLY a JSON object: {{"toxic": true|false, "categories": [..], "confidence": 0.0-1.0, "reasoning": "<max 100 words>"}}

Review: {text}"""

PROMPTS = {
    "sentiment": SENTIMENT_PROMPT,
    "aspect_extraction": ASPECT_PROMPT,
    "toxicity_detection": TOXICITY_PROMPT,
}
