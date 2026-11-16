"""The endpoint device's own policy: what a daemon may run, and its version."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.conductor_cli.daemon_policy import (
    PolicyError,
    PolicyStore,
    classify,
    executable_free_operations,
    executable_request_rejection,
    load_policy,
    request_operations,
    parse_policy,
    unclassified_executable_segments,
)

POLICY = r"""
format: 1
read_only: false
read_only_operations:
  - repo.read
  - test.run
operations:
  - operation: repo.read
    patterns:
      - '\bgit\s+(status|diff)\b'
  - operation: test.run
    patterns:
      - '\bpytest\b'
  - operation: repo.push
    patterns:
      - '\bgit\s+push\b'
providers:
  codex:
    allow:
      - repo.read
      - test.run
    deny:
      - repo.push
  claude-code:
    read_only: true
    allow:
      - repo.read
"""


def _write(tmp_path: Path, text: str = POLICY) -> Path:
    path = tmp_path / "daemon_policy.yaml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _body(**overrides) -> dict:
    body = {
        "format": 1,
        "read_only_operations": ["repo.read", "test.run"],
        "operations": [
            {"operation": "repo.read", "patterns": ["git status"]},
            {"operation": "test.run", "patterns": ["pytest"]},
            {"operation": "repo.push", "patterns": ["git push"]},
        ],
        "providers": {
            "codex": {
                "allow": ["repo.read", "test.run"],
                "deny": ["repo.push"],
            }
        },
    }
    body.update(overrides)
    return body


class TestLoading:
    def test_a_valid_policy_loads_with_its_providers(self, tmp_path):
        policy = load_policy(_write(tmp_path))

        assert policy.provider_policy("codex") is not None
        assert policy.provider_policy("Codex") is not None
        assert policy.provider_policy("kimi") is None
        assert policy.read_only_operations == frozenset({"repo.read", "test.run"})

    def test_the_version_is_stable_across_reads(self, tmp_path):
        path = _write(tmp_path)

        assert load_policy(path).version == load_policy(path).version

    def test_the_version_ignores_comments(self, tmp_path):
        first = load_policy(_write(tmp_path))
        second = load_policy(_write(tmp_path, "# a comment\n" + POLICY))

        assert first.version == second.version

    def test_a_changed_allowlist_changes_the_version(self, tmp_path):
        first = load_policy(_write(tmp_path))
        second = load_policy(
            _write(tmp_path, POLICY.replace(r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"))
        )

        assert first.version != second.version

    def test_a_missing_file_is_a_policy_error(self, tmp_path):
        with pytest.raises(PolicyError, match="cannot read"):
            load_policy(tmp_path / "absent.yaml")

    def test_broken_yaml_is_a_policy_error(self, tmp_path):
        with pytest.raises(PolicyError, match="not valid YAML"):
            load_policy(_write(tmp_path, "operations: [oops\n"))

    def test_a_policy_without_providers_is_refused(self, tmp_path):
        with pytest.raises(PolicyError, match="providers"):
            load_policy(
                _write(
                    tmp_path,
                    "format: 1\noperations:\n  - operation: x\n    patterns: ['x']\n",
                )
            )

    def test_a_policy_without_operations_is_refused(self, tmp_path):
        with pytest.raises(PolicyError, match="operations"):
            parse_policy({"providers": {"codex": {"allow": []}}})

    def test_an_unknown_format_is_refused(self, tmp_path):
        with pytest.raises(PolicyError, match="unsupported policy format"):
            load_policy(_write(tmp_path, "format: 99\nproviders: {}\n"))


class TestValidation:
    def test_an_operation_needs_patterns(self):
        with pytest.raises(PolicyError, match="patterns"):
            parse_policy(
                {
                    "operations": [{"operation": "x"}],
                    "providers": {"codex": {"allow": ["x"]}},
                }
            )

    def test_an_entry_needs_an_operation(self):
        with pytest.raises(PolicyError, match="no operation"):
            parse_policy(
                {
                    "operations": [{"patterns": ["x"]}],
                    "providers": {"codex": {"allow": []}},
                }
            )

    def test_a_duplicate_operation_is_refused(self):
        with pytest.raises(PolicyError, match="twice"):
            parse_policy(
                {
                    "operations": [
                        {"operation": "x", "patterns": ["a"]},
                        {"operation": "x", "patterns": ["b"]},
                    ],
                    "providers": {"codex": {"allow": ["x"]}},
                }
            )

    def test_an_operation_cannot_be_allowed_and_denied_at_once(self):
        with pytest.raises(PolicyError, match="both allowed and denied"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "providers": {"codex": {"allow": ["x"], "deny": ["x"]}},
                }
            )

    def test_a_provider_may_not_name_an_unknown_operation(self):
        with pytest.raises(PolicyError, match="unknown operation"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "providers": {"codex": {"allow": ["typo"]}},
                }
            )

    def test_a_read_only_operation_must_be_known(self):
        with pytest.raises(PolicyError, match="unknown operation"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "read_only_operations": ["typo"],
                    "providers": {"codex": {"allow": ["x"]}},
                }
            )

    def test_an_invalid_pattern_is_refused(self):
        with pytest.raises(PolicyError, match="invalid pattern"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["("]}],
                    "providers": {"codex": {"allow": ["x"]}},
                }
            )

    def test_a_non_boolean_flag_is_refused(self):
        with pytest.raises(PolicyError, match="must be true or false"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "providers": {"codex": {"read_only": "yes"}},
                }
            )

    def test_an_empty_provider_name_is_refused(self):
        with pytest.raises(PolicyError, match="provider name is empty"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "providers": {"  ": {"allow": ["x"]}},
                }
            )

    def test_an_empty_operation_name_is_refused(self):
        with pytest.raises(PolicyError, match="empty name"):
            parse_policy(
                {
                    "operations": [{"operation": "x", "patterns": ["a"]}],
                    "providers": {"codex": {"allow": [" "]}},
                }
            )


class TestEvaluation:
    def test_an_allowed_operation_passes(self):
        decision = parse_policy(_body()).evaluate("codex", "Run git status on the repo")

        assert decision.allowed is True
        assert decision.operations == ("repo.read",)

    def test_an_operation_outside_the_allowlist_is_refused(self):
        body = _body()
        body["providers"]["codex"]["allow"] = ["repo.read"]

        decision = parse_policy(body).evaluate("codex", "run pytest")

        assert decision.allowed is False
        assert "not in the allowlist" in decision.reason

    def test_a_denied_operation_is_refused(self):
        decision = parse_policy(_body()).evaluate("codex", "git push the branch")

        assert decision.allowed is False
        assert "denied" in decision.reason

    def test_a_provider_without_an_entry_is_refused(self):
        decision = parse_policy(_body()).evaluate("kimi", "git status")

        assert decision.allowed is False
        assert "no entry in the local policy" in decision.reason

    def test_a_request_naming_no_operation_is_refused(self):
        decision = parse_policy(_body()).evaluate("codex", "Please look into the ticket")

        assert decision.allowed is False
        assert "matches no operation" in decision.reason

    def test_a_request_naming_no_operation_passes_when_allowed(self):
        body = _body()
        body["providers"]["codex"]["allow_unclassified"] = True

        decision = parse_policy(body).evaluate("codex", "look into the ticket")

        assert decision.allowed is True

    def test_a_read_only_policy_accepts_a_read_only_operation(self):
        decision = parse_policy(_body(read_only=True)).evaluate("codex", "run pytest")

        assert decision.allowed is True

    def test_a_read_only_policy_refuses_an_operation_outside_the_read_only_set(self):
        body = _body(read_only=True)
        body["operations"].append({"operation": "file.write", "patterns": ["write"]})
        body["providers"]["codex"]["allow"].append("file.write")

        decision = parse_policy(body).evaluate("codex", "write the file")

        assert decision.allowed is False
        assert "read-only" in decision.reason

    def test_a_read_only_provider_refuses_a_mutating_operation(self):
        body = _body()
        body["operations"].append({"operation": "file.write", "patterns": ["write"]})
        body["providers"]["codex"]["allow"].append("file.write")
        body["providers"]["codex"]["read_only"] = True

        decision = parse_policy(body).evaluate("codex", "write the file")

        assert decision.allowed is False
        assert "read-only" in decision.reason

    def test_a_speculative_run_is_read_only_whatever_the_policy_says(self):
        body = _body()
        body["operations"].append({"operation": "file.write", "patterns": ["write"]})
        body["providers"]["codex"]["allow"].append("file.write")

        decision = parse_policy(body).evaluate("codex", "write the file", speculative=True)

        assert decision.allowed is False
        assert "read-only" in decision.reason

    def test_a_speculative_run_may_still_read(self):
        decision = parse_policy(_body()).evaluate("codex", "git status", speculative=True)

        assert decision.allowed is True

    def test_the_provider_name_is_matched_case_insensitively(self):
        assert parse_policy(_body()).evaluate("CODEX", "git status").allowed is True

    def test_a_request_matching_two_operations_needs_both_in_the_allowlist(self):
        body = _body()
        body["providers"]["codex"]["allow"] = ["repo.read"]

        decision = parse_policy(body).evaluate("codex", "git status and run pytest")

        assert decision.allowed is False
        assert "test.run" in decision.reason


class TestOperationVerdict:
    """The verdict for a request that is already classified."""

    def test_a_denied_operation_is_refused(self):
        decision = parse_policy(_body()).evaluate_operations("codex", {"repo.push"})

        assert decision.allowed is False
        assert "denied" in decision.reason

    def test_an_operation_outside_the_allowlist_is_refused(self):
        body = _body()
        body["providers"]["codex"]["allow"] = ["repo.read"]

        decision = parse_policy(body).evaluate_operations("codex", {"test.run"})

        assert decision.allowed is False
        assert "test.run" in decision.reason

    def test_an_empty_set_of_operations_is_allowed(self):
        """A text that names no operation is not a request to classify."""
        decision = parse_policy(_body()).evaluate_operations("codex", set())

        assert decision.allowed is True

    def test_a_read_only_run_refuses_a_mutating_operation(self):
        body = _body(read_only=True)
        body["operations"].append({"operation": "file.write", "patterns": ["write"]})
        body["providers"]["codex"]["allow"].append("file.write")

        decision = parse_policy(body).evaluate_operations("codex", {"file.write"})

        assert decision.allowed is False
        assert "read-only" in decision.reason

    def test_a_provider_without_an_entry_is_refused(self):
        decision = parse_policy(_body()).evaluate_operations("kimi", {"repo.read"})

        assert decision.allowed is False
        assert "no entry in the local policy" in decision.reason


class TestEffectiveReadOnly:
    """The strictest of the three local restrictions."""

    def test_a_writable_policy_allows_a_writable_run(self):
        assert parse_policy(_body()).is_read_only("codex") is False

    def test_a_read_only_policy_forces_a_read_only_run(self):
        assert parse_policy(_body(read_only=True)).is_read_only("codex") is True

    def test_a_read_only_provider_forces_a_read_only_run(self):
        body = _body()
        body["providers"]["codex"]["read_only"] = True

        assert parse_policy(body).is_read_only("codex") is True

    def test_a_speculative_run_is_read_only_whatever_the_policy_says(self):
        assert parse_policy(_body()).is_read_only("codex", speculative=True) is True


class TestClassify:
    def test_only_matching_rules_contribute_an_operation(self, tmp_path):
        policy = load_policy(_write(tmp_path))

        assert classify("run pytest now", policy.operations) == {"test.run"}
        assert classify("nothing here", policy.operations) == set()


    def test_a_replacement_racing_the_read_is_retried(self, tmp_path, monkeypatch):
        """A read/stat mismatch re-reads instead of caching old rules."""
        from src.conductor_cli import daemon_policy as policy_module

        path = _write(tmp_path)
        store = PolicyStore(path)
        real_stat = policy_module.os.stat
        calls = {"n": 0}

        class _Mismatch:
            st_mode = real_stat(path).st_mode
            st_uid = real_stat(path).st_uid
            st_dev = -1
            st_ino = -1

        def flaky_stat(target, *args, **kwargs):
            if str(target) == str(path) and calls["n"] == 0:
                calls["n"] += 1
                return _Mismatch()
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(policy_module.os, "stat", flaky_stat)
        policy = store.load()

        assert policy.providers
        assert calls["n"] >= 1  # the first snapshot was discarded as inconsistent


    def test_the_cached_policy_always_matches_its_bytes(self, tmp_path):
        """Replacement and restoration never leave mismatched policy/stamp."""
        path = _write(tmp_path)
        store = PolicyStore(path)
        first = store.load()
        assert store.current().version == load_policy(path).version

        stricter = POLICY.replace(
            r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"
        )
        replacement = tmp_path / "replacement.yaml"
        replacement.write_text(stricter, encoding="utf-8")
        import os

        os.chmod(replacement, 0o600)
        os.replace(replacement, path)
        second = store.current()
        assert second.version == load_policy(path).version
        assert second.version != first.version

        # Restore the original content under a new inode/stamp: the cache must
        # follow the file, not a stale policy/stamp pairing.
        restored = tmp_path / "restored.yaml"
        restored.write_text(POLICY, encoding="utf-8")
        os.chmod(restored, 0o600)
        os.replace(restored, path)
        third = store.current()
        assert third.version == load_policy(path).version
        assert third.version == first.version


    def test_the_policy_location_authorization_uses_the_pinned_path(self, tmp_path):
        """Location auth must check the pinned policy, not a re-resolved alias."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        policy_file = workspace / "daemon_policy.yaml"
        policy_file.write_text(POLICY, encoding="utf-8")
        import os

        os.chmod(policy_file, 0o600)
        outside = tmp_path / "outside"
        outside.mkdir()
        alias = tmp_path / "config"
        alias.symlink_to(workspace, target_is_directory=True)
        store = PolicyStore(alias / "daemon_policy.yaml")  # pins inside workspace
        alias.unlink()
        alias.symlink_to(outside, target_is_directory=True)  # repointed after pinning

        assert str(store.resolved_path).startswith(str(workspace.resolve()))

    def test_a_replacement_between_snapshot_stages_cannot_cache_other_rules(
        self, tmp_path, monkeypatch
    ):
        """The cache returns the snapshot it read, not a later separate read."""
        from src.conductor_cli import daemon_policy as policy_module

        path = _write(tmp_path)
        store = PolicyStore(path)
        first_version = load_policy(path).version
        stricter = POLICY.replace(
            r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"
        )
        replacement = tmp_path / "replacement.yaml"
        replacement.write_text(stricter, encoding="utf-8")
        import os

        os.chmod(replacement, 0o600)
        real_read = policy_module._read_policy_snapshot

        def racing_read(target):
            text, stamp = real_read(target)
            # The file is replaced after the snapshot read but before any
            # second read the old implementation performed.
            os.replace(replacement, path)
            return text, stamp

        monkeypatch.setattr(policy_module, "_read_policy_snapshot", racing_read)
        cached = store.load()

        assert cached.version == first_version  # the policy matching the stamp
        monkeypatch.undo()
        # A later current() notices the replacement and follows the file.
        assert store.current().version == load_policy(path).version
        assert store.current().version != first_version


class TestShippedExample:
    """The template a device copies must stay loadable and keep refusing."""

    def _example(self) -> Path:
        return (
            Path(__file__).resolve().parents[3] / "config" / "daemon_policy.example.yaml"
        )

    def _policy(self):
        path = self._example()
        return parse_policy(
            yaml.safe_load(path.read_text(encoding="utf-8")), path=path
        )

    def test_the_example_policy_is_valid(self):
        policy = self._policy()

        assert policy.provider_policy("codex") is not None
        assert policy.provider_policy("claude-code") is not None

    def test_the_example_policy_refuses_a_push(self):
        policy = self._policy()

        decision = policy.evaluate("codex", "git push origin main")

        assert decision.allowed is False
        assert "denied" in decision.reason

    def test_the_example_policy_allows_a_read(self):
        policy = self._policy()

        assert policy.evaluate("codex", "run git status").allowed is True


class TestPolicyStore:
    def test_the_policy_is_read_again_after_the_file_changes(self, tmp_path):
        path = _write(tmp_path)
        store = PolicyStore(path)
        first = store.current()

        path.write_text(
            POLICY.replace(r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"), encoding="utf-8"
        )
        second = store.current()

        assert second.version != first.version

    def test_an_unreadable_policy_is_refused(self, tmp_path):
        store = PolicyStore(tmp_path / "absent.yaml")

        with pytest.raises(PolicyError):
            store.current()

    def test_a_deleted_policy_is_refused_after_it_was_read(self, tmp_path):
        path = _write(tmp_path)
        store = PolicyStore(path)
        store.current()

        path.unlink()

        with pytest.raises(PolicyError):
            store.current()


class TestUnknownPolicyKeys:
    """A misspelled restriction must not be silently dropped."""

    def test_an_unknown_top_level_key_is_refused(self):
        with pytest.raises(PolicyError, match="denyy"):
            parse_policy(_body(denyy=["repo.push"]))

    def test_an_unknown_operation_key_is_refused(self):
        body = _body()
        entry = body["operations"][0]
        entry["pattern"] = list(entry["patterns"])  # typo stays next to patterns
        with pytest.raises(PolicyError, match="pattern"):
            parse_policy(body)

    def test_a_valid_operation_entry_still_requires_patterns(self):
        body = _body()
        body["operations"][0].pop("patterns")
        with pytest.raises(PolicyError, match="patterns"):
            parse_policy(body)

    def test_an_unknown_provider_key_is_refused(self):
        body = _body()
        body["providers"]["codex"]["read-only"] = True
        with pytest.raises(PolicyError, match="read-only"):
            parse_policy(body)


class TestPrivatePolicyFile:
    """The policy is the authority boundary, so it is read like one."""

    def test_a_policy_writable_by_others_is_refused(self, tmp_path):
        path = _write(tmp_path)
        os.chmod(path, 0o644)

        with pytest.raises(PolicyError, match="0600"):
            load_policy(path)

    def test_a_symlinked_policy_is_refused(self, tmp_path):
        real = _write(tmp_path)
        link = tmp_path / "policy-link.yaml"
        link.symlink_to(real)

        with pytest.raises(PolicyError, match="symlink"):
            load_policy(link)

    def test_an_owner_only_policy_is_accepted(self, tmp_path):
        assert load_policy(_write(tmp_path)).providers


class TestUnclassifiedExecutableContent:
    """Text the CLI executes must not carry commands the policy never named."""

    def test_prose_is_not_executable_content(self):
        policy = parse_policy(_body())

        assert (
            unclassified_executable_segments(
                "Do not modify files, run mutating commands, or push.",
                policy.operations,
            )
            == ()
        )

    def test_a_named_command_is_classified(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments("git status", policy.operations) == ()
        assert (
            unclassified_executable_segments("git status && pytest", policy.operations)
            == ()
        )

    def test_an_unclassified_command_is_reported(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "rm -rf .", policy.operations
        ) == ("rm -rf .",)

    def test_an_unclassified_command_chained_after_an_allowed_one_is_reported(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status && rm -rf .", policy.operations
        ) == ("rm -rf .",)

    def test_list_markers_do_not_hide_a_command(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "- rm -rf .", policy.operations
        ) == ("rm -rf .",)

    def test_a_background_separator_does_not_hide_a_command(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status & rm -rf .", policy.operations
        ) == ("rm -rf .",)

    def test_an_assignment_prefix_does_not_hide_a_command(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status\nX=1 rm -rf .", policy.operations
        ) == ("X=1 rm -rf .",)

    def test_a_comment_does_not_launder_a_command(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "rm -rf . # git status", policy.operations
        ) == ("rm -rf .",)

    def test_command_substitutions_are_checked_as_commands(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status $(rm -rf .)", policy.operations
        ) == ("rm -rf .",)
        assert unclassified_executable_segments(
            "git status `rm -rf .`", policy.operations
        ) == ("rm -rf .",)

    def test_an_unclosed_substitution_is_refused_whole(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status `rm -rf .", policy.operations
        )

    def test_a_prose_path_reference_is_not_a_command(self):
        policy = parse_policy(_body())

        assert (
            unclassified_executable_segments(
                "Keep every file you create or modify inside "
                "`.orchestrator/autonomy/abcdef12/` in this workspace.",
                policy.operations,
            )
            == ()
        )

    def test_quoted_arguments_do_not_authorise_a_command(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "rm -rf ./victim 'git status'", policy.operations
        )

    def test_compound_and_negated_commands_are_executable(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status && { rm -rf ./victim; }", policy.operations
        )
        assert unclassified_executable_segments(
            "git status && ! rm -rf ./victim", policy.operations
        )

    def test_a_quoted_hash_is_not_a_comment(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status ' # literal'; rm -rf ./victim", policy.operations
        )

    def test_a_conditional_substitution_is_checked(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status $(if true; then rm -rf ./victim; fi)", policy.operations
        )

    def test_a_redirection_target_is_executable_content(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            "git status > /tmp/out", policy.operations
        )

    def test_an_unclosed_quote_refuses_the_line(self):
        policy = parse_policy(_body())

        assert unclassified_executable_segments(
            'git status "rm -rf .', policy.operations
        )

    def test_the_real_server_prompt_is_not_executable_content(self):
        from src.server.services.autonomy.worker import worker_prompt

        policy = parse_policy(_body())
        ticket = {
            "ticket_id": "12345678-1234-4123-8123-123456789012",
            "title": "Check the tree",
            "scope": "run git status and report",
            "workspace_id": "workspace-a",
        }

        for speculative in (False, True):
            prompt = worker_prompt(ticket, {}, native=True, speculative=speculative)
            assert (
                unclassified_executable_segments(prompt, policy.operations) == ()
            ), speculative

        # The prompt is still the text the CLI runs, and it names an allowed
        # operation: the full verdict accepts it.
        prompt = worker_prompt(ticket, {}, native=True)
        assert policy.evaluate("codex", prompt).allowed is True


class TestExecutableRequestVerdict:
    """Command lines are authorized by their normalized signature, not by text."""

    def _policy(self, *, read_only: bool = False):
        body = _body()
        body["read_only"] = read_only
        body["operations"].append(
            {"operation": "file.write", "patterns": [r"\bwrite the file\b"]}
        )
        body["providers"]["codex"]["allow"] = ["repo.read", "test.run", "file.write"]
        return parse_policy(body)

    def test_a_named_command_is_allowed(self):
        assert executable_request_rejection(
            self._policy(), "codex", "git status"
        ) == ""

    def test_normalized_words_are_judged_by_allow_and_deny(self):
        reason = executable_request_rejection(
            self._policy(), "codex", 'git p""ush origin main'
        )
        assert "denied" in reason and "repo.push" in reason

    def test_argument_text_does_not_authorize_a_command(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "rm -rf ./victim git status"
        )
        assert "cannot classify" in reason

    def test_an_argument_only_match_does_not_authorize_another_executable(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "echo git status"
        )
        assert "cannot classify" in reason

    def test_quote_concatenation_is_decoded_before_detection(self):
        reason = executable_request_rejection(
            self._policy(), "codex", 'git status\nr"m" -rf ./victim'
        )
        assert "cannot classify" in reason

    def test_output_redirection_is_unsupported_syntax(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status > victim"
        )
        assert "redirection" in reason

    def test_an_escaped_command_word_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status\nr\\m -rf ./victim"
        )
        assert "expansion or substitution" in reason

    def test_quoted_parameter_expansion_is_refused(self):
        reason = executable_request_rejection(
            self._policy(),
            "codex",
            'git status\n"${SHELL:-sh}" -c "rm -rf ./victim"',
        )
        assert "expansion or substitution" in reason

    def test_a_spaced_backtick_delimiter_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status\n` rm -rf ./victim `"
        )
        assert "expansion or substitution" in reason

    def test_quoted_substitution_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", 'git status\n"$(rm -rf ./victim)"'
        )
        assert "expansion or substitution" in reason

    def test_parameter_expansion_at_command_position_is_refused(self):
        reason = executable_request_rejection(
            self._policy(),
            "codex",
            "git status\n${SHELL:-sh} -c 'rm -rf ./victim'",
        )
        assert "expansion or substitution" in reason

    def test_ansi_c_octal_quoting_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status\n$'\\162\\155' -rf ./victim"
        )
        assert "expansion or substitution" in reason

    def test_ansi_c_quoting_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status\n$'rm' -rf ./victim"
        )
        assert "expansion or substitution" in reason

    def test_an_expansion_generated_command_word_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "$(git log -1 --format=rm) -rf ./victim"
        )
        assert "expansion or substitution" in reason

    def test_compound_commands_are_unsupported(self):
        reason = executable_request_rejection(
            self._policy(),
            "codex",
            "if rm -rf ./victim; then git status; fi",
        )
        assert "compound command" in reason

    def test_attached_redirection_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status>victim"
        )
        assert "redirection" in reason

    def test_list_markers_do_not_hide_a_command(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "- rm -rf ./victim"
        )
        assert "cannot classify" in reason
        for marked in ("1. rm -rf ./victim", "1) rm -rf ./victim"):
            numbered = executable_request_rejection(
                self._policy(), "codex", marked
            )
            assert "cannot classify" in numbered, marked

    def test_redirection_attached_to_the_executable_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git>victim"
        )
        assert "redirection" in reason

    def test_request_operations_normalizes_list_marked_commands(self):
        rules = self._policy().operations

        assert request_operations(rules, '1) git "status"') == {"repo.read"}

    def test_a_listed_allowed_command_is_still_allowed(self):
        assert (
            executable_request_rejection(self._policy(), "codex", "- git status")
            == ""
        )

    def test_a_redirection_only_line_is_refused(self):
        reason = executable_request_rejection(
            self._policy(), "codex", "git status\n> victim"
        )
        assert "redirection" in reason

    def test_the_obsolete_checker_matrix_is_portable_to_the_active_validator(self):
        policy = self._policy()
        for probe in (
            "git status && { rm -rf ./victim; }",
            "git status && ! rm -rf ./victim",
            "git status ' # literal'; rm -rf ./victim",
            "git status $(if true; then rm -rf ./victim; fi)",
            "git status & rm -rf ./victim",
        ):
            assert executable_request_rejection(policy, "codex", probe), probe

    def test_allow_unclassified_does_not_admit_executable_content(self):
        body = _body()
        body["providers"]["codex"]["allow_unclassified"] = True
        policy = parse_policy(body)

        assert executable_request_rejection(policy, "codex", "rm -rf ./victim")

    def test_the_read_only_cut_applies_to_command_signatures(self):
        reason = executable_request_rejection(
            self._policy(read_only=True), "codex", "write the file"
        )
        assert "read-only" in reason

    def test_prose_and_the_real_server_prompt_are_allowed(self):
        from src.server.services.autonomy.worker import worker_prompt

        policy = self._policy()
        ticket = {
            "ticket_id": "12345678-1234-4123-8123-123456789012",
            "title": "Check the tree",
            "scope": "run git status and report",
            "workspace_id": "workspace-a",
        }
        assert (
            executable_request_rejection(
                policy, "codex", "The Ticket's own request is checked."
            )
            == ""
        )
        for speculative in (False, True):
            prompt = worker_prompt(ticket, {}, native=True, speculative=speculative)
            assert (
                executable_request_rejection(
                    policy, "codex", prompt, speculative=speculative
                )
                == ""
            )


class TestRequestOperations:
    """Capability/result checks share normalized command classification."""

    def _rules(self):
        return parse_policy(_body()).operations

    def test_command_signatures_are_normalized(self):
        assert request_operations(self._rules(), 'pytest\ngit "status"') == {
            "test.run",
            "repo.read",
        }

    def test_a_result_line_is_normalized_too(self):
        assert request_operations(
            self._rules(), 'git "status"\nMARKER-leak'
        ) == {"repo.read"}

    def test_prose_keeps_the_raw_classification(self):
        assert request_operations(
            self._rules(), "run git status and report"
        ) == {"repo.read"}


class TestExecutableFreeOperations:
    """Strict classification for untrusted output: unresolved syntax is None."""

    def _rules(self):
        return parse_policy(_body()).operations

    def test_plain_text_returns_operations(self):
        assert executable_free_operations(
            self._rules(), "run git status and report"
        ) == {"repo.read"}

    def test_unresolved_syntax_is_unclassifiable(self):
        assert (
            executable_free_operations(
                self._rules(), 'git "status"\ngit>victim'
            )
            is None
        )

    def test_command_substitution_is_unclassifiable(self):
        assert (
            executable_free_operations(
                self._rules(), "git status\n$(echo rm) -rf ./victim"
            )
            is None
        )

    def test_the_real_server_prompt_is_classifiable(self):
        from src.server.services.autonomy.worker import worker_prompt

        ticket = {
            "ticket_id": "12345678-1234-4123-8123-123456789012",
            "title": "Check the tree",
            "scope": "run git status and report",
            "workspace_id": "workspace-a",
        }
        for speculative in (False, True):
            prompt = worker_prompt(ticket, {}, native=True, speculative=speculative)
            assert executable_free_operations(self._rules(), prompt) is not None
