"""Local-daemon script tests must not read the developer's real HOME."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_daemon_home(request):
    # Only the local-daemon modules: other script tests own their HOME layout.
    if request.module.__name__.rpartition(".")[2].startswith(
        "test_conductor_local_daemon"
    ):
        request.getfixturevalue("isolated_daemon_home")
        request.getfixturevalue("daemon_claims_have_completion_contract")
