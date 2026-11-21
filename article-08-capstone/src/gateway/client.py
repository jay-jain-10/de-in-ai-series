"""SDK for business teams: `gateway.extract(text, schema=...)`.

Works with any httpx-style client (httpx.Client in prod, FastAPI TestClient in tests).
"""

from typing import Any, Optional

from ..contracts import CallerMetadata, GatewayResponse


class GatewayError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"gateway returned {status}: {detail}")
        self.status, self.detail = status, detail


class GatewayClient:
    def __init__(self, http, api_key: str, team: str, pipeline: str, base_path: str = "/v1"):
        self._http, self._key, self.team, self.pipeline, self._base = http, api_key, team, pipeline, base_path

    def _post(self, path: str, task_type: str, body: dict) -> GatewayResponse:
        body = {**body, "caller": CallerMetadata(team=self.team, pipeline=self.pipeline,
                                                 task_type=task_type).model_dump()}
        r = self._http.post(f"{self._base}/{path}", json=body, headers={"X-API-Key": self._key})
        if r.status_code != 200:
            raise GatewayError(r.status_code, r.json() if r.content else None)
        return GatewayResponse(**r.json())

    def extract(self, text: str, schema: dict[str, str], task_type: str = "extract") -> GatewayResponse:
        return self._post("extract", task_type, {"text": text, "extraction_schema": schema})

    def classify(self, text: str, categories: list[str], task_type: str = "classify") -> GatewayResponse:
        return self._post("classify", task_type, {"text": text, "categories": categories})

    def summarize(self, text: str, max_words: int = 100, task_type: str = "summarize") -> GatewayResponse:
        return self._post("summarize", task_type, {"text": text, "max_words": max_words})

    def validate(self, record: dict, rules: list[str], task_type: str = "validate") -> GatewayResponse:
        return self._post("validate", task_type, {"record": record, "rules": rules})

    def score(self, record: dict, scoring_logic: str, complexity: str = "low",
              task_type: str = "score") -> GatewayResponse:
        return self._post("score", task_type, {"record": record, "scoring_logic": scoring_logic,
                                               "complexity": complexity})
