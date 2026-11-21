"""Prompt registry: versioned prompts per task, so lineage can say *which* prompt ran."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str
    domain: str = "platform"


class PromptRegistry:
    def __init__(self):
        self._prompts: dict[str, dict[str, Prompt]] = {}

    def register(self, prompt: Prompt) -> None:
        self._prompts.setdefault(prompt.name, {})[prompt.version] = prompt

    def get(self, name: str, version: str = "") -> Prompt:
        versions = self._prompts[name]
        if version:
            return versions[version]
        latest = max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
        return versions[latest]

    def catalog(self) -> list[dict]:
        return [{"name": p.name, "version": p.version, "domain": p.domain}
                for vs in self._prompts.values() for p in vs.values()]


_JSON_ONLY = "Return ONLY valid JSON. No markdown, no explanation outside the JSON."


def default_registry() -> PromptRegistry:
    reg = PromptRegistry()
    reg.register(Prompt("extract", "1.0", (
        "Extract the following fields from the document. Use null when a field is absent.\n"
        "Fields (name: description):\n{schema}\n\nDocument:\n{text}\n\n"
        'Return JSON: {{"fields": {{<field>: <value>}}, "confidence": <0.0-1.0>}}. ' + _JSON_ONLY)))
    reg.register(Prompt("classify", "1.0", (
        "Classify the text into exactly one of these categories: {categories}.\n\nText:\n{text}\n\n"
        'Return JSON: {{"category": <one of the categories>, "confidence": <0.0-1.0>, '
        '"reasoning": <max 50 words>}}. ' + _JSON_ONLY)))
    reg.register(Prompt("summarize", "1.0", (
        "Summarize the text in at most {max_words} words.\n\nText:\n{text}\n\n"
        'Return JSON: {{"summary": <string>}}. ' + _JSON_ONLY)))
    reg.register(Prompt("validate", "1.0", (
        "You are a semantic data-quality validator. Check this record against the rules. "
        "Look for contradictions, implausible values and domain inconsistencies.\n"
        "Rules:\n{rules}\n\nRecord:\n{record}\n\n"
        'Return JSON: {{"valid": <bool>, "issues": [<string>], "confidence": <0.0-1.0>}}. '
        + _JSON_ONLY)))
    reg.register(Prompt("score", "1.0", (
        "Score the record from 0.0 to 1.0 using this logic: {scoring_logic}\n\nRecord:\n{record}\n\n"
        'Return JSON: {{"score": <0.0-1.0>, "rationale": <max 50 words>}}. ' + _JSON_ONLY)))
    return reg
