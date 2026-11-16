"""Unit tests for session persistence functions."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock


class TestLoadPersistedSession:
    """Test load_persisted_session function."""

    def test_load_valid_session(self, tmp_path, monkeypatch, mock_persisted_session_data):
        """Test loading a valid persisted session."""
        from ptc_cli.agent.persistence import load_persisted_session

        # Create mock settings
        agent_name = "test-agent"
        session_file = tmp_path / ".ptc-agent" / agent_name / "session.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps(mock_persisted_session_data, indent=2))

        # Mock settings to return our test path
        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session(agent_name)

        assert result is not None
        assert result["workspace_id"] == "test-workspace-456"
        assert "created_at" in result
        assert "last_used" in result

    def test_load_nonexistent_session(self, tmp_path, monkeypatch):
        """Test loading a session that doesn't exist."""
        from ptc_cli.agent.persistence import load_persisted_session

        # Create a path that doesn't exist
        session_file = tmp_path / "nonexistent" / "session.json"

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session("test-agent")
        assert result is None

    def test_load_expired_session(self, tmp_path, monkeypatch):
        """Test loading a session that has expired (>24 hours old)."""
        from ptc_cli.agent.persistence import load_persisted_session

        # Create session data that's 25 hours old
        old_time = datetime.now(tz=UTC) - timedelta(hours=25)
        expired_data = {
            "workspace_id": "test-workspace-old",
            "created_at": old_time.isoformat(),
            "last_used": old_time.isoformat(),
        }

        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps(expired_data, indent=2))

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session("test-agent")

        # Should return None and delete the file
        assert result is None
        assert not session_file.exists()

    def test_load_session_missing_required_fields(self, tmp_path, monkeypatch):
        """Test loading a session with missing required fields."""
        from ptc_cli.agent.persistence import load_persisted_session

        # Create session data without required fields
        invalid_data = {
            "created_at": datetime.now(tz=UTC).isoformat(),
            # Missing sandbox_id and config_hash
        }

        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps(invalid_data, indent=2))

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session("test-agent")
        assert result is None

    def test_load_corrupted_json(self, tmp_path, monkeypatch):
        """Test loading a session with corrupted JSON."""
        from ptc_cli.agent.persistence import load_persisted_session

        session_file = tmp_path / "session.json"
        session_file.write_text("{ invalid json content")

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session("test-agent")

        # Should return None and delete corrupted file
        assert result is None
        assert not session_file.exists()

    def test_load_session_with_naive_datetime(self, tmp_path, monkeypatch):
        """Test loading a session with naive datetime (old format)."""
        from ptc_cli.agent.persistence import load_persisted_session

        # Create session with naive datetime (no timezone)
        naive_time = datetime.now().replace(tzinfo=None)
        session_data = {
            "workspace_id": "test-workspace-naive",
            "created_at": naive_time.isoformat(),
            "last_used": naive_time.isoformat(),
        }

        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps(session_data, indent=2))

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        result = load_persisted_session("test-agent")

        # Should still load successfully (assumes UTC for naive datetimes)
        assert result is not None
        assert result["workspace_id"] == "test-workspace-naive"


class TestSavePersistedSession:
    """Test save_persisted_session function."""

    def test_save_creates_file(self, tmp_path, monkeypatch):
        """Test that save_persisted_session creates a file with correct data."""
        from ptc_cli.agent.persistence import save_persisted_session

        agent_name = "test-agent"
        agent_dir = tmp_path / ".ptc-agent" / agent_name
        session_file = agent_dir / "session.json"

        mock_settings = Mock()
        mock_settings.ensure_agent_dir = Mock(return_value=agent_dir)
        mock_settings.get_session_file_path = Mock(return_value=session_file)

        monkeypatch.setattr("ptc_cli.agent.persistence.settings", mock_settings)

        # Create the directory (simulating ensure_agent_dir)
        agent_dir.mkdir(parents=True)

        # Save session
        save_persisted_session(agent_name, "workspace-123")

        # Verify file was created
        assert session_file.exists()

        # Verify contents
        data = json.loads(session_file.read_text())
        assert data["workspace_id"] == "workspace-123"
        assert "created_at" in data
        assert "last_used" in data

        # Verify timestamps are recent and timezone-aware
        created_at = datetime.fromisoformat(data["created_at"])
        last_used = datetime.fromisoformat(data["last_used"])
        assert created_at.tzinfo == UTC
        assert last_used.tzinfo == UTC
        assert (datetime.now(tz=UTC) - created_at).total_seconds() < 5

    def test_save_overwrites_existing_file(self, tmp_path, monkeypatch):
        """Test that save_persisted_session overwrites existing file."""
        from ptc_cli.agent.persistence import save_persisted_session

        agent_name = "test-agent"
        agent_dir = tmp_path / ".ptc-agent" / agent_name
        agent_dir.mkdir(parents=True)
        session_file = agent_dir / "session.json"

        # Create existing file
        old_data = {"workspace_id": "old-workspace"}
        session_file.write_text(json.dumps(old_data))

        mock_settings = Mock()
        mock_settings.ensure_agent_dir = Mock(return_value=agent_dir)
        mock_settings.get_session_file_path = Mock(return_value=session_file)

        monkeypatch.setattr("ptc_cli.agent.persistence.settings", mock_settings)

        # Save new session
        save_persisted_session(agent_name, "new-workspace")

        # Verify new data
        data = json.loads(session_file.read_text())
        assert data["workspace_id"] == "new-workspace"


class TestUpdateSessionLastUsed:
    """Test update_session_last_used function."""

    def test_updates_timestamp(self, tmp_path, monkeypatch, mock_persisted_session_data):
        """Test updating the last_used timestamp."""
        from ptc_cli.agent.persistence import update_session_last_used

        session_file = tmp_path / "session.json"

        # Create initial session with old timestamp
        old_time = datetime.now(tz=UTC) - timedelta(hours=2)
        initial_data = mock_persisted_session_data.copy()
        initial_data["last_used"] = old_time.isoformat()
        session_file.write_text(json.dumps(initial_data, indent=2))

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        # Update timestamp
        update_session_last_used("test-agent")

        # Verify timestamp was updated
        data = json.loads(session_file.read_text())
        last_used = datetime.fromisoformat(data["last_used"])

        # Should be recent (within last 5 seconds)
        assert (datetime.now(tz=UTC) - last_used).total_seconds() < 5

        # Other fields should remain unchanged
        assert data["workspace_id"] == mock_persisted_session_data["workspace_id"]

    def test_update_nonexistent_session(self, tmp_path, monkeypatch):
        """Test updating a nonexistent session does nothing."""
        from ptc_cli.agent.persistence import update_session_last_used

        session_file = tmp_path / "nonexistent.json"

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        # Should not raise an error
        update_session_last_used("test-agent")

        # File should still not exist
        assert not session_file.exists()

    def test_update_corrupted_session(self, tmp_path, monkeypatch):
        """Test updating a corrupted session silently fails."""
        from ptc_cli.agent.persistence import update_session_last_used

        session_file = tmp_path / "session.json"
        session_file.write_text("invalid json")

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        # Should not raise an error
        update_session_last_used("test-agent")


class TestDeletePersistedSession:
    """Test delete_persisted_session function."""

    def test_deletes_existing_session(self, tmp_path, monkeypatch, mock_persisted_session_data):
        """Test deleting an existing session file."""
        from ptc_cli.agent.persistence import delete_persisted_session

        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps(mock_persisted_session_data))

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        # Verify file exists before deletion
        assert session_file.exists()

        # Delete session
        delete_persisted_session("test-agent")

        # Verify file was deleted
        assert not session_file.exists()

    def test_delete_nonexistent_session(self, tmp_path, monkeypatch):
        """Test deleting a nonexistent session does nothing."""
        from ptc_cli.agent.persistence import delete_persisted_session

        session_file = tmp_path / "nonexistent.json"

        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        # Should not raise an error
        delete_persisted_session("test-agent")


class TestSessionAge:
    """Test workspace persistence age handling."""

    def test_session_without_last_used_is_kept(self, tmp_path, monkeypatch):
        from ptc_cli.agent.persistence import load_persisted_session

        session_file = tmp_path / "session.json"
        data = {"workspace_id": "workspace-no-age"}
        session_file.write_text(json.dumps(data))
        monkeypatch.setattr(
            "ptc_cli.agent.persistence.settings",
            Mock(get_session_file_path=Mock(return_value=session_file)),
        )

        assert load_persisted_session("test-agent") == data
