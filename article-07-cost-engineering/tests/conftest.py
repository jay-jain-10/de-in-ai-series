"""Shared fakes: no network, no real keys."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def make_response(text='{"sentiment": "neutral"}', inp=400, out=50):
    return SimpleNamespace(content=[SimpleNamespace(text=text)],
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


class FakeAnthropic:
    """Scripted stand-in for anthropic.Anthropic."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return make_response()


@pytest.fixture
def fake_client():
    return FakeAnthropic()


@pytest.fixture
def cw_client():
    return MagicMock()
