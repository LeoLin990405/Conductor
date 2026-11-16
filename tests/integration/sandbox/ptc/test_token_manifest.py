from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="class")]


class TestManifest:
    """PTCSandbox manifest lifecycle -- write, read, and sync detection."""

    async def test_write_and_read_manifest(self, shared_sandbox):
        # Compute and write
        manifest = await shared_sandbox._compute_sandbox_manifest()
        await shared_sandbox._write_unified_manifest(manifest)

        # Read back
        remote = await shared_sandbox._read_unified_manifest()
        assert remote is not None
        assert remote.get("schema_version") == 1
        assert "modules" in remote

    async def test_manifest_versioning(self, shared_sandbox):
        """Two consecutive manifests should have matching versions when
        nothing changed."""
        m1 = await shared_sandbox._compute_sandbox_manifest()
        m2 = await shared_sandbox._compute_sandbox_manifest()

        # Module versions should match for same config
        for module_name in m1.get("modules", {}):
            v1 = m1["modules"][module_name].get("version")
            v2 = m2["modules"][module_name].get("version")
            if v1 is not None:
                assert v1 == v2, f"Module {module_name} version mismatch"
