from src.config import product


def test_product_is_core_capabilities():
    caps = product.product_capabilities()
    assert caps["name"] == "Conductor"
    assert caps["vertical"] == "core"
    assert caps["orchestrator"] is True
    assert "multi-agent" == caps["platform"]
    assert "super-agent" in caps["capabilities"]
    assert "workspaces" in caps["capabilities"]
