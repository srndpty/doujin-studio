from __future__ import annotations

import pytest

from tests.helpers import create_stub_project, latest_revision, make_png_bytes, make_stub_client


@pytest.fixture
def make_client():
    return make_stub_client


@pytest.fixture
def create_project():
    return create_stub_project


@pytest.fixture
def current_revision():
    return latest_revision


@pytest.fixture
def png_bytes():
    return make_png_bytes
