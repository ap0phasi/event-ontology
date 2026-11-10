import pytest

from helpers import Ledger


@pytest.fixture()
def ledger(tmp_path):
    return Ledger(tmp_path / "proj")
