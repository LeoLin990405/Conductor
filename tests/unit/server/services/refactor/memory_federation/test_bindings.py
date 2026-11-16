import pytest
from pydantic import ValidationError

from src.config.models import MemoryFederationConfig, MemoryFederationUserBinding


@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"memfw_path": "/srv/memory/user-a", "memfw_url": "http://memfw:18080"},
    ],
)
def test_binding_requires_exactly_one_source(binding):
    with pytest.raises(ValidationError, match="exactly one"):
        MemoryFederationUserBinding(**binding)


def test_remote_url_can_be_bound_to_only_one_owner():
    with pytest.raises(ValidationError, match="one user only"):
        MemoryFederationConfig(
            user_bindings={
                "user-a": {"memfw_url": "http://memfw:18080"},
                "user-b": {"memfw_url": "http://memfw:18080/"},
            }
        )
