"""Shared test setup. Run the tests with:  pytest"""

import pytest

from app import convert


@pytest.fixture(autouse=True)
def wav_plays_as_it_is(monkeypatch, request):
    """Most tests use small WAV files as stand-ins for any recording that
    plays fine. The real app converts WAVs (they're big, and podcast apps
    can't play them), so here WAV is left out of the converted formats,
    except in tests marked ``wav_converts``."""
    if request.node.get_closest_marker("wav_converts") is None:
        monkeypatch.setattr(convert, "CONVERT_EXTENSIONS", convert.CONVERT_EXTENSIONS - {".wav"})


def pytest_configure(config):
    config.addinivalue_line("markers", "wav_converts: WAV files are converted, as in the real app")
