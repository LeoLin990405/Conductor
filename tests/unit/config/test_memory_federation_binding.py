import pytest
from pydantic import ValidationError

from src.config.models import MemoryFederationUserBinding


def test_tenant_may_be_set_with_a_remote_url():
    binding = MemoryFederationUserBinding(
        memfw_url="http://memfw:18080", memfw_tenant="acme"
    )
    assert binding.memfw_tenant == "acme"
    assert binding.memfw_url == "http://memfw:18080"


def test_tenant_of_exactly_120_characters_is_accepted():
    binding = MemoryFederationUserBinding(
        memfw_url="http://memfw:18080", memfw_tenant="t" * 120
    )
    assert binding.memfw_tenant == "t" * 120


def test_tenant_longer_than_120_characters_is_rejected():
    with pytest.raises(ValidationError, match="120 characters"):
        MemoryFederationUserBinding(
            memfw_url="http://memfw:18080", memfw_tenant="t" * 121
        )


def test_tenant_requires_the_remote_url():
    with pytest.raises(ValidationError, match="requires memfw_url"):
        MemoryFederationUserBinding(
            memfw_path="/srv/memory/user-a", memfw_tenant="acme"
        )


def test_tenant_without_any_source_still_needs_exactly_one_source():
    with pytest.raises(ValidationError, match="exactly one"):
        MemoryFederationUserBinding(memfw_tenant="acme")
